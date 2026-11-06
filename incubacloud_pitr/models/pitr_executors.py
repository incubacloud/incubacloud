"""The jobs of continuous recovery, each a few ``pitr.sh`` steps.

``pitr_enable``     install wal-g, archiving on, first base copy
``pitr_disable``    archiving off, the agent removed
``pitr_base_backup`` daily: a base copy, then whatever left the window
``pitr_status``     every ten minutes: what the archiver and the agent say
``pitr_restore``    the database to a moment, the filestore with it

The scripts run in the instance's directory; their output is parsed from
one marker line each (``IC_PITR_*``), never from free text. A job whose
steps did part of their work records that part too: a base copy taken
before a housekeeping step failed is a base copy.
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from operator import itemgetter

import psycopg2

from odoo import fields

from odoo.addons.incubacloud.models.abstract_executor import (
    CANCELLED_WHILE_RUNNING,
    AbstractSSHExecutor,
)

from ._constants import WALG_SHA256, WALG_VERSION

_logger = logging.getLogger(__name__)

ALERT_FAILING = "pitr_failing"
ALERT_LAGGING = "pitr_archive_lagging"
ALERT_BASE_MISSING = "pitr_base_missing"

#: How long one job follows a restore before handing it to the next one:
#: under the hour a job may run.
RESTORE_WAIT_S = 3000
#: Jobs that follow a restore whose process still runs, before the alert
#: says it is taking long (they go on following it).
RESTORE_FOLLOW_MAX = 8
#: Jobs that follow a restore without reaching the host, before giving up.
RESTORE_UNREACHABLE_MAX = 8
#: How long a cancel waits on the host for the restore to stop and put
#: back what it changed, in the job's own run (a worker of the job runner):
#: a phase stops in seconds and a rollback only renames, so most cancels
#: end within it; one that does not is followed by the next job.
CANCEL_WAIT_S = 300
#: The same wait outside the job's run: the reconciliation of lost jobs
#: (``cloud.job._cron_reconcile_lost_jobs``) runs it in the cron thread
#: the other crons wait for, and stops starting clean-ups once a pass
#: took 60 s (``_RECONCILE_HOOK_BUDGET_S``). Within that budget, with the
#: connection and the upload; a longer undo is followed by the next job.
CANCEL_WAIT_OUTSIDE_RUN_S = 30
#: What the connection, the upload and the host's first wait for the
#: process (up to 10 s) add to a cancel's wait.
CANCEL_SSH_MARGIN_S = 120

_CLUSTER_RE = re.compile(r"^IC_PITR_CLUSTER (\d+)", re.MULTILINE)
_BASES_RE = re.compile(r"^IC_PITR_BASES (\d+) (\[.*\])\s*$", re.MULTILINE)
_STATUS_RE = re.compile(r"^IC_PITR_STATUS (.+)$", re.MULTILINE)
_RESTORE_RE = re.compile(r"^IC_PITR_RESTORE (.+)$", re.MULTILINE)
_PURGED_RE = re.compile(r"^IC_PITR_PURGED (\d+)$", re.MULTILINE)
#: The line ``restart_db`` prints once it started odoo again.
_ODOO_RESTARTED = "IC_PITR_ODOO_RESTARTED"


def _stdout(results):
    """All the steps' output, in order."""
    return "\n".join((data.get("stdout") or "") for data in results.values())


def _ran(results, label):
    """Whether step *label* ran and exited 0."""
    return (results.get(label) or {}).get("exit_status") == 0


def _utc(raw):
    """A wal-g timestamp (``2026-10-18T22:10:27.072124Z``) as naive UTC."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
        timezone.utc,
    ).replace(tzinfo=None, microsecond=0)


def _pairs(text):
    """``key=value`` words as a dict; other words are ignored."""
    return dict(word.split("=", 1) for word in (text or "").split() if "=" in word)


def parse_bases(stdout):
    """Return ``(cluster, bases)`` from the last ``IC_PITR_BASES`` line.

    :return: the cluster id and its base copies, oldest first, each a
        dict with at least ``backup_name`` and ``finish_time`` (naive
        UTC); ``(None, [])`` without the line
    """
    match = None
    for match in _BASES_RE.finditer(stdout or ""):
        pass
    if not match:
        return None, []
    try:
        raw = json.loads(match.group(2))
    except ValueError:
        return match.group(1), []
    bases = [
        {**b, "finish_time": _utc(b["finish_time"])}
        for b in raw if b.get("backup_name") and b.get("finish_time")
    ]
    bases.sort(key=itemgetter("finish_time"))
    return match.group(1), bases


def parse_status(stdout):
    """Return the ``IC_PITR_STATUS`` fields as a dict, or ``{}``."""
    match = _STATUS_RE.search(stdout or "")
    return _pairs(match.group(1)) if match else {}


def parse_restore(stdout):
    """Return the last ``IC_PITR_RESTORE`` line's fields, or ``{}``."""
    match = None
    for match in _RESTORE_RE.finditer(stdout or ""):
        pass
    return _pairs(match.group(1)) if match else {}


