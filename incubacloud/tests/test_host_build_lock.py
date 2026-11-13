"""Per-host serialisation of image builds (``HostBuildLockMixin``).

Jobs serialise per *instance*, so two rebuilds of different instances on
one host used to run at the same time — and both run ``docker compose
build`` against the same daemon, with an apt cache that BuildKit shares
across every build on the machine. Production, 2026-08-20: two rebuilds
enqueued in the same second on the same host, one built for 456 s and
the other died 75 s in on ``Could not get lock
/var/cache/apt/archives/lock``. That failure latched the release rollout
for the whole fleet.

Contention needs a SECOND real DB connection: ``pg_try_advisory_xact_lock``
is re-entrant within a session, so two calls on one cursor both succeed
and a single connection cannot simulate a busy host.
"""
import asyncio
from datetime import timedelta
from unittest.mock import MagicMock, patch

import odoo
from odoo import fields
from odoo.tests.common import TransactionCase, new_test_user

from odoo.addons.queue_job.exception import RetryableJobError

from ..models.delete_instance_executor import DeleteInstanceExecutor
from ..models.host_build_lock import (
    _HOST_BUILD_LOCK_NS,
    BUILD_LOCK_WAIT_MARKER,
    BUILD_LOCK_YIELD_MAX_S,
    HostBuildLockMixin,
)
from ..models.rebuild_instance_executor import RebuildInstanceExecutor
from ..models.transport import SSHTransport


class _HostLockBase(TransactionCase):
    """A host, an instance on it, and a way to hold its lock from outside."""

    def setUp(self):
        super().setUp()
        # A deferral reads the host's jobs and writes a line in the job's
        # log on cursors of its own: in test mode they ride on this
        # transaction; outside it they would be real connections that see
        # nothing of the test.
        self.registry.enter_test_mode(self.cr)
        self.addCleanup(self.registry.leave_test_mode)
        self.host = self.env['cloud.host'].create({
            'name': 'Build Lock Host',
            'ip_address': '10.0.0.21',
            'user': 'ubuntu',
            'wildcard_domain': 'buildlock.example.com',
        })
        project = self.env['cloud.project'].create({
            'name': 'build-lock-project',
        })
        self.instance = self.env['cloud.instance'].create({
            'name': 'build-lock-instance',
            'project_id': project.id,
            'environment': 'staging',
            'host_id': self.host.id,
        })
        self._seq = 0

    def _executor(self):
        """Return a rebuild executor bound to the fixture instance."""
        JobType = self.env['cloud.job.type']
        job_type = JobType.search(
            [('code', '=', 'rebuild_instance')], limit=1,
        ) or JobType.create({
            'name': 'rebuild_instance',
            'code': 'rebuild_instance',
            'apply_to': 'instance',
        })
        job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'instance_id': self.instance.id,
            'job_type_id': job_type.id,
            'name': 'Rebuild Instance',
        })
        return RebuildInstanceExecutor(job, self.host)

    def _queued_executor(self, priority=10, age=0):
        """A rebuild executor whose job a runner started, as queue_job does.

        :param int priority: its queue job's priority
        :param int age: seconds since its queue job was created
        """
        executor = self._executor()
        self._seq += 1
        qjob = self.env['queue.job'].sudo().create({
            'uuid': f'build-lock-run-{self._seq}',
            'name': 'rebuild',
            'state': 'started',
            'priority': priority,
            'date_created': fields.Datetime.now() - timedelta(seconds=age),
            'method_name': 'execute',
            'model_name': 'cloud.job',
            'func_string': 'execute()',
        })
        executor.job.sudo().write({
            'queue_job_uuid': qjob.uuid, 'queue_job_id': qjob.id,
        })
        self.env.flush_all()
        return executor

    def _other(self, state='pending', result=None, priority=10,
               code='rebuild_instance', instance=None):
        """A job of the host, with the queue job a runner gave it.

        :param str state: its state and its queue job's
        :param str result: what its queue job says, e.g. a deferral
        :param int priority: its queue job's priority
        :param str code: its type
        :param instance: its instance; by default a new one of the project
        :return: the ``cloud.job``
        """
        self._seq += 1
        instance = instance or self.env['cloud.instance'].create({
            'name': f'build-lock-other-{self._seq}',
            'project_id': self.instance.project_id.id,
            'environment': 'staging',
            'host_id': self.host.id,
        })
        qjob = self.env['queue.job'].sudo().create({
            'uuid': f'build-lock-other-{self._seq}',
            'name': code,
            'state': state,
            'result': result,
            'priority': priority,
            'eta': (
                fields.Datetime.now() + timedelta(seconds=30)
                if state == 'pending' else False
            ),
            'method_name': 'execute',
            'model_name': 'cloud.job',
            'func_string': 'execute()',
        })
        JobType = self.env['cloud.job.type']
        job_type = JobType.search([('code', '=', code)], limit=1)
        self.assertTrue(job_type, code)
        job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'instance_id': instance.id,
            'job_type_id': job_type.id,
            'name': code,
            'state': state,
            'queue_job_uuid': qjob.uuid,
        })
        self.env.flush_all()
        return job

    def _deferred(self, holder=None):
        """What queue_job keeps of a deferral for the lock, naming *holder*."""
        if holder:
            return f"{BUILD_LOCK_WAIT_MARKER} (job #{holder.id}); deferring."
        return f"{BUILD_LOCK_WAIT_MARKER}; deferring."

    def _deferral(self, executor):
        """Run *executor*'s pre-run checks and return the deferral raised.

        Not ``assertRaises``: it runs the block in a savepoint it rolls
        back, which would undo the line a test cursor wrote in the log.
        """
        try:
            executor.pre_run_checks()
        except RetryableJobError as exc:
            return exc
        self.fail("the job was not deferred")

    def _waiting_lines(self, job):
        """The lines of *job*'s log that say what it waits for to build."""
        return self.env['cloud.job.log.chunk'].sudo().search([
            ('job_id', '=', job.id),
            ('source', '=', 'system'),
            ('content', '=like', 'Waiting for %'),
        ], order='id').mapped('content')

    def _hold_lock(self, host_id):
        """Take the build lock for *host_id* on an independent connection.

        Returns the open cursor; the caller must roll it back and close
        it to release the transactional advisory lock.
        """
        cr = odoo.sql_db.db_connect(self.env.cr.dbname).cursor()
        cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)",
            (_HOST_BUILD_LOCK_NS, host_id),
        )
        self.assertTrue(
            cr.fetchone()[0], "fixture connection must acquire the lock",
        )
        return cr


