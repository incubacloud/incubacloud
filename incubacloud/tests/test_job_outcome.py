"""A job whose success hook committed runs nothing again; its end waits for the commit.

``on_success`` commits on a cursor of its own before the job's
transaction stores ``done``. When that store then fails — a serialization
race, five seconds later queue_job runs the job again outside its
``max_retries`` — or the worker dies before the commit, the next attempt
used to run the whole job in a world where it was done: a restore looked
for the archive its first success had removed and its move was rolled
back (7-nov-2026). The hook now leaves ``cloud.job.outcome``; the next
attempt finds it in ``cloud.job.execute`` and ends there.

And the store of ``done`` keeps to the job's own rows: what it did to
other rows — the state probe of the instance, the mails and messages of
the end, the layers' hooks — waits for its commit, on cursors of their
own (``run_after_commit``).

Registry test mode: the durable cursors ride on the test's transaction
(``idempotency_harness`` says what that does not reproduce;
``test_job_outcome_real_cursors`` reproduces it).
"""
import contextlib
import logging
import re
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase
from odoo.tools.misc import Callbacks

from odoo.addons.queue_job.exception import JobError, RetryableJobError

from ..models import _concurrency, abstract_executor
from ..models.abstract_executor import CANCELLED_WHILE_RUNNING, AbstractSSHExecutor
from ..models.cloud_host import CloudHost
from ..models.transport import CommandResult, SSHTransport

#: What the probe writes on its job's own row.
_OWN = {"result": {"probe": "fingerprint"}}
#: A statement that writes rows, and its table.
_WRITE = re.compile(
    r'\s*(INSERT INTO|UPDATE|DELETE FROM)\s+"?(\w+)"?', re.IGNORECASE,
)
#: A statement that locks rows, and the table it reads them from.
_ROW_LOCK = re.compile(
    r'\bFROM\s+"?(\w+)"?.*\bFOR\s+(UPDATE|NO\s+KEY\s+UPDATE|SHARE|KEY\s+SHARE)\b',
    re.IGNORECASE | re.DOTALL,
)


class _OutcomeProbe(AbstractSSHExecutor):
    """One step; a success that writes its instance, its job and owes an unlink."""

    _job_type = None

    def get_commands(self):
        """One step, answered by the test's transport."""
        return [("Probe step", "true")]

    async def on_success(self, results):
        """Finish a removal on the hook's cursor, write the own row, owe the unlink."""
        inst = self.job.instance_id
        inst._finalize_removal(False, unlink=False)
        self._write_own_job(_OWN.copy())
        self._unlink_after_job_commit(inst, sudo=True)

    async def on_failure(self, results, errors):
        """Write the instance in error on the hook's cursor."""
        self.job.instance_id.write({"status": "error"})


class _RetryAfterFailureProbe(_OutcomeProbe):
    """A failure hook that hands the job back to queue_job to run again."""

    async def on_failure(self, results, errors):
        """Write the instance, then ask queue_job to run the job again."""
        await super().on_failure(results, errors)
        raise RetryableJobError("the host asked for another go", seconds=1)


class _OutcomeCase(TransactionCase):
    """A deployed instance and a dispatched job of a type that refreshes it."""

    def setUp(self):
        """Fixtures, in registry test mode."""
        super().setUp()
        self.registry.enter_test_mode(self.cr)
        self.addCleanup(self.registry.leave_test_mode)
        self.host = self.env["cloud.host"].create({
            "name": "outcome-host",
            "ip_address": "192.0.2.91",
            "user": "ubuntu",
            "wildcard_domain": "outcome.example.com",
        })
        project = self.env["cloud.project"].create({"name": "Outcome"})
        self.inst = self.env["cloud.instance"].create({
            "name": "outcome-inst",
            "project_id": project.id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self.inst._transition("deploying")
        self.inst._transition("deployed")
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "restart_instance")], limit=1,
        )
        self.qjob = self.env["queue.job"].sudo().create({
            "uuid": f"outcome-{self.inst.id}",
            "name": "probe",
            "state": "started",
            "method_name": "execute",
            "model_name": "cloud.job",
            "func_string": "execute()",
        })
        self.job = self.env["cloud.job"].sudo().create({
            "host_id": self.host.id,
            "instance_id": self.inst.id,
            "job_type_id": job_type.id,
            "name": "Probe",
            "queue_job_uuid": self.qjob.uuid,
            "queue_job_id": self.qjob.id,
        })
        self.env.flush_all()
        self.transport = MagicMock(spec=SSHTransport)
        self.transport.execute.return_value = CommandResult(stdout="", exit_status=0)
        self.transport.run.return_value = CommandResult(stdout="", exit_status=0)

    @contextlib.contextmanager
    def _runner(self, executor_cls=_OutcomeProbe):
        """Run ``execute`` with *executor_cls* and the test's transport.

        Yields the ``postcommit`` queue the attempt fills, a queue of its
        own: the test runs it (the commit) or drops it (a lost commit).
        """
        transport = self.transport

        @contextlib.asynccontextmanager
        async def connect(_host):
            yield transport

        Job = type(self.env["cloud.job"])
        cr = self.env.cr
        original = cr.postcommit
        cr.postcommit = armed = Callbacks()
        try:
            with patch.object(
                Job, "_get_executor", autospec=True, return_value=executor_cls,
            ), patch.object(
                CloudHost, "get_transport", autospec=True, side_effect=connect,
            ), patch.object(executor_cls, "_refresh_host_disk", autospec=True):
                yield armed
        finally:
            cr.postcommit = original

    def _outcomes(self):
        """The marks of the test's job, by SQL."""
        self.env.cr.execute(
            "SELECT job_id, queue_job_uuid, own_job_vals, owed"
            " FROM cloud_job_outcome WHERE job_id = %s",
            (self.job.id,),
        )
        return self.env.cr.fetchall()

    def _stored(self, record, column):
        """*column* of *record* as the database holds it."""
        self.env.flush_all()
        self.env.cr.execute(
            f'SELECT "{column}" FROM "{record._table}" WHERE id = %s', (record.id,),
        )
        row = self.env.cr.fetchone()
        return row and row[0]


