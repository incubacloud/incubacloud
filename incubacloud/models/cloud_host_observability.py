"""Keep every host enrolled in observability, and its label map current.

Two jobs live here, and they answer different questions.

**Labels.** vmagent's scrape config carries one relabelling block per
instance on the host — that is what turns a raw cAdvisor container sample
into something attributable to a ``cloud.instance``, a project and a
tenant. The block is rendered from the instance list at the moment the
agents are installed, so it is a snapshot, and every instance deployed,
moved or removed afterwards left it stale. The playbook is idempotent, so
the fix is to re-run it whenever the set changes.

**Enrolment.** There is no host to which observability does not apply, so
it is not a decision anyone should have to make. It used to be one: the
agents were installed once, chained to host setup, and a host could end
up unmonitored in three ways that all failed silently — set up before
observability was switched on, set up with no remote-write URL, or a
single failed install that nobody retried. The reconciliation cron below
replaces that one-shot event with a state that converges: it is the
authority, the setup chain is only an accelerator, and turning
observability on is itself the instruction to enrol everything pending.
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

#: Back-off between install attempts, in minutes, indexed by how many
#: consecutive failures a host has had. A host nobody can reach must not
#: be hammered every tick, but must not be abandoned either.
_BACKOFF_MINUTES = (0, 15, 30, 60, 120)

#: Consecutive failures after which we stop being quiet about it.
_ALERT_AFTER_ATTEMPTS = 3


class CloudHost(models.Model):
    _inherit = "cloud.host"

    # ── Label refresh ─────────────────────────────────────────────────────

    def refresh_observability_labels(self, reason=""):
        """Re-apply the metrics agents so the label map matches reality.

        Best-effort by design: this is called from the tail of operations
        that have already succeeded (a deploy, a move, a removal), and a
        monitoring refresh must never turn one of those into a failure.

        Only hosts whose agents are actually installed are refreshed. That
        used to be decided by ``last_probed``, which the SSH telemetry job
        also writes — so with the fallback alive, every host looked
        enrolled and the guard did the opposite of what its comment
        claimed. ``metrics_agents_state`` is maintained by the install job
        itself and cannot be set by anything else.

        :param reason: short free text for the job log, e.g. 'deploy'.
        :return: the queued ``cloud.job`` id, or ``False`` if skipped.
        """
        self.ensure_one()
        if not self._observability_target():
            return False
        if self.metrics_agents_state != "installed":
            # Not enrolled yet. Enrolling here would make monitoring a
            # surprise side effect of deploying an instance; the
            # reconciliation cron owns that decision and will pick it up.
            return False
        return self._enqueue_observability(reason or "label refresh")

    # ── Enrolment ─────────────────────────────────────────────────────────

    def _observability_target(self):
        """Return True when this host should be running the agents.

        The genuine gate is "is there a central to push to", which is a
        settings-level fact evaluated continuously — not "did somebody
        press a button once".

        Hosts that cannot take agents yet, or are on their way out, are
        excluded: queuing an install against a half-built or dying machine
        produces noise, not monitoring.

        "Can take agents" is expressed as ``known_hosts_key``: the panel
        only has one once it has actually reached the host over SSH, so a
        record somebody typed in but never prepared is skipped without
        needing a lifecycle field that ``cloud.host`` does not have.
        """
        self.ensure_one()
        settings = self.env["cloud.settings"].sudo()._get_system()
        if not settings.metrics_enabled:
            return False
        if not (settings.metrics_remote_write_url or "").strip():
            return False
        if not settings.metrics_account:
            return False
        if not self.active:
            return False
        if not (self.known_hosts_key or "").strip():
            return False
        return True

    def _observability_due(self):
        """Return True when this host is due for an install attempt.

        Never-installed hosts are due immediately. Failed ones wait out a
        back-off that grows with the number of consecutive failures, so an
        unreachable host is retried at a decreasing rate rather than every
        tick.
        """
        self.ensure_one()
        if self.metrics_agents_state == "installed":
            return False
        if self.metrics_agents_state == "never":
            return True
        attempts = min(self.metrics_agents_attempts, len(_BACKOFF_MINUTES) - 1)
        wait = _BACKOFF_MINUTES[attempts]
        if not self.metrics_agents_since:
            return True
        age = (
            fields.Datetime.now() - self.metrics_agents_since
        ).total_seconds()
        return age >= wait * 60

    def _enqueue_observability(self, reason):
        """Queue the agent install for this host, swallowing failures.

        :param reason: short free text for the log.
        :return: the queued job id, or ``False``.
        """
        self.ensure_one()
        try:
            return self.env["cloud.job"].sudo().enqueue(
                self.id, False, "install_observability",
                # Often called from the on_success of a job that is still
                # 'started' on this host, so the active-job guard would
                # match that parent and refuse its own follow-up.
                bypass_running_check=True,
            )
        except Exception as exc:  # noqa: BLE001 — never fail the caller
            _logger.warning(
                "[metrics] could not queue agents on host %s (%s): %s",
                self.name, reason, exc,
            )
            return False

    @api.model
    def _cron_reconcile_observability(self):
        """Enrol every host that should be reporting and is not.

        This is the single mechanism that keeps observability applied.
        Switching it on in Settings needs no companion "enrol existing
        hosts" step: the next tick sees hosts that are targets and not
        installed, and queues them. Switching it off stops the cron dead,
        which is why the gate lives in ``_observability_target``.
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        if not settings.metrics_enabled:
            return
        candidates = self.sudo().search([
            ("metrics_agents_state", "!=", "installed"),
            ("active", "=", True),
        ])
        queued = 0
        for host in candidates:
            if not host._observability_target():
                continue
            if not host._observability_due():
                continue
            if host._enqueue_observability("reconciliation"):
                queued += 1
        if queued:
            _logger.info(
                "[metrics] reconciliation queued agents on %s host(s)", queued,
            )

    # ── State transitions, called by the install job ──────────────────────

    def _mark_observability_installed(self):
        """Record a successful agent install and clear the failure state."""
        self.ensure_one()
        self.sudo().write({
            "metrics_agents_state": "installed",
            "metrics_agents_since": fields.Datetime.now(),
            "metrics_agents_attempts": 0,
        })

    def _mark_observability_failed(self):
        """Record a failed install and return the attempt count so far.

        The count drives both the back-off and the point at which a host
        that keeps refusing the agents stops being a silent retry loop and
        becomes an alert.
        """
        self.ensure_one()
        attempts = (self.metrics_agents_attempts or 0) + 1
        self.sudo().write({
            "metrics_agents_state": "failed",
            "metrics_agents_since": fields.Datetime.now(),
            "metrics_agents_attempts": attempts,
        })
        return attempts

    def _observability_alert_due(self, attempts):
        """Return True when repeated install failures deserve an alert.

        :param attempts: consecutive failures, as returned by
            ``_mark_observability_failed``.
        """
        return attempts >= _ALERT_AFTER_ATTEMPTS