class TestHostBuildLock(_HostLockBase):
    """A build defers when another build already holds its host."""

    def test_rebuild_defers_when_the_host_is_building(self):
        executor = self._executor()
        cr = self._hold_lock(self.host.id)
        try:
            with self.assertRaises(RetryableJobError):
                executor.pre_run_checks()
        finally:
            cr.rollback()
            cr.close()

    def test_rebuild_proceeds_when_the_host_is_free(self):
        executor = self._executor()
        executor.pre_run_checks()  # must not raise

    def test_no_contention_across_hosts(self):
        """A build on another host must not defer this one."""
        executor = self._executor()
        cr = self._hold_lock(self.host.id + 1_000_000)
        try:
            executor.pre_run_checks()  # must not raise
        finally:
            cr.rollback()
            cr.close()

    def test_every_rebuild_variant_inherits_the_lock(self):
        """The lock must sit on the class that issues the build.

        Every rebuild, a layer's included, subclasses
        ``RebuildInstanceExecutor``; core's deploy takes no lock. Asserting it here is what makes
        "one build per host" true for all of them rather than for the one
        that happened to be wired.
        """
        self.assertTrue(
            issubclass(RebuildInstanceExecutor, HostBuildLockMixin),
        )

    def test_the_mixin_chains_to_the_rest_of_the_preflight(self):
        """Taking the lock must not swallow the other pre-run checks.

        A mixin that forgot ``super()`` would silently drop whatever the
        parent verifies before connecting, and nothing would say so.
        """
        import inspect

        source = inspect.getsource(HostBuildLockMixin.pre_run_checks)
        self.assertIn('super().pre_run_checks()', source)

    def test_the_lock_is_taken_before_any_ssh_work(self):
        """The loser must pay nothing remote.

        ``pre_run_checks`` runs before the connection is opened, so a
        deferred build costs a rescheduled job and no host round-trip.
        The guard lives there and nowhere else.
        """
        self.assertIn('pre_run_checks', HostBuildLockMixin.__dict__)
        self.assertNotIn('before_execute', HostBuildLockMixin.__dict__)