class TestTheSuccessLeavesItsMark(_OutcomeCase):
    """Only ``on_success`` leaves the mark, with what a repetition needs."""

    def test_the_mark_holds_the_own_row_and_what_is_owed(self):
        """Job, queue uuid, the own-row values and the unlink owed."""
        with self._runner():
            self.job.execute()
        self.assertEqual(self._outcomes(), [(
            self.job.id, self.qjob.uuid, _OWN,
            [{"kind": "instance_unlink", "instance_id": self.inst.id}],
        )])

    def test_a_failure_leaves_no_mark(self):
        """A failed step: ``on_failure`` commits, and there is no mark."""
        self.transport.execute.return_value = CommandResult(stdout="", exit_status=1)
        # Not ``assertRaises``: it rolls back what the hook wrote.
        with self._runner():
            try:
                self.job.execute()
            except JobError:
                pass
            else:
                self.fail("the failed step did not fail the job")
        self.assertEqual(self._outcomes(), [])
        self.assertEqual(self._stored(self.inst, "status"), "error")

    def test_a_retry_after_a_failure_runs_again(self):
        """``on_failure`` then ``RetryableJobError``: no mark; the steps run again."""
        self.transport.execute.return_value = CommandResult(stdout="", exit_status=1)
        for _attempt in range(2):
            with self._runner(_RetryAfterFailureProbe), \
                    self.assertRaises(RetryableJobError):
                self.job.execute()
            self.env.clear()
        self.assertEqual(self._outcomes(), [])
        self.assertEqual(self.transport.execute.call_count, 2)

    def test_a_job_outside_the_queue_leaves_no_mark(self):
        """No queue job, no attempt to answer for: nothing is written."""
        self.job.write({"queue_job_uuid": False})
        self.env.flush_all()
        with self._runner():
            self.job.execute()
        self.assertEqual(self._outcomes(), [])


