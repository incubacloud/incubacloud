"""The server's restart for its updates: when it happens, and what it holds.

Until 1.0.232 a hardened host with ``auto_security_updates`` restarted by
itself at 04:00 whenever an update asked for it (unattended-upgrades'
``Automatic-Reboot``), and cut whatever the panel was running there: a
rebuild, a backup, a step run apart. Now the server only installs its
patches, and the panel restarts it:

* ``host_reboot_check`` (hidden, every 30 minutes by
  ``cron_host_reboot_check``) reads whether the server waits for a restart
  (``/run/reboot-required``) and its ``boot_id``, and turns the server's own
  restart off in the policy hardening wrote (``host_reboot.sh check``).
* Inside the host's window, with the restart pending and the host free as
  the panel sees it (``_reboot_blockers``), it queues ``host_reboot``.
* ``host_reboot`` holds the host (a ``cloud.host.reboot`` row): no job of
  the host or of its instances is dispatched or starts while it is held
  (``cloud.job._reboot_hold_of``). It asks the server for its own veto,
  restarts it with ``shutdown -r +1`` and waits for a new ``boot_id``.

The hold is a row of its own, not a column of ``cloud.job.checkpoint``:
``cloud.job.execute`` resumes any checkpoint row as a plan of steps run
apart, and the reconciliation treats it as its own. Like a checkpoint's
build lock, the hold lasts while its job runs: the query joins the row to
the job's state, so a job that failed, was cancelled or was closed by the
reconciliation holds nothing, whatever is left of its row.

Hold and dispatch never cross: the restart writes its hold under an
exclusive advisory lock of the host (``REBOOT_LOCK_NS``; the key 0 too on
the panel's own server), tried and never waited for, and reads what blocks
it after taking it; the dispatcher, and a job about to start, take the
shared lock of every host they touch, and of the key 0, before they read
the holds. Either the dispatch commits first and the restart sees the job,
or the hold commits first and the dispatch waits.

A restart is the panel's once per host and per opening of its window
(``_reboot_done_in_window``): a server whose update still asks for a
restart once it is back waits for the next night, rather than restarting
again every few minutes while the window stays open. The job queued by the
check reads the window, the policy and the server's pending restart again
when it starts (``host_reboot_executor``): it may have waited in the queue.

A panel away. Every check leaves on the server the panel's mark (when it
checked, whether it restarts the server by itself, its window), and the
instances whose jobs a layer counts inside. A server the panel has not
checked for ``REBOOT_PANEL_AWAY_H`` restarts by itself again
(``host_reboot.sh away``, its timer every 30 minutes): inside the same
window, once per opening, with the same veto of the server's
(``reboot-guard.d`` included), no SSH session open and nothing running
inside its instances. Once the panel checks it again, the panel decides.
The owner's decision of 15-nov-2026: a panel that sleeps through the
window, or that is gone, would otherwise leave its servers waiting for a
restart with nobody to give it.

The values below are the owner's choices of 15-nov-2026, kept here so that
changing one is a one-line change: a window from 04:00 for 3 hours, in UTC,
editable per host (a server carries instances of several projects, and
``cloud.project`` has no time zone); the alert of a restart pending for 7
days whether or not the host restarts by itself; a day without a check
before the server restarts by itself.
"""
import json
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.addons.base.models.res_partner import _tz_get

from .registry import executor_registry

_logger = logging.getLogger(__name__)

#: Hour of the day, in the host's ``reboot_tz``, the window opens at.
REBOOT_WINDOW_START_DEFAULT = 4.0
#: Hours the window stays open: 6 passes of the cron.
REBOOT_WINDOW_HOURS_DEFAULT = 3
#: Time zone of a new host's window.
REBOOT_TZ_DEFAULT = "UTC"
#: Hours between two checks of a host outside its window, or with no
#: restart pending: the pending restart shows without loading ``root.bg``.
REBOOT_CHECK_EVERY_H = 6
#: Days a restart may stay pending before ``host_reboot_pending`` asks a
#: person: a week of nights without a free moment.
REBOOT_PENDING_ALERT_DAYS = 7
#: Whether that alert is raised for a host that never restarts by itself
#: too (``auto_security_updates`` off, or not hardened): the owner's
#: choice of 15-nov-2026. False limits it to the hosts the panel restarts.
REBOOT_PENDING_ALERT_ALWAYS = True
#: Hours without a check of the panel after which the server restarts by
#: itself (``host_reboot.sh away``): four checks missed in a row
#: (``REBOOT_CHECK_EVERY_H``), so a panel down for a few hours, or one
#: restarted, never makes a server act alone. The owner's decision of
#: 15-nov-2026.
REBOOT_PANEL_AWAY_H = 24
#: Seconds from the order after which a server that does not answer, or
#: answers with the boot it had, failed to restart: a restart measured in
#: production takes about 2 minutes, and a disk check may take longer.
#: Until then the server's silence is the restart's, not an incident
#: (``cloud.host._reboot_recent``).
RETURN_DEADLINE_S = 900

