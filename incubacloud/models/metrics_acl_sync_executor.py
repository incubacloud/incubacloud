"""Grant and revoke metrics accounts on an already-deployed central.

The access-control list vmauth enforces used to be written by exactly one
thing: the full central deployment. For a self-hosted panel that is
airtight — there is one account, minted by that same deployment, so
minting and granting are the same act and cannot drift.

A shared central breaks the equivalence. Accounts are minted whenever a
tenant appears, while the list stays a snapshot of whoever existed the
last time somebody deployed. Everyone minted after it holds a credential
the gateway has never heard of, and their agents retry against a 401
forever — indistinguishable, from their side, from our software being
broken.

This executor restores the equivalence by making "grant" an act of its
own, cheap enough to run on every change: rewrite the document, reload
the proxy, provision the organisations the new accounts need. It shares
every task file with the full deployment, so the two cannot disagree
about what the boundary looks like.

It deliberately cannot recreate containers — it never touches the compose
file. Granting an account is a sub-second reload of one proxy, not a blip
for the whole fleet, which is what makes automating it uncontroversial.
"""
import base64
import logging

from .observability_central_executor import ObservabilityCentralExecutor

_logger = logging.getLogger(__name__)


class MetricsAclSyncExecutor(ObservabilityCentralExecutor):
    """Reconcile vmauth's user list with the accounts that should exist."""

    _job_type = "sync_metrics_accounts"
    _playbook = "playbooks/metrics_acl_sync.yml"

    def get_extra_vars(self):
        """Hand the whole document, and only the new accounts, to Ansible.

        The document is complete because it replaces the file: a partial
        list would revoke everyone missing from it. The account list is
        the delta because it only drives Grafana provisioning, which is
        idempotent — so the difference is cost, and at a thousand tenants
        the difference is the whole point.
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        # Minted before the list is read, exactly as the deployment does:
        # a central whose own account does not exist accepts no writes
        # from anyone and looks healthy from every angle.
        settings._ensure_metrics_credential()
        accounts = self._accounts_for_deployment()
        operator_token = settings._ensure_operator_credential()
        admin_password = settings._ensure_grafana_admin_password()
        granted = set((settings.metrics_accounts_deployed or "").split())
        new = [
            {"user": user, "password": password}
            for user, password in accounts
            if user not in granted
        ]
        revoked = granted - {user for user, _p in accounts}
        self._sys(
            f"Syncing the access-control list on {self._host().name}: "
            f"{len(accounts)} account(s) total, {len(new)} to grant, "
            f"{len(revoked)} to revoke."
        )
        grafana_admin_basic = base64.b64encode(
            f"admin:{admin_password}".encode()
        ).decode()
        return {
            "ic_vmauth_config": self._vmauth_config(
                accounts, operator_token, grafana_admin_basic,
            ),
            "ic_accounts": new,
            # The boundary probe and every Grafana call authenticate AS
            # the operator, so the plaintext has to travel alongside the
            # document that grants it. Left out, the probe authenticates
            # with no password at all and vmauth answers 401 — which
            # reads as the frontier being broken rather than the caller,
            # and strands every account minted since the last sync.
            "ic_operator_plain": operator_token,
            # The organisation mapping has to name every account, not
            # just the new ones: it replaces the setting rather than
            # adding to it, so a partial map would strand everyone
            # missing from it in Grafana's default organisation.
            "ic_grafana_oidc": settings._grafana_oidc(accounts),
        }

    def parse_results(self, results):
        """Require the boundary to have answered before calling it synced."""
        errors = []
        rc = results.get(self._playbook, {}).get("exit_status", 1)
        if rc != 0:
            errors.append(
                f"Metrics account sync failed (rc={rc}). See the log."
            )
            return errors
        if str(self.playbook_facts().get("ic_acl_synced", "")).lower() not in (
            "true", "1",
        ):
            errors.append(
                "vmauth did not answer through the gateway after the "
                "access-control list was rewritten."
            )
        return errors

    async def on_success(self, results):
        """Record the list that is now in force, and clear the alarm.

        Written only here, and only from what the playbook was built
        from: the record's whole purpose is to distinguish "granted on
        the gateway" from "minted in our database", so filling it with
        intent rather than fact would defeat it.
        """
        self._record_shipped_accounts()
        self._sys("✓ The access-control list is in force on the gateway.")
        self._resolve_alert("metrics_acl_sync_failed")

    async def on_failure(self, results, errors):
        """Alert: every account minted since the last sync is locked out."""
        for err in errors:
            self._sys(f"✗ {err}")
        self._alert(
            "metrics_acl_sync_failed",
            "The metrics access-control list could not be synced. Panels "
            "whose account is not on the gateway yet will keep their "
            "observability switched off until this succeeds.",
            level="warning",
        )