class TestARepetitionRunsNothing(_OutcomeCase):
    """The attempt after a lost commit ends at the mark."""

    def _first_attempt_loses_its_commit(self):
        """Run once, store done, and lose the commit, as the 7-nov restore did.

        What the runner's transaction held is gone: the job's own row is
        put back by hand (here the test's transaction cannot roll it
        back alone) and the unlink left for its commit is dropped.
        """
        with self._runner() as armed:
            self.job.execute()
            self.qjob.write({"state": "done"})
        armed.clear()
        self.env.cr.execute(
            "UPDATE cloud_job SET result = NULL WHERE id = %s", (self.job.id,),
        )
        self.env.clear()
        self.qjob.write({"state": "started"})
        self.env.flush_all()

    def test_it_runs_nothing_and_says_so(self):
        """No connection, no step; the log says why; ALREADY_DONE is returned."""
        self._first_attempt_loses_its_commit()
        self.transport.reset_mock()
        with self._runner():
            result = self.job.execute()
        self.assertEqual(result, abstract_executor.ALREADY_DONE)
        self.transport.execute.assert_not_called()
        self.assertIn(
            "outcome was saved",
            "\n".join(self.job.log_chunk_ids.mapped("content")),
        )

    def test_it_writes_the_own_row_again(self):
        """What ``_write_own_job`` wrote, lost with the commit, is back."""
        self._first_attempt_loses_its_commit()
        with self._runner():
            self.job.execute()
        self.env.invalidate_all()
        self.assertEqual(self.job.result, _OWN["result"])

    def test_it_arms_the_owed_unlink_for_its_commit(self):
        """The instance the first attempt finished removing goes after this commit."""
        self._first_attempt_loses_its_commit()
        self.assertTrue(self.inst.exists())
        with self._runner() as armed:
            self.job.execute()
            self.qjob.write({"state": "done"})
            self.env.flush_all()
            armed.run()
        self.env.invalidate_all()
        self.assertFalse(self.inst.exists())

    def test_the_mark_comes_before_a_stop(self):
        """A stop that came after the success does not undo it: the job ends done."""
        self._first_attempt_loses_its_commit()
        self.env.cr.execute(
            "UPDATE cloud_job SET state = 'cancelled' WHERE id = %s", (self.job.id,),
        )
        self.env.invalidate_all()
        with self._runner():
            result = self.job.execute()
            self.assertEqual(result, abstract_executor.ALREADY_DONE)
            # What the runner stores once ``execute`` returns.
            self.qjob.write({"state": "done", "result": result})
        self.env.invalidate_all()
        self.assertEqual(self.qjob.state, "done")
        self.assertEqual(self.job.state, "done")
        self.assertEqual(self.job.result, _OWN["result"])

    def test_a_stop_stored_after_the_mark_ends_done(self):
        """``done`` with the stop's result, and a mark: done, its own row back."""
        with self._runner() as armed:
            self.job.execute()
        armed.clear()
        self.env.cr.execute(
            "UPDATE cloud_job SET state = 'cancelled', result = NULL WHERE id = %s",
            (self.job.id,),
        )
        self.env.invalidate_all()
        with self._runner():
            self.qjob.write({"state": "done", "result": CANCELLED_WHILE_RUNNING})
        self.env.invalidate_all()
        self.assertEqual(self.qjob.state, "done")
        self.assertEqual(self.job.state, "done")
        self.assertEqual(self.job.result, _OWN["result"])


    def test_a_stop_after_the_mark_put_back_in_the_queue_ends_done(self):
        """The store of done lost its race, a stop came after the mark: done.

        queue_job puts the job back in the queue (``retry_postpone``, on a
        cursor of its own). Read as the stop alone, the job ended
        cancelled and its chain was cut.
        """
        with self._runner() as armed:
            self.job.execute()
        armed.clear()
        self.env.cr.execute(
            "UPDATE cloud_job SET state = 'cancelled', result = NULL WHERE id = %s",
            (self.job.id,),
        )
        self.env.cr.execute(
            "UPDATE queue_job SET graph_uuid = 'graph-outcome' WHERE id = %s",
            (self.qjob.id,),
        )
        self.env.invalidate_all()
        QueueJob = type(self.env["queue.job"])
        with self._runner(), patch.object(
            QueueJob, "_end_chain_after", autospec=True,
        ) as ended:
            # ``retry_postpone``: postponed, then pending.
            self.qjob.write({
                "state": "pending",
                "result": "could not serialize access due to concurrent update",
                "date_started": False,
                "eta": fields.Datetime.now(),
            })
        ended.assert_not_called()
        self.env.invalidate_all()
        self.assertEqual(self.qjob.state, "done")
        self.assertEqual(self.job.state, "done")
        self.assertEqual(self.job.result, _OWN["result"])


