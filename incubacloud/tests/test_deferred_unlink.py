"""A teardown's unlink waits for the job's commit.

Production, 2026-09-19: every successful ``delete_instance`` ran twice.
The success hook unlinked the instance on its own cursor; ``cloud_job``
references the instance ``ON DELETE SET NULL``, so that commit updated
the job's own row; and the job's transaction — whose snapshot
``job.lock()`` had fixed before the run — then failed to mark itself
done and queue_job ran the whole job again. Over three days: 2 re-runs
of 3 deletes, 0 of ~10,000 jobs of every other type.

The unlink now waits for the commit (``postcommit``), on a cursor of its
own. What these pin, in order of how badly each would hurt:

  * the hook no longer unlinks — it marks;
  * exactly one unlink is armed on the *job's* cursor, not the hook's,
    and running it removes the record;
  * a dropped unlink (rolled-back transaction) is re-armed by the re-run
    without touching the host again;
  * a failing unlink alerts instead of failing a job that already
    finished;
  * the archived purge, which unlinks from a hook the same way, defers
    the same way.

The race itself is not reproducible here — a test cursor never commits —
so the proof is production: ``queue_job.retry`` of a delete stays at 1.
"""
import asyncio
import inspect
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from ..models.abstract_executor import AbstractExecutor
from ..models.delete_instance_executor import DeleteInstanceExecutor


class _Armed:
    """Stand-in for ``cr.postcommit`` that records what gets armed."""

    def __init__(self):
        self.funcs = []

    def add(self, func):
        self.funcs.append(func)


class _DeferredUnlinkBase(TransactionCase):

    def setUp(self):
        # The deferred unlink opens ``registry.cursor()``; outside test
        # mode that is a second connection which cannot see this
        # transaction's fixtures.
        self.registry_enter_test_mode()
        super().setUp()
        self.project = self.env['cloud.project'].create({'name': 'du-proj'})
        self.host = self.env['cloud.host'].create({
            'name': 'du-host', 'ip_address': '10.0.9.1', 'port': 22,
            'user': 'root', 'login_type': 'ssh_key',
            'wildcard_domain': 'du.example.com',
            'status': 'compatible', 'traefik_deployed': True,
        })
        self.instance = self.env['cloud.instance'].create({
            'name': 'du-inst', 'project_id': self.project.id,
            'environment': 'staging', 'host_id': self.host.id,
            'state': 'deployed',
        })

    def _job(self, payload=None):
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', 'delete_instance')], limit=1,
        )
        job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'instance_id': self.instance.id,
            'job_type_id': job_type.id,
            'name': 'delete_instance deferred unlink test',
        })
        job.payload = payload or {}
        return job

    def _executor(self, job, cls=DeleteInstanceExecutor):
        ex = object.__new__(cls)
        ex.job = job
        ex.env = job.env
        ex._log_buffer = []
        ex._scripts_requested = False
        ex._scripts_uploaded = False
        ex._script_overlay_cache = None
        return ex

    def _run_success(self, ex, refresh=None):
        """Run ``on_success`` and return the unlinks it armed.

        ``Callbacks`` has ``__slots__`` and cannot be patched, so the
        cursor's ``postcommit`` is swapped for a recorder for the call.
        """
        armed = _Armed()
        cr = ex.job.env.cr
        original = cr.postcommit
        cr.postcommit = armed
        try:
            with patch.object(
                type(self.host), 'refresh_observability_labels',
                new=refresh,
            ) if refresh is not None else patch.object(
                type(self.host), 'refresh_observability_labels',
            ):
                asyncio.run(ex.on_success({}))
        finally:
            cr.postcommit = original
        # In production the hook's cursor commits before ``postcommit``
        # fires; here the hook wrote into this transaction's cache, and
        # the armed unlink reads the database through a cursor of its
        # own. Flush so it sees what the hook did.
        ex.job.env.flush_all()
        return armed.funcs

    def _record(self):
        self.env.invalidate_all()
        return self.env['cloud.instance'].with_context(
            active_test=False,
        ).browse(self.instance.id)


