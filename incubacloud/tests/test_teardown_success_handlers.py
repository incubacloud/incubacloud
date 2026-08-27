"""The success handlers of the two teardown executors actually run.

Observed in production on 2026-08-27: a `delete_instance` job tore the
instance down completely — containers, images, volumes, logrotate config
and directory all gone — and then reported **failed**:

    ✓ Instance 'prod' removed from host.
    ✓ Instance '?' removed from host.
    ✗ AttributeError: 'DeleteInstanceExecutor' object has no attribute '_host'

`_host()` was a convention: six host-scoped executors each defined it as
`return self.job.host_id`, and two instance-scoped callers assumed it
existed. `AbstractSSHExecutor` never did, so those two raised from inside
a success handler — which turns finished work into a reported failure and
an alert about a host that is already clean.

Neither call site had a test. The suite referenced `move_cleanup_source`
only as a job-type string in the gate and chain tests, so no `on_success`
here was ever executed and an `AttributeError` sat in one for as long as
it took someone to move a host or delete an instance twice.

What these pin is that both handlers run to completion. The `delete`
re-run is the rarer case; the move cleanup raised on **every** successful
move, leaving the source host's observability labels pointing at an
instance that had left.
"""
import asyncio
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from ..models.abstract_executor import AbstractSSHExecutor
from ..models.delete_instance_executor import DeleteInstanceExecutor
from ..models.move_cutover_executor import MoveCleanupSourceExecutor


class _TeardownBase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({'name': 'td-proj'})
        self.source = self.env['cloud.host'].create({
            'name': 'td-src', 'ip_address': '10.0.7.1', 'port': 22,
            'user': 'root', 'login_type': 'ssh_key',
            'wildcard_domain': 'tdsrc.example.com',
            'status': 'compatible', 'traefik_deployed': True,
        })
        self.instance = self.env['cloud.instance'].create({
            'name': 'td-inst', 'project_id': self.project.id,
            'environment': 'staging', 'host_id': self.source.id,
            'state': 'deployed',
        })

    def _job(self, code):
        job_type = self.env['cloud.job.type'].search(
            [('code', '=', code)], limit=1,
        )
        return self.env['cloud.job'].create({
            'host_id': self.source.id,
            'instance_id': self.instance.id,
            'job_type_id': job_type.id,
            'name': f'{code} teardown test',
        })

    def _remove_record(self):
        """Unlink the instance the way a finished teardown does.

        ``unlink`` refuses a 'deployed' record outright, so forcing the
        state would be testing a situation the product cannot reach.
        ``_finalize_removal`` is the real path — deployed → deleting →
        draft → unlink — and it is what leaves the job holding a null
        ``instance_id``, which is the state the re-run meets.
        """
        self.instance._finalize_removal(keep_in_panel=False)
        self.assertFalse(self.instance.exists())

    def _executor(self, cls, job):
        """Build *cls* over a real job without opening an SSH connection."""
        ex = object.__new__(cls)
        ex.job = job
        ex.env = job.env
        ex._log_buffer = []
        ex._scripts_requested = False
        ex._scripts_uploaded = False
        ex._script_overlay_cache = None
        return ex


class TestHostAccessorIsGuaranteed(_TeardownBase):
    """The convention is now the base class's promise, not a habit."""

    def test_the_base_class_provides_it(self):
        self.assertTrue(hasattr(AbstractSSHExecutor, '_host'))

    def test_it_returns_the_job_s_host(self):
        job = self._job('delete_instance')
        ex = self._executor(DeleteInstanceExecutor, job)
        self.assertEqual(ex._host(), self.source)

    def test_it_survives_the_instance_being_unlinked(self):
        """The case that exposed the gap: the accessor is reached only
        after the instance record is gone, so it must not depend on it."""
        job = self._job('delete_instance')
        ex = self._executor(DeleteInstanceExecutor, job)
        self._remove_record()
        self.assertEqual(ex._host(), self.source)


class TestDeleteInstanceRerun(_TeardownBase):
    """A re-run after the record is already gone must be a clean no-op.

    ``get_commands`` documents this path — the job's transaction can lose
    a serialization race and be re-queued while the remote work has
    already been committed. Reporting that as a failure raises an alert
    for a host that is in exactly the state it should be.
    """

    def test_get_commands_is_empty_once_the_record_is_gone(self):
        job = self._job('delete_instance')
        ex = self._executor(DeleteInstanceExecutor, job)
        self._remove_record()
        self.assertEqual(ex.get_commands(), [])

    def test_on_success_completes_with_no_instance_left(self):
        """This is what raised in production."""
        job = self._job('delete_instance')
        ex = self._executor(DeleteInstanceExecutor, job)
        self._remove_record()

        with patch.object(
            type(self.source), 'refresh_observability_labels',
        ) as refresh:
            asyncio.run(ex.on_success({}))

        refresh.assert_called_once()

    def test_on_success_still_finalises_when_the_instance_is_there(self):
        """The normal path must keep working — the fix is additive."""
        job = self._job('delete_instance')
        ex = self._executor(DeleteInstanceExecutor, job)

        with patch.object(
            type(self.instance), '_finalize_removal',
        ) as finalize, patch.object(
            type(self.source), 'refresh_observability_labels',
        ):
            asyncio.run(ex.on_success({}))

        finalize.assert_called_once()


class TestMoveCleanupSource(_TeardownBase):
    """The last step of a move, which raised on *every* success.

    A move that worked end to end still finished with a failed job, and
    the source host kept advertising an instance that had left it.
    """

    def test_on_success_refreshes_the_source_host_labels(self):
        job = self._job('move_cleanup_source')
        ex = self._executor(MoveCleanupSourceExecutor, job)

        with patch.object(
            type(self.source), 'refresh_observability_labels',
        ) as refresh:
            asyncio.run(ex.on_success({}))

        refresh.assert_called_once()
        self.assertEqual(
            refresh.call_args.kwargs.get('reason'), 'move cleanup',
        )

    def test_on_success_leaves_the_instance_record_alone(self):
        """By now the instance lives on the destination host: this
        teardown touches the abandoned copy only."""
        job = self._job('move_cleanup_source')
        ex = self._executor(MoveCleanupSourceExecutor, job)
        state_before = self.instance.state

        with patch.object(
            type(self.source), 'refresh_observability_labels',
        ):
            asyncio.run(ex.on_success({}))

        self.assertTrue(self.instance.exists())
        self.assertEqual(self.instance.state, state_before)