def base_timeline(base):
    """The timeline a base copy was taken on, from its name, as an int."""
    return int(base["backup_name"][5:13], 16)


def choose_base(bases, target, switches):
    """Pick the base copy and the timeline to restore *target* from.

    Each restore to a moment starts a new timeline at its promotion
    (``since``), branching off the moment it went back to (``target``).
    The history live at *target* is the timeline of the last such switch
    before it; a base copy serves if it was taken on that timeline before
    *target*, or serves the moment that timeline branched off (a copy of
    an older timeline taken after the branch holds a history that was
    abandoned). Before any switch, the timelines older than the first one.

    :param list bases: the cluster's base copies (``parse_bases``)
    :param datetime target: the moment, naive UTC
    :param list switches: the cluster's ``pitr_timelines`` entries
    :return: ``(base, timeline)``, or ``(None, None)`` when none serves
    """
    parsed = sorted(
        (
            {
                "since": fields.Datetime.to_datetime(s["since"]),
                "target": fields.Datetime.to_datetime(s["target"]),
                "timeline": int(s["timeline"], 16),
            }
            for s in switches or ()
            if s.get("since") and s.get("target") and s.get("timeline")
        ),
        key=itemgetter("since"),
    )

    def serving(at, known):
        """Bases that serve *at* given the first *known* switches, and its timeline."""
        live = None
        for index, switch in enumerate(parsed[:known]):
            if switch["since"] <= at:
                live = index
        if live is None:
            first = parsed[0]["timeline"] if parsed else None
            found = [
                b for b in bases
                if b["finish_time"] <= at
                and (first is None or base_timeline(b) < first)
            ]
            return found, None
        switch = parsed[live]
        own = [
            b for b in bases
            if b["finish_time"] <= at and base_timeline(b) == switch["timeline"]
        ]
        older, _timeline = serving(switch["target"], live)
        return own + older, switch["timeline"]

    found, timeline = serving(target, len(parsed))
    if not found:
        return None, None
    chosen = max(found, key=itemgetter("finish_time"))
    return chosen, timeline or base_timeline(chosen)


class _PitrExecutor(AbstractSSHExecutor):
    """What every continuous recovery job shares."""

    _job_type = None

    def _inst(self):
        """The instance the job is about."""
        return self.job.instance_id

    def _common(self):
        """``(dir, pg_user)`` for ``pitr.sh``."""
        inst = self._inst()
        return self._inst_dir(inst), inst.postgres_username or "odoo"

    def _record_pitr_bases(self, stdout):
        """Keep the window the base copies of the current cluster span.

        A listing with no copy left (a purge took them all) empties it:
        the panel offers no moment to restore to, and the next copy is
        due at once (``_pitr_base_due``). Without the line (a listing
        that failed) it is left as it was.

        :param str stdout: output holding an ``IC_PITR_BASES`` line
        :return: the cluster id read, or None
        """
        cluster, bases = parse_bases(stdout)
        if not cluster:
            return None
        self._inst().sudo().write({
            "pitr_cluster_id": cluster,
            "pitr_oldest_base_at": bases[0]["finish_time"] if bases else False,
            "pitr_last_base_at": bases[-1]["finish_time"] if bases else False,
        })
        return cluster

    def _record_cluster(self, cluster):
        """Archiving goes to *cluster* now; an older one is kept as history.

        A new cluster (a move, a new deploy) leaves the old one's archive
        in the bucket until it is out of the window.

        :param str cluster: the system identifier archiving now
        """
        inst = self._inst()
        previous = inst.pitr_cluster_id
        vals = {"pitr_cluster_id": cluster}
        if previous and previous != cluster:
            history = list(inst.pitr_previous_clusters or [])
            history.append({
                "id": previous,
                "until": fields.Datetime.to_string(fields.Datetime.now()),
            })
            vals |= {
                "pitr_previous_clusters": history,
                "pitr_oldest_base_at": False,
                "pitr_last_base_at": False,
            }
        inst.sudo().write(vals)

    def _pitr_alert(self, code, message, level="warning"):
        """Raise *code* on the instance, from an outcome hook (durable).

        Not core's ``_alert``, which is about the job's host and opens a
        cursor of its own.
        """
        self.env["cloud.alert"].raise_alert(
            code, message, level=level, instance=self._inst(),
        )

    def _pitr_resolve(self, *codes):
        """Close *codes* on the instance, from an outcome hook (durable)."""
        for code in codes:
            self.env["cloud.alert"].resolve_alert(code, instance=self._inst())

    def _follow_up_restore(self, inst):
        """Copy a restored database at once and tell the addons (``pitr``).

        Once per restore to a moment, by whichever records it first
        (``cloud.instance._pitr_record_restore``): the job that followed
        it, or the watch when it outlived its jobs. One that cannot be
        recorded, by its job alone.

        :param inst: the restored ``cloud.instance``
        """
        # With ``sudo``, as every head of a queue is: a restore asked for
        # without superuser rights runs without them.
        self.env["cloud.job"].sudo().enqueue(
            inst.host_id.id, inst.id, "pitr_base_backup",
            payload={"full": True}, bypass_running_check=True,
        )
        self._call_addon_hook(inst, "_after_restore_hook", "pitr")

    def _log_errors(self, errors):
        """Put a failure's errors in the job's log."""
        for err in errors:
            self._sys(f"✗ {err}")

    async def on_stdout(self, line):
        """Log *line*; on odoo's restart, confirm the instance running.

        A restart of the database stops odoo around it (``restart_db``)
        and says so once it started it again, whatever the step does
        next. Odoo's series is then gone for about a minute, which the
        metrics cron would read as a stop: the instance is confirmed
        running there and then, durably (``_persist``), and not when the
        job ends — the first base copy that follows a switch takes
        minutes, longer than the grace the confirmation opens.

        A row busy past the durable lock timeout leaves it unconfirmed,
        and the step goes on: the switch matters more than the badge.

        :param str line: a line of a step's output, stripped
        """
        await super().on_stdout(line)
        if line != _ODOO_RESTARTED:
            return
        inst = self._inst()
        try:
            self._persist(inst.sudo(), inst._running_confirmed_vals())
        except psycopg2.errors.LockNotAvailable:
            self._sys(
                f"⚠ Odoo of '{inst.name}' runs again, but the instance could "
                f"not be confirmed running: the panel may show it stopped "
                f"for up to two minutes."
            )


