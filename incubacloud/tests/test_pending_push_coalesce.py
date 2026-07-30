"""Tier 2 — pending push queue and coalesced rebuild on-success hook.

Covers the post-rebuild side of the "never drop a push" contract:

* ``cloud.instance.pending.push`` model basics + ``_to_payload`` shape
  (the dict the rebuild executor folds into the next job's payload).
* ``rebuild_instance_executor._enqueue_coalesced_rebuild_if_pending``:

  - empty queue → no-op (no enqueue, no writes)
  - non-empty queue → exactly one new ``rebuild_instance`` job carrying
    every queued push, ordered by ``create_date``, with the HEAD push
    surfaced at the top level and pending rows unlinked atomically with
    the enqueue
  - enqueue failure → pending rows are preserved so the next webhook /
    manual rebuild can pick them up
"""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _ensure_job_type(env, code, apply_to='instance'):
    jt = env['cloud.job.type'].search([('code', '=', code)], limit=1)
    if not jt:
        jt = env['cloud.job.type'].create({
            'name': code,
            'code': code,
            'apply_to': apply_to,
        })
    return jt


class _PendingPushBase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'PP Host',
            'ip_address': '10.0.0.3',
            'user': 'ubuntu',
            'wildcard_domain': 'pp.example.com',
        })
        self.project = self.env['cloud.project'].create(
            {'name': 'PP Project'},
        )
        self.instance = self.env['cloud.instance'].create({
            'name': 'staging-pp',
            'project_id': self.project.id,
            'host_id': self.host.id,
            'environment': 'staging',
            'auto_rebuild': True,
            'state': 'deployed',
        })
        # Make sure the job type exists so any real enqueue would resolve it;
        # the executor tests below patch enqueue, but the model tests need a
        # real foreign key target if they ever stop patching.
        _ensure_job_type(self.env, 'rebuild_instance')

    def _create_pending(self, **kw):
        defaults = {
            'instance_id': self.instance.id,
            'push_repo': 'owner/repo',
            'push_branch': 'main',
            'push_sha': 'abc1234',
            'push_message': 'feat: x',
            'push_by': 'dev',
            'skip_reason': 'cooldown',
        }
        return self.env['cloud.instance.pending.push'].create(defaults | kw)


# ── model ────────────────────────────────────────────────────────────────────

class TestPendingPushModel(_PendingPushBase):

    def test_to_payload_returns_full_dict(self):
        payload = self._create_pending()._to_payload()
        # Every field the executor folds into the coalesced job must be
        # serializable to a plain dict — Python primitives only.
        for k in (
            'push_repo', 'push_branch', 'push_sha',
            'push_message', 'push_by', 'skip_reason', 'queued_at',
        ):
            self.assertIn(k, payload)
            self.assertIsInstance(payload[k], str)
        self.assertEqual(payload['push_sha'], 'abc1234')
        self.assertEqual(payload['skip_reason'], 'cooldown')

    def test_instance_cascade_on_unlink(self):
        pp = self._create_pending()
        Pending = self.env['cloud.instance.pending.push']
        # Use SQL to bypass instance unlink guards; the FK cascade is what we
        # are validating, not the instance's own delete protocol.
        self.env.cr.execute(
            "DELETE FROM cloud_instance WHERE id = %s",
            (self.instance.id,),
        )
        self.instance.invalidate_recordset()
        self.assertFalse(Pending.browse(pp.id).exists())

    def test_pending_push_count_compute(self):
        self._create_pending(push_sha='aaa1')
        self._create_pending(push_sha='bbb2')
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.pending_push_count, 2)


# ── executor on_success hook (coalesce + enqueue) ────────────────────────────