class TestTheHookMarksInsteadOfUnlinking(_DeferredUnlinkBase):

    def test_the_record_survives_the_hook_finalised(self):
        self._run_success(self._executor(self._job()))
        rec = self._record()
        self.assertTrue(rec.exists())
        self.assertEqual(rec.state, 'draft')
        self.assertFalse(rec.running)
        self.assertTrue(rec.removal_finalized)

    def test_exactly_one_unlink_is_armed(self):
        armed = self._run_success(self._executor(self._job()))
        self.assertEqual(len(armed), 1)

    def test_running_the_armed_unlink_removes_the_record(self):
        armed = self._run_success(self._executor(self._job()))
        armed[0]()
        self.assertFalse(self._record().exists())

    def test_keeping_the_record_arms_nothing(self):
        """Archiving is not a removal: the record stays, so there is no
        unlink to wait for and nothing to mark."""
        job = self._job(payload={'keep_in_panel': True})
        armed = self._run_success(self._executor(job))
        self.assertEqual(armed, [])
        rec = self._record()
        self.assertTrue(rec.exists())
        self.assertFalse(rec.active)
        self.assertFalse(rec.removal_finalized)

    def test_the_unlink_is_armed_on_the_job_cursor_not_the_hook_s(self):
        """``_dispatch_outcome`` swaps the executor onto the hook's cursor
        for the duration of the hook and pins the job's cursor first.
        Armed on the hook's cursor, the unlink would fire on *its*
        commit — which is exactly the commit that must be waited for."""
        src = inspect.getsource(AbstractExecutor._dispatch_outcome)
        self.assertLess(
            src.index('self._job_cr = self.job.env.cr'),
            src.index('with read_committed_cursor('),
        )
        src = inspect.getsource(AbstractExecutor._unlink_after_job_commit)
        self.assertIn("getattr(self, \"_job_cr\", None)", src)


class TestTheArmedUnlink(_DeferredUnlinkBase):

    def test_a_record_already_gone_is_a_no_op(self):
        armed = self._run_success(self._executor(self._job()))
        self._record().unlink()
        armed[0]()  # must not raise
        self.assertFalse(self._record().exists())

    def test_a_failing_unlink_alerts_instead_of_raising(self):
        """By the time it runs the job is done; a failure here must not
        turn that into a failed job after the fact, and must not be
        silent either."""
        armed = self._run_success(self._executor(self._job()))
        with patch.object(
            type(self.instance), 'unlink', side_effect=RuntimeError('boom'),
        ):
            armed[0]()  # must not raise
        self.assertTrue(self._record().exists())
        alert = self.env['cloud.alert'].sudo().search([
            ('code', '=', 'instance_unlink_failed'),
            ('instance_id', '=', self.instance.id),
            ('state', '=', 'active'),
        ])
        self.assertTrue(alert)
        self.assertIn(self.instance.name, alert.message)
        self.assertEqual(alert.host_id, self.host)


class TestTheRerunOnAFinalisedRecord(_DeferredUnlinkBase):
    """The job's transaction rolled back after the hook committed: the
    record is marked, the host is clean, and the unlink was dropped."""

    def setUp(self):
        super().setUp()
        self.instance._finalize_removal(keep_in_panel=False, unlink=False)
        self.assertTrue(self.instance.removal_finalized)

    def test_get_commands_touches_nothing_on_the_host(self):
        ex = self._executor(self._job())
        self.assertEqual(ex.get_commands(), [])

    def test_on_success_only_rearms_the_unlink(self):
        ex = self._executor(self._job())
        with patch.object(
            type(self.instance), '_finalize_removal',
        ) as finalize:
            armed = self._run_success(ex)
        finalize.assert_not_called()
        self.assertEqual(len(armed), 1)
        self.assertFalse(
            [m for m, _kind in ex._log_buffer if 'removed from host' in m],
        )
        armed[0]()
        self.assertFalse(self._record().exists())

    def test_the_rerun_does_not_refresh_the_labels(self):
        refresh = MagicMock()
        self._run_success(self._executor(self._job()), refresh=refresh)
        refresh.assert_not_called()