REBOOT_PENDING_ALERT = "host_reboot_pending"
REBOOT_FAILED_ALERT = "host_reboot_failed"
REBOOT_INCOMPLETE_ALERT = "host_reboot_incomplete"

#: Namespace of the hold's advisory lock, next to the queues'
#: (``cloud.job``) and the stop's (``cloud.job.no.return``).
REBOOT_LOCK_NS = 0x0C10D111
#: The key of that lock for the hold of every queue (the panel's server).
REBOOT_PANEL_KEY = 0

#: Where the panel's own process reads the boot it runs in.
_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def panel_boot_id():
    """Return the ``boot_id`` of the machine the panel's process runs on.

    A container sees the host's (spike of 15-nov-2026: ``/proc/sys`` is
    read-only there, ``kernel/random`` is not masked). Equal to a host's,
    that host is the panel's own server.

    :return: the id, or ``''`` when it cannot be read
    :rtype: str
    """
    try:
        with open(_BOOT_ID_PATH, encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return ""


class CloudHostReboot(models.Model):
    """A restart of a server by the panel, and the hold it keeps."""

    _name = "cloud.host.reboot"
    _description = "Server Restart"
    _log_access = False

    job_id = fields.Many2one(
        "cloud.job", required=True, ondelete="cascade", index=True,
    )
    host_id = fields.Many2one(
        "cloud.host", required=True, ondelete="cascade", index=True,
    )
    holds_panel = fields.Boolean(
        help="The host is the panel's own server: the hold is of every"
             " queue.",
    )
    boot_id = fields.Char(help="The server's boot before the restart.")
    containers = fields.Text(
        help="JSON: the containers running before the restart.",
    )
    shutdown_at = fields.Datetime(
        help="When the restart was asked for; the job waits from then.",
    )

    _job_uniq = models.Constraint(
        "unique(job_id)", "A restart job has one row.",
    )

    def _containers(self):
        """Return the names of the containers running before the restart.

        :rtype: list
        """
        self.ensure_one()
        return json.loads(self.containers) if self.containers else []


class CloudHost(models.Model):
    _inherit = "cloud.host"

    reboot_window_start = fields.Float(
        string="Restart Window From",
        default=REBOOT_WINDOW_START_DEFAULT,
        help="Hour of the day, in the window's time zone, from which the"
             " panel may restart this server for its updates.",
    )
    reboot_window_hours = fields.Integer(
        string="Restart Window Hours",
        default=REBOOT_WINDOW_HOURS_DEFAULT,
        help="How long the window stays open. Inside it the panel tries"
             " every 30 minutes, and only while nothing of its runs here.",
    )
    reboot_tz = fields.Selection(
        _tz_get,
        string="Restart Window Time Zone",
        default=REBOOT_TZ_DEFAULT,
    )
    reboot_required_since = fields.Datetime(
        string="Restart Pending Since",
        readonly=True,
        copy=False,
        help="Since when the server says an update waits for a restart.",
    )
    reboot_required_pkgs = fields.Char(
        string="Restart Asked For By",
        readonly=True,
        copy=False,
        help="The packages whose update waits for the restart.",
    )
    reboot_checked_at = fields.Datetime(readonly=True, copy=False)
    reboot_boot_id = fields.Char(
        readonly=True,
        copy=False,
        help="The server's boot as the last check read it.",
    )

    @api.constrains("reboot_window_start", "reboot_window_hours")
    def _check_reboot_window(self):
        """Refuse a window that does not start within a day or lasts none."""
        for host in self:
            if not 0 <= host.reboot_window_start < 24:
                raise ValidationError(_(
                    "The restart window starts at an hour from 0 to 24."
                ))
            if not 1 <= host.reboot_window_hours <= 24:
                raise ValidationError(_(
                    "The restart window lasts from 1 to 24 hours."
                ))

    def _reboot_window_open(self, now=None):
        """Whether the host's restart window is open at *now*.

        The hour is the host's time zone's: across midnight (from 23:00
        for 3 hours) and across a change of the clock, as the wall clock
        of that zone reads it.

        :param datetime now: naive UTC, as ``fields.Datetime.now()``
        :rtype: bool
        """
        self.ensure_one()
        now = now or fields.Datetime.now()
        try:
            zone = ZoneInfo(self.reboot_tz or REBOOT_TZ_DEFAULT)
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo(REBOOT_TZ_DEFAULT)
        local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(zone)
        hour = local.hour + local.minute / 60 + local.second / 3600
        return (hour - self.reboot_window_start) % 24 < self.reboot_window_hours

    def _reboot_window_opened_at(self, now=None):
        """Return when the opening of the window *now* is in began, or None.

        Counted back from *now* by the wall clock of the window's zone: on
        the night the clock changes, an hour off at most.

        :param datetime now: naive UTC
        :return: naive UTC, or None when the window is closed
        :rtype: datetime | None
        """
        self.ensure_one()
        now = now or fields.Datetime.now()
        try:
            zone = ZoneInfo(self.reboot_tz or REBOOT_TZ_DEFAULT)
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo(REBOOT_TZ_DEFAULT)
        local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(zone)
        hour = local.hour + local.minute / 60 + local.second / 3600
        elapsed = (hour - self.reboot_window_start) % 24
        if elapsed >= self.reboot_window_hours:
            return None
        return now - timedelta(hours=elapsed)

    def _reboot_done_in_window(self, now=None):
        """Whether the panel ordered a restart of this host in this opening.

        A ``cloud.host.reboot`` row of the host whose order
        (``shutdown_at``) is since the window opened: one restart per host
        and per opening, whatever it ended in.

        :param datetime now: naive UTC
        :rtype: bool
        """
        self.ensure_one()
        opened = self._reboot_window_opened_at(now)
        if opened is None:
            return False
        return bool(self.env["cloud.host.reboot"].sudo().search_count([
            ("host_id", "=", self.id), ("shutdown_at", ">=", opened),
        ], limit=1))

    def _reboot_not_due(self, now=None):
        """Say why the panel does not restart this host by itself now.

        Read by the check before it queues the restart, and by the
        restart it queued when it starts, after its wait in the queue.
        Whether a restart is pending is the caller's: the server says it.

        :param datetime now: naive UTC
        :return: the reason, ``''`` when the panel may restart it now
        :rtype: str
        """
        self.ensure_one()
        now = now or fields.Datetime.now()
        if not self._reboot_by_panel():
            return (
                "The panel does not restart this server by itself: Auto"
                " Security Updates is off, or it is not hardened."
            )
        if not self._reboot_window_open(now):
            return (
                f"Its restart window (from {self._reboot_window_label()} for"
                f" {self.reboot_window_hours} h,"
                f" {self.reboot_tz or REBOOT_TZ_DEFAULT}) is closed."
            )
        if self._reboot_done_in_window(now):
            return (
                "The panel restarted it once in this window already: an"
                " update that still asks for a restart waits for the next one."
            )
        return ""

    def _reboot_last_row(self):
        """Return the host's last restart by the panel, if any.

        :return: a ``cloud.host.reboot``, empty when there is none
        """
        self.ensure_one()
        return self.env["cloud.host.reboot"].sudo().search(
            [("host_id", "=", self.id)], order="id desc", limit=1,
        )

    def _reboot_recent(self, now=None):
        """Whether the panel ordered a restart of this host a moment ago.

        Within ``RETURN_DEADLINE_S`` of its order: the server's silence is
        the restart's, and ``host_unreachable`` or ``metrics_host_absent``
        would alert on a cut the panel asked for. Past it, a server that
        did not come back has ``host_reboot_failed``.

        :param datetime now: naive UTC
        :rtype: bool
        """
        self.ensure_one()
        now = now or fields.Datetime.now()
        return bool(self.env["cloud.host.reboot"].sudo().search_count([
            ("host_id", "=", self.id),
            ("shutdown_at", ">=", now - timedelta(seconds=RETURN_DEADLINE_S)),
        ], limit=1))

    def _reboot_by_panel(self):
        """Whether the panel restarts this host by itself for its updates.

        Hardened, with ``auto_security_updates``: the policy the host's
        owner accepted. Any other host is never restarted by the panel on
        its own; its pending restart is shown, and alerted after
        ``REBOOT_PENDING_ALERT_DAYS``.

        :rtype: bool
        """
        self.ensure_one()
        return bool(self.active and self.hardened and self.auto_security_updates)

    def _reboot_check_due(self, now=None):
        """Whether the cron checks this host in this pass.

        Every pass inside the window while a restart is pending, the
        panel restarts the host by itself and has not in this opening of
        the window; every ``REBOOT_CHECK_EVERY_H`` hours otherwise.

        :param datetime now: naive UTC
        :rtype: bool
        """
        self.ensure_one()
        now = now or fields.Datetime.now()
        if (
            self.reboot_required_since and self._reboot_by_panel()
            and self._reboot_window_open(now)
            and not self._reboot_done_in_window(now)
        ):
            return True
        return not self.reboot_checked_at or (
            now - self.reboot_checked_at >= timedelta(hours=REBOOT_CHECK_EVERY_H)
        )

    def _reboot_job_active(self):
        """Return the restart job of the host that waits or runs, if any.

        :return: a ``cloud.job``, empty when there is none
        """
        self.ensure_one()
        Job = self.env["cloud.job"].sudo()
        return Job.search([
            ("host_id", "=", self.id),
            ("instance_id", "=", False),
            ("job_type_id.code", "=", "host_reboot"),
            ("state", "in", Job._active_states),
        ], limit=1)

    def _reboot_hosts_of_panel(self):
        """Return every host whose work the panel's own restart waits for.

        :return: ``cloud.host`` records, archived ones included
        """
        return self.sudo().with_context(active_test=False).search([])

    def _reboot_blockers(self, exclude_job_id=0, holds_panel=False):
        """Say why the panel would not restart this host now.

        Read in this environment's transaction, which the restart opens
        at READ COMMITTED once it holds the lock (``REBOOT_LOCK_NS``), and
        the check reads before it queues the restart:

        * a job of the host's queue, or of the queue of an instance that
          lives on it or arrives by a move, runs, or holds its queue
          (``cloud.job._scope_busy``): a visible job dispatched and not
          ended, a chain until its last step, a job stopped that has not
          closed, a step past its point of no return left running;
        * a build parked with the host's build lock (its checkpoint's
          ``build_lock_host_id``);
        * a job of the host whose queue job runs and that is no probe
          (``AbstractExecutor._reboot_probe``): the hidden types that do
          work, a prune or the removal of a handoff;
        * a restore upload granted on the host;
        * what a layer adds (``_reboot_blockers_extra``).

        On the panel's own server (*holds_panel*) all of it is read for
        the whole fleet: its restart stops every job of the panel.

        :param int exclude_job_id: the restart job itself
        :param bool holds_panel: read for every host
        :return: one sentence per reason, empty when the host is free
        :rtype: list
        """
        self.ensure_one()
        Job = self.env["cloud.job"].sudo()
        hosts = self._reboot_hosts_of_panel() if holds_panel else self.sudo()
        Instance = self.env["cloud.instance"].sudo().with_context(active_test=False)
        instances = Instance.search(
            [("host_id", "in", hosts.ids)],
        ) | hosts.with_context(active_test=False).incoming_instance_ids
        reasons = []
        for host in hosts:
            if Job._scope_busy(None, host.id, exclude_job_id=exclude_job_id):
                reasons.append(_("A job of the server %(host)s runs.", host=host.name))
                break
        for inst in instances:
            if Job._scope_busy(inst.id, None, exclude_job_id=exclude_job_id):
                reasons.append(_(
                    "A job of the instance %(instance)s runs.", instance=inst.name,
                ))
                break
        cr = self.env.cr
        self.env["cloud.job.checkpoint"].sudo().flush_model()
        cr.execute(
            "SELECT job_id FROM cloud_job_checkpoint"
            " WHERE build_lock_host_id IN %s"
            " AND (closed IS NOT TRUE OR detached_started) LIMIT 1",
            (tuple(hosts.ids),),
        )
        row = cr.fetchone()
        if row:
            reasons.append(_("Job #%(job)s keeps a build on the server.", job=row[0]))
        self.env["queue.job"].sudo().flush_model(["state", "uuid"])
        cr.execute(
            "SELECT c.id, t.code FROM cloud_job c"
            " JOIN queue_job q ON q.uuid = c.queue_job_uuid"
            " JOIN cloud_job_type t ON t.id = c.job_type_id"
            " WHERE c.host_id IN %s AND c.id != %s AND q.state = 'started'"
            " ORDER BY c.id",
            (tuple(hosts.ids), exclude_job_id or 0),
        )
        for job_id, code in cr.fetchall():
            executor_cls = executor_registry.get(code)
            if executor_cls is None or not executor_cls._reboot_probe:
                reasons.append(_("Job #%(job)s runs on the server.", job=job_id))
                break
        grants = self.env["cloud.restore.upload.grant"].sudo().search_count([
            ("host_id", "in", hosts.ids), ("state", "=", "granted"),
        ])
        if grants:
            reasons.append(_("A backup upload to the server is open."))
        reasons.extend(self._reboot_blockers_extra(holds_panel=holds_panel))
        return reasons

    def _reboot_blockers_extra(self, holds_panel=False):
        """What a layer adds to ``_reboot_blockers``; none in core.

        :param bool holds_panel: the restart is the panel's own server's
        :return: one sentence per reason
        :rtype: list
        """
        return []

    def _reboot_alert_pending(self, now=None):
        """Raise or close ``host_reboot_pending`` for this host.

        Raised once the restart has been pending for
        ``REBOOT_PENDING_ALERT_DAYS``; for a host the panel does not
        restart by itself only with ``REBOOT_PENDING_ALERT_ALWAYS``. Closed
        once nothing is pending.

        :param datetime now: naive UTC
        """
        self.ensure_one()
        Alert = self.env["cloud.alert"].sudo()
        now = now or fields.Datetime.now()
        since = self.reboot_required_since
        due = since and now - since >= timedelta(days=REBOOT_PENDING_ALERT_DAYS)
        if due and (REBOOT_PENDING_ALERT_ALWAYS or self._reboot_by_panel()):
            if self._reboot_by_panel():
                why = _(
                    "The panel found no free moment in its restart window"
                    " (from %(start)s for %(hours)s h, %(tz)s): a job ran each"
                    " time, or the server said no.",
                    start=self._reboot_window_label(), hours=self.reboot_window_hours,
                    tz=self.reboot_tz or REBOOT_TZ_DEFAULT,
                )
            else:
                why = _(
                    "The panel does not restart this server by itself:"
                    " Auto Security Updates is off, or it is not hardened."
                )
            Alert.raise_alert(
                REBOOT_PENDING_ALERT,
                _(
                    "The server %(host)s has waited %(days)s days for a"
                    " restart that installs its updates (%(pkgs)s). %(why)s"
                    " Restart it when nothing runs there.",
                    host=self.name, days=(now - since).days,
                    pkgs=self.reboot_required_pkgs or _("updates"), why=why,
                ),
                level="warning",
                host=self,
            )
        else:
            Alert.resolve_alert(REBOOT_PENDING_ALERT, host=self)

    def _reboot_window_label(self):
        """Return the window's start as ``HH:MM``.

        :rtype: str
        """
        self.ensure_one()
        minutes = round(self.reboot_window_start * 60)
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    # Private: ``ir.cron`` runs it as the platform; nothing calls it
    # through ``call_kw``.
    @api.private
    @api.model
    def cron_host_reboot_check(self):
        """Queue ``host_reboot_check`` for the hosts whose check is due.

        Every 30 minutes; a host is due by ``_reboot_check_due``. The jobs
        are hidden and of the platform: queued with ``sudo``, as the cron
        bot is no cloud manager. ``reboot_checked_at`` is stamped as the
        check is queued: a check that fails, a server that does not answer,
        waits its turn as one that answered, and is not queued again on
        every pass.
        """
        now = fields.Datetime.now()
        Job = self.env["cloud.job"].sudo()
        for host in self._ssh_ready_hosts():
            try:
                if not host._reboot_check_due(now):
                    continue
                if Job.search_count([
                    ("host_id", "=", host.id),
                    ("job_type_id.code", "=", "host_reboot_check"),
                    ("state", "in", Job._active_states),
                ], limit=1):
                    continue
                Job.enqueue(host.id, False, "host_reboot_check")
                host.reboot_checked_at = now
            except Exception as exc:  # one host never stops the others
                _logger.warning(
                    "Could not queue the restart check of host %s: %s",
                    host.name, exc,
                )


class CloudJob(models.Model):
    _inherit = "cloud.job"

    @api.model
    def _reboot_hold_of(self, host_ids, lock=True):
        """Return the restart job whose hold covers one of *host_ids*, or 0.

        A hold is a ``cloud.host.reboot`` row whose job is in
        ``_running_states``, of one of the hosts, or of the panel's own
        server (``holds_panel``), which holds them all. Read on a fresh
        cursor: the restart commits its hold on a cursor of its own. With
        *lock*, the shared advisory lock of each host and of the panel's
        key (``REBOOT_LOCK_NS``) is taken first, in this transaction,
        until it ends: a restart writing its hold meanwhile is waited for,
        and one that comes after sees what this transaction dispatched.

        :param host_ids: ids of ``cloud.host``
        :param bool lock: take the shared locks first
        :return: the id of the ``cloud.job``, or 0
        :rtype: int
        """
        host_ids = sorted({int(h) for h in host_ids if h})
        if lock:
            for key in (REBOOT_PANEL_KEY, *host_ids):
                self.env.cr.execute(
                    "SELECT pg_advisory_xact_lock_shared(%s, %s)",
                    (REBOOT_LOCK_NS, key),
                )
        with self._fresh_env() as env:
            env.cr.execute(
                "SELECT r.job_id FROM cloud_host_reboot r"
                " JOIN cloud_job j ON j.id = r.job_id"
                " WHERE j.state IN %s AND (r.holds_panel OR r.host_id IN %s)"
                " ORDER BY r.job_id LIMIT 1",
                (tuple(self._running_states), tuple(host_ids) or (0,)),
            )
            row = env.cr.fetchone()
        return row[0] if row else 0

    @api.model
    def _reboot_probe_entry(self, entry):
        """Whether every job of *entry* is a probe, which no hold stops.

        :param entry: ``cloud.job`` records, one queue entry
        :rtype: bool
        """
        for job in entry:
            executor_cls = executor_registry.get(job.job_type_id.code)
            if executor_cls is None or not executor_cls._reboot_probe:
                return False
        return True

    @api.model
    def _reboot_holds_entry(self, entry, scope):
        """Whether a restart's hold keeps *entry* from starting now.

        The hosts of its jobs, of their instances, and of the queue
        (``_reboot_hold_of``, with its locks). An entry of probes only is
        never held. The first job of a held entry says once in its log
        what it waits for.

        :param entry: ``cloud.job`` records, first in *scope*'s queue
        :param scope: ``(instance_id, host_id)``
        :rtype: bool
        """
        if self._reboot_probe_entry(entry):
            return False
        host_ids = set(entry.host_id.ids) | set(entry.instance_id.host_id.ids)
        if scope[1]:
            host_ids.add(scope[1])
        holder = self._reboot_hold_of(host_ids)
        if not holder:
            return False
        entry[:1]._note_once(_(
            "Waiting for the restart of the server (job #%(job)s).", job=holder,
        ))
        return True

    def _reboot_released_scopes(self):
        """Return the queues a restart job's end lets go.

        Its host's own, and those of the instances on it or arriving by a
        move; every queue that has a job waiting when it held the panel's
        own server. For the dispatch registered as it ends.

        :return: set of ``(instance_id, host_id)``
        """
        self.ensure_one()
        row = self.env["cloud.host.reboot"].sudo().search(
            [("job_id", "=", self.id)], limit=1,
        )
        if not row:
            return set()
        if row.holds_panel:
            return self._queue_scopes_of(self.sudo().search([("state", "=", "queued")]))
        host = row.host_id.with_context(active_test=False)
        instances = self.env["cloud.instance"].sudo().with_context(
            active_test=False,
        ).search([("host_id", "=", host.id)]) | host.incoming_instance_ids
        return {(None, host.id)} | {(inst.id, None) for inst in instances}


def parse_marks(stdout):
    """Return the ``IC_<NAME>=<value>`` lines ``host_reboot.sh`` printed.

    :param str stdout: what the script printed
    :return: ``{name: [value, ...]}``, in the order printed
    :rtype: dict
    """
    found = {}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("IC_") and "=" in line:
            key, value = line.split("=", 1)
            found.setdefault(key, []).append(value)
    return found


def first_mark(found, key):
    """Return the first value of *key* in what ``parse_marks`` read, or ''.

    :rtype: str
    """
    return (found.get(key) or [""])[0]