class PitrEnableExecutor(_PitrExecutor):
    """Install wal-g, switch archiving on and take the first base copy."""

    _job_type = "pitr_enable"

    def get_commands(self):
        """Install, enable (which starts the agent), first base copy.

        The base copy is a full one only when the cluster has none on its
        timeline: a changed objective does not upload the database again.
        """
        inst = self._inst()
        d, user = self._common()
        return [
            (
                "Install wal-g",
                self.run_script("pitr.sh", [
                    "install", d, WALG_VERSION,
                    WALG_SHA256["amd64"], WALG_SHA256["aarch64"],
                ]),
                {"stop_on_failure": True},
            ),
            (
                "Switch archiving on",
                self.run_script("pitr.sh", [
                    "enable", d, user, inst._pitr_objective_minutes() * 60,
                ]),
                {"stop_on_failure": True},
            ),
            (
                "First base copy",
                self.run_script("pitr.sh", ["base-backup", d, user]),
            ),
        ]

    async def on_success(self, results):
        """Archiving: record the cluster, the window, and say so."""
        self._record_archiving(results)
        self._pitr_resolve(ALERT_FAILING, ALERT_BASE_MISSING)
        self._sys("✓ Continuous recovery is on.")

    def _record_archiving(self, results):
        """Record the cluster archiving now and its base copies.

        :return: whether archiving was switched on and checked to work
        """
        out = _stdout(results)
        match = _CLUSTER_RE.search(out)
        if match:
            self._record_cluster(match.group(1))
        self._record_pitr_bases(out)
        works = _ran(results, "Switch archiving on")
        self._inst().sudo().write({
            "pitr_state": "ok" if works else "failed",
            "pitr_checked_at": fields.Datetime.now(),
        })
        return works

    async def on_failure(self, results, errors):
        """Archiving may be on with no base copy yet: say which.

        A switch that never reached the database waits a day before the
        next try (``_pitr_reconcile``); one whose upload does not work is
        failing, which the watch keeps reading.
        """
        self._log_errors(errors)
        inst = self._inst()
        if self._record_archiving(results):
            self._pitr_alert(
                ALERT_BASE_MISSING,
                f"Continuous recovery is on for '{inst.name}', but its first "
                f"base copy failed: nothing can be restored until one is "
                f"taken (retried within the hour). See the job's log.",
            )
            return
        if not _CLUSTER_RE.search(_stdout(results)):
            inst.sudo().pitr_state = "off"
        self._pitr_alert(
            ALERT_FAILING,
            f"Continuous recovery could not be switched on for "
            f"'{inst.name}'; it is retried in a day. See the job's log.",
            level="critical",
        )

    async def on_abort(self, exc):
        """A run that never reached its steps failed like one that did."""
        await self.on_failure({}, [f"{type(exc).__name__}: {exc}"])


class PitrDisableExecutor(_PitrExecutor):
    """Switch archiving off and remove what continuous recovery added."""

    _job_type = "pitr_disable"

    def get_commands(self):
        """One step: ``pitr.sh disable``."""
        d, user = self._common()
        return [(
            "Switch archiving off",
            self.run_script("pitr.sh", ["disable", d, user]),
        )]

    async def on_success(self, results):
        """Off; the archive stays in the bucket until the instance goes."""
        self._inst().sudo().write({"pitr_state": "off"})
        self._pitr_resolve(ALERT_FAILING, ALERT_LAGGING, ALERT_BASE_MISSING)
        self._sys("✓ Continuous recovery is off. The archive stays in the bucket.")

    async def on_failure(self, results, errors):
        """Still on; tried again in a day (``_pitr_reconcile``)."""
        self._log_errors(errors)


