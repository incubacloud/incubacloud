"""The steps a move between hosts adds around its cut.

``move_quiet_check`` decides, right before the source would stop, whether
the move goes on or is postponed; ``move_fingerprint`` counts what the
source holds once it stopped, for the restore to check against; and
``move_freeze_source`` stops the old copy after the cutover and keeps it
for a while instead of removing it. See ``cloud.instance.move_to_host``.
"""
import json
from datetime import timedelta

from odoo import fields

from .abstract_executor import (
    CANCELLED_WHILE_RUNNING,
    AbstractSSHExecutor,
    JobRefused,
)

#: Label of the count of what runs inside the instance.
QUIET_LABEL = "Count what runs inside the instance"
#: Label of the source's count.
FINGERPRINT_LABEL = "Count rows and files"
#: Label of the old copy's stop.
FREEZE_LABEL = "Stop and keep the old copy"


def parse_fingerprint(stdout):
    """Return the fingerprint ``move_steps.sh fingerprint`` printed, or None.

    :param str stdout: the step's output; the JSON is its last line
    :return: ``{'tables': {name: rows}, 'files': n}``, or None when the
        output holds none
    :rtype: dict | None
    """
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            return None
        tables = data.get("tables")
        files = data.get("files")
        if not isinstance(tables, dict) or not isinstance(files, int):
            return None
        if not all(isinstance(n, int) for n in tables.values()):
            return None
        return {"tables": tables, "files": files}
    return None


class _MoveSourceStep(AbstractSSHExecutor):
    """What the steps a move runs on its source share.

    Pinned to the host they were asked for: sent where the instance lives
    once the move ended, they would act on the live copy.
    """

    _pinned_to_host = True

    def _inst(self):
        """Return the instance the move is about."""
        return self.job.instance_id

    def _refusal(self, inst):
        """Say why this step may not run on its host now, or None.

        Before the cut, a step of the source belongs to the move under
        way only while the instance still lives on that host, and while
        the move is not being rolled back.

        :param inst: the ``cloud.instance``
        :rtype: str | None
        """
        host = self.job.host_id
        if inst.move_rollback_in_progress:
            return "Refused: the move this step belongs to is being rolled back."
        if inst.move_origin_host_id != host or inst.host_id != host:
            return f"Refused: no move of this instance from {host.name} is in progress."
        return None

    def pre_run_checks(self):
        """Refuse a step that no longer belongs to a move under way.

        :raises JobRefused: see ``_refusal``
        """
        inst = self._inst()
        refusal = self._refusal(inst) if inst else None
        if refusal:
            raise JobRefused(refusal)
        super().pre_run_checks()

    def _db_args(self, inst):
        """Return ``[dir, pg_user, dbname]`` for ``move_steps.sh``."""
        return [
            self._inst_dir(inst),
            inst.postgres_username or "odoo",
            inst.postgres_dbname or "prod",
        ]


