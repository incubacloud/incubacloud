"""Deploy the central metrics stack (Fase 4 / A5).

VictoriaMetrics + Loki + Grafana behind **vmauth**, deployed by the panel
like any other host state — the owner's condition was "no manual steps,
deployed the way prepare-host is" (P9-5).

The target host is whichever ``cloud.host`` the job runs against, which
is what makes "co-locate now, move to a dedicated VPS later" a matter of
re-running the job elsewhere rather than a migration.

**Why vmauth and not a hand-written nginx.** The central is shared, and
VictoriaMetrics has no notion of accounts, so the boundary is imposed by
the proxy in front. vmauth is the auth proxy VictoriaMetrics ships for
exactly this: measured (lab, 2026-08-10) it DROPS a client's query arg
that collides with the one the route forces, where an nginx that appends
its own filter was bypassable by a client sending its own — so the
account filter cannot be widened, and it needs no hand-written reject
rules that would rot when VictoriaMetrics changes a query arg's meaning.
It cannot proxy websockets, which is why Grafana's browser path does not
go through it (Traefik carries that); everything vmauth serves is plain
request/response.
"""
import base64
import logging

import yaml

from .ansible_executor import AnsibleExecutor

_logger = logging.getLogger(__name__)

#: Compose service names inside the central stack. vmauth resolves these
#: over the compose network; they are not host ports and not reachable
#: from off the box.
_VM_URL = "http://victoriametrics:8428/"
_LOKI_URL = "http://loki:3100/"
_GRAFANA_URL = "http://grafana:3000/"


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

        The list becomes vmauth's user list, and the authenticated user
        *is* the label the central forces on every series. So adding an
        account here is the single act that grants a panel the right to
        write, and removing it is the single act that revokes it.
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        user, token = settings._metrics_auth()
        return [(user, token)] if (user and token) else []

    def _account_url_map(self, user):
        """Return the vmauth ``url_map`` for one account.

        Every route points ``url_prefix`` at the backend ROOT (carrying
        the forced query arg where needed) and drops the first path
        segment, so the client sends the real endpoint under the account
        prefix — ``/w/api/v1/write`` becomes ``/api/v1/write``. Measured
        (lab, 2026-08-10): vmauth APPENDS the remaining path to
        ``url_prefix`` where nginx replaced it, so a fixed-path prefix
        either duplicates the path or leaves a trailing slash Loki 404s.

        The forced ``extra_label`` (write) and ``extra_filters`` (read)
        are what pin every series to this account: a client that sends
        its own copy has it dropped for colliding, not merged.

        :param user: the account name, used both as the credential's user
            half and as the ``ic_account`` label value.
        :return: a list of vmauth url_map entries.
        """
        # {ic_account="<user>"} url-encoded: raw braces/quotes would break
        # the url_prefix as a URL. Verified in the lab.
        acct_filter = f'%7Bic_account%3D%22{user}%22%7D'
        return [
            {
                "src_paths": ["/w/.*"],
                "url_prefix": f"{_VM_URL}?extra_label=ic_account={user}",
                "drop_src_path_prefix_parts": 1,
            },
            {
                "src_paths": ["/r/.*"],
                "url_prefix": f"{_VM_URL}?extra_filters[]={acct_filter}",
                "drop_src_path_prefix_parts": 1,
            },
            {
                "src_paths": ["/lw/.*"],
                "url_prefix": _LOKI_URL,
                # SET, not appended: a client cannot spoof it. The header
                # belongs on the url_map entry, not the user — at user
                # level it did not apply in v1.102 (lab).
                "headers": [f"X-Scope-OrgID: {user}"],
                "drop_src_path_prefix_parts": 1,
            },
            {
                "src_paths": ["/lr/.*"],
                "url_prefix": _LOKI_URL,
                "headers": [f"X-Scope-OrgID: {user}"],
                "drop_src_path_prefix_parts": 1,
            },
        ]

    def _vmauth_config(self, accounts, operator_token, grafana_admin_basic):
        """Render the vmauth ``-auth.config`` YAML for the whole central.

        One vmauth ``user`` per account (writes and reads only its own
        series and logs) plus one operator (the cross-account read, and
        the Grafana admin API used to provision organisations). There is
        deliberately NO ``unauthorized_user`` section: its absence is what
        makes a credential-less request 401 rather than proxied.

        Passwords are in clear here — vmauth has no hash support. It is
        mitigated the same way the agents' copy of the token is: the file
        lives in the central's 0700 directory and every task that writes
        it is ``no_log``. The token already exists in clear on every agent
        and in each Grafana datasource, so this adds no new exposure.

        :param accounts: ``[(user, token), ...]``.
        :param operator_token: the cross-account operator credential.
        :param grafana_admin_basic: base64 ``admin:<pw>`` for the header
            swap on the Grafana admin path.
        :return: the YAML document as a string.
        """
        users = [
            {
                "username": user,
                "password": token,
                "url_map": self._account_url_map(user),
            }
            for user, token in accounts
            if user and token
        ]
        users.append({
            "username": "operator",
            "password": operator_token,
            "url_map": [
                {
                    "src_paths": ["/admin-r/.*"],
                    "url_prefix": _VM_URL,
                    "drop_src_path_prefix_parts": 1,
                },
                {
                    "src_paths": ["/gadmin/.*"],
                    "url_prefix": _GRAFANA_URL,
                    # vmauth consumes the operator's Authorization to
                    # authenticate, then this REPLACES it with Grafana's
                    # admin credential before forwarding (verified in the
                    # lab). So the panel never holds Grafana's password.
                    "headers": [f"Authorization: Basic {grafana_admin_basic}"],
                    "drop_src_path_prefix_parts": 1,
                },
            ],
        })
        return yaml.safe_dump({"users": users}, sort_keys=False)

    def get_extra_vars(self):
        """Hand retention, credentials and Grafana auth to the playbook."""
        settings = self.env["cloud.settings"].sudo()._get_system()
        # Mint this panel's own credential BEFORE listing the accounts.
        # It used to be created in on_success, which meant the very first
        # deployment wrote an empty account file: a central that came up
        # healthy and accepted no writes at all, needing a second
        # deployment to become usable. Nothing about that was visible
        # from the job log.
        settings._ensure_metrics_credential()
        accounts = self._metrics_accounts()
        operator_token = settings._ensure_operator_credential()
        admin_password = settings._ensure_grafana_admin_password()
        self._sys(
            f"Deploying the metrics central on {self._host().name} "
            f"(retention {settings.metrics_retention_days or 90} days, "
            f"{len(accounts)} account(s))."
        )
        if not accounts:
            # Unreachable for core, which just minted its own. A layer
            # above could still hand back nothing, and a central that
            # accepts no writes looks healthy from every angle — so say
            # it rather than let it pass silently.
            self._sys(
                "⚠ No metrics account resolved, so this central will "
                "accept no writes from anyone."
            )
        grafana_admin_basic = base64.b64encode(
            f"admin:{admin_password}".encode()
        ).decode()
        return {
            "ic_retention_days": settings.metrics_retention_days or 90,
            # The whole vmauth access-control list in one document: the
            # user list IS the boundary. The playbook writes it verbatim.
            "ic_vmauth_config": self._vmauth_config(
                accounts, operator_token, grafana_admin_basic,
            ),
            # Plaintext, because each organisation's datasource has to
            # authenticate AS that account — a hash cannot be used to
            # make a request. Every task consuming it is ``no_log``.
            "ic_accounts": [
                {"user": user, "password": password}
                for user, password in accounts
            ],
            # Grafana's datasource and the post-deploy health check both
            # need to *use* the credential, not just verify it, so the
            # plaintext travels too. It never lands on disk in clear: the
            # tasks that consume it are ``no_log``.
            "ic_operator_plain": operator_token,
            "ic_grafana_admin_password": admin_password,
            "ic_grafana_admin_basic": grafana_admin_basic,
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
        # Already minted before the playbook ran; read it back for the log.
        account = settings.metrics_account or ""

        vals = {"metrics_enabled": True}
        if gateway:
            # Read path: the panel queries from inside its own container,
            # so the docker bridge address the playbook reports is
            # exactly right — and is not routable from off the host.
            # ``/r`` alone: promql_query appends ``/api/v1/query``, and
            # vmauth drops the ``/r`` segment before forwarding.
            vals["metrics_central_url"] = f"{gateway}/r"
            if not (settings.metrics_remote_write_url or "").strip():
                # Write path: correct for agents on THIS host, which is
                # the co-located starting point. The endpoint travels in
                # the URL because vmauth appends it under the account
                # prefix — vmagent posts straight to this. Anything else
                # needs a public endpoint, flagged below.
                vals["metrics_remote_write_url"] = (
                    f"{gateway}/w/api/v1/write"
                )
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
