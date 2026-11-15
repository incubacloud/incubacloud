"""The check for a pending restart, and the restart itself.

``host_reboot_check`` reads the server; ``host_reboot`` restarts it. See
``cloud_host_reboot`` for when, and for the hold the restart keeps.

The restart runs in three attempts of one job, never with two
connections at once on the server going down:

1. **Before**, in one connection: the server's ``boot_id``; the hold
   (``_take_hold``: lock, what blocks it, the row), unless something of
   the panel runs; the server's own veto (``host_reboot.sh guard``); the
   job's point of no return (``cloud.job.no.return``), after which a stop
   is refused; ``shutdown -r +1``, checked in the same connection. From
   that order on the server opens no new SSH session (logind's
   ``/run/nologin``, measured 15-nov-2026), so the job parks
   (``JobParked``) ``PARK_FIRST_S`` and connects again only once the
   server has gone down.
2. **Waiting**: every ``PARK_RETRY_S`` it tries to connect; a transient
   error parks it again, up to ``RETURN_DEADLINE_S`` from the order.
3. **Back**: a new ``boot_id``. The job ends done, and its end serves the
   queues its hold kept (``cloud.job._reboot_released_scopes``).

A veto, or what blocks the hold, ends the job ``done`` with a line saying
why and no alert: the next pass of the check tries again inside the
window. A server that does not come back, or comes back with the same
``boot_id``, fails the job with ``host_reboot_failed``.
"""
import logging

from odoo import fields

from .abstract_executor import (
    AbstractSSHExecutor,
    JobParked,
    is_transient_connection_error,
)
from .cloud_host_reboot import (
    REBOOT_FAILED_ALERT,
    REBOOT_LOCK_NS,
    REBOOT_PANEL_KEY,
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
#: Seconds from the order after which a server that does not answer, or
#: answers with the boot it had, failed to restart: a restart measured in
#: production takes about 2 minutes, and a disk check may take longer.
RETURN_DEADLINE_S = 900
#: Exit status of ``host_reboot.sh guard`` when the server says no.
EXIT_VETO = 75

#: The label of the order: the job's point of no return.
REBOOT_LABEL = "Restart the server"
#: What the panel reads of the server's boot.
_READ_BOOT = (
    "cat /proc/sys/kernel/random/boot_id;"
    " if [ -e /run/systemd/shutdown/scheduled ]; then echo IC_SCHEDULED=1; fi"
)


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
        """One order: ``host_reboot.sh check``."""
        return [("Check", self.run_script("host_reboot.sh", ["check"]))]

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
        host._reboot_alert_pending(now)
        if not required:
            self._sys("No restart pending.")
            return
        self._sys(
            f"A restart is pending since {host.reboot_required_since}"
            f" ({host.reboot_required_pkgs or 'updates'})."
        )
        if not host._reboot_by_panel():
            self._sys(
                "The panel does not restart this server by itself: Auto"
                " Security Updates is off, or it is not hardened."
            )
            return
        if not host._reboot_window_open(now):
            return
        if host._reboot_job_active():
            return
        holds_panel = boot_id == panel_boot_id()
        blockers = host._reboot_blockers(holds_panel=holds_panel)
        if blockers:
            self._sys("Not restarted in this pass: " + " ".join(blockers))
            return
        job_id = self.env["cloud.job"].sudo().enqueue(host.id, False, "host_reboot")
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

        :return: ``{'id', 'boot_id', 'shutdown_at', 'holds_panel'}``
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
        boot_id = (read.stdout or "").strip().splitlines()[0] if read.stdout else ""
        if read.exit_status != 0 or not boot_id:
            raise RuntimeError("Could not read the server's boot.")
        reasons = self._take_hold(boot_id)
        if reasons:
            self._verdict = ("skipped", " ".join(reasons))
            return
        guard = self.run_script("host_reboot.sh", ["guard"])
        reboot = self.run_script("host_reboot.sh", ["reboot"])
        await self._upload_scripts(transport)
        self._sys("Running: Ask the server")
        said = await transport.execute(guard, self.on_stdout, self.on_stderr)
        if said.exit_status == EXIT_VETO:
            veto = first_mark(parse_marks(said.stdout), "IC_VETO")
            self._verdict = ("skipped", f"The server said no: {veto}.")
            return
        if said.exit_status != 0:
            raise RuntimeError(f"Asking the server failed (exit {said.exit_status}).")
        # Before the order, so an attempt that dies right after it waits
        # for the server instead of giving the order again.
        self._write_row({"shutdown_at": fields.Datetime.now()})
        self._pass_no_return(REBOOT_LABEL)
        self._sys(f"Running: {REBOOT_LABEL}")
        done = await transport.execute(reboot, self.on_stdout, self.on_stderr)
        if done.exit_status != 0:
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
                    await self._dispatch_outcome(
                        {"Back": {"stdout": read.stdout, "exit_status": 0}},
                        transport,
                    )
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
        host.write({
            "reboot_boot_id": lines[0].strip() if lines else False,
            "reboot_checked_at": False,
        })
        self._sys("✓ Restarted. The next check reads what is still pending.")
        # The check runs at once: whether an update still waits, and the
        # alert of a pending restart closed with it.
        self.env["cloud.job"].sudo().enqueue(host.id, False, "host_reboot_check")

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