class MoveQuietCheckExecutor(_MoveSourceStep):
    """Postpone a move whose instance is busy, before anything stops.

    Runs on the source right after the destination was built and its
    Odoo stopped, and before the steps that change the source. Busy is a
    job of this panel running on the instance besides the move's own, or
    what the layer above counts inside the instance's database
    (``cloud.instance._move_quiet_sql``), or, for the triggers that
    depend on it, recent requests (``_move_requests_check``). A busy
    instance ends this step ``done`` and hands the move to
    ``cloud.instance._postpone_move``, with what was found (``postponed``
    and ``detail``), which travels to the layers' hook in the rollback's
    outcome: the rest of the chain is cancelled and the destination taken
    down, without a failure or an alert. The source never stopped. A
    count inside the instance that could not be read postpones the move
    the same way (``unknown``), rather than failing it: nothing stopped,
    and a failed move is rolled back with a critical alert and paused.

    The step writes nothing on its own job: its hooks run on a cursor
    that commits before the job's transaction stores it ``done``, and a
    write of this job's row there makes that store fail (see
    ``AbstractExecutor._write_own_job``).
    """

    _job_type = "move_quiet_check"

    def get_commands(self):
        """Count inside the instance only when a layer gives the SQL."""
        inst = self._inst()
        sql = inst._move_quiet_sql() if inst else ""
        if not sql:
            return []
        return [(
            QUIET_LABEL,
            self.run_script("move_steps.sh", ["quiet", *self._db_args(inst), sql]),
        )]

    def parse_results(self, results):
        """Never fail on the count: one that could not be read postpones.

        The source has not stopped yet, so a count that failed for a
        moment is no reason to roll the move back (``_count_problem``).
        """
        return []

    @staticmethod
    def _count_problem(results):
        """Say why the count inside the instance could not be read, or None.

        :param dict results: what the commands printed
        :rtype: str | None
        """
        data = results.get(QUIET_LABEL)
        if data is None:
            return None
        if data.get("exit_status", 1) != 0:
            return f"the count exited with status {data.get('exit_status')}"
        if not MoveQuietCheckExecutor._inside_count(results).isdigit():
            return "the count did not answer a number"
        return None

    @staticmethod
    def _inside_count(results):
        """Return what the count inside the instance printed, stripped."""
        stdout = (results.get(QUIET_LABEL) or {}).get("stdout") or ""
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        return lines[-1] if lines else ""

    def _panel_jobs_running(self, inst):
        """Count this panel's jobs running on the instance, the move's aside.

        Background jobs (``_get_hidden_job_types``) are the platform's
        own and do not count.

        :rtype: int
        """
        Job = self.env["cloud.job"].sudo()
        chain = inst._get_move_chain_jobs()
        return Job.search_count([
            ("instance_id", "=", inst.id),
            ("state", "=", "started"),
            ("id", "not in", (chain | self.job).ids),
            ("job_type_id.code", "not in", Job._get_hidden_job_types()),
        ])

    def _verdict(self, results):
        """Return ``(reason, detail)`` when the move must wait, or None.

        :param dict results: what the commands printed
        :rtype: tuple | None
        """
        inst = self._inst()
        problem = self._count_problem(results)
        if problem:
            return "unknown", problem
        inside = self._inside_count(results)
        busy = self._panel_jobs_running(inst) + (
            int(inside) if inside.isdigit() else 0
        )
        if busy:
            return "busy", f"{busy} jobs running"
        options = (self.job.payload or {}).get("move_options") or {}
        return inst._move_requests_check(options.get("trigger") or "manual")

    async def on_success(self, results):
        """Let the move go on, or postpone it."""
        inst = self._inst()
        verdict = self._verdict(results)
        if not verdict:
            self._sys("✓ Nothing is running on the instance: the move goes on.")
            return
        reason, detail = verdict
        result = {"postponed": reason, "detail": detail}
        self._sys(f"Move postponed: {reason} ({detail}). The instance keeps running here.")
        inst._postpone_move(self.job, result)


class MoveFingerprintExecutor(_MoveSourceStep):
    """Count what the source holds once its Odoo stopped.

    The rows of ``cloud.instance._move_fingerprint_tables`` and the files
    of the filestore, stored in ``job.result`` for the restore on the
    destination to compare with (``RestoreInstanceExecutor``, payload
    ``verify_job``).
    """

    _job_type = "move_fingerprint"

    def get_commands(self):
        """One count, with the tables the instance's layers name."""
        inst = self._inst()
        return [(
            FINGERPRINT_LABEL,
            self.run_script("move_steps.sh", [
                "fingerprint", *self._db_args(inst),
                *inst._move_fingerprint_tables(),
            ]),
        )]

    def parse_results(self, results):
        """Fail when the count failed or printed nothing to compare with."""
        data = results.get(FINGERPRINT_LABEL)
        if data is None:
            return []
        if data.get("exit_status", 1) != 0:
            return [f"'{FINGERPRINT_LABEL}' exited with status {data.get('exit_status')}"]
        if parse_fingerprint(data.get("stdout")) is None:
            return [f"'{FINGERPRINT_LABEL}' printed no fingerprint"]
        return []

    async def on_success(self, results):
        """Keep the fingerprint on the job, for the restore to read."""
        data = results.get(FINGERPRINT_LABEL) or {}
        fingerprint = parse_fingerprint(data.get("stdout")) or {}
        # On the job's own transaction, which stores it done: the
        # restore reads it once that transaction committed.
        self._write_own_job({"result": fingerprint})
        self._sys(
            f"✓ Fingerprint taken: {len(fingerprint.get('tables') or {})} "
            f"tables, {fingerprint.get('files', 0)} files."
        )


