"""Keep every host enrolled in observability, and its label map current.

Two jobs live here, and they answer different questions.

**Labels.** vmagent's scrape config carries one relabelling block per
instance on the host — that is what turns a raw cAdvisor container sample
into something attributable to a ``cloud.instance``, a project and an
account. The block is rendered from the instance list at the moment the
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

import asyncssh

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .observability_agents_executor import AGENTS_REVISION
from .observability_central_executor import CENTRAL_REVISION

_logger = logging.getLogger(__name__)

#: Back-off between install attempts, in minutes, indexed by how many
#: consecutive failures a host has had. A host nobody can reach must not
#: be hammered every tick, but must not be abandoned either.
_BACKOFF_MINUTES = (0, 15, 30, 60, 120)

#: Consecutive failures after which we stop being quiet about it.
_ALERT_AFTER_ATTEMPTS = 3

#: Installed hosts whose agents are re-applied per pass of the cron when
#: their last install ran an older playbook (``AGENTS_REVISION``): five
#: every fifteen minutes, twenty an hour, so an upgrade spreads the
#: installs over the fleet instead of queuing them all at once, each
#: pulling images and installing a package on its host (12-nov-2026).
_REAPPLY_PER_PASS = 5

#: Job states in which an install is still to end: a host with one is
#: left to it rather than counted against the pass.
_UNFINISHED_JOB_STATES = ("queued", "pending", "enqueued", "wait_dependencies", "started")


#: What the agents' install reads from a host besides its instances: the
#: switch, and the key and user that tell the panel's own sign-ins and
#: sudo from anyone else's. A change re-applies that host's agents.
_AGENTS_HOST_FIELDS = ("security_logs_enabled", "key_file", "user", "login_type")


class CloudHost(models.Model):
    _inherit = "cloud.host"

    security_logs_enabled = fields.Boolean(
        string="Send security logs",
        default=lambda self: self._security_logs_default(),
        help="SSH sign-ins and failed attempts (address, user, key), sudo "
             "commands, fail2ban's blocks and changes to passwd, shadow, "
             "sudoers and the SSH configuration, sent to the metrics central "
             "and kept there 90 days. This server can add to them but cannot "
             "delete them. Off stops sending; what was sent expires on its "
             "own.",
    )

    @api.model
    def _security_logs_default(self):
        """Return the value Send security logs starts with on a new server.

        On: a panel's servers are its own, and their security logs are
        what an incident is read from (plan of 9-nov-2026). A layer whose
        panels belong to others may start them off until it may receive
        them.

        :rtype: bool
        """
        return True

    @api.model
    def _security_logs_available(self):
        """Return whether this panel's servers may send their security logs.

        Always here. A layer whose panels belong to others withholds it
        until it may receive them: the switch is then off on every server
        and cannot be turned on, by the form, the route or an agent.

        :rtype: bool
        """
        return True

    def _check_security_logs_available(self, vals):
        """Refuse to turn a server's security logs on where they are withheld.

        Run by ``create`` and ``write``. Turning them off, or writing them
        off, is always allowed.

        :param dict vals: the values being written.
        :raise UserError: when *vals* turn them on and this panel may not.
        """
        if vals.get("security_logs_enabled") and not self._security_logs_available():
            raise UserError(_(
                "Security logs are not available on this panel yet."
            ))

    @api.model_create_multi
    def create(self, vals_list):
        """Refuse a server created sending security logs where they are withheld."""
        for vals in vals_list:
            self._check_security_logs_available(vals)
        return super().create(vals_list)

    @api.model
    def _security_logs_after_upgrade(self, shipped_before):
        """Return the value every server takes when the switch moves to them.

        Called once, by the update that replaced the panel-wide *Send
        hosts' security logs* with the switch of each server. On, also
        where the panel-wide setting was off: the owner's decision of
        10-nov-2026, said in the changelog. A layer whose panels belong
        to others keeps what they had.

        :param bool shipped_before: the panel-wide setting before the
            update.
        :rtype: bool
        """
        return True

    def _panel_key_fingerprints(self):
        """Return the SHA256 fingerprints of the panel's key for this host.

        In the form ``ssh-keygen -lf`` prints and sshd logs them
        (``SHA256:…``), so a sign-in with it counts as the panel's. Empty
        for a host that logs in with a password, or whose key cannot be
        read (a passphrase): every sign-in then counts as another's.

        :return: zero or one fingerprint.
        :rtype: list[str]
        """
        self.ensure_one()
        if self.login_type != "ssh_key":
            return []
        try:
            raw = self._get_ssh_private_key_bytes()
            if not raw:
                return []
            key = asyncssh.import_private_key(raw)
            return [key.get_fingerprint("sha256")]
        except Exception as exc:  # noqa: BLE001 — the install goes on without it
            _logger.warning(
                "[metrics] cannot read the key of cloud.host(id=%s) for its "
                "fingerprint: %s", self.id, exc,
            )
            return []

    def write(self, vals):
        """Re-apply a host's agents when what they read from it changes.

        Send security logs, the key and the user: the playbook renders
        them, so a change applies now rather than at the next install. A
        host whose last install failed is re-applied too: turning the
        switch off is how a Fluent Bit that does not start is removed, and
        the reconciliation would wait out its back-off (review of
        12-nov-2026).
        """
        self._check_security_logs_available(vals)
        watched = [f for f in _AGENTS_HOST_FIELDS if f in vals]
        before = {}
        if watched:
            before = {
                host.id: tuple(host.sudo()[f] for f in watched) for host in self
            }
        result = super().write(vals)
        for host in self.filtered(lambda h: h.id in before):
            if tuple(host.sudo()[f] for f in watched) != before[host.id]:
                host._reapply_observability_now("server settings changed")
        return result

    def _reapply_observability_now(self, reason):
        """Queue this host's agents' install now, installed or failed.

        :param str reason: short free text for the log.
        :return: the queued job id, or ``False`` when nothing was queued.
        """
        self.ensure_one()
        if not self._observability_target():
            return False
        if self.metrics_agents_state not in ("installed", "failed"):
            return False
        return self._enqueue_observability(reason)

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

        And as ``traefik_deployed``, which Full Setup sets when it ends: a
        host bought on demand has its key a minute after the purchase,
        long before Full Setup installs Docker, and the cron queued its
        agents then, beside the hardening, to fail on a missing ``docker``
        (2026-10-09). Full Setup queues them itself as it finishes.
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
        if not self.traefik_deployed:
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

        Collapses onto a job that waits in the host's queue: the type is
        declared ``queue_collapse`` (``data/job_type.xml``), so
        ``enqueue`` returns the waiting one. Deleting five instances from
        one host queued five identical installs, and that stampede is
        what fed the serialization race of 2026-08-13: the losers were
        rolled back, re-run by queue_job, and reported failures for
        teardowns that had already completed.

        Collapsing loses nothing because the playbook renders its label
        map *when it runs*, not when it was queued, so a job still
        waiting will pick up the final state of the host whether it
        covers one removal or five. A job already dispatched is
        deliberately not matched: it rendered, or is about to render,
        its map on its own, so a change landing mid-flight does need its
        own job or it would wait for the reconciliation cron to be
        noticed.

        :param reason: short free text for the log.
        :return: the queued job id, the id of the job it collapsed onto,
            or ``False``.
        """
        self.ensure_one()
        try:
            return self.env["cloud.job"].sudo().enqueue(
                self.id, False, "install_observability",
                # Often called from the on_success of a job that is still
                # 'started' on this host: the install belongs to that
                # job and goes before whatever else waits on the host.
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
        # Installed hosts too: the URL the direct path offers them moves,
        # and a change of the path asks
        # for this cron at once (``_direct_path_changed``).
        self._reconcile_direct_agents()
        # And those whose last install ran an older playbook.
        self._reconcile_agents_revision()
        # And the central, when its last deployment ran an older one.
        self._reconcile_central_revision()

    @api.model
    def _reconcile_central_revision(self):
        """Deploy the central again while its deployment is behind this code.

        The panel records the revision its central's last good deployment
        ran (``metrics_central_revision``). Behind ``CENTRAL_REVISION``,
        the central is deployed again: an update that changes it converges
        even when the first deployment was run by a worker on older code,
        failed (an unreachable host, a pull), or never ran. Not while one
        waits or runs on the central's host, and after a failure with the
        same growing back-off as the agents' installs. Not with
        observability off: the deployment switches it on, so it waits for
        the operator to switch it back on.

        :return: the queued job id, or ``False``.
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        if not settings.metrics_enabled:
            return False
        if (settings.metrics_central_revision or 0) >= CENTRAL_REVISION:
            return False
        host = settings.metrics_central_host_id.sudo()
        if not (host and host.active):
            return False
        Job = self.env["cloud.job"].sudo()
        jobs = Job.search([
            ("host_id", "=", host.id),
            ("job_type_id.code", "=", "deploy_metrics_central"),
        ], order="id desc", limit=len(_BACKOFF_MINUTES))
        if jobs.filtered(lambda j: j.state in _UNFINISHED_JOB_STATES):
            return False
        failures = 0
        for job in jobs:
            if job.state not in ("failed", "cancelled"):
                break
            failures += 1
        if failures:
            wait = _BACKOFF_MINUTES[min(failures, len(_BACKOFF_MINUTES) - 1)]
            last = jobs[0].date_done or jobs[0].write_date
            if last and (fields.Datetime.now() - last).total_seconds() < wait * 60:
                return False
        try:
            with self.env.cr.savepoint():
                job_id = Job.enqueue(host.id, False, "deploy_metrics_central")
        except Exception as exc:  # noqa: BLE001 — a cron pass never fails on it
            _logger.info(
                "[metrics] could not queue the central's deployment on %s: %s",
                host.name, exc,
            )
            return False
        _logger.info(
            "[metrics] the central on %s is on revision %s, behind %s: "
            "deploying it again", host.name,
            settings.metrics_central_revision or 0, CENTRAL_REVISION,
        )
        return job_id

    @api.model
    def _reconcile_agents_revision(self):
        """Re-apply, a few per pass, the agents installed by an older playbook.

        A host records the revision its last good install ran
        (``metrics_agents_revision``). One below ``AGENTS_REVISION`` was
        installed before this version, or by a panel still on older code
        (a panel rebuilt by another), and nothing else would run the
        playbook there again until its instances change. Oldest first,
        ``_REAPPLY_PER_PASS`` per pass; a host whose install is already
        waiting or running is left to it and not counted. One that fails
        leaves "installed" and is retried with the back-off of any other.

        :return: how many installs were queued.
        :rtype: int
        """
        settings = self.env["cloud.settings"].sudo()._get_system()
        if not settings.metrics_enabled:
            return 0
        behind = self.sudo().search([
            ("metrics_agents_state", "=", "installed"), ("active", "=", True),
        ]).filtered(
            lambda h: (h.metrics_agents_revision or 0) < AGENTS_REVISION,
        ).sorted(lambda h: (h.metrics_agents_revision or 0, h.id))
        if not behind:
            return 0
        busy = self.env["cloud.job"].sudo().search([
            ("host_id", "in", behind.ids),
            ("job_type_id.code", "=", "install_observability"),
            ("state", "in", _UNFINISHED_JOB_STATES),
        ]).host_id
        queued = 0
        for host in behind - busy:
            if queued >= _REAPPLY_PER_PASS:
                break
            if not host._observability_target():
                continue
            if host._enqueue_observability("agents revision"):
                queued += 1
        if queued:
            _logger.info(
                "[metrics] re-applied the agents of %s host(s) on an older "
                "revision (%s left)", queued, len(behind) - queued,
            )
        return queued

    @api.model
    def _reapply_observability_everywhere(self):
        """Have the cron re-apply the agents of every installed host.

        For a setting the playbook reads, such as the database collector's
        switch: the hosts converge at the cron's pace, as after an upgrade.
        """
        self.sudo().search([
            ("metrics_agents_state", "=", "installed"),
            ("metrics_agents_revision", "!=", 0),
        ]).write({"metrics_agents_revision": 0})

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
