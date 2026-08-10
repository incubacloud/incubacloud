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

from .ansible_executor import AnsibleExecutor

_logger = logging.getLogger(__name__)


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
        base = (settings.metrics_remote_write_url or "").strip().rstrip("/")
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
        account, token = settings._metrics_auth()
        return {
            "ic_host_id": str(host.id),
            "ic_host_name": host.name or "",
            "ic_remote_write_url": settings.metrics_remote_write_url or "",
            "ic_logs_write_url": self._logs_write_url(settings),
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
                self._instance_labels(inst) for inst in instances
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
        return errors

    async def on_success(self, results):
        """Report the outcome, record enrolment and clear the alert."""
        facts = self.playbook_facts()
        self._sys(
            f"✓ Observability agents running. "
            f"{facts.get('ic_instances_labelled') or 0} instance(s) labelled."
        )
        self._host()._mark_observability_installed()
        self._resolve_alert("observability_agents_failed")

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
