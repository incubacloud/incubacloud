"""Install the per-host observability agents (Fase 4 / A2).

Machine state on a host, so it is Ansible — the frontier drawn in Fase 3
("machine state = Ansible; application operation = versioned script")
applies unchanged.

What this executor owns that the playbook cannot: the **label map**. Only
the panel knows which compose project belongs to which instance, project
and tenant, so it generates that mapping and hands it over as extra-vars.
Labels are attached at ingest because they cannot be added to historical
series afterwards.
"""
import logging

from odoo import _
from odoo.exceptions import UserError

from . import _direct_probe
from .abstract_executor import CANCELLED_WHILE_RUNNING
from .ansible_executor import AnsibleExecutor

_logger = logging.getLogger(__name__)

#: What the playbook installs, as a number the host keeps
#: (``cloud.host.metrics_agents_revision``) once an install with it ends
#: well. The reconciliation cron re-applies, a few hosts per pass, every
#: installed host below it, so a host whose last install ran an older
#: playbook (a panel still on older code, or one rebuilt by another
#: panel) converges without a burst at upgrade. Raise it with every change
#: of the playbook that installed hosts need.
#:
#: 1. 12-nov-2026: the database collector (``collect_pg.sh``) and the
#:    sizes of each instance's database and filestore.
AGENTS_REVISION = 1


