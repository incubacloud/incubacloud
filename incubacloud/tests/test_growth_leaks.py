"""Regression tests for three unbounded-growth leaks.

They share a shape, which is why they share a file: in each case a
retention mechanism existed, read as configured, and silently failed to
cover the rows it was written for. Nothing raised, nothing was logged,
and the only symptom was a table that kept growing — so the tests here
assert the *reach* of each mechanism, not that it runs.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.cloud_github_event import (
    GITHUB_APP_REVOKED_CODE,
)

_CHUNK_MODULE = 'odoo.addons.incubacloud.models.cloud_job_log_chunk'


def _job_type(env, code, apply_to='host'):
    """Return the job type for ``code``, creating a minimal one if absent."""
    jt = env['cloud.job.type'].search([('code', '=', code)], limit=1)
    if not jt:
        jt = env['cloud.job.type'].create({
            'name': code, 'code': code, 'apply_to': apply_to,
        })
    return jt


class _HostFixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'Leak Host',
            'ip_address': '10.0.0.77',
            'user': 'root',
            'wildcard_domain': 'leak.example.com',
        })


# ── Leak 1: the log-chunk purge deleted one batch and stopped ────────────

class TestJobLogChunkPurgeConverges(_HostFixture):
    """The cron fires once a day and the purge did one batch per call.

    That capped deletion at ``_PURGE_BATCH`` rows per day. A panel
    producing chunks faster than that grew without bound while the
    retention setting sat in the UI looking applied.
    """

    def setUp(self):
        super().setUp()
        self.job = self.env['cloud.job'].create({
            'host_id': self.host.id,
            'job_type_id': _job_type(self.env, 'host_probe').id,
            'name': 'purge fixture',
        })
        self._force_state('done')
        self.Chunk = self.env['cloud.job.log.chunk']
        # ``_purge_old`` is global and these tests assert exact counts.
        # The pre-production snapshot carries real chunks old enough to
        # match, which would drown the fixture. Rolled back with the test
        # transaction, so nothing persists.
        self.env.cr.execute("DELETE FROM cloud_job_log_chunk")
        self.Chunk.invalidate_model()

    def _force_state(self, state):
        """Write the stored-related ``state`` column directly.

        ``state`` is a stored related on ``queue_job_id.state``. Flushing
        first is mandatory: otherwise the invalidate below re-resolves the
        related against a job with no ``queue_job_id`` and clobbers the
        raw value back to NULL.
        """
        self.job.flush_recordset(['state'])
        self.env.cr.execute(
            "UPDATE cloud_job SET state = %s WHERE id = %s",
            (state, self.job.id),
        )
        self.env['cloud.job'].invalidate_model(['state'])

    def _aged_chunks(self, n, days_old=10):
        """Create *n* chunks on the fixture job, backdated past the cutoff."""
        chunks = self.Chunk.create([
            {'job_id': self.job.id, 'source': 'stdout', 'content': f'l{i}'}
            for i in range(n)
        ])
        self.env.cr.execute(
            "UPDATE cloud_job_log_chunk SET create_date = %s WHERE id IN %s",
            (fields.Datetime.now() - timedelta(days=days_old),
             tuple(chunks.ids)),
        )
        self.Chunk.invalidate_model(['create_date'])
        return chunks

    def _remaining(self):
        return self.Chunk.search_count([('job_id', '=', self.job.id)])

    def test_purge_drains_a_backlog_bigger_than_one_batch(self):
        """The regression: five old rows, batch of two, all five must go."""
        self._aged_chunks(5)
        with patch(f'{_CHUNK_MODULE}._PURGE_BATCH', 2):
            deleted = self.Chunk._purge_old(1)
        self.assertEqual(
            deleted, 5,
            "the purge stopped after the first batch — with a daily cron "
            "that is a hard ceiling of _PURGE_BATCH rows per day",
        )
        self.assertEqual(self._remaining(), 0)

    def test_purge_stays_bounded_at_the_batch_ceiling(self):
        """Draining must not turn into an unbounded single transaction."""
        self._aged_chunks(5)
        with patch(f'{_CHUNK_MODULE}._PURGE_BATCH', 2), \
                patch(f'{_CHUNK_MODULE}._PURGE_MAX_BATCHES', 1):
            deleted = self.Chunk._purge_old(1)
        self.assertEqual(deleted, 2)
        self.assertEqual(
            self._remaining(), 3,
            "the ceiling must leave the remainder for the next run",
        )

    def test_chunks_of_an_active_job_survive(self):
        """A streaming deploy keeps its whole log however old the rows are."""
        self._force_state('started')
        self._aged_chunks(3)
        with patch(f'{_CHUNK_MODULE}._PURGE_BATCH', 2):
            self.assertEqual(self.Chunk._purge_old(1), 0)
        self.assertEqual(self._remaining(), 3)

    def test_retention_disabled_deletes_nothing(self):
        self._aged_chunks(3)
        self.assertEqual(self.Chunk._purge_old(0), 0)
        self.assertEqual(self._remaining(), 3)


# ── Leak 2: cron background jobs wrote an audit row each ─────────────────

class TestBackgroundJobsAreNotAudited(_HostFixture):
    """``host_metrics`` and ``instance_health`` fire per target per tick.

    On the pre-production database they were 99.2% of the audit table
    (25,858 of 26,062 rows), burying every real operator action and
    growing without bound.
    """

    def setUp(self):
        super().setUp()
        self.Audit = self.env['cloud.audit.log']
        self.Job = self.env['cloud.job']

    def _audit_count(self):
        """Audit rows this host's *jobs* produced.

        Scoped to ``job_id`` on purpose: ``cloud.host.create`` writes its
        own "Host created" row, which is a genuine operator action and
        must keep being audited.
        """
        return self.Audit.search_count([
            ('host_id', '=', self.host.id), ('job_id', '!=', False),
        ])

    def test_a_hidden_job_type_writes_no_audit_row(self):
        self.Job.enqueue(self.host.id, False, 'host_metrics')
        self.assertEqual(
            self._audit_count(), 0,
            "cron background jobs must not be audited — they are machine "
            "noise, not operator actions",
        )

    def test_a_real_action_is_still_audited(self):
        """The filter must not swallow the rows the audit log exists for."""
        _job_type(self.env, 'leak_audited_action')
        self.Job.enqueue(self.host.id, False, 'leak_audited_action')
        self.assertEqual(self._audit_count(), 1)

    def test_a_chain_does_not_audit_its_hidden_steps(self):
        """``enqueue_chain`` carried a second, separate unfiltered create.

        Fixing only ``enqueue`` would have left the leak open through
        every chained operation.
        """
        _job_type(self.env, 'leak_chain_action')
        self.Job.enqueue_chain([
            {'host_id': self.host.id, 'instance_id': False,
             'job_type_code': 'host_metrics'},
            {'host_id': self.host.id, 'instance_id': False,
             'job_type_code': 'leak_chain_action'},
        ])
        self.assertEqual(self._audit_count(), 1)

    def test_the_audit_filter_uses_the_drawer_predicate(self):
        """One predicate, two consumers.

        If these ever diverge, a type could be hidden from the drawer but
        still audited (or the reverse) with nothing to catch it.
        """
        hidden = self.Job._get_hidden_job_types()
        self.assertIn('host_metrics', hidden)
        self.assertIn('instance_health', hidden)
        self.assertIn('docker_prune', hidden)


# ── Leak 3: GitHub events nobody ever marked processed ───────────────────

class TestGitHubEventRetentionReach(TransactionCase):
    """Both retention stages filter on ``processed=True``.

    Only ``push`` and ``pull_request`` ever set it. ``installation`` —
    and the ``create``/``delete`` types the App manifest subscribes to —
    were persisted with their full payload and then abandoned, exempt
    from a retention policy that appeared to cover them.
    """

    def setUp(self):
        super().setUp()
        self.Event = self.env['cloud.github.event']
        self.Alert = self.env['cloud.alert']

    def _event(self, event_type, action='', delivery='d0'):
        return self.Event.create({
            'event_type': event_type,
            'action': action,
            'delivery_id': delivery,
            'payload': '{}',
        })

    def _active_revocation_alert(self):
        return self.Alert.search([
            ('code', '=', GITHUB_APP_REVOKED_CODE), ('state', '=', 'active'),
        ])

    def test_an_unhandled_event_type_is_marked_processed(self):
        """``create`` is subscribed by the App manifest — a live path."""
        ev = self._event('create', delivery='leak-create')
        ev._dispatch({})
        self.assertTrue(
            ev.processed,
            "an event no handler claims is still exempt from retention "
            "unless something marks it processed",
        )

    def test_installation_created_is_marked_processed(self):
        ev = self._event('installation', 'created', 'leak-inst-created')
        ev._dispatch({'installation': {'id': 4242}})
        self.assertTrue(ev.processed)

    def test_installation_deleted_raises_a_critical_alert(self):
        """Losing the App silently disables auto-rebuild and PR previews."""
        ev = self._event('installation', 'deleted', 'leak-inst-deleted')
        ev._dispatch({})
        alert = self._active_revocation_alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, 'critical')
        self.assertTrue(ev.processed)

    def test_installation_suspend_raises_the_same_alert(self):
        self._event('installation', 'suspend', 'leak-inst-susp')._dispatch({})
        self.assertTrue(self._active_revocation_alert())

    def test_unsuspend_clears_the_revocation_alert(self):
        self._event('installation', 'suspend', 'leak-s1')._dispatch({})
        self._event('installation', 'unsuspend', 'leak-s2')._dispatch({})
        self.assertFalse(self._active_revocation_alert())

    def test_reinstalling_clears_the_revocation_alert(self):
        self._event('installation', 'deleted', 'leak-r1')._dispatch({})
        self._event('installation', 'created', 'leak-r2')._dispatch(
            {'installation': {'id': 77}},
        )
        self.assertFalse(self._active_revocation_alert())

    def test_retention_now_reaches_a_dispatched_unhandled_event(self):
        """End to end: dispatched, aged, and actually deleted by the cron."""
        settings = self.env['cloud.settings'].sudo()._get()
        settings.write({
            'github_event_retention_days': 30,
            'github_event_truncate_days': 0,
        })
        ev = self._event('delete', delivery='leak-aged')
        ev._dispatch({})
        self.env.cr.execute(
            "UPDATE cloud_github_event SET create_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=60), ev.id),
        )
        self.Event.invalidate_model(['create_date'])
        self.Event._purge_old()
        self.assertFalse(
            ev.exists(),
            "an unhandled event must age out like every other event",
        )