class TestTeardownsShareTheHostLock(_HostLockBase):
    """Instance teardowns take the same lock the builds do.

    Production, 2026-09-19: "Recycle all" on a host enqueued one
    ``delete_instance`` per instance, ``enqueue`` only serialises per
    instance, and both ran ``docker compose`` in the same millisecond.
    One of them saw ``compose config`` die in under 110 ms and reported
    the instance's compose as missing its backup service — a diagnosis
    the same command contradicted 11 minutes later.
    """

    def _delete_executor(self, cls=DeleteInstanceExecutor, code=None,
                         host=None):
        """Return a teardown executor bound to the fixture instance."""
        code = code or cls._job_type
        JobType = self.env['cloud.job.type']
        job_type = JobType.search([('code', '=', code)], limit=1) \
            or JobType.create({
                'name': code, 'code': code, 'apply_to': 'instance',
            })
        job = self.env['cloud.job'].create({
            'host_id': (host or self.host).id,
            'instance_id': self.instance.id,
            'job_type_id': job_type.id,
            'name': code,
        })
        return cls(job, host or self.host)

    def test_a_delete_defers_when_the_host_is_busy(self):
        executor = self._delete_executor()
        cr = self._hold_lock(self.host.id)
        try:
            with self.assertRaises(RetryableJobError):
                executor.pre_run_checks()
        finally:
            cr.rollback()
            cr.close()

    def test_a_delete_proceeds_when_the_host_is_free(self):
        self._delete_executor().pre_run_checks()  # must not raise

    def test_two_deletes_on_one_host_serialise(self):
        """The 2026-09-19 case: a sibling's teardown on the same host,
        of a *different* instance, must make this one wait. ``enqueue``
        would have let both through — it only serialises per instance.

        The sibling is simulated by the second connection: an advisory
        lock is re-entrant within a session, so two executors on the
        test cursor could never contend.
        """
        sibling = self.env['cloud.instance'].create({
            'name': 'build-lock-sibling',
            'project_id': self.instance.project_id.id,
            'environment': 'staging',
            'host_id': self.host.id,
        })
        self.instance = sibling
        second = self._delete_executor()
        cr = self._hold_lock(self.host.id)  # the first delete, running
        try:
            with self.assertRaises(RetryableJobError):
                second.pre_run_checks()
        finally:
            cr.rollback()
            cr.close()

    def test_a_delete_and_a_build_share_the_namespace(self):
        """Two families each holding their own lock would still collide
        on the daemon. ``_hold_lock`` takes the *build* namespace, and
        every deferral above is against it; this pins the reason."""
        self.assertTrue(
            issubclass(DeleteInstanceExecutor, HostBuildLockMixin),
        )
        self.assertTrue(
            issubclass(RebuildInstanceExecutor, HostBuildLockMixin),
        )

    def test_the_lock_follows_the_job_host_not_the_instance_host(self):
        """The move cleanups tear down a copy on a host the instance
        does not live on. Keying on the instance's host would lock the
        machine they never touch and leave the one they do touch open."""
        elsewhere = self.env['cloud.host'].create({
            'name': 'Build Lock Elsewhere',
            'ip_address': '10.0.0.22',
            'user': 'ubuntu',
            'wildcard_domain': 'buildlock2.example.com',
        })
        executor = self._delete_executor(host=elsewhere)
        # Job's host busy: that is the one that matters. Checked first,
        # because a successful ``pre_run_checks`` leaves the test cursor
        # holding the lock for the rest of the transaction.
        cr = self._hold_lock(elsewhere.id)
        try:
            with self.assertRaises(RetryableJobError):
                executor.pre_run_checks()
        finally:
            cr.rollback()
            cr.close()
        # Instance's host busy: irrelevant to a job running elsewhere.
        cr = self._hold_lock(self.host.id)
        try:
            executor.pre_run_checks()  # must not raise
        finally:
            cr.rollback()
            cr.close()

    def test_the_rollback_cleanup_does_not_take_the_lock(self):
        """It can wait ten minutes for the move chain before touching
        the host; holding the lock through that wait would park every
        build on the target behind it."""
        from ..models.move_rollback_cleanup_executor import (
            MoveRollbackCleanupExecutor,
        )

        # It runs on the target of a move being rolled back, and only there.
        target = self.env['cloud.host'].create({
            'name': 'Build Lock Target',
            'ip_address': '10.0.0.22',
            'user': 'ubuntu',
            'wildcard_domain': 'buildlock-target.example.com',
        })
        self.instance.write({
            'move_origin_host_id': self.host.id,
            'move_target_host_id': target.id,
            'move_chain_job_ids': '1',
            'move_rollback_in_progress': True,
        })
        executor = self._delete_executor(MoveRollbackCleanupExecutor, host=target)
        executor.job.payload = {'move_rollback': True, 'move_chain_job_ids': '1'}
        cr = self._hold_lock(target.id)
        try:
            executor.pre_run_checks()  # must not raise
        finally:
            cr.rollback()
            cr.close()

    def test_the_source_cleanup_keeps_the_lock(self):
        """It runs the real teardown on the source host and waits for
        nothing, so it contends like any other teardown."""
        from ..models.move_cutover_executor import MoveCleanupSourceExecutor

        # The instance moved away: the old copy is the one it removes.
        self.instance.host_id = self.env['cloud.host'].create({
            'name': 'Build Lock New Host',
            'ip_address': '10.0.0.23',
            'user': 'ubuntu',
            'wildcard_domain': 'buildlock-new.example.com',
        })
        # It left its copy frozen there, the one this removes.
        self.instance.move_frozen_source_host_id = self.host
        executor = self._delete_executor(MoveCleanupSourceExecutor)
        cr = self._hold_lock(self.host.id)
        try:
            with self.assertRaises(RetryableJobError):
                executor.pre_run_checks()
        finally:
            cr.rollback()
            cr.close()