class MoveFreezeSourceExecutor(AbstractSSHExecutor):
    """Stop the old copy after a cutover, and keep it.

    The last step of a move, on the old host, in place of removing the
    copy there: every container stopped, the directory, volumes and
    database kept, for ``cloud.settings.move_frozen_source_hours``. It is
    the operator's copy for a restore by hand; ``move_cleanup_source``
    removes it later (``cloud.instance.cron_clean_frozen_move_sources``,
    the instance's delete or a new move). A copy kept costs disk until
    then, which the host's free-disk reading counts, and no CPU, memory or
    Docker network: the host no longer counts the instance, and the
    freeze removes the networks of the stack's project, each one of the
    host's address pools (#103).
    """

    _job_type = "move_freeze_source"

    # The old host, named on purpose: a retry sent where the instance
    # lives now would stop the live copy.
    _pinned_to_host = True

    def _inst(self):
        """Return the instance that moved."""
        return self.job.instance_id

    def pre_run_checks(self):
        """Never stop the copy the instance lives in.

        :raises JobRefused: when the job's host is the instance's, or a
            move of it is under way again
        """
        inst = self._inst()
        host = self.job.host_id
        if inst and host == inst.host_id:
            raise JobRefused(
                f"Refused: the instance lives on {host.name}. This step only "
                "stops the copy a finished move left on its old host."
            )
        if inst and inst.move_origin_host_id:
            raise JobRefused(
                "Refused: another move of this instance is under way."
            )
        super().pre_run_checks()

    def get_commands(self):
        """Stop the whole stack, restart policies off first."""
        inst = self._inst()
        return [(
            FREEZE_LABEL,
            self.run_script("move_steps.sh", ["freeze", self._inst_dir(inst)]),
        )]

    async def on_success(self, results):
        """Record the copy as frozen until its time is up."""
        until = self._record_frozen_copy()
        self._sys(
            f"✓ Old copy on '{self.job.host_id.name}' stopped and kept until "
            f"{fields.Datetime.to_string(until)} UTC."
        )
        # The host no longer serves the instance: its label map must
        # forget it.
        self.job.host_id.refresh_observability_labels(reason="move freeze")

    async def on_failure(self, results, errors):
        """Record the copy all the same, and tell the operator.

        The move succeeded and the instance lives elsewhere, but the old
        copy may still run, its ``db`` and ``backup`` with
        ``restart=always``. Recorded as frozen, the hourly clean-up
        removes it when its time is up, as for a copy that stopped; the
        alert says it may be running meanwhile.
        """
        for err in errors:
            self._sys(f"✗ Freezing the old copy: {err}")
        inst = self._inst()
        host = self.job.host_id
        until = self._record_frozen_copy()
        self._sys(
            f"The old copy on '{host.name}' is recorded all the same, and is "
            f"removed after {fields.Datetime.to_string(until)} UTC."
        )
        self.env["cloud.alert"].sudo().raise_alert(
            "move_freeze_failed",
            self.env._(
                "The old copy of %(name)s on %(host)s could not be stopped "
                "after its move and may still be running. It is removed on "
                "%(until)s UTC, or now with «Clean frozen copy now».",
                name=inst.name, host=host.name,
                until=fields.Datetime.to_string(until),
            ),
            level="warning",
            host=host,
            instance=inst,
        )

    async def on_cancelled_running(self, results, exc):
        """A freeze stopped by hand is closed as a failed one, alert included.

        Before its step or during it, the old copy may still run beside
        the moved instance, and the alert is the one word that says so:
        it is not a failure the person who stopped the job is told of,
        but what the stop left. Raised although a cancelled job raises
        none (``ic_job_cancelled``).

        :param results: what the step answered, or None before it
        :param JobCancelled exc: the stop
        """
        self.job = self.job.with_context(ic_job_cancelled=False)
        self.env = self.job.env
        await self.on_failure(results or {}, [CANCELLED_WHILE_RUNNING])

    def _record_frozen_copy(self):
        """Record the old copy on this job's host as frozen, until its time.

        A copy an earlier move froze on another host, never removed, is
        forgotten with a note: nothing would name it once this one takes
        its place.

        :return: when the copy's time is up, naive UTC
        :rtype: datetime
        """
        inst = self._inst()
        host = self.job.host_id
        hours = self.env["cloud.settings"].sudo()._get().move_frozen_source_hours
        until = fields.Datetime.now() + timedelta(hours=max(hours or 0, 0))
        older = inst.move_frozen_source_host_id
        if older and older != host:
            # A copy an earlier move froze that was never removed: nothing
            # would name it once this one takes its place.
            inst._forget_frozen_copy(f"a later move froze another copy on {host.name}")
        # With ``sudo``: the fields are the platform's
        # (``cloud.instance._check_guarded_vals``).
        inst.sudo().write({
            "move_frozen_source_host_id": host.id,
            "move_frozen_until": until,
        })
        return until