class PitrBaseBackupExecutor(_PitrExecutor):
    """The daily base copy, and what has left the window since."""

    _job_type = "pitr_base_backup"

    def _cutoff(self):
        """The oldest moment the window still covers, as naive UTC."""
        return fields.Datetime.now() - timedelta(days=self._inst()._pitr_window_days())

    def get_commands(self):
        """Base copy; then retention, old deletions and old clusters."""
        d, user = self._common()
        cutoff = self._cutoff()
        # Full after a restore (payload): the newest copy is ahead of the
        # restored database, and a delta cannot build on it.
        kind = "full" if (self.job.payload or {}).get("full") else "delta"
        return [
            (
                "Base copy",
                self.run_script("pitr.sh", ["base-backup", d, user, kind]),
                {"stop_on_failure": True},
            ),
            (
                "Drop what left the window",
                self.run_script("pitr.sh", [
                    "housekeep", d, user,
                    cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    cutoff.strftime("%Y-%m-%d"),
                    *self._expired_clusters(cutoff),
                ]),
            ),
        ]

    def _expired_clusters(self, cutoff):
        """Previous clusters whose last moment is out of the window.

        Never the one archiving now, whatever the history says.
        """
        inst = self._inst()
        expired = []
        for entry in inst.pitr_previous_clusters or []:
            cluster = str(entry.get("id", ""))
            until = fields.Datetime.to_datetime(entry.get("until"))
            if (
                until and until < cutoff and cluster.isdigit()
                and cluster != inst.pitr_cluster_id
            ):
                expired.append(cluster)
        return expired

    def _record(self, results):
        """Record the window and forget the clusters that were purged."""
        inst = self._inst()
        out = _stdout(results)
        self._record_pitr_bases(out)
        purged = set(_PURGED_RE.findall(out))
        if purged:
            inst.sudo().pitr_previous_clusters = [
                e for e in (inst.pitr_previous_clusters or [])
                if str(e.get("id")) not in purged
            ]
        if _ran(results, "Base copy"):
            self._pitr_resolve(ALERT_BASE_MISSING)

    async def on_success(self, results):
        """Record what the copy and the housekeeping did."""
        self._record(results)

    async def on_failure(self, results, errors):
        """Record what did happen; the watch says so if copies stop."""
        self._log_errors(errors)
        self._record(results)


class PitrStatusExecutor(_PitrExecutor):
    """Read the archiver and the agent; tell the panel how far behind."""

    _job_type = "pitr_status"

    def get_commands(self):
        """One reading."""
        d, user = self._common()
        return [(
            "Read the archiver",
            self.run_script("pitr.sh", ["status", d, user]),
        )]

    async def on_success(self, results):
        """Judge the reading: off, failing, behind, or fine."""
        inst = self._inst()
        status = parse_status(_stdout(results))
        if not status:
            return
        restore = {
            k.removeprefix("restore_"): v for k, v in status.items()
            if k.startswith("restore_")
        }
        if restore.get("state") in ("done", "db-only") and (
            # A restore that outlived the jobs that followed it.
            inst._pitr_record_restore(restore)
        ):
            self._follow_up_restore(inst)
        if status.get("restoring") == "lost":
            self._pitr_alert(
                ALERT_FAILING,
                f"A restore of '{inst.name}' (job {status.get('job')}) stopped "
                f"without finishing: its process is gone and its lock is left. "
                f"The instance may be stopped. Check .walg/restore/log on its "
                f"host, then remove .walg/restore/lock there.",
                level="critical",
            )
            return
        if status.get("restoring") == "1":
            return
        now = fields.Datetime.now()
        minutes = inst._pitr_objective_minutes()
        vals = {"pitr_checked_at": now}
        last = int(status.get("last_archived") or 0)
        if last:
            vals["pitr_last_archived_at"] = datetime.fromtimestamp(
                last, tz=timezone.utc,
            ).replace(tzinfo=None)
        failed = int(status.get("failed") or 0)
        vals["pitr_failed_count"] = failed
        pending = int(status.get("pending") or 0)
        oldest = int(status.get("oldest_pending") or 0)
        last_failed = int(status.get("last_failed") or 0)
        cluster = status.get("cluster")
        host_now = int(status.get("now") or 0)
        fs_ok = int(status.get("fs_ok") or 0)
        fs_behind = host_now - fs_ok if host_now and fs_ok else 0
        # Archiving for a day and not one filestore copy done yet.
        fs_never = not fs_ok and bool(
            inst.pitr_oldest_base_at
            and inst.pitr_oldest_base_at < now - timedelta(days=1)
        )

        if status.get("mode") != "on" or (
            inst.pitr_cluster_id and cluster and cluster != inst.pitr_cluster_id
        ):
            # A new cluster, or one whose settings were lost: switched
            # on again by the watch (once a day at most), or by hand on
            # a stack the panel does not render.
            vals["pitr_state"] = "failed" if inst.pitr_stack_external else "off"
            self._pitr_alert(
                ALERT_FAILING,
                f"'{inst.name}' is not archiving its database: continuous "
                f"recovery has nothing new since "
                f"{inst.pitr_last_archived_at or 'it was switched on'}.",
                level="critical",
            )
        elif status.get("agent") == "0":
            vals["pitr_state"] = "failed"
            self._pitr_alert(
                ALERT_FAILING,
                f"The continuous recovery agent of '{inst.name}' is not "
                f"running: neither its database's log nor its new files "
                f"reach the archive.",
                level="critical",
            )
        elif pending and last_failed > last:
            vals["pitr_state"] = "failed"
            self._pitr_alert(
                ALERT_FAILING,
                f"'{inst.name}' cannot upload its database log "
                f"({failed} failures, {pending} segment(s) waiting). "
                f"See the pitr container's log.",
                level="critical",
            )
        elif (
            oldest > 3 * minutes * 60 or fs_behind > 3 * minutes * 60 + 600
            or fs_never
        ):
            vals["pitr_state"] = "lagging"
            if oldest > 3 * minutes * 60:
                what = f"its database log by {oldest // 60} min"
            elif fs_never:
                what = "its files, none of which was copied yet"
            else:
                what = f"its new files by {fs_behind // 60} min"
            self._pitr_alert(
                ALERT_LAGGING,
                f"'{inst.name}' is behind archiving {what} "
                f"(objective: {minutes} min).",
            )
            self._pitr_resolve(ALERT_FAILING)
        else:
            vals["pitr_state"] = "ok"
            self._pitr_resolve(ALERT_FAILING, ALERT_LAGGING)
        base = inst.pitr_last_base_at
        if not base:
            self._pitr_alert(
                ALERT_BASE_MISSING,
                f"'{inst.name}' archives its log but has no base copy yet: "
                f"nothing can be restored until one is taken.",
            )
        elif base < now - timedelta(hours=36):
            self._pitr_alert(
                ALERT_BASE_MISSING,
                f"The last base copy of '{inst.name}' is from {base} UTC: "
                f"a restore replays more log every day it is missing.",
            )
        inst.sudo().write(vals)