class TestTheLockOutlivesAPark(_HostLockBase):
    """A rebuild parked between two attempts still holds its host (``HostBuildLockMixin``).

    The lock is transactional, and a parked job's transaction is gone: the
    checkpoint keeps the host instead, until the job ends. Registry test
    mode (the base's), so the fresh cursor that reads it sees the test's
    rows; the contention of a lock needs a second real connection all the
    same.
    """

    def setUp(self):
        """A rebuild of the fixture instance, run by queue_job."""
        super().setUp()
        self.parked = self._executor()
        self.qjob = self.env['queue.job'].sudo().create({
            'uuid': 'build-lock-parked',
            'name': 'parked rebuild',
            'state': 'started',
            'method_name': 'execute',
            'model_name': 'cloud.job',
            'func_string': 'execute()',
        })
        self.parked.job.sudo().write({
            'queue_job_uuid': self.qjob.uuid,
            'queue_job_id': self.qjob.id,
        })
        self.env.flush_all()

    def _park(self, take_lock=True):
        """The rebuild takes its lock, runs, and parks.

        :param bool take_lock: take it in this session, which then keeps
            it; without, the lock is only said held, as by a transaction
            that has ended since
        """
        if take_lock:
            self.parked.pre_run_checks()
        else:
            self.parked._held_build_lock_host_id = self.host.id
        self.parked._checkpoint = True
        self.parked._checkpoint_data = {"plan": [], "results": {}}
        self.parked._step_results = {}
        self.parked._write_checkpoint({'queue_job_uuid': self.qjob.uuid})
        self.parked._park(0, "parked for the test")
        return self.env['cloud.job.checkpoint'].sudo().search(
            [('job_id', '=', self.parked.job.id)],
        )

    def _other_build(self):
        """A rebuild of another instance of the same host."""
        self.instance = self.env['cloud.instance'].create({
            'name': 'build-lock-other',
            'project_id': self.instance.project_id.id,
            'environment': 'staging',
            'host_id': self.host.id,
        })
        return self._executor()

    def test_a_build_defers_while_another_is_parked(self):
        """The lock is free (this session's own), the checkpoint is not."""
        row = self._park()
        self.assertEqual(row.build_lock_host_id, self.host)
        other = self._other_build()
        with self.assertRaises(RetryableJobError):
            other.pre_run_checks()
        row.unlink()
        other.pre_run_checks()  # the parked job ended: must not raise

    def test_a_closed_row_holds_the_host_only_while_its_step_may_run(self):
        """Kept by its closed job: no hold, unless a step apart was started."""
        row = self._park()
        row.write({'closed': True, 'parked': False, 'detached_started': False})
        self.env.flush_all()
        other = self._other_build()
        other.pre_run_checks()  # a row kept for what is left: must not raise
        row.write({'detached_started': True})
        self.env.flush_all()
        with self.assertRaises(RetryableJobError):
            other.pre_run_checks()

    def test_the_deferral_names_the_job_that_holds_the_host(self):
        """The log of the build that waits says which job it waits for."""
        self._park()
        other = self._other_build()
        with self.assertRaises(RetryableJobError) as caught:
            other.pre_run_checks()
        self.assertIn(f"(job #{self.parked.job.id})", str(caught.exception))

    def test_a_parked_build_waits_for_the_one_about_to_defer(self):
        """It comes back while another build holds the lock for an instant."""
        row = self._park(take_lock=False)
        cr = self._hold_lock(self.host.id)
        try:
            with patch.object(
                RebuildInstanceExecutor, 'run', autospec=True,
            ) as run, self.assertRaises(RetryableJobError) as caught:
                self.parked.job.execute()
            run.assert_not_called()
        finally:
            cr.rollback()
            cr.close()
        self.assertEqual(caught.exception.seconds, 5)
        self.assertTrue(caught.exception.ignore_retry)
        row.invalidate_recordset()
        self.assertTrue(row.parked)

    def test_the_row_holds_the_host_from_its_opening(self):
        """A worker that dies before any park still left the hold."""
        self.parked.pre_run_checks()

        async def _no_upload(_self, _transport):
            """The scripts stay here."""

        with patch.object(RebuildInstanceExecutor, '_upload_scripts', _no_upload):
            asyncio.run(self.parked._open_checkpoint(
                MagicMock(spec=SSHTransport), [("Build", "true", {"detached": True})],
            ))
        row = self.env['cloud.job.checkpoint'].sudo().search(
            [('job_id', '=', self.parked.job.id)],
        )
        self.assertEqual(row.build_lock_host_id, self.host)
        self.assertFalse(row.parked)
        with self.assertRaises(RetryableJobError):
            self._other_build().pre_run_checks()

    def test_waiting_for_the_lock_marks_the_row_parked(self):
        """A row an attempt took up and then lost, parked again while it waits."""
        row = self._park(take_lock=False)
        row.write({'parked': False})
        self.env.flush_all()
        cr = self._hold_lock(self.host.id)
        # Not ``assertRaises``: it rolls back what the attempt wrote.
        raised = None
        try:
            with patch.object(RebuildInstanceExecutor, 'run', autospec=True):
                self.parked.job.execute()
        except RetryableJobError as exc:
            raised = exc
        finally:
            cr.rollback()
            cr.close()
        self.assertIsInstance(raised, RetryableJobError)
        row.invalidate_recordset()
        self.assertTrue(row.parked)

    def test_a_parked_build_takes_its_lock_back(self):
        """Free: the job goes on, with the lock and the checkpoint's hold."""
        self._park(take_lock=False)
        with patch.object(RebuildInstanceExecutor, 'run', autospec=True) as run:
            self.parked.job.execute()
        run.assert_called_once()
        executor = run.call_args.args[0]
        self.assertTrue(executor._resumed)
        self.assertEqual(executor._held_build_lock_host_id, self.host.id)


    def test_a_parked_build_comes_back_before_a_better_one_that_waits(self):
        """It takes its lock back: what it builds is never given away."""
        self._park(take_lock=False)
        self._other(priority=5, result=self._deferred())
        with patch.object(RebuildInstanceExecutor, 'run', autospec=True) as run:
            self.parked.job.execute()
        run.assert_called_once()
        self.assertEqual(
            run.call_args.args[0]._held_build_lock_host_id, self.host.id,
        )