class ObservabilityAgentsExecutor(AnsibleExecutor):
    """Deploy node_exporter + cAdvisor + vmagent on this job's host."""

    _job_type = "install_observability"
    _playbook = "playbooks/host_observability.yml"

    def _host(self):
        """Return the host this job targets."""
        return self.job.host_id

    def _instance_labels(self, instance):
        """Return the label map for one instance.

        Core knows host, project and instance. ``tenant`` is deliberately
        empty here: core ships to partners and has no notion of tenants —
        the SaaS layer overrides this to fill it in. Keeping the key
        present (and empty) means the playbook template does not have to
        care which layer produced the map.
        """
        return {
            "compose_project": instance.doodba_project_name or instance.name,
            "instance_id": str(instance.id),
            "instance": instance.name or "",
            "project": (
                instance.project_id.name if instance.project_id else ""
            ),
            "tenant": "",
            "tenant_id": "",
            "dir": instance.get_remote_dir(),
            # What the database collectors connect to, through the
            # socket of the instance's own ``db`` container.
            "dbname": instance.postgres_dbname or "prod",
            "dbuser": instance.postgres_username or "odoo",
            # Prefix of the Traefik service/router names this instance
            # answers under. The panel runs ``copier copy`` itself and
            # feeds it ``project_name = doodba_project_name`` — the same
            # string it forces into COMPOSE_PROJECT_NAME — so the names
            # are derived, never guessed. Everything after the prefix
            # varies (``-prod-main``, ``-prod-longpolling``,
            # ``-test-…``), which is why only the prefix is pinned here.
            "traefik_prefix": self._traefik_prefix(instance),
            # Extra service names an upper layer put in front of this
            # instance. Core never does; the SaaS manager adds its
            # sleep-proxy router here, because only it knows one exists.
            "traefik_extra": [],
        }

    #: The labels people name freely: a project's name here, and a module
    #: that fills in another such label adds it. Ansible templates the
    #: extra vars it is handed (``-e @file``); these reach it marked as not
    #: templates.
    _FREE_TEXT_LABELS = ("project",)

    def _labels_as_data(self, labels):
        """Mark the free-text values of *labels* as data for Ansible.

        Ansible reads the extra-vars file as JSON first, and a value
        written as ``{"__ansible_unsafe": text}`` reaches the playbook as
        *text* that is never rendered as a template.

        :param dict labels: one instance's map (``_instance_labels``).
        :return: a copy with each of ``_FREE_TEXT_LABELS`` so marked.
        :rtype: dict
        """
        return labels | {
            key: {"__ansible_unsafe": labels.get(key) or ""}
            for key in self._FREE_TEXT_LABELS
        }

    def _traefik_prefix(self, instance):
        """Return the prefix Traefik names this instance's services with.

        ``19.0`` becomes ``19-0`` because that is what the copier
        template does when it builds the names.

        :param instance: the ``cloud.instance`` to describe.
        :return: e.g. ``acme-prod-19-0-``, or ``""`` when the version is
            unknown and a prefix would match too much.
        """
        name = instance.doodba_project_name or instance.name or ""
        version = (instance.odoo_version or "").replace(".", "-")
        if not (name and version):
            return ""
        return f"{name}-{version}-"

    def _instances(self):
        """Return the instances whose metrics this host should label.

        Draft instances are skipped: nothing of theirs runs on the host
        yet, so a relabel rule for them would never match anything.
        """
        return self._host().instance_ids.filtered(
            lambda i: i.state != "draft",
        )

    #: The write endpoint our own central exposes, under the account
    #: prefix vmauth strips. Recognising it is what tells "this is our
    #: gateway, derive the log endpoint" apart from "a foreign backend,
    #: collect nothing".
    _METRICS_WRITE_SUFFIX = "/w/api/v1/write"
    _LOGS_WRITE_SUFFIX = "/lw/loki/api/v1/push"

    def _logs_write_url(self, settings):
        """Return where access logs are pushed, or '' to skip collection.

        Derived from the metrics endpoint rather than configured
        separately: on our gateway the two live side by side (``/w/`` and
        ``/lw/`` under vmauth), and one fewer field to fill in is one
        fewer field to get wrong.

        An endpoint that does not follow that shape — a self-hosted
        operator pointing at their own VictoriaMetrics, say — yields ''
        and the collector is simply not deployed. Guessing a URL there
        would start a container that retries forever against something
        that does not exist.

        :param settings: the ``cloud.settings`` singleton.
        :return: the push URL, or ''.
        """
        return self._logs_url_for(settings.metrics_remote_write_url)

    def _logs_url_for(self, write_url):
        """Return the logs push URL that goes with *write_url*, or ''.

        :param str write_url: a metrics remote-write URL.
        :rtype: str
        """
        base = (write_url or "").strip().rstrip("/")
        if not base.endswith(self._METRICS_WRITE_SUFFIX):
            return ""
        root = base[: -len(self._METRICS_WRITE_SUFFIX)]
        return f"{root}{self._LOGS_WRITE_SUFFIX}"

    def get_extra_vars(self):
        """Hand the agent configuration and the label map to the playbook."""
        host = self._host()
        settings = self.env["cloud.settings"].sudo()._get_system()
        # The reconciliation cron already refuses to queue this job while
        # observability is unconfigured, so reaching here that way should
        # be impossible — but the job is still enqueueable directly, and
        # deploying agents that push into the void leaves three
        # containers that look healthy on the host and report nowhere.
        # Cheap to assert, and the failure is otherwise invisible.
        if not settings.metrics_enabled:
            raise UserError(_(
                "Observability is disabled. Enable it in Settings and set "
                "the remote-write URL before installing agents."
            ))
        if not (settings.metrics_remote_write_url or "").strip():
            raise UserError(_(
                "No remote-write URL is configured, so the agents would "
                "have nowhere to push. Set it in Settings first."
            ))
        instances = self._instances()
        self._sys(
            f"Installing observability agents on {host.name} "
            f"({len(instances)} instance(s) to label)."
        )
        offer = host._direct_write_offer()
        # Kept for on_success: what the host tried is judged against them.
        self._direct_probes = host._direct_reach_probes(offer) if offer else []
        account, token = settings._metrics_auth()
        # Kept for on_success: a switch moved meanwhile is applied again.
        self._sent_collect_pg = bool(settings.metrics_collect_pg)
        return {
            "ic_host_id": str(host.id),
            "ic_host_name": host.name or "",
            "ic_remote_write_url": settings.metrics_remote_write_url or "",
            "ic_logs_write_url": self._logs_write_url(settings),
            # The direct path on offer, tried from the host before any
            # configuration is written; the URLs above are the fallback.
            "ic_direct_write_url": offer,
            "ic_direct_logs_write_url": self._logs_url_for(offer) if offer else "",
            "ic_direct_probes": [
                {"url": p["url"], "method": p["method"], "status": p["status"]}
                for p in self._direct_probes
            ],
            # SSH, sudo, auditd and fail2ban, besides the access log. Only
            # where the panel says so: the hosts have to be its own.
            "ic_ship_security_logs": bool(settings.metrics_ship_security_logs),
            # The per-minute database collector, root's; off removes it.
            "ic_collect_pg": self._sent_collect_pg,
            # User half of the credential. It is also the label the
            # central forces on everything this agent writes, so the copy
            # the agent sends is only a hint — the gateway overwrites it
            # from whoever authenticated. Sending it anyway keeps local
            # debugging honest: what you see in the agent config is what
            # you will see in the series.
            "ic_account": account,
            "ic_remote_write_user": account,
            "ic_remote_write_token": token,
            "ic_instances": [
                self._labels_as_data(self._instance_labels(inst))
                for inst in instances
            ],
        }

    def parse_results(self, results):
        """Fail when the playbook did, and report what came up."""
        errors = []
        rc = results.get(self._playbook, {}).get("exit_status", 1)
        if rc != 0:
            errors.append(
                f"Observability agent install failed (rc={rc}). See the log."
            )
            return errors
        facts = self.playbook_facts()
        running = int(facts.get("ic_agents_running") or 0)
        if running < 3:
            errors.append(
                f"Only {running}/3 agent containers came up "
                f"(node-exporter, cadvisor, vmagent)."
            )
        errors.extend(self._collector_errors(facts))
        return errors

    def _collector_errors(self, facts):
        """Return the job's errors for a collector the playbook could not set up.

        The playbook goes on past them, so the agents come up and the
        server's board says what is missing; the job still fails, with
        what to do, so that it is fixed now and not found a month later
        without the figures (the owner's decision, 12-nov-2026). Without
        cron neither collector can run: that is said once.

        :param dict facts: what the playbook exported.
        :return: the error lines, empty when both collectors are in place.
        :rtype: list[str]
        """
        if str(facts.get("ic_disk_collector") or "") == "failed":
            reason = str(facts.get("ic_cron_error") or "").strip()
            return [
                "cron is not installed on the server and could not be "
                "installed: install it (apt install cron) or use the "
                "IncubaCloud image."
                + (f" Reason: {reason}" if reason else "")
            ]
        if str(facts.get("ic_pg_collector") or "") != "failed":
            return []
        reason = str(facts.get("ic_pg_collector_error") or "").strip()
        if getattr(self, "_sent_collect_pg", True):
            what = (
                "The database collector could not be installed on the "
                "server: check that sudo asks no password and that apt is "
                "free, then run the agents' install again, or turn off "
                "Collect database figures in Settings → Monitoring."
            )
        else:
            what = (
                "The database collector, switched off, could not be "
                "removed from the server: check that sudo asks no "
                "password, then run the agents' install again."
            )
        return [what + (f" Reason: {reason}" if reason else "")]

    async def on_success(self, results):
        """Report the outcome, record enrolment and clear the alert."""
        facts = self.playbook_facts()
        self._sys(
            f"✓ Observability agents running. "
            f"{facts.get('ic_instances_labelled') or 0} instance(s) labelled."
        )
        host = self._host()
        host._mark_observability_installed()
        revision = self._revision_to_record(host, facts)
        if revision is not None:
            host.sudo().write({"metrics_agents_revision": revision})
        self._resolve_alert("observability_agents_failed")
        self._report_pg_collector(facts)
        installed = str(facts.get("ic_write_url_installed") or "")
        if installed:
            host.sudo().write({"metrics_agents_write_url": installed})
        probes = getattr(self, "_direct_probes", None) or []
        if probes:
            before = host.direct_reach_urls or ""
            report = _direct_probe.reach_report(facts.get("ic_direct_report"), probes)
            host._record_direct_reach(report)
            self._sys(
                "Direct path tried from the host: "
                + ", ".join(f"{a['url']} → {a['status']}" for a in report)
                + f". Agents write to {installed or 'the remote-write URL'}."
            )
            if (host.direct_reach_urls or "") != before:
                self.env["cloud.host"]._direct_path_changed(
                    f"direct URLs reached from {host.name} changed",
                )

    def _report_pg_collector(self, facts):
        """Say in the job's log what became of the database collector.

        A collector that failed fails the job instead
        (:meth:`_collector_errors`).

        :param dict facts: what the playbook exported.
        """
        state = str(facts.get("ic_pg_collector") or "")
        if state == "installed":
            self._sys("Database collector installed (every minute, as root).")
        elif state == "off":
            self._sys("Database collector switched off in Settings: removed.")

    def _revision_to_record(self, host, facts):
        """Return the revision this install leaves on *host*, or None for none.

        None when the database collector's switch moved while the install
        ran: the install applied the old value, and the switch already
        asked the reconciliation cron to re-apply every server; recording
        the current revision would cancel that for this one.

        :param host: the ``cloud.host`` the install ran on.
        :param dict facts: what the playbook exported.
        :return: the revision to write, or None to leave the host's.
        :rtype: int | None
        """
        sent = getattr(self, "_sent_collect_pg", None)
        settings = self.env["cloud.settings"].sudo()._get_system()
        if sent is not None and sent != bool(settings.metrics_collect_pg):
            self._sys(
                "The database collector's switch moved during this install: "
                "the reconciliation cron applies it again."
            )
            return None
        return AGENTS_REVISION

    async def on_cancelled_running(self, results, exc):
        """A stop is not a failed install: the host's agent state stays as it was.

        ``on_failure`` counts an attempt, moves the host to "failed" and
        says the cron will retry, and the count is what raises the
        alert after three. A person who cancelled the install caused
        none of that: the host keeps its state and its attempt count,
        and the cron decides on them as before.
        """
        self._sys(
            f"✗ {CANCELLED_WHILE_RUNNING} The host's agent state is unchanged; "
            "if its agents are not installed yet, the reconciliation cron "
            "queues the install again when the host is due."
        )

    async def on_failure(self, results, errors):
        """Record the failure so the cron retries, and alert if it persists.

        Deliberately quiet for the first couple of attempts: a failed
        install is usually a transient network blip or the containerd
        pull race the playbook documents, and the reconciliation cron
        will retry on its own. Alerting on every one of those would
        train operators to ignore the alert.
        """
        for err in errors:
            self._sys(f"✗ {err}")
        host = self._host()
        attempts = host._mark_observability_failed()
        if host._observability_alert_due(attempts):
            self._alert(
                "observability_agents_failed",
                f"Observability agents have failed to install on "
                f"{host.name} {attempts} times in a row, so it reports no "
                f"metrics. Retries continue with a growing delay.",
                level="warning",
            )
        else:
            self._sys(
                f"Attempt {attempts} failed; the reconciliation cron will "
                f"retry automatically."
            )
