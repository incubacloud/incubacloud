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

from ..models.host_build_lock import _HOST_BUILD_LOCK_NS, HostBuildLockMixin
from ..models.rebuild_instance_executor import RebuildInstanceExecutor


class TestHostBuildLock(TransactionCase):
    """A build defers when another build already holds its host."""

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