class TestTheDeferralSaysWhatItWaitsFor(_HostLockBase):
    """A build deferred by the lock names the job it waits for (#68)."""

    def _deferral_while_held(self, executor):
        """The deferral of *executor* while another connection holds the lock."""
        cr = self._hold_lock(self.host.id)
        try:
            return self._deferral(executor)
        finally:
            cr.rollback()
            cr.close()

    def test_it_names_the_one_build_running_on_the_host(self):
        running = self._other(state='started')
        self._other(state='started', code='restart_instance')  # takes no lock
        self._other()  # waits, holds nothing
        exc = self._deferral_while_held(self._executor())
        self.assertEqual(str(exc), self._deferred(running))
        self.assertEqual(exc.seconds, 30)
        self.assertTrue(exc.ignore_retry)

    def test_with_none_or_several_running_it_names_none(self):
        executor = self._executor()
        self.assertEqual(
            str(self._deferral_while_held(executor)), self._deferred(),
        )
        self._other(state='started')
        self._other(state='started')
        self.assertEqual(
            str(self._deferral_while_held(executor)), self._deferred(),
        )

    def test_the_log_says_it_once_per_job_it_waits_for(self):
        first = self._other(state='started')
        executor = self._executor()
        self._deferral_while_held(executor)
        self._deferral_while_held(executor)
        self.assertEqual(self._waiting_lines(executor.job), [
            f"Waiting for job #{first.id} to finish building on this server:"
            " tried again every 30 s.",
        ])
        self.env.cr.execute(
            "UPDATE queue_job SET state = 'done' WHERE uuid = %s",
            (first.queue_job_uuid,),
        )
        second = self._other(state='started')
        self._deferral_while_held(executor)
        self.assertEqual(self._waiting_lines(executor.job), [
            f"Waiting for job #{first.id} to finish building on this server:"
            " tried again every 30 s.",
            f"Waiting for job #{second.id} to finish building on this server:"
            " tried again every 30 s.",
        ])

    def test_the_log_says_it_without_a_number_when_none_is_named(self):
        executor = self._executor()
        self._deferral_while_held(executor)
        self._deferral_while_held(executor)
        self.assertEqual(self._waiting_lines(executor.job), [
            "Waiting for another build on this server to finish:"
            " tried again every 30 s.",
        ])

    def test_the_parked_job_s_deferral_writes_the_line_too(self):
        """The deferral for a parked job's checkpoint names it in the log."""
        parked = self._other()
        self.env['cloud.job.checkpoint'].sudo().create({
            'job_id': parked.id,
            'queue_job_uuid': parked.queue_job_uuid,
            'parked': True,
            'build_lock_host_id': self.host.id,
        })
        executor = self._executor()
        exc = self._deferral(executor)
        self.assertEqual(str(exc), self._deferred(parked))
        self.assertEqual(self._waiting_lines(executor.job), [
            f"Waiting for job #{parked.id} to finish building on this server:"
            " tried again every 30 s.",
        ])


