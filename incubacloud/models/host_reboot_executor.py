"""The check for a pending restart, and the restart itself.

``host_reboot_check`` reads the server; ``host_reboot`` restarts it. See
``cloud_host_reboot`` for when, and for the hold the restart keeps.

The restart runs in three attempts of one job, never with two
connections at once on the server going down:

1. **Before**, in one connection: the server's ``boot_id``; the hold
   (``_take_hold``: lock, what blocks it, the row), unless something of
   the panel runs; the server's own veto (``host_reboot.sh guard``), and
   what runs inside each instance as a layer counts it
   (``cloud.instance._move_quiet_sql``, the count a move asks too); the
   containers running, and a ``CHECKPOINT`` in each database
   (``host_reboot.sh prepare``); the job's point of no return (``cloud.job.no.return``), after which a stop
   is refused; ``shutdown -r +1``, checked in the same connection. From
   that order on the server opens no new SSH session (logind's
   ``/run/nologin``, measured 15-nov-2026), so the job parks
   (``JobParked``) ``PARK_FIRST_S`` and connects again only once the
   server has gone down.
2. **Waiting**: every ``PARK_RETRY_S`` it tries to connect; a transient
   error parks it again, up to ``RETURN_DEADLINE_S`` from the order.
3. **Back**: a new ``boot_id``. In the same connection, ``host_reboot.sh
   verify`` waits for the containers that ran before and reads whether a
   PostgreSQL started after a stop that was not clean. One missing, or
   Docker down, raises ``host_reboot_incomplete``, naming them; no
   container is started by hand. The job ends done, queues the health
   probe of each instance on the host, and its end serves the queues its
   hold kept (``cloud.job._reboot_released_scopes``).

The check asks the server's veto and counts inside the instances itself,
inside the window with a restart pending, and queues no restart the
server would refuse: a Restart Server job is not left behind, done, for
each pass the server said no. What changes in the seconds between the
check and the job's start still ends the job ``done`` with a line saying
why and no alert: the next pass of the check tries again inside the
window. The job the check queued (``by_check`` in its payload) reads the
window, the policy and the pending restart again as it starts: it may
have waited in the queue behind another job of the host, for hours. A
server that does not come back, or comes back with the same ``boot_id``,
fails the job with ``host_reboot_failed``.

Between the order and the park: a connection lost as the order is given,
or an order that answered badly, may have left the restart scheduled.
The job calls it off (``host_reboot.sh cancel``) and fails, letting its
hold go, only when the server says nothing is scheduled any more; else it
parks, as after the order, and the server's boot decides.

The check closes the restart's alerts once the problem is over, whoever
solved it: ``host_reboot_failed`` once the server answers with another
boot than the one it had before the restart, ``host_reboot_incomplete``
once Docker and every container the alert waited for run.
"""
import json
import logging
from datetime import datetime, timezone

from odoo import fields

from .abstract_executor import (
    AbstractSSHExecutor,
    JobParked,
    is_transient_connection_error,
)
from .cloud_host_reboot import (
    REBOOT_FAILED_ALERT,
    REBOOT_INCOMPLETE_ALERT,
    REBOOT_LOCK_NS,
    REBOOT_PANEL_AWAY_H,
    REBOOT_PANEL_KEY,
    REBOOT_TZ_DEFAULT,
    RETURN_DEADLINE_S,
    first_mark,
    panel_boot_id,
    parse_marks,
)

_logger = logging.getLogger(__name__)

#: Seconds the job parks after ``shutdown -r +1``: the minute of the
#: order, and the half minute the server takes to go down. Measured
#: 15-nov-2026: back with a new boot 92 to 98 s after the order.
PARK_FIRST_S = 90
#: Seconds between two tries to reach the server while it comes back.
PARK_RETRY_S = 30
#: Exit status of ``host_reboot.sh guard`` when the server says no.
EXIT_VETO = 75
#: Seconds ``host_reboot.sh verify`` may take: the 300 it waits for the
#: containers to come back, and the reading of the databases' logs.
VERIFY_TIMEOUT_S = 420

