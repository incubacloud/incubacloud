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
import odoo
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.tests.common import TransactionCase

from ..models.delete_instance_executor import DeleteInstanceExecutor
from ..models.host_build_lock import _HOST_BUILD_LOCK_NS, HostBuildLockMixin
from ..models.rebuild_instance_executor import RebuildInstanceExecutor


class _HostLockBase(TransactionCase):
    """A host, an instance on it, and a way to hold its lock from outside."""

    def setUp(self):
        super().setUp()
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

        ``RebuildInstanceExecutor`` is the only place in the codebase
        that runs ``docker compose build``; every other rebuild — tenant,
        warm, apply-plan — subclasses it. Asserting it here is what makes
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
    ``delete_instance`` per warm, ``enqueue`` only serialises per
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

        executor = self._delete_executor(MoveRollbackCleanupExecutor)
        cr = self._hold_lock(self.host.id)
        try:
            executor.pre_run_checks()  # must not raise
        finally:
            cr.rollback()
            cr.close()

    def test_the_source_cleanup_keeps_the_lock(self):
        """It runs the real teardown on the source host and waits for
        nothing, so it contends like any other teardown."""
        from ..models.move_cutover_executor import MoveCleanupSourceExecutor

        executor = self._delete_executor(MoveCleanupSourceExecutor)
        cr = self._hold_lock(self.host.id)
        try:
            with self.assertRaises(RetryableJobError):
                executor.pre_run_checks()
        finally:
            cr.rollback()
            cr.close()