class TestTheListsSayWhatItWaitsFor(_HostLockBase):
    """``build_wait``: a job deferred by the lock, and the job it waits for."""

    def test_a_job_deferred_by_the_lock(self):
        holder = self._other(state='started')
        waiting = self._other(result=self._deferred(holder))
        generic = self._other(result=self._deferred())
        waits = (waiting | generic)._build_waits()
        self.assertEqual(waits, {
            waiting.id: {
                'job_id': holder.id,
                'until': fields.Datetime.to_string(waiting.queue_job_id.eta),
            },
            generic.id: {
                'job_id': None,
                'until': fields.Datetime.to_string(generic.queue_job_id.eta),
            },
        })

    def test_nothing_for_a_job_running_ended_or_deferred_for_another_reason(self):
        holder = self._other(state='started')
        running = self._other(state='started', result=self._deferred(holder))
        ended = self._other(state='done', result=self._deferred(holder))
        busy = self._other(result="The instance's crons are busy; deferring.")
        plain = self._other()
        self.assertEqual((running | ended | busy | plain)._build_waits(), {})

    def test_the_job_ahead_only_for_a_reader_who_may_read_it(self):
        """On a shared host it may be another project's, or another tenant's."""
        elsewhere = self.env['cloud.project'].create({'name': 'build-lock-elsewhere'})
        holder = self._other(state='started', instance=self.env['cloud.instance'].create({
            'name': 'build-lock-elsewhere',
            'project_id': elsewhere.id,
            'environment': 'staging',
            'host_id': self.host.id,
        }))
        waiting = self._other(result=self._deferred(holder))
        member = new_test_user(
            self.env, login='build-lock-member',
            groups='base.group_user,incubacloud.group_cloud_user',
        )
        self.instance.project_id.member_ids = [(4, member.id)]
        waits = waiting.with_user(member)._build_waits()
        self.assertIsNone(waits[waiting.id]['job_id'])
        self.assertTrue(waits[waiting.id]['until'])
        elsewhere.member_ids = [(4, member.id)]
        waits = waiting.with_user(member)._build_waits()
        self.assertEqual(waits[waiting.id]['job_id'], holder.id)

    def test_the_lists_and_the_history_carry_it(self):
        holder = self._other(state='started')
        waiting = self._other(result=self._deferred(holder))
        plain = self._other()
        for rows in (
            (waiting | plain)._format(), (waiting | plain)._format_history(),
        ):
            by_id = {row['id']: row for row in rows}
            self.assertEqual(by_id[waiting.id]['build_wait']['job_id'], holder.id)
            self.assertIsNone(by_id[plain.id]['build_wait'])