class TestCoalescedRebuildEnqueue(_PendingPushBase):
    """Bind the unbound method onto a spec'd executor mock so we can assert
    behaviour without instantiating an asyncssh-backed executor.

    Spec'ing against RebuildInstanceExecutor catches accidental drift if the
    parent class renames ``_sys`` or the unbound method starts depending on
    a new helper — calls outside the real API raise AttributeError."""

    def _executor_mock(self):
        # spec includes ``_sys`` (from AbstractExecutor) and any future
        # helper the on_success path might pull in.
        exe = MagicMock(spec=RebuildInstanceExecutor)
        exe.env = self.env
        return exe

    def test_no_pending_no_enqueue(self):
        exe = self._executor_mock()
        Job = self.env['cloud.job']
        with patch.object(type(Job), 'enqueue') as mock_enqueue:
            RebuildInstanceExecutor._enqueue_coalesced_rebuild_if_pending(
                exe, self.instance,
            )
            mock_enqueue.assert_not_called()
        exe._sys.assert_not_called()

    def test_no_host_no_enqueue(self):
        # The executor protects against running against an instance whose
        # host has been detached (the host_id FK is set null on cascade).
        self._create_pending()
        self.instance.write({'host_id': False})
        self.instance.invalidate_recordset()
        exe = self._executor_mock()
        Job = self.env['cloud.job']
        with patch.object(type(Job), 'enqueue') as mock_enqueue:
            RebuildInstanceExecutor._enqueue_coalesced_rebuild_if_pending(
                exe, self.instance,
            )
            mock_enqueue.assert_not_called()

    def test_pending_pushes_coalesced_into_single_job(self):
        # Three pending pushes; the youngest is the HEAD that the new job
        # surfaces at the top-level push_* keys.
        self._create_pending(push_sha='first1', push_message='first commit')
        self._create_pending(push_sha='midd22', push_message='second commit')
        self._create_pending(push_sha='headd3', push_message='third commit')
        self.instance.invalidate_recordset()

        calls = []

        def _capture(host_id, instance_id, job_type_code, payload=None,
                     **kwargs):
            calls.append({
                'host_id': host_id,
                'instance_id': instance_id,
                'job_type_code': job_type_code,
                'payload': payload or {},
                'kwargs': kwargs,
            })
            return 12345

        exe = self._executor_mock()
        Job = self.env['cloud.job']
        with patch.object(type(Job), 'enqueue', side_effect=_capture):
            RebuildInstanceExecutor._enqueue_coalesced_rebuild_if_pending(
                exe, self.instance,
            )

        # Exactly one new job, of the right type, on the right host/instance.
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c['host_id'], self.host.id)
        self.assertEqual(c['instance_id'], self.instance.id)
        self.assertEqual(c['job_type_code'], 'rebuild_instance')
        # This runs from on_success, while the rebuild that triggered it is
        # still 'started' on this very instance. Without the bypass the
        # active-job guard matches that parent job and refuses to queue the
        # follow-up — and the UserError is swallowed by the caller, so the
        # coalesced pushes are simply lost with a warning in the log.
        self.assertTrue(
            c['kwargs'].get('bypass_running_check'),
            "the coalesced rebuild must bypass the active-job guard",
        )
        # Trigger marks the job as coalesced so the SPA / log terminal can
        # render the multi-push trigger bar.
        self.assertEqual(c['payload']['trigger'], 'coalesced')
        # HEAD is the most recent pending push (sort by create_date asc).
        self.assertEqual(c['payload']['push_sha'], 'headd3')
        self.assertEqual(c['payload']['push_message'], 'third commit')
        # All three pushes are carried, in creation order.
        coalesced = c['payload']['coalesced_pushes']
        self.assertEqual(len(coalesced), 3)
        self.assertEqual(
            [p['push_sha'] for p in coalesced],
            ['first1', 'midd22', 'headd3'],
        )
        # Pending rows consumed atomically — the next webhook starts fresh.
        self.assertFalse(self.instance.pending_push_ids)

    def test_enqueue_failure_preserves_pending_rows(self):
        pp = self._create_pending(push_sha='keep01')
        self.instance.invalidate_recordset()
        exe = self._executor_mock()
        Job = self.env['cloud.job']
        with patch.object(
            type(Job), 'enqueue',
            side_effect=Exception('boom'),
        ):
            RebuildInstanceExecutor._enqueue_coalesced_rebuild_if_pending(
                exe, self.instance,
            )
        # Row not unlinked — the operator still sees what was queued and the
        # next manual rebuild / webhook can retry the coalesce.
        self.assertTrue(pp.exists())
        # The executor surfaced a warning to the operator log.
        warning_msgs = [
            args[0][0]
            for args in exe._sys.call_args_list
            if args[0] and 'Failed' in args[0][0]
        ]
        self.assertTrue(warning_msgs)