#: The label of the order: the job's point of no return.
REBOOT_LABEL = "Restart the server"
#: What the panel reads of the server's boot: its id, whether a restart
#: is scheduled, whether an update waits for one.
_READ_BOOT = (
    "cat /proc/sys/kernel/random/boot_id;"
    " if [ -e /run/systemd/shutdown/scheduled ]; then echo IC_SCHEDULED=1; fi;"
    " if [ -e /run/reboot-required ]; then echo IC_REQUIRED=1; fi"
)
#: The payload of the restart the check queues: it reads again, as it
#: starts, whether it is still due.
BY_CHECK = "by_check"


def inside_specs(executor):
    """Return what to count inside each instance of the executor's host.

    For each deployed instance whose ``cloud.instance._move_quiet_sql``
    gives a statement: core gives none, a layer whose instances are
    applications it knows counts their jobs, as before a move.

    :param executor: an executor of the host
    :return: ``[(instance name, [dir, pg_user, dbname, sql])]``
    :rtype: list
    """
    instances = executor.env["cloud.instance"].sudo().search([
        ("host_id", "=", executor._host().id), ("state", "=", "deployed"),
    ])
    specs = []
    for inst in instances:
        sql = inst._move_quiet_sql()
        if sql:
            specs.append((inst.name, [
                executor._inst_dir(inst), inst.postgres_username or "odoo",
                inst.postgres_dbname or "prod", sql,
            ]))
    return specs