class TestABetterJobTakesTheLockFirst(_HostLockBase):
    """A job does not try the lock while a better one waits for it (#68).

    The reserve's deploys go by ``root.warm`` with priority 15 and the
    tenants' by ``root.user`` with 10, or 5 in production: in different
    channels, the lock is the only place they meet.
    """

    def test_it_gives_way_to_a_better_one_that_waits(self):
        better = self._other(priority=10, result=self._deferred())
        executor = self._queued_executor(priority=15)
        with patch.object(
            RebuildInstanceExecutor, '_try_host_build_lock', autospec=True,
        ) as trying:
            exc = self._deferral(executor)
        trying.assert_not_called()
        self.assertEqual(str(exc), self._deferred(better))
        self.assertEqual(exc.seconds, 30)
        self.assertTrue(exc.ignore_retry)
        self.assertEqual(self._waiting_lines(executor.job), [
            f"Waiting for job #{better.id} to finish building on this server:"
            " tried again every 30 s.",
        ])

    def test_one_that_starts_after_waiting_still_counts(self):
        """Between its start and its try of the lock, or building already."""
        better = self._other(state='started', priority=10, result=self._deferred())
        exc = self._deferral(self._queued_executor(priority=15))
        self.assertEqual(str(exc), self._deferred(better))

    def test_no_way_given_at_the_same_priority_or_to_a_worse_one(self):
        self._other(priority=10, result=self._deferred())
        self._other(priority=15, result=self._deferred())
        self._queued_executor(priority=10).pre_run_checks()  # must not raise

    def test_no_way_given_to_one_cancelled_ended_or_waiting_for_another_thing(self):
        self._other(state='cancelled', priority=5, result=self._deferred())
        self._other(state='done', priority=5, result=self._deferred())
        self._other(priority=5, result="The instance's crons are busy; deferring.")
        self._queued_executor(priority=15).pre_run_checks()  # must not raise

    def test_it_stops_giving_way_past_the_cap(self):
        """The reserve is not left unfilled on a host that is always busy."""
        self._other(priority=10, result=self._deferred())
        self._deferral(self._queued_executor(
            priority=15, age=BUILD_LOCK_YIELD_MAX_S - 60,
        ))
        self._queued_executor(
            priority=15, age=BUILD_LOCK_YIELD_MAX_S + 5,
        ).pre_run_checks()  # must not raise
