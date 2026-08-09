"""Deploy the central metrics stack (Fase 4 / A5).

VictoriaMetrics + Grafana, deployed by the panel like any other host
state — the owner's condition was "no manual steps, deployed the way
prepare-host is" (P9-5).

The target host is whichever ``cloud.host`` the job runs against, which
is what makes "co-locate now, move to a dedicated VPS later" a matter of
re-running the job elsewhere rather than a migration.
"""
import base64
import logging

from passlib.hash import apr_md5_crypt

from .ansible_executor import AnsibleExecutor

_logger = logging.getLogger(__name__)


class ObservabilityCentralExecutor(AnsibleExecutor):
    """Deploy VictoriaMetrics + Grafana on this job's host."""

    _job_type = "deploy_metrics_central"
    _playbook = "playbooks/observability_central.yml"

    def _host(self):
        """Return the host this job targets."""
        return self.job.host_id

    def _metrics_accounts(self):
        """Return ``[(user, password), ...]`` allowed to write and read.

        Core knows exactly one account: this panel's own. The SaaS
        manager overrides this to append one entry per tenant, because
        only it knows tenants exist — core ships to partners and must
        stay ignorant of them.

        The list becomes the central's htpasswd, and the authenticated
        user *is* the label the central forces on every series. So adding
        an account here is the single act that grants a panel the right
        to write, and removing it is the single act that revokes it.
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        user, token = settings._metrics_auth()
        return [(user, token)] if (user and token) else []

    def _htpasswd(self, pairs):
        """Render ``user:hash`` lines for nginx's ``auth_basic``.

        apr1 rather than bcrypt: nginx supports it everywhere, and the
        bcrypt backend is not usable in this image.

        :param pairs: iterable of ``(user, plaintext_password)``.
        :return: the file contents, newline-terminated.
        """
        return "".join(
            f"{user}:{apr_md5_crypt.hash(password)}\n"
            for user, password in pairs
            if user and password
        )

    def get_extra_vars(self):
        """Hand retention, credentials and Grafana auth to the playbook."""
        settings = self.env["cloud.settings"].sudo()._get_system()
        accounts = self._metrics_accounts()
        operator_token = settings._ensure_operator_credential()
        admin_password = settings._ensure_grafana_admin_password()
        self._sys(
            f"Deploying the metrics central on {self._host().name} "
            f"(retention {settings.metrics_retention_days or 90} days, "
            f"{len(accounts)} account(s))."
        )
        if not accounts:
            self._sys(
                "⚠ No metrics account is configured yet, so the central "
                "will accept no writes. Enable observability in Settings "
                "to generate one."
            )
        return {
            "ic_retention_days": settings.metrics_retention_days or 90,
            "ic_accounts_htpasswd": self._htpasswd(accounts),
            # Plaintext, because each organisation's datasource has to
            # authenticate AS that account — a hash cannot be used to
            # make a request. Every task consuming it is ``no_log``.
            "ic_accounts": [
                {"user": user, "password": password}
                for user, password in accounts
            ],
            "ic_operator_htpasswd": self._htpasswd(
                [("operator", operator_token)]
            ),
            # Grafana's datasource and the post-deploy health check both
            # need to *use* the credential, not just verify it, so the
            # plaintext travels too. It never lands on disk in clear: the
            # tasks that consume it are ``no_log``.
            "ic_operator_plain": operator_token,
            "ic_grafana_admin_password": admin_password,
            # Pre-encoded: nginx has no base64 filter, and building the
            # header here keeps the password out of the config in clear.
            "ic_grafana_admin_basic": base64.b64encode(
                f"admin:{admin_password}".encode()
            ).decode(),
            "ic_grafana_root_url": settings.grafana_base_url or "",
            # Filled by the SaaS layer once Grafana is registered as an
            # OIDC client (see the manager's override). Empty here means
            # "no identity provider available", which is the self-hosted
            # case: the playbook falls back to trusting a header set by
            # the panel's own reverse proxy.
            "ic_grafana_oidc": self._grafana_oidc(),
        }

    def _grafana_oidc(self):
        """Return the OIDC settings for Grafana, or ``{}`` if none.

        Core has no identity provider of its own, so it always returns
        empty and the playbook wires ``auth.proxy`` instead. The SaaS
        manager overrides this.
        """
        return {}

    def parse_results(self, results):
        """Require the backend to actually answer before calling it up."""
        errors = []
        rc = results.get(self._playbook, {}).get("exit_status", 1)
        if rc != 0:
            errors.append(
                f"Metrics central deployment failed (rc={rc}). See the log."
            )
            return errors
        if str(self.playbook_facts().get("ic_central_up", "")).lower() not in (
            "true", "1",
        ):
            errors.append(
                "VictoriaMetrics did not answer its health endpoint after "
                "deployment."
            )
        return errors

    async def on_success(self, results):
        """Wire the panel to the central it just deployed, then report.

        The operator pressed one button; asking them to now go and paste
        two URLs they cannot guess would defeat the point, and getting
        either one subtly wrong produces a stack that looks deployed and
        collects nothing.

        What can be derived is derived. What cannot — a public endpoint
        for agents on OTHER hosts, which needs DNS and a certificate —
        is left empty and said out loud, because inventing it would
        create a fleet quietly failing to push.
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        gateway = str(self.playbook_facts().get("ic_central_gateway") or "")
        account, _token = settings._ensure_metrics_credential()

        vals = {"metrics_enabled": True}
        if gateway:
            # Read path: the panel queries from inside its own container,
            # so the docker bridge address the playbook reports is
            # exactly right — and is not routable from off the host.
            vals["metrics_central_url"] = f"{gateway}/r/"
            if not (settings.metrics_remote_write_url or "").strip():
                # Write path: correct for agents on THIS host, which is
                # the co-located starting point. Anything else needs a
                # public endpoint, flagged below.
                vals["metrics_remote_write_url"] = f"{gateway}/w/"
        settings.write(vals)

        self._sys(
            f"✓ Metrics central is up and observability is enabled "
            f"(account {account})."
        )
        if gateway:
            self._sys(
                "Agents on this host will push to the local gateway. "
                "For agents on OTHER hosts, set a public HTTPS "
                "remote-write URL under Advanced — the local address is "
                "not reachable from them."
            )
        self._resolve_alert("metrics_central_failed")

    async def on_failure(self, results, errors):
        """Alert: without the central every metric alert is blind."""
        for err in errors:
            self._sys(f"✗ {err}")
        self._alert(
            "metrics_central_failed",
            "The metrics central could not be deployed. Metric-based "
            "alerts will not be evaluated until it is running.",
            level="critical",
        )