class PitrRestoreExecutor(_PitrExecutor):
    """Restore the database to a moment, and the filestore with it.

    The restore itself runs on the host, on its own (``pitr.sh
    restore-start``): it stops the instance for as long as it takes, which
    can be longer than one job may run. This job starts it and follows
    it; when it runs out of time, a job of the same type (payload
    ``follow``) takes over, so the instance stays busy until it ends.
    """

    _job_type = "pitr_restore"

    #: Set by ``before_execute``: the base copy and timeline to start from.
    _plan = None
    #: Set by ``before_execute``: this job's restore already runs, or ran,
    #: on the host (the job is running again), so it only follows it.
    _rejoined = False
    #: Set by ``before_execute`` once the host said no restore of this job
    #: is there. Unset with no plan, the run never learnt whether one was
    #: started: an executor built anew for a lost job
    #: (``JobWorkerLost``), or a run whose question to the host failed.
    _probed = False

    def _target(self):
        """The payload's target, checked (naive UTC)."""
        raw = (self.job.payload or {}).get("target") or ""
        return self._inst()._pitr_parse_target(raw)

    def _followed(self):
        """The job whose restore this one follows: its own, or the payload's."""
        return int((self.job.payload or {}).get("follow") or self.job.id)

    async def before_execute(self, transport):
        """Pick the base copy and timeline that serve the target.

        First asks the host whether this job's restore is already there:
        queue_job runs a job that died once more, and its restore may be
        under way with the database stopped (where listing the copies
        would fail) or done.

        :raise ValueError: when no base copy does
        """
        if (self.job.payload or {}).get("follow"):
            return
        inst = self._inst()
        target = self._target()
        d, user = self._common()
        probe = self.run_script("pitr.sh", ["restore-wait", d, self.job.id, 0])
        await self._upload_scripts(transport)
        known = parse_restore((await transport.run(probe, timeout=120)).stdout)
        if known.get("state") not in (None, "lost"):
            self._rejoined = True
            self._sys("This job's restore is already on the host: following it.")
            return
        # A lock of its own with no process is a restore of this job that
        # was lost: not "nothing there".
        self._probed = known.get("lock") != str(self.job.id)
        command = self.run_script("pitr.sh", ["list", d, user])
        result = await transport.run(command, timeout=300)
        cluster, bases = parse_bases(result.stdout)
        if not cluster:
            raise ValueError(f"Could not list the base copies of '{inst.name}'.")
        switches = [
            s for s in inst.pitr_timelines or [] if s.get("cluster") == cluster
        ]
        chosen, timeline = choose_base(bases, target, switches)
        if not chosen:
            raise ValueError(
                f"No base copy of '{inst.name}' serves {target} UTC yet: "
                f"the first one after a restore is taken right after it."
            )
        self._plan = {
            "cluster": cluster,
            "backup": chosen["backup_name"],
            "timeline": timeline,
        }
        self._sys(
            f"Restoring '{inst.name}' to {target} UTC from base copy "
            f"{chosen['backup_name']} ({chosen['finish_time']} UTC), "
            f"timeline {timeline}."
        )

    def get_commands(self):
        """Start the restore on the host (unless following one), then wait.

        :raise ValueError: when no base copy was chosen
        """
        d, user = self._common()
        wait = (
            "Wait for the restore",
            self.run_script("pitr.sh", [
                "restore-wait", d, self._followed(), RESTORE_WAIT_S,
            ]),
        )
        if (self.job.payload or {}).get("follow") or self._rejoined:
            return [wait]
        if not self._plan:
            raise ValueError("No base copy was chosen for the restore.")
        target = self._target()
        return [
            (
                "Start the restore",
                self.run_script("pitr.sh", [
                    "restore-start", d, user, self.job.id,
                    self._plan["cluster"], self._plan["backup"],
                    self._plan["timeline"],
                    target.strftime("%Y-%m-%d %H:%M:%S") + "+00",
                    target.strftime("%Y-%m-%d"),
                ]),
                {"stop_on_failure": True},
            ),
            wait,
        ]

    def _restored(self, result):
        """The database is restored: record its timeline, copy it at once.

        Then the addons hear of it, as after any restore (``pitr``): the
        database is the one of a moment before, files restored or not.
        The watch may have recorded it first, and followed it up then. One
        that cannot be recorded (its timeline unread) is followed up here
        all the same: the watch, which cannot record it either, never does.

        :param dict result: the restore's ``IC_PITR_RESTORE`` fields
        """
        inst = self._inst()
        recorded = inst._pitr_record_restore(result)
        inst.sudo().write({
            "pitr_state": "ok", "status": "ok", **inst._running_confirmed_vals(),
        })
        if recorded is not False:
            self._follow_up_restore(inst)

    def _follow_on(self, reached=True):
        """Hand the restore, still running, to a job of the same type.

        While the host says its process runs, it is followed for as long
        as it takes: no job holding the instance would let a rebuild start
        a second database on the same data. One that cannot be reached is
        given up after a few jobs.

        :param bool reached: whether this job heard from the host
        :return: whether a job took it over
        """
        payload = dict(self.job.payload or {})
        rounds = int(payload.get("round") or 0) + 1
        missed = 0 if reached else int(payload.get("missed") or 0) + 1
        if missed > RESTORE_UNREACHABLE_MAX:
            return False
        if rounds == RESTORE_FOLLOW_MAX:
            inst = self._inst()
            self._pitr_alert(
                ALERT_FAILING,
                f"The restore of '{inst.name}' has been running for hours; "
                f"it is still followed. Do not touch the instance meanwhile.",
            )
        payload |= {"follow": self._followed(), "round": rounds, "missed": missed}
        inst = self._inst()
        # With ``sudo``: a restore asked for without superuser rights runs
        # without them, and follows itself for hours.
        self.env["cloud.job"].sudo().enqueue(
            inst.host_id.id, inst.id, "pitr_restore",
            payload=payload, bypass_running_check=True,
        )
        return True

    async def on_success(self, results):
        """Restored, or still restoring and followed by the next job."""
        inst = self._inst()
        result = parse_restore(_stdout(results))
        if result.get("state") == "running":
            self._follow_on()
            self._sys("The restore goes on on the host; the next job follows it.")
            return
        self._restored(result)
        self._pitr_resolve(ALERT_FAILING)
        if result.get("cancelled") == "1":
            self._sys(
                "The cancel came once the restored database was up: the "
                "restore had already been applied, and went on to the end."
            )
        self._sys(f"✓ '{inst.name}' restored to {self._target()} UTC.")

    async def on_failure(self, results, errors):
        """Say what state the instance was left in, which the host recorded.

        A restore a person cancelled and the host put back (``cancelled=1``)
        ended as asked: its database is as it was, and no alert is raised.
        """
        self._log_errors(errors)
        inst = self._inst()
        result = parse_restore(_stdout(results))
        state = result.get("state")
        when = self._target()
        if state == "failed" and result.get("cancelled") == "1":
            inst.sudo().write(inst._running_confirmed_vals())
            self._sys(
                f"✓ The restore of '{inst.name}' was cancelled at "
                f"'{result.get('stopped_at', '?')}' and undone: its database "
                f"is as it was and it runs again."
            )
            self._pitr_resolve(ALERT_FAILING)
            return
        if state == "db-only":
            self._restored(result)
            message = (
                f"'{inst.name}' was restored to {when} UTC, but not its "
                f"files: attachments may be missing. See the job's log."
            )
        elif state == "failed":
            # The script's ``finish`` brings the stack up whatever
            # happened: a stop the metrics cron reads now is the restart.
            inst.sudo().write(inst._running_confirmed_vals())
            message = (
                f"Restoring '{inst.name}' to {when} UTC failed at "
                f"'{result.get('stopped_at', '?')}'. Its database is as it "
                f"was and it runs again. See the job's log."
            )
        elif not result and not _ran(results, "Start the restore"):
            message = (
                f"The restore of '{inst.name}' to {when} UTC could not "
                f"start; nothing was changed. See the job's log."
            )
        else:
            message = (
                f"Restoring '{inst.name}' to {when} UTC ended in an unknown "
                f"state ({state or 'no answer'}): check .walg/restore/log "
                f"on its host. The instance may be stopped."
            )
        self._pitr_alert(ALERT_FAILING, message, level="critical")

    def _in_own_run(self):
        """Whether a hook runs inside the job's own run (``run``).

        Not for an executor built anew by the reconciliation of lost jobs
        (``cloud.job._cron_reconcile_lost_jobs``), nor for a job closed
        before its attempt began (``cloud.job._close_stopped_before_run``):
        both call the hook through ``asyncio.run``, outside ``run``'s loop.

        :rtype: bool
        """
        return self._loop is not None

    async def _live_line(self, line):
        """Log a line of the host's answer and write it to the job log now.

        A terminal hook runs outside ``run``'s ticks, which write the log
        while the steps run: the person who cancelled sees the host undo
        the restore as it does.

        :param str line: a line of the command's output, stripped
        """
        await self.on_stdout(line)
        if self._flush_logs():
            self._publish_bus()

    async def _ask_host(self, args, timeout):
        """Run ``pitr.sh <args>`` on the job's host, on a connection of its own.

        A terminal hook after a stop finds the run's connection closed and
        its directory removed, or there never was one (an executor built
        anew): a new directory, the scripts uploaded to it, and everything
        removed once the host answered.

        :param list args: the arguments of ``pitr.sh``
        :param int timeout: seconds the command may take
        :return: the command's ``CommandResult``
        :raise Exception: whatever the connection or the command raised
        """
        self._job_dir_path = None
        self._job_dir_created = False
        self._scripts_uploaded = False
        command = self.run_script("pitr.sh", args)
        async with self._host_record.get_transport() as transport:
            try:
                await self._upload_scripts(transport)
                return await transport.execute(
                    command, self._live_line, self._live_line, timeout=timeout,
                )
            finally:
                await self._discard_staging(transport)

    async def on_cancelled_running(self, results, exc):
        """Stop the restore on the host and wait for it to put back what it did.

        Always asked of the host, whatever the run reached: the restore
        runs there on its own, and an executor built anew (the
        reconciliation, a job stopped before its attempt) knows nothing of
        the run that started it. ``restore-cancel`` sends the restore's
        process a TERM, which stops the phase it is in and puts the
        previous data back, and waits for its result; it answers ``none``
        when nothing of this job's started, and asks again of a restore it
        already stopped. From the promotion of the restored database the
        restore goes on to the end instead (``done``).

        The hook holds the job's queue while it waits (``_scope_busy``
        counts a cancelled job whose queue.job runs). One the host has not
        finished undoing within the wait, or that could not be asked, is
        handed to a job of the same type, which follows it to its end
        (``_follow_on``). Alerts are raised again, for what the stop left
        (``ic_job_cancelled``).

        :param results: what the steps answered, or None before them
        :param JobCancelled exc: the stop
        """
        self._sys(f"✗ {CANCELLED_WHILE_RUNNING}")
        self.job = self.job.with_context(ic_job_cancelled=False)
        self.env = self.job.env
        inst = self._inst()
        d, _user = self._common()
        wait = CANCEL_WAIT_S if self._in_own_run() else CANCEL_WAIT_OUTSIDE_RUN_S
        self._sys(
            f"Asking the host to stop the restore of '{inst.name}' and put "
            f"back what it changed; waiting up to {wait} s."
        )
        self._flush_logs()
        try:
            answer = await self._ask_host(
                ["restore-cancel", d, self._followed(), wait],
                timeout=wait + CANCEL_SSH_MARGIN_S,
            )
        except Exception as err:  # noqa: BLE001 — said, and followed
            self._cancel_unanswered(f"{type(err).__name__}: {err}")
            return
        result = parse_restore(answer.stdout)
        if not result.get("state"):
            self._cancel_unanswered(f"exit {answer.exit_status}, no answer")
            return
        self._judge_cancel(result)

    def _cancel_unanswered(self, why):
        """The host could not be told to stop the restore, or did not answer.

        Whether it got the stop is unknown: the next job follows the
        restore to its end, and the alert says not to touch the instance.

        :param str why: what went wrong, for the log
        """
        inst = self._inst()
        self._sys(f"✗ The host could not be told to stop the restore: {why}.")
        followed = self._follow_on(reached=False)
        self._pitr_alert(
            ALERT_FAILING,
            f"The restore of '{inst.name}' was cancelled, but its host could "
            f"not be asked to stop it: it may still be running"
            f"{', and the next job follows it' if followed else ''}. Check "
            f".walg/restore/log there before touching the instance.",
            level="critical",
        )

    def _judge_cancel(self, result):
        """Record how a cancelled restore ended on the host, and say so.

        :param dict result: the ``IC_PITR_RESTORE`` fields of
            ``restore-cancel``
        """
        inst = self._inst()
        state = result.get("state")
        when = self._target()
        if state == "failed":
            # ``finish`` started the stack again: a stop the metrics cron
            # reads now is the restart.
            inst.sudo().write(inst._running_confirmed_vals())
            self._pitr_resolve(ALERT_FAILING)
            if result.get("cancelled") == "1":
                self._sys(
                    f"✓ The restore was stopped at "
                    f"'{result.get('stopped_at', '?')}' and undone: the "
                    f"database of '{inst.name}' is as it was before it, and "
                    f"the instance runs again."
                )
            else:
                self._sys(
                    f"The restore had already failed at "
                    f"'{result.get('stopped_at', '?')}' on its own: the "
                    f"database of '{inst.name}' is as it was, and the "
                    f"instance runs again."
                )
        elif state == "none":
            self._sys("The restore had not started on the host: nothing changed.")
            if result.get("held_by"):
                self._sys(
                    f"Another restore (job {result['held_by']}) holds the "
                    f"instance there, and goes on."
                )
        elif state == "done":
            self._restored(result)
            self._pitr_resolve(ALERT_FAILING)
            self._sys(
                f"The restore had already been applied when the cancel came: "
                f"'{inst.name}' is restored to {when} UTC, its files with it."
            )
        elif state == "db-only":
            self._restored(result)
            self._sys("The restore had already been applied when the cancel came.")
            self._pitr_alert(
                ALERT_FAILING,
                f"'{inst.name}' was restored to {when} UTC before its cancel "
                f"came, but not its files: attachments may be missing. See "
                f"the job's log.",
                level="critical",
            )
        elif state == "running":
            self._follow_on()
            self._sys(
                "The host is still undoing the restore; the next job follows "
                "it to its end."
            )
            self._pitr_alert(
                ALERT_FAILING,
                f"The cancelled restore of '{inst.name}' is still being "
                f"undone on its host; a job follows it to its end. Do not "
                f"touch the instance meanwhile.",
            )
        elif state == "lost":
            self._pitr_alert(
                ALERT_FAILING,
                f"The cancelled restore of '{inst.name}' stopped without "
                f"finishing: its process is gone and its lock is left. The "
                f"instance may be stopped. Check .walg/restore/log on its "
                f"host, then remove .walg/restore/lock there.",
                level="critical",
            )
        else:
            # ``failed-unknown``: the previous data could not be put back.
            self._pitr_alert(
                ALERT_FAILING,
                f"The cancelled restore of '{inst.name}' ended in an unknown "
                f"state ({state}): the previous data could not be put back. "
                f"Do not touch the instance: read .walg/restore/log on its "
                f"host, and look for .pre-pitr-* and .pitr-failed-* in its "
                f"database's data directory, which hold the data from "
                f"before the restore and the restored copy.",
                level="critical",
            )

    async def on_abort(self, exc):
        """No base copy, or the job stopped while following the restore.

        Before anything started (no plan, not following, the host said
        nothing of this job was there) nothing changed. Otherwise the
        restore may still be running on the host: another job follows it,
        or the alert says to look.

        An executor that never asked the host — built anew for a job whose
        worker died (``JobWorkerLost``), or whose question failed — asks it
        now (``_abort_unknown``).
        """
        self._sys(f"✗ {type(exc).__name__}: {exc}")
        following = (self.job.payload or {}).get("follow") or self._rejoined
        if not (self._plan or following):
            if not self._probed:
                await self._abort_unknown()
            return
        inst = self._inst()
        if self._follow_on(reached=False):
            self._sys("The restore may go on on the host; the next job follows it.")
            return
        self._pitr_alert(
            ALERT_FAILING,
            f"The job following the restore of '{inst.name}' stopped "
            f"({type(exc).__name__}). It may still be running on the host: "
            f"check .walg/restore/log there before touching the instance.",
            level="critical",
        )

    async def _abort_unknown(self):
        """Ask the host what became of this job's restore, and act on it.

        For a job that ended without knowing whether its restore started:
        one still running is followed by the next job, one that ended is
        recorded as a wait for it would have, and nothing is done when no
        restore of this job is there. A host that cannot be asked leaves
        the restore to the next job, which gives it up in the end.
        """
        d, _user = self._common()
        try:
            answer = await self._ask_host(
                ["restore-wait", d, self._followed(), 0], timeout=120,
            )
        except Exception as err:  # noqa: BLE001 — said, and followed
            self._sys(
                f"✗ The host could not be asked whether this job's restore "
                f"started: {type(err).__name__}: {err}."
            )
            if self._follow_on(reached=False):
                self._sys("The restore may go on on the host; the next job follows it.")
            return
        result = parse_restore(answer.stdout)
        state = result.get("state")
        if not state:
            self._sys(
                f"✗ The host gave no answer about this job's restore "
                f"(exit {answer.exit_status})."
            )
            if self._follow_on(reached=False):
                self._sys("The restore may go on on the host; the next job follows it.")
        elif state == "running":
            self._follow_on()
            self._sys("This job's restore goes on on the host; the next job follows it.")
        elif state == "lost" and result.get("lock") != str(self._followed()):
            self._sys("No restore of this job is on the host: nothing changed.")
        else:
            results = {"Wait for the restore": {
                "stdout": answer.stdout, "exit_status": answer.exit_status,
            }}
            if answer.exit_status == 0:
                await self.on_success(results)
            else:
                await self.on_failure(results, [])