class TestTheEndWaitsForTheCommit(_OutcomeCase):
    """What ``done`` sets going outside the job's rows runs after the commit."""

    @contextlib.contextmanager
    def _store_done(self, qjob=None):
        """Store the job done; yield the ``postcommit`` it filled.

        The queue stays the cursor's until the block ends, so its
        callbacks find their data there when the test runs them.

        :param qjob: the ``queue.job`` to store, the test's by default
        """
        cr = self.env.cr
        original = cr.postcommit
        cr.postcommit = armed = Callbacks()
        try:
            (qjob or self.qjob).write({"state": "done"})
            self.env.flush_all()
            yield armed
        finally:
            cr.postcommit = original

    def test_announced_after_the_commit_only(self):
        """Mail, Telegram/webhook and the bus wait; a lost commit sends none."""
        Job = type(self.env["cloud.job"])
        with patch.object(Job, "_notify_by_email", autospec=True) as mailed, \
                patch.object(Job, "_notify_external", autospec=True) as pushed, \
                patch.object(Job, "_broadcast_job_update", autospec=True) as told:
            with self._store_done() as armed:
                for sent in (mailed, pushed, told):
                    sent.assert_not_called()
                armed.run()
            mailed.assert_called_once()
            pushed.assert_called_once()
            # The bus also hears of the state probe's job, created then.
            ours = [call for call in told.call_args_list if call.args[1] == self.job.id]
            self.assertEqual(len(ours), 1)
            self.assertEqual(ours[0].kwargs["state"], "done")

    def test_the_state_probe_waits_for_the_commit(self):
        """A restart's instance is probed after the commit, not in the store."""
        Instance = type(self.env["cloud.instance"])
        with patch.object(Instance, "_refresh_state_now", autospec=True) as probed:
            with self._store_done() as armed:
                probed.assert_not_called()
                armed.run()
            probed.assert_called_once()
            self.assertEqual(probed.call_args.args[0].ids, self.inst.ids)

    def _dispatched(self, code):
        """A started job of type *code* on the test's instance, and its queue job.

        :return: ``(cloud.job, queue.job)``
        """
        qjob = self.env["queue.job"].sudo().create({
            "uuid": f"outcome-{code}-{self.inst.id}",
            "name": code,
            "state": "started",
            "method_name": "execute",
            "model_name": "cloud.job",
            "func_string": "execute()",
        })
        job = self.env["cloud.job"].sudo().create({
            "host_id": self.host.id,
            "instance_id": self.inst.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", code)], limit=1,
            ).id,
            "name": code,
            "queue_job_uuid": qjob.uuid,
            "queue_job_id": qjob.id,
        })
        self.env.flush_all()
        return job, qjob

    def _rows_the_store_touches(self, qjob):
        """Store *qjob* done and run its pre-commit; return what it wrote or locked.

        :return: ``{(statement, table)}``: ``INSERT INTO``, ``UPDATE``,
            ``DELETE FROM``, and the row locks ``FOR UPDATE``, ``FOR NO
            KEY UPDATE``, ``FOR SHARE`` and ``FOR KEY SHARE``
        :rtype: set
        """
        # What the fixtures left for the commit is not the store's.
        self.env.cr.flush()
        touched = set()
        Cursor = type(self.env.cr)
        execute = Cursor.execute

        def spy(cr, query, params=None, log_exceptions=True):
            """Note the table of each write and of each row lock."""
            code = str(getattr(query, "code", query))
            write = _WRITE.match(code)
            if write:
                touched.add((write.group(1).upper(), write.group(2)))
            lock = _ROW_LOCK.search(code)
            if lock:
                touched.add((
                    "FOR " + " ".join(lock.group(2).upper().split()),
                    lock.group(1),
                ))
            return execute(cr, query, params, log_exceptions)

        with patch.object(Cursor, "execute", autospec=True, side_effect=spy), \
                self._store_done(qjob):
            # The pre-commit hooks run in the store's transaction too.
            self.env.cr.flush()
        return touched

    def test_the_store_writes_only_the_jobs_rows(self):
        """The store updates ``cloud_job`` and ``queue_job``, and nothing else.

        An insert is another row — a probe's job with its key on the
        instance, a mail —, and a row another transaction may have
        changed under the job's snapshot; a row lock on another table
        meets the same refusal. Its pre-commit hooks count, and the job
        types whose ends act on other rows: a restart probes its
        instance, a deploy and a backup end with layers' hooks.
        """
        jobs = {"restart_instance": self.qjob}
        for code in ("deploy_instance", "backup_create", "rebuild_instance"):
            jobs[code] = self._dispatched(code)[1]
        for code, qjob in jobs.items():
            with self.subTest(code=code):
                touched = self._rows_the_store_touches(qjob)
                self.assertIn(("UPDATE", "queue_job"), touched)
                self.assertLessEqual(touched, {
                    ("UPDATE", "cloud_job"), ("UPDATE", "queue_job"),
                    ("FOR UPDATE", "queue_job"), ("FOR NO KEY UPDATE", "queue_job"),
                    ("FOR UPDATE", "cloud_job"), ("FOR NO KEY UPDATE", "cloud_job"),
                })


class TestRunAfterCommit(TransactionCase):
    """A callback of the commit that raises is logged, and the rest still run."""

    def test_a_failing_callback_is_logged_and_the_next_runs(self):
        """No exception reaches the commit; the log names what failed."""
        ran = []

        def boom():
            """Fail."""
            raise RuntimeError("boom")

        armed = Callbacks()
        cr = MagicMock(postcommit=armed)
        _concurrency.run_after_commit(cr, "the failing one", boom)
        _concurrency.run_after_commit(cr, "the next one", lambda: ran.append(1))
        with self.assertLogs(_concurrency.__name__, logging.ERROR) as logs:
            armed.run()
        self.assertEqual(ran, [1])
        self.assertIn("the failing one", "\n".join(logs.output))