class HostRebootCheckExecutor(AbstractSSHExecutor):
    """Read whether the server waits for a restart; queue it if it is time.

    Hidden, from ``cloud.host.cron_host_reboot_check``. Writes on the host
    when the restart became pending and for which packages, and the boot
    read; turns the server's own restart off in hardening's policy when it
    is still on. Inside the window of a host the panel restarts by itself,
    with nothing blocking it, it queues ``host_reboot``.
    """

    _job_type = "host_reboot_check"
    _reboot_probe = True

    def get_commands(self):
        """One order: ``host_reboot.sh check``, told what the panel knows.

        Always the panel's mark: whether it restarts the server by itself,
        its window and ``REBOOT_PANEL_AWAY_H`` (``host_reboot.sh away``).
        For a host the panel restarts, the instances to count inside; and
        the server's veto when the panel may restart it now
        (``_reboot_not_due``). The containers to wait for while
        ``host_reboot_incomplete`` is open.
        """
        host = self.job.host_id
        auto = host._reboot_by_panel()
        args = [
            "check", "--auto", "1" if auto else "0",
            "--window", host._reboot_window_label(),
            str(host.reboot_window_hours), host.reboot_tz or REBOOT_TZ_DEFAULT,
            "--away", str(REBOOT_PANEL_AWAY_H * 3600),
        ]
        if auto:
            if not host._reboot_not_due():
                args.append("--guard")
            for _name, spec in inside_specs(self):
                args += ["--inside", *spec]
        if self._alert_open(REBOOT_INCOMPLETE_ALERT):
            row = host._reboot_last_row()
            args += ["--expect", *(row._containers() if row else [])]
        return [("Check", self.run_script("host_reboot.sh", args))]

    def _alert_open(self, code):
        """Whether the alert *code* of the job's host is active.

        :param str code: a ``cloud.alert`` code
        :rtype: bool
        """
        return bool(self.env["cloud.alert"].sudo().search_count([
            ("code", "=", code), ("host_id", "=", self.job.host_id.id),
            ("state", "=", "active"),
        ], limit=1))

    def _close_restart_alerts(self, host, boot_id, marks):
        """Close the restart's alerts the server shows are over.

        ``host_reboot_failed`` once the server answers with another boot
        than the one it had before the panel's last restart: it restarted
        since, by a person, by itself or by the panel. ``host_reboot_incomplete``
        once Docker runs and every container it waited for does
        (``--expect``).

        :param host: the job's ``cloud.host``
        :param str boot_id: the boot the check read
        :param dict marks: what ``parse_marks`` read of the check
        """
        if self._alert_open(REBOOT_FAILED_ALERT):
            row = host._reboot_last_row()
            if row and row.boot_id and boot_id != row.boot_id:
                self._resolve_alert(REBOOT_FAILED_ALERT)
                self._sys(
                    "The server answers with a new boot since its restart"
                    " failed: host_reboot_failed closed."
                )
        if (
            self._alert_open(REBOOT_INCOMPLETE_ALERT)
            and first_mark(marks, "IC_DOCKER") == "active"
            and not marks.get("IC_MISSING")
        ):
            self._resolve_alert(REBOOT_INCOMPLETE_ALERT)
            self._sys(
                "Docker and every container the restart waited for run:"
                " host_reboot_incomplete closed."
            )

    async def on_stdout(self, line):
        """Keep the marks out of the job log: they are read, not shown."""

    def parse_results(self, results):
        """Fail a check that did not answer.

        :return: list of error strings
        """
        data = results.get("Check") or {}
        if data.get("exit_status", 1) != 0:
            return [f"The check exited with status {data.get('exit_status')}."]
        if not first_mark(parse_marks(data.get("stdout")), "IC_BOOT_ID"):
            return ["The check did not say the server's boot."]
        return []

    async def on_success(self, results):
        """Record what the server said, and queue the restart if it is time."""
        host = self.job.host_id
        now = fields.Datetime.now()
        marks = parse_marks(results["Check"]["stdout"])
        boot_id = first_mark(marks, "IC_BOOT_ID")
        required = first_mark(marks, "IC_REBOOT_REQUIRED") == "1"
        vals = {"reboot_checked_at": now, "reboot_boot_id": boot_id}
        if required:
            vals["reboot_required_pkgs"] = first_mark(marks, "IC_REBOOT_PKGS") or False
            if not host.reboot_required_since:
                vals["reboot_required_since"] = now
        else:
            vals |= {"reboot_required_since": False, "reboot_required_pkgs": False}
        host.write(vals)
        if first_mark(marks, "IC_POLICY") == "fixed":
            self._sys(
                "The server restarted by itself for its updates: turned off,"
                " the panel restarts it inside its window."
            )
        away = first_mark(marks, "IC_AWAY_RESTART")
        if away.isdigit():
            self._sys(
                "The server restarted by itself at"
                f" {datetime.fromtimestamp(int(away), timezone.utc):%Y-%m-%d %H:%M}"
                f" UTC: the panel had not checked it for {REBOOT_PANEL_AWAY_H} h."
            )
        self._close_restart_alerts(host, boot_id, marks)
        host._reboot_alert_pending(now)
        if not required:
            self._sys("No restart pending.")
            return
        self._sys(
            f"A restart is pending since {host.reboot_required_since}"
            f" ({host.reboot_required_pkgs or 'updates'})."
        )
        not_due = host._reboot_not_due(now)
        if not_due:
            # A closed window is every check's but those of the night.
            if host._reboot_window_open(now) or not host._reboot_by_panel():
                self._sys(not_due)
            return
        if host._reboot_job_active():
            return
        veto = first_mark(marks, "IC_VETO")
        if veto:
            self._sys(f"Not restarted in this pass: the server said no: {veto}.")
            return
        holds_panel = boot_id == panel_boot_id()
        blockers = host._reboot_blockers(holds_panel=holds_panel)
        if blockers:
            self._sys("Not restarted in this pass: " + " ".join(blockers))
            return
        # Not chained: the restart waits its turn behind what the server's
        # queue holds, and reads the window, the policy and the pending
        # restart again as it starts (``HostRebootExecutor``).
        job_id = self.env["cloud.job"].sudo().enqueue(
            host.id, False, "host_reboot", payload={BY_CHECK: True},
            bypass_running_check=False,
        )
        self._sys(f"Restart queued (job #{job_id}).")


