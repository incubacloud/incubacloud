"""Deploy the central metrics stack (Fase 4 / A5).

VictoriaMetrics + Loki behind **vmauth**, deployed by the panel
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

The panel draws every chart itself; the central ran Grafana until
12-nov-2026, and a deployment removes what it left (``ic_purge_grafana``).
"""
import logging

import yaml

from odoo import _

from ..net.trusted_proxies import parse_ranges
from .abstract_executor import JobRefused
from .ansible_executor import AnsibleExecutor

_logger = logging.getLogger(__name__)

#: Compose service names inside the central stack. vmauth resolves these
#: over the compose network; they are not host ports and not reachable
#: from off the box.
_VM_URL = "http://victoriametrics:8428/"
_LOKI_URL = "http://loki:3100/"

#: What the playbook deploys, as a number the panel keeps
#: (``cloud.settings.metrics_central_revision``) once a deployment with it
#: ends well. The reconciliation cron deploys the central again while the
#: number is behind: an update that changes the central converges even
#: when its first deployment was run by older code, failed, or waited for
#: observability to be switched back on (review of 12-nov-2026). Raise it
#: with every change of the playbook a deployed central needs.
#:
#: 1. 12-nov-2026: the central without Grafana, whose volume, provisioning
#:    and image it removes.
CENTRAL_REVISION = 1

#: Its existence keeps what Grafana left on the central through the
#: deployments, for an operator who wants it before the update removes it
#: (the owner decided on 10-nov-2026 to remove it; review of 12-nov-2026).
_KEEP_GRAFANA_PARAM = "incubacloud.metrics_keep_grafana_data"

# What an account may reach on the central, and nothing else. Each is a
# vmauth ``src_paths`` regex, which vmauth matches against the WHOLE path.
#
# The routes used to be ``/w/.*`` and friends, and VictoriaMetrics serves
# writes, deletions and its admin endpoints on the same port as reads.
# Measured (lab, 2026-09-27): through the read route an account could
# POST ``/r/api/v1/import/prometheus`` with another account's
# ``ic_account`` — that route forces a read filter, not a write label —
# and the other account then saw the series as its own. The same route
# created snapshots, forced merges and read the backend's flags.
#
# Writes are the one path each agent posts to. Reads are the query API a
# dashboard and a metrics browser call; the panel uses ``query`` and
# ``query_range``, and which of the rest the security view needs is
# decided with it (review of 12-nov-2026). The log read route is
# read-only, for the panel's reading of the security logs.
_ACCOUNT_WRITE_PATH = "/w/api/v1/write"
_READ_ENDPOINTS = (
    "query|query_range|query_exemplars|series|labels"
    "|label/[^/]+/values|metadata|rules|status/buildinfo"
)
_ACCOUNT_READ_PATHS = f"/r/api/v1/({_READ_ENDPOINTS})"
_ACCOUNT_LOG_WRITE_PATH = "/lw/loki/api/v1/push"
_ACCOUNT_LOG_READ_PATHS = (
    "/lr/loki/api/v1/(query|query_range|series|labels"
    "|label/[^/]+/values|index/stats|index/volume|index/volume_range)"
)

#: The operator's cross-account read: the accounts' read endpoints and
#: ``health``. It was ``/admin-r/.*``, the whole backend: imports,
#: snapshots, flags. Closed (12-nov-2026) before the route can be opened
#: under the public name to the addresses in
#: ``metrics_central_operator_sources``. ``health`` reveals nothing and
#: is what the deployment, the account sync and RB-11 probe.
_OPERATOR_READ_PATHS = f"/admin-r/(health|api/v1/({_READ_ENDPOINTS}))"

#: The operator's deletion route. Exact, so the key it carries opens
#: ``delete_series`` and no other endpoint that honours ``authKey``.
_OPERATOR_DELETE_PATH = "/admin-d/api/v1/admin/tsdb/delete_series"

#: Requests the operator may have in flight at once; vmauth answers the
#: next one 429 at once, without a queue. Only the operator has a cap
#: (12-nov-2026): vmauth counts per user, not per route, and an account's
#: user is shared by its agents' writes, its logs and its panel, so a cap
#: there refused writes while reads ran — and after an outage every host
#: empties its queue at once. The operator never writes; 16 covers four
#: fleet boards at four queries each. VictoriaMetrics queues the reads themselves
#: (``-search.maxConcurrentRequests``, twice the cores, 10 s wait).
_OPERATOR_MAX_CONCURRENT = 16