class HostRebootExecutor(AbstractSSHExecutor):
    """Restart the server once nothing of the panel runs there, and wait.

    See the module's docstring for the attempts. ``_verdict`` says how the
    attempt that reaches the hooks ended: ``("skipped", reason)`` when it
    did not restart, ``("back", None)`` when the server came back,
    ``("failed", reason)`` when it did not.
    """

    _job_type = "host_reboot"
    # Its failure files this alert, not ``job_failed`` beside it.
    _failure_alert_code = REBOOT_FAILED_ALERT

    _verdict = None

    # ── the row of the hold ──────────────────────────────────────────────

    def _row_values(self):
        """Return this job's ``cloud.host.reboot`` row as committed, or None.

        :return: ``{'id', 'boot_id', 'shutdown_at', 'holds_panel',
            'containers'}``
        :rtype: dict | None
        """
        with self._durable_env() as env:
            row = env["cloud.host.reboot"].sudo().search(
                [("job_id", "=", self.job.id)], limit=1,
            )
            if not row:
                return None
            return {
                "id": row.id,
                "boot_id": row.boot_id,
                "shutdown_at": row.shutdown_at,
                "holds_panel": row.holds_panel,
                "containers": row._containers(),
            }

    def _write_row(self, vals):
        """Write *vals* on this job's row, on a cursor that commits now.

        :param dict vals: ``cloud.host.reboot`` values
        """
        with self._durable_env() as env:
            env["cloud.host.reboot"].sudo().search(
                [("job_id", "=", self.job.id)], limit=1,
            ).write(vals)

    def _take_hold(self, boot_id):
        """Hold the host, unless something of the panel runs there.

        On a cursor of its own, committed on return: the exclusive lock of
        the host (and of every queue, ``REBOOT_PANEL_KEY``, when the host
        is the panel's own server), tried, never waited for; then what
        blocks the restart, read after the lock at READ COMMITTED; then
        the row. A lock held means a job is being dispatched or runs there:
        that is a reason too.

        :param str boot_id: the server's boot, as read now
        :return: the reasons not to restart, empty when the host is held
        :rtype: list
        """
        holds_panel = bool(boot_id) and boot_id == panel_boot_id()
        host_id = self._host().id
        keys = sorted({host_id} | ({REBOOT_PANEL_KEY} if holds_panel else set()))
        with self._durable_env() as env:
            for key in keys:
                env.cr.execute(
                    "SELECT pg_try_advisory_xact_lock(%s, %s)", (REBOOT_LOCK_NS, key),
                )
                if not env.cr.fetchone()[0]:
                    return [env._("A job of the panel is starting or runs there.")]
            host = env["cloud.host"].sudo().browse(host_id)
            reasons = host._reboot_blockers(
                exclude_job_id=self.job.id, holds_panel=holds_panel,
            )
            if reasons:
                return reasons
            Reboot = env["cloud.host.reboot"].sudo()
            vals = {
                "host_id": host_id,
                "holds_panel": holds_panel,
                "boot_id": boot_id,
                "shutdown_at": False,
            }
            row = Reboot.search([("job_id", "=", self.job.id)], limit=1)
            if row:
                row.write(vals)
            else:
                Reboot.create(vals | {"job_id": self.job.id})
        if holds_panel:
            self._sys(
                "This is the panel's own server: every job of the panel waits"
                " for the restart."
            )
        return []

    # ── the attempts ────────────────────────────────────────────────────

    async def _async_entry(self):
        """Wait for the server once the order was given; else run as usual."""
        row = self._row_values()
        if row and row["shutdown_at"]:
            return await self._wait_for_return(row)
        return await super()._async_entry()

    async def before_execute(self, transport):
        """Take the hold, ask the server, restart it, and park.

        :raises JobParked: once the order is given
        """
        self._sys("Restarting the server for its updates...")
        read = await transport.run(_READ_BOOT)
        lines = (read.stdout or "").strip().splitlines()
        boot_id = lines[0].strip() if lines else ""
        if read.exit_status != 0 or not boot_id:
            raise RuntimeError("Could not read the server's boot.")
        not_due = self._not_due_any_more("IC_REQUIRED=1" in lines)
        if not_due:
            self._verdict = ("skipped", not_due)
            return
        reasons = self._take_hold(boot_id)
        if reasons:
            self._verdict = ("skipped", " ".join(reasons))
            return
        guard = self.run_script("host_reboot.sh", ["guard"])
        counts = self._inside_counts()
        prepare = self.run_script("host_reboot.sh", ["prepare"])
        reboot = self.run_script("host_reboot.sh", ["reboot"])
        cancel = self.run_script("host_reboot.sh", ["cancel"])
        await self._upload_scripts(transport)
        self._sys("Running: Ask the server")
        said = await transport.execute(guard, self.on_stdout, self.on_stderr)
        if said.exit_status == EXIT_VETO:
            veto = first_mark(parse_marks(said.stdout), "IC_VETO")
            self._verdict = ("skipped", f"The server said no: {veto}.")
            return
        if said.exit_status != 0:
            raise RuntimeError(f"Asking the server failed (exit {said.exit_status}).")
        busy = await self._busy_inside(transport, counts)
        if busy:
            self._verdict = ("skipped", busy)
            return
        self._sys("Running: Prepare the restart")
        prepared = await transport.execute(prepare, self.on_stdout, self.on_stderr)
        prepared_marks = parse_marks(prepared.stdout)
        containers = prepared_marks.get("IC_CONTAINER", [])
        left = prepared_marks.get("IC_NOT_EXPECTED", [])
        if left:
            self._sys(
                "Not expected back after the boot, no restart policy brings"
                " them: " + ", ".join(left) + "."
            )
        # A stop asked for until here ends the job, with no restart.
        self._pass_no_return(REBOOT_LABEL)
        # Before the order, so an attempt that dies right after it waits
        # for the server instead of giving the order again.
        self._write_row({
            "shutdown_at": fields.Datetime.now(),
            "containers": json.dumps(containers),
        })
        self._sys(f"Running: {REBOOT_LABEL}")
        try:
            done = await transport.execute(reboot, self.on_stdout, self.on_stderr)
        except Exception as exc:
            # The order may have reached the server: its boot decides.
            raise self._park_unsure(
                f"the connection was lost as the order was given ({type(exc).__name__})"
            ) from exc
        if done.exit_status != 0:
            if not await self._call_off(transport, cancel):
                raise self._park_unsure(
                    f"the order exited {done.exit_status}, and the restart"
                    " could not be called off"
                )
            self._write_row({"shutdown_at": False})
            raise RuntimeError(
                f"The server did not schedule its restart (exit {done.exit_status})."
            )
        # The last order on this connection: no other one opens until the
        # server is back.
        await self._discard_staging(transport)
        self._sys(
            "The server restarts in a minute. The panel connects again in"
            f" {PARK_FIRST_S} s."
        )
        raise JobParked("The server restarts; waiting for it.", seconds=PARK_FIRST_S)

    def get_commands(self):
        """No steps of its own: ``before_execute`` gives the orders."""
        return []

    def _not_due_any_more(self, required):
        """Say why the restart the check queued is not due any more, or ''.

        Only for a job the check queued (``BY_CHECK``): it may have waited
        in the queue past its window, after its policy was switched off,
        or after someone restarted the server (``_reboot_not_due``, and
        the server's pending restart read now).

        :param bool required: the server says an update waits for a restart
        :rtype: str
        """
        if not (self.job.payload or {}).get(BY_CHECK):
            return ""
        not_due = self.job.host_id._reboot_not_due()
        if not_due:
            return not_due
        if not required:
            return "No update waits for a restart any more."
        return ""

    def _park_unsure(self, why):
        """Return the park of an order that may have scheduled the restart.

        The row keeps ``shutdown_at``, and with it the hold: the next
        attempt waits for the server, and its boot says whether it
        restarted (``_wait_for_return``).

        :param str why: what happened to the order
        :return: the ``JobParked`` to raise
        """
        self._sys(
            f"Not sure the server restarts: {why}. The panel connects again"
            f" in {PARK_FIRST_S} s, and the server's boot says."
        )
        return JobParked(f"The restart may be scheduled: {why}.", seconds=PARK_FIRST_S)

    async def _call_off(self, transport, cancel):
        """Call off a restart the order may have scheduled; never raises.

        :param transport: the open connection of the order
        :param str cancel: the command of ``host_reboot.sh cancel``
        :return: whether the server says nothing is scheduled
        :rtype: bool
        """
        try:
            result = await transport.execute(cancel, self.on_stdout, self.on_stderr)
        except Exception:  # noqa: BLE001 — the boot decides then
            return False
        return result.exit_status == 0

    def _inside_counts(self):
        """Return the count of what runs inside each instance a layer reads.

        For each instance ``inside_specs`` gives, ``move_steps.sh quiet``.
        A stack that is parked counts 0.

        :return: ``[(instance name, command)]``
        :rtype: list
        """
        return [
            (name, self.run_script("move_steps.sh", ["quiet", *spec]))
            for name, spec in inside_specs(self)
        ]

    async def _busy_inside(self, transport, counts):
        """Say what runs inside an instance, or that it could not be read.

        :param counts: what ``_inside_counts`` returned
        :return: the reason not to restart, or None
        :rtype: str | None
        """
        for name, command in counts:
            result = await transport.run(command)
            lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
            count = lines[-1] if lines else ""
            if result.exit_status != 0 or not count.isdigit():
                return f"What runs inside {name} could not be counted."
            if int(count) > 0:
                return f"{count} job(s) run inside {name}."
        return None

    async def _wait_for_return(self, row):
        """Try to reach the server; park again, or close the job.

        :param dict row: this job's row, as ``_row_values`` gives it
        :raises JobParked: while the server is on its way back
        """
        elapsed = (fields.Datetime.now() - row["shutdown_at"]).total_seconds()
        late = elapsed >= RETURN_DEADLINE_S
        try:
            async with self._host_record.get_transport() as transport:
                read = await transport.run(_READ_BOOT)
                lines = (read.stdout or "").strip().splitlines()
                boot_id = lines[0].strip() if lines else ""
                scheduled = "IC_SCHEDULED=1" in lines
                if boot_id and boot_id != row["boot_id"]:
                    self._verdict = ("back", None)
                    self._sys(
                        f"The server is back, {int(elapsed)} s after the order."
                    )
                    verified = await self._verify(transport, row["containers"])
                    await self._dispatch_outcome({
                        "Back": {"stdout": read.stdout, "exit_status": 0},
                        "Verify": verified,
                    }, transport)
                    return
                if scheduled and not late:
                    raise self._park_again("its restart is still scheduled")
                self._verdict = ("failed", (
                    "The server answered with the boot it had before the"
                    f" restart, {int(elapsed)} s after the order: it did not"
                    " restart."
                ))
        except JobParked:
            raise
        except Exception as exc:
            if not is_transient_connection_error(exc):
                raise
            if not late:
                raise self._park_again("it does not answer yet") from exc
            self._verdict = ("failed", (
                f"The server has not answered for {int(elapsed)} s since the"
                f" order to restart ({type(exc).__name__})."
            ))
        await self._dispatch_outcome({}, None)

    async def _verify(self, transport, containers):
        """Run ``host_reboot.sh verify`` on what ran before; never raises.

        :param list containers: the names ``prepare`` gave
        :return: ``{'stdout': str, 'exit_status': int}``
        """
        self._sys("Running: Check what came back")
        try:
            command = self.run_script("host_reboot.sh", ["verify", *containers])
            await self._upload_scripts(transport)
            result = await transport.execute(
                command, self.on_stdout, self.on_stderr, timeout=VERIFY_TIMEOUT_S,
            )
            return {"stdout": result.stdout, "exit_status": result.exit_status}
        except Exception as exc:  # noqa: BLE001 — the server is back anyway
            if is_transient_connection_error(exc):
                raise
            return {"stdout": "", "exit_status": -1, "error": str(exc)}
        finally:
            await self._discard_staging(transport)

    def _park_again(self, why):
        """Say once that the job waits for the server; return the park.

        :param str why: what the last try found
        :return: the ``JobParked`` to raise
        """
        self.job._note_once(self.job.env._(
            "Waiting for the server to come back: tried every %(s)s s.",
            s=PARK_RETRY_S,
        ))
        return JobParked(f"Waiting for the server: {why}.", seconds=PARK_RETRY_S)

    # ── the outcome ─────────────────────────────────────────────────────

    def parse_results(self, results):
        """A restart that did not come back is the job's failure.

        :return: list of error strings
        """
        if self._verdict and self._verdict[0] == "failed":
            return [self._verdict[1]]
        return []

    async def on_success(self, results):
        """Say why there was no restart, or record the server's new boot."""
        host = self.job.host_id
        verdict = self._verdict or ("skipped", "")
        if verdict[0] == "skipped":
            self._sys(
                f"Not restarted: {verdict[1]} Tried again in the next pass"
                " of the window."
            )
            return
        self._resolve_alert(REBOOT_FAILED_ALERT)
        lines = (results.get("Back", {}).get("stdout") or "").strip().splitlines()
        verified = results.get("Verify") or {}
        marks = parse_marks(verified.get("stdout"))
        vals = {
            "reboot_boot_id": lines[0].strip() if lines else False,
            "reboot_checked_at": False,
        }
        if first_mark(marks, "IC_REBOOT_REQUIRED") == "0":
            vals |= {"reboot_required_since": False, "reboot_required_pkgs": False}
        host.write(vals)
        for name in marks.get("IC_UNCLEAN", []):
            self._sys(
                f"⚠ The database {name} started after a stop that was not"
                " clean: it recovered from its log."
            )
        missing = marks.get("IC_MISSING", [])
        problems = []
        if verified.get("exit_status") != 0:
            problems.append(self.env._("what came back could not be checked"))
        if first_mark(marks, "IC_DOCKER") == "inactive":
            problems.append(self.env._("Docker is not running"))
        if missing:
            problems.append(self.env._(
                "these containers did not come back: %(names)s",
                names=", ".join(missing),
            ))
        if problems:
            self._alert(
                REBOOT_INCOMPLETE_ALERT,
                self.env._(
                    "The server %(host)s came back from its restart, but %(what)s."
                    " The panel started nothing by hand: look at the server, then"
                    " start what is missing from its instances' pages.",
                    host=host.name, what="; ".join(problems),
                ),
                level="warning",
            )
            self._sys("⚠ Restarted, incomplete: " + "; ".join(problems) + ".")
        else:
            self._resolve_alert(REBOOT_INCOMPLETE_ALERT)
            self._sys("✓ Restarted: everything that ran is running again.")
        Job = self.env["cloud.job"].sudo()
        # What each instance is now; its streaks and alerts are left to the
        # cycle of ``cron_instance_health``, with Odoo maybe still starting.
        for inst in self.env["cloud.instance"].sudo().search([
            ("host_id", "=", host.id), ("deployed", "=", True),
        ]):
            Job.enqueue(
                host.id, inst.id, "instance_health", payload={"state_only": True},
                bypass_running_check=True,
            )
        # The check runs at once: whether an update still waits, and the
        # alert of a pending restart closed with it.
        Job.enqueue(host.id, False, "host_reboot_check", bypass_running_check=True)

    async def on_failure(self, results, errors):
        """Raise ``host_reboot_failed``: the server did not come back."""
        host = self.job.host_id
        self._alert(
            REBOOT_FAILED_ALERT,
            self.env._(
                "The server %(host)s did not come back from its restart for its"
                " updates. %(why)s Look at the server through its provider's"
                " console; once it answers, the panel's jobs run there again.",
                host=host.name, why=" ".join(errors),
            ),
            level="critical",
        )
        self._sys("✗ " + " ".join(errors))