#: Set once the recorded deletion key has only ever reached the backend
#: in a file. Before 12-nov-2026 it was a flag of the VictoriaMetrics
#: container, which ``docker inspect`` shows; the next deployment
#: replaces such a key once and hands the new one in the file.
_DELETE_KEY_IN_FILE_PARAM = "incubacloud.metrics_delete_key_in_file"

#: The ``cloud.settings`` credentials a run may mint. Minted on a durable
#: cursor, then copied into the job's cache so the rest of the run reads
#: them from there.
_CENTRAL_SECRET_FIELDS = (
    "metrics_account",
    "metrics_remote_write_token",
    "metrics_operator_token",
    "metrics_delete_auth_key",
)


class ObservabilityCentralExecutor(AnsibleExecutor):
    """Deploy VictoriaMetrics, Loki and vmauth on this job's host."""

    _job_type = "deploy_metrics_central"
    _playbook = "playbooks/observability_central.yml"

    def _host(self):
        """Return the host this job targets."""
        return self.job.host_id

    def _metrics_accounts(self, settings=None):
        """Return ``[(user, password), ...]`` allowed to write and read.

        Core knows exactly one account: this panel's own. A layer
        that serves other panels overrides this to append one entry per
        panel, because only it knows they exist — core ships to partners
        and must stay ignorant of them.

        The list becomes vmauth's user list, and the authenticated user
        *is* the label the central forces on every series. So adding an
        account here is the single act that grants a panel the right to
        write, and removing it is the single act that revokes it.

        :param settings: the ``cloud.settings`` record to resolve the
            list through. Listing may mint a credential for every
            account that lacks one, so the caller's environment decides whether
            those survive; defaults to this executor's, for callers that
            only read.
        """
        if settings is None:
            settings = self.env["cloud.settings"].sudo()
        return settings._desired_metrics_accounts()

    def _accounts_for_deployment(self, settings=None):
        """Return the account list this run is built from, memoised.

        ``_metrics_accounts()`` is read more than once while the playbook
        is assembled, and what gets recorded afterwards has to be what
        was actually shipped rather than what the database says by the
        time the job finishes. An account minted while the playbook runs
        must not be recorded as granted, because on the gateway it is
        not — and a record of intent instead of fact would defeat the
        record's only purpose.

        :param settings: passed to :meth:`_metrics_accounts` on the first
            call; :meth:`_ensure_central_secrets` hands it the durable one.
        """
        if not hasattr(self, "_shipped_accounts"):
            self._shipped_accounts = self._metrics_accounts(settings)
        return self._shipped_accounts

    def _ensure_central_secrets(self, mint_delete_key):
        """Mint what the central needs, durably, and return what this run ships.

        Every credential here is generated on first use and must then
        outlive the job. This runs in the job's asynchronous phase, whose
        own writes the runner discards: measured in devel on 2026-09-28,
        the three secrets were generated, handed to the playbook and
        dropped when the job ended — and core 1.0.135 lost the deletion
        key the same way in production. So they are minted in
        :meth:`_durable_env` and then copied into the job's cache, where
        the rest of the run finds them.

        The panel's own account is minted before the list is built. It
        used to be created in ``on_success``, so the very first
        deployment wrote an empty account file: a central that came up
        healthy and accepted no writes at all.

        The list is built in the same environment, because a layer may
        mint credentials while listing and those have to survive
        too.

        :param bool mint_delete_key: whether to mint the deletion key
            when absent. Only the full deployment restarts the backend
            with it (see ``cloud.settings._ensure_metrics_delete_key``);
            otherwise it is only read.
        :return: ``(accounts, operator_token, delete_key)`` where
            ``accounts`` is ``[(user, password), ...]`` and ``delete_key``
            may be empty
        """
        with self._durable_env() as env:
            settings = env["cloud.settings"].sudo()._get_system()
            settings._ensure_metrics_credential()
            accounts = self._accounts_for_deployment(settings)
            operator_token = settings._ensure_operator_credential()
            if mint_delete_key:
                # Minted once and then kept, except for a key recorded
                # before the backend read it from a file: that one was a
                # flag of the container, readable with ``docker inspect``
                # by anyone in the host's docker group, so it is replaced
                # the first time. The mark is written with the key, on
                # this durable cursor (written here, not in a helper, so
                # the executor write rule sees the durable block): a
                # deployment that fails afterwards leaves the new key for
                # the next one, as it always left a freshly minted one.
                params = env["ir.config_parameter"].sudo()
                if (
                    settings.metrics_delete_auth_key
                    and not params.get_param(_DELETE_KEY_IN_FILE_PARAM)
                ):
                    settings.write({"metrics_delete_auth_key": False})
                    self._sys("Replacing the metrics deletion key: the "
                              "backend received the old one as a visible "
                              "flag.")
                delete_key = settings._ensure_metrics_delete_key()
                params.set_param(_DELETE_KEY_IN_FILE_PARAM, "1")
            else:
                delete_key = settings.metrics_delete_auth_key or ""
            values, volatile = self._durable_values(
                settings, _CENTRAL_SECRET_FIELDS,
            )
            settings_id = settings.id
        self._mirror_to_job_cache(
            self.env["cloud.settings"].sudo().browse(settings_id),
            values, volatile,
        )
        return accounts, operator_token, delete_key

    def _record_shipped_accounts(self):
        """Write down the access-control list this run put in force.

        Without it, "the gateway has never heard of this account" and
        "we have not told this panel about it yet" are indistinguishable
        from the panel — and nothing can safely decide whether handing a
        credential out will authenticate or 401 forever.
        """
        accounts = getattr(self, "_shipped_accounts", None)
        if accounts is None:
            # The playbook ran, so the list was built; only a subclass
            # bypassing _accounts_for_deployment() reaches this. Guessing
            # would grant access on paper that does not exist on disk.
            _logger.warning(
                "Metrics central job finished without a recorded account "
                "list; the account record was left untouched.",
            )
            return
        settings = self.env["cloud.settings"].sudo()._get_system()
        settings.write({
            "metrics_accounts_deployed": "\n".join(
                sorted(user for user, _password in accounts)
            ),
        })
        # The accounts authenticate from now on: whoever hands them out
        # hears it here, from both jobs that write the list.
        settings._metrics_accounts_shipped()

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
        its own copy has it dropped for colliding, not merged. Forcing is
        only half the boundary, though: each route also names the exact
        endpoints it serves (see ``_ACCOUNT_READ_PATHS``), because a
        forced read filter does nothing to a write sent down the read
        route.

        :param user: the account name, used both as the credential's user
            half and as the ``ic_account`` label value.
        :return: a list of vmauth url_map entries.
        """
        # {ic_account="<user>"} url-encoded: raw braces/quotes would break
        # the url_prefix as a URL. Verified in the lab.
        acct_filter = f'%7Bic_account%3D%22{user}%22%7D'
        return [
            {
                "src_paths": [_ACCOUNT_WRITE_PATH],
                "url_prefix": f"{_VM_URL}?extra_label=ic_account={user}",
                "drop_src_path_prefix_parts": 1,
            },
            {
                "src_paths": [_ACCOUNT_READ_PATHS],
                "url_prefix": f"{_VM_URL}?extra_filters[]={acct_filter}",
                "drop_src_path_prefix_parts": 1,
            },
            {
                "src_paths": [_ACCOUNT_LOG_WRITE_PATH],
                "url_prefix": _LOKI_URL,
                # SET, not appended: a client cannot spoof it. The header
                # belongs on the url_map entry, not the user — at user
                # level it did not apply in v1.102 (lab).
                "headers": [f"X-Scope-OrgID: {user}"],
                "drop_src_path_prefix_parts": 1,
            },
            {
                "src_paths": [_ACCOUNT_LOG_READ_PATHS],
                "url_prefix": _LOKI_URL,
                "headers": [f"X-Scope-OrgID: {user}"],
                "drop_src_path_prefix_parts": 1,
            },
        ]

    def _vmauth_config(self, accounts, operator_token, delete_key=""):
        """Render the vmauth ``-auth.config`` YAML for the whole central.

        One vmauth ``user`` per account (writes and reads only its own
        series and logs) plus one operator (the cross-account read of the
        same endpoints, and series deletion), the only user with a cap on
        concurrent requests (see ``_OPERATOR_MAX_CONCURRENT``). There is
        deliberately NO ``unauthorized_user`` section:
        its absence is what makes a credential-less request 401 rather
        than proxied.

        Passwords are in clear here — vmauth has no hash support. It is
        mitigated the same way the agents' copy of the token is: the file
        lives in the central's 0700 directory and every task that writes
        it is ``no_log``. The token already exists in clear on every agent,
        so this adds no new exposure.

        The deletion key is the one secret here that no caller holds:
        VictoriaMetrics refuses any deletion without it, and only the
        operator's deletion route adds it. Measured (lab, 2026-09-27): an
        ``authKey`` the client sends collides with the forced one and is
        dropped, and the prefix has to be the backend ROOT — vmauth
        appends the path, so a prefix naming ``delete_series`` answers
        400.

        :param accounts: ``[(user, token), ...]``.
        :param operator_token: the cross-account operator credential.
        :param delete_key: VictoriaMetrics' ``-deleteAuthKey``; empty
            leaves the deletion route out, for a central not yet started
            with a key.
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
        operator_map = [
            {
                "src_paths": [_OPERATOR_READ_PATHS],
                "url_prefix": _VM_URL,
                "drop_src_path_prefix_parts": 1,
            },
        ]
        if delete_key:
            operator_map.append({
                "src_paths": [_OPERATOR_DELETE_PATH],
                "url_prefix": f"{_VM_URL}?authKey={delete_key}",
                "drop_src_path_prefix_parts": 1,
            })
        users.append({
            "username": "operator",
            "password": operator_token,
            "max_concurrent_requests": _OPERATOR_MAX_CONCURRENT,
            "url_map": operator_map,
        })
        return yaml.safe_dump({"users": users}, sort_keys=False)

    def pre_run_checks(self):
        """Refuse a run that would orphan the central or publish it wrongly.

        On a host other than the central's it would start an empty
        central and leave the history behind; on a host without Full
        Setup or without a direct port, the router would name a proxy or
        an entrypoint the host does not have.

        :raises JobRefused: saying what to do.
        """
        super().pre_run_checks()
        problem = (
            self._central_elsewhere_problem()
            or self._public_proxy_problem()
            or self._direct_port_problem()
        )
        if problem:
            self._write_system_direct(f"⚠ {problem}")
            raise JobRefused(problem)

    def _discards_history(self):
        """Return whether this run was asked to start an empty central.

        Only *Start an empty central* sets ``discard_history``, through
        ``/cloud/monitoring/empty_central``; ``enqueue`` refuses it to
        anyone without superuser rights (``_check_internal_queue_flags``).

        :rtype: bool
        """
        payload = self.job.payload
        return isinstance(payload, dict) and payload.get("discard_history") is True

    def _central_elsewhere_problem(self):
        """Return why this run must not touch a central on this host, or ''.

        A deployment on a host other than the central's started an empty
        central there and moved the panel to it, leaving the history
        behind on the old host, unread. While the central runs elsewhere,
        only *Start an empty central* may do that. The account sync,
        which inherits this check, only ever targets the central's host.

        :rtype: str
        """
        Settings = self.env["cloud.settings"].sudo()
        central = Settings._live_central_host()
        if not central or central == self._host():
            return ""
        if self._discards_history():
            return ""
        return Settings._central_elsewhere_refusal(central)

    def _public_proxy_problem(self):
        """Return why this run cannot publish the central under a name, or ''.

        The central installs no Traefik: under a public name it joins the
        one Full Setup leaves on every managed host (``traefik_deployed``
        is set when Full Setup ends).

        :rtype: str
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        if not (settings.metrics_central_public_host or "").strip():
            return ""
        if self._host().traefik_deployed:
            return ""
        return _(
            "Full Setup has not been run on this server: run it and enable "
            "observability again."
        )

    def _direct_port_problem(self):
        """Return why this run cannot publish the central's direct name, or ''.

        :rtype: str
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        if not (settings.metrics_central_direct_host or "").strip():
            return ""
        if self._host().direct_port_shipped:
            return ""
        return _(
            "The central's host has no direct port applied. Set the port on "
            "the host and press Full Setup."
        )

    def get_extra_vars(self):
        """Hand retention, credentials and the public name to the playbook."""
        settings = self.env["cloud.settings"].sudo()._get_system()
        accounts, operator_token, delete_key = (
            self._ensure_central_secrets(mint_delete_key=True)
        )
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
        public = self._public_endpoint(settings)
        keep_grafana = bool(
            self.env["ir.config_parameter"].sudo().get_param(_KEEP_GRAFANA_PARAM)
        )
        if keep_grafana:
            self._sys(
                f"{_KEEP_GRAFANA_PARAM} is set: what Grafana left on the "
                "central is kept."
            )
        return {
            "ic_retention_days": settings.metrics_retention_days or 90,
            # The whole vmauth access-control list in one document: the
            # user list IS the boundary. The playbook writes it verbatim.
            "ic_vmauth_config": self._vmauth_config(
                accounts, operator_token, delete_key,
            ),
            # Written to a file the VictoriaMetrics container reads. The
            # same key sits on the operator's deletion route above;
            # nothing else has it.
            "ic_delete_auth_key": delete_key,
            # The post-deploy health check has to *use* the credential, not
            # just verify it, so the plaintext travels. It never lands on
            # disk in clear: the tasks that consume it are ``no_log``.
            "ic_operator_plain": operator_token,
            # False while the parameter exists: what Grafana left stays.
            "ic_purge_grafana": not keep_grafana,
            # Empty keeps the central on this host's docker bridge only,
            # exactly as before. Set, the playbook also publishes vmauth
            # through this host's Traefik under the name.
            "ic_public_host": public["host"] if public else "",
            "ic_public_tls": public["tls"] if public else "",
            "ic_public_forwarded": public["forwarded"] if public else False,
            "ic_operator_sources": public["operator_sources"] if public else [],
            # The central's direct name, for the platform's own servers on
            # the host's direct entrypoint. Empty publishes nothing more.
            "ic_direct_host": (
                settings.metrics_central_direct_host or ""
            ).strip().lower(),
        }

    def _public_endpoint(self, settings):
        """Where the central is published, or None to keep it on the bridge.

        :param settings: the ``cloud.settings`` record.
        :returns: ``{"host", "tls", "forwarded", "operator_sources",
            "read", "write"}`` for a public name, else None.
        :rtype: dict or None
        """
        host_name = (settings.metrics_central_public_host or "").strip().lower()
        if not host_name:
            return None
        base = f"https://{host_name}"
        return {
            "host": host_name,
            # Behind a CDN that already holds a certificate for the name,
            # the host's default one; otherwise one of its own.
            "tls": self._host()._router_tls_mode(host_name),
            # Reached through a CDN, the connection comes from the CDN's
            # edge, not from the operator: the deletion route then reads
            # the address the CDN forwards, as the host's rate limit does.
            "forwarded": self._host()._name_is_proxied(host_name),
            # The host itself always; whoever the operator names besides
            # (the panel's public address once it runs elsewhere).
            "operator_sources": ["127.0.0.1/32"]
            + parse_ranges(settings.metrics_central_operator_sources),
            "read": f"{base}/r",
            "write": f"{base}/w/api/v1/write",
        }

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
        # The central this one replaces, when it was started empty.
        left_behind = settings._live_central_host() - self._host()

        # Where the central lives, so a later account sync knows which
        # host to reach. Recorded on success rather than on enqueue: the
        # job's target host is only the central's home once the stack is
        # actually running on it, and a wrong answer here sends the sync
        # to write an access-control list nobody serves.
        vals = {
            "metrics_enabled": True,
            "metrics_central_host_id": self._host().id,
        }
        public = self._public_endpoint(settings)
        if public:
            # Published under its own name: the panel and every agent use
            # it, wherever they run. The bridge address below is only
            # right for what runs on this very host.
            vals["metrics_central_url"] = public["read"]
            vals["metrics_remote_write_url"] = public["write"]
        elif gateway:
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
        vals["metrics_central_revision"] = CENTRAL_REVISION
        settings.write(vals)
        self._record_shipped_accounts()
        self._report_grafana_purge()
        if (settings.metrics_central_direct_host or "").strip():
            # Probe the name now rather than at the cron's next pass.
            self._host()._enqueue_direct_sync("central deployed", chained=True)

        self._sys(
            f"✓ Metrics central is up and observability is enabled "
            f"(account {account})."
        )
        if public:
            self._sys(
                f"Published as https://{public['host']}: the panel and every "
                "agent use it. Agents already installed pick it up when "
                "they are next applied."
            )
        elif gateway:
            self._sys(
                "Agents on this host will push to the local gateway. "
                "For agents on OTHER hosts, set a public HTTPS "
                "remote-write URL under Advanced — the local address is "
                "not reachable from them."
            )
        if left_behind:
            self._sys(
                f"The central on {left_behind.name} is left as it was, with "
                "its history: the panel no longer reads it. It goes "
                "with that server, or by hand with `docker compose -p "
                "incubacloud-central down -v` there."
            )
            if public:
                # Not checked here: behind a CDN the name answers with the
                # CDN's addresses, and a host's own address may be one only
                # the panel reaches.
                self._sys(
                    f"⚠ {public['host']} must point at {self._host().name} "
                    f"({self._host().ip_address}) in DNS: until it does, the "
                    "panel and the agents still reach the central on "
                    f"{left_behind.name}."
                )
        self._resolve_alert("metrics_central_failed")

    def _report_grafana_purge(self):
        """Say in the job's log what became of what Grafana left.

        A purge that failed does not fail the job: the central works, and
        ``metrics_central_failed`` would say otherwise.
        """
        state = str(self.playbook_facts().get("ic_grafana_purge") or "")
        if state == "removed":
            self._sys("What Grafana left on the central is removed.")
        elif state.startswith("failed"):
            self._sys(
                "⚠ What Grafana left on the central could not be removed "
                f"({state[len('failed:'):].strip()}). Remove the volume "
                "incubacloud-central_grafana-data on the central's server "
                "by hand; the central works without it."
            )

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
