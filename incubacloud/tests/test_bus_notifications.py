"""Bus notification volume and scope.

Two properties are asserted here, both regressions waiting to happen:

* The executor only publishes on a tick that produced log rows. It
  used to publish every ``sleep_interval`` regardless, so a job sitting
  in a silent ``docker compose build`` sent 2 events/s to every watcher
  and cost each open SPA tab a full refetch per event.

* The payload names the job's target, and it only reaches users who may
  read the job. The audience filter is what makes naming the target
  safe — the bus does not apply record rules.
"""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from ..models.abstract_executor import AbstractExecutor


class _TestExecutor(AbstractExecutor):
    """Concrete stand-in so ``object.__new__`` works.

    ``AbstractExecutor`` is an ABC and cannot be instantiated even
    through ``object.__new__``. ``_job_type`` stays empty so
    ``__init_subclass__`` does not register this in the executor
    registry.
    """

    def get_commands(self):
        return []


class _BusTestCase(TransactionCase):

    def _job_type(self, code, apply_to='host'):
        jt = self.env['cloud.job.type'].search(
            [('code', '=', code)], limit=1,
        )
        if not jt:
            jt = self.env['cloud.job.type'].create({
                'name': code, 'code': code, 'apply_to': apply_to,
            })
        return jt

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'bus-host',
            'ip_address': '10.0.0.7',
            'user': 'ubuntu',
            'wildcard_domain': 'bus.example.com',
        })
        self.jt = self._job_type('bus_test')
        self.job = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': self.jt.id,
            'name': 'Bus Test',
        })


class TestFlushLogsCount(_BusTestCase):
    """``_flush_logs`` reports how many chunks it actually wrote."""

    def _executor(self):
        ex = object.__new__(_TestExecutor)
        ex.job = self.job
        ex._log_buffer = []
        ex._chunks_persisted = 0
        ex._cap_marker_emitted = False
        return ex

    def _no_write(self):
        """Neutralise the chunk INSERT, keeping the counting logic real.

        ``_flush_logs`` persists on its own registry cursor, so rows
        created by the test transaction are invisible to it and the FK
        on ``job_id`` fails. Patching the model's real ``create`` keeps
        the loop, the cap branch and the counter under test while
        skipping the write.
        """
        return patch.object(
            type(self.env['cloud.job.log.chunk']), 'create',
            return_value=self.env['cloud.job.log.chunk'],
        )

    def test_empty_buffer_reports_zero(self):
        self.assertEqual(self._executor()._flush_logs(), 0)

    def test_counts_the_rows_it_created(self):
        ex = self._executor()
        ex._log_buffer = [('one', 'stdout'), ('two', 'stderr')]
        with self._no_write():
            self.assertEqual(ex._flush_logs(), 2)

    def test_the_cap_marker_counts_as_output(self):
        """Hitting the cap still publishes once, then never again.

        The marker line is real content a watcher should see; the
        silence that follows is what must not keep the bus busy.
        """
        from ..models.abstract_executor import MAX_CHUNKS_PER_JOB
        ex = self._executor()
        ex._chunks_persisted = MAX_CHUNKS_PER_JOB
        ex._log_buffer = [('over the cap', 'stdout')]
        with self._no_write():
            self.assertEqual(ex._flush_logs(), 1)
            self.assertTrue(ex._cap_marker_emitted)
            ex._log_buffer = [('more', 'stdout')]
            self.assertEqual(ex._flush_logs(), 0)

    def test_capped_job_reports_zero_forever(self):
        """A capped job drains its buffer without writing anything.

        Counting buffered entries instead of created rows would keep
        the bus firing for the rest of the job with no new content
        behind it — the reason this returns a row count and not a
        boolean over ``_log_buffer``.
        """
        ex = self._executor()
        ex._cap_marker_emitted = True
        ex._log_buffer = [('dropped', 'stdout')]
        self.assertEqual(ex._flush_logs(), 0)
        self.assertEqual(ex._log_buffer, [])


class TestSilentTicksDoNotPublish(_BusTestCase):
    """``run()`` notifies on output, not on the clock."""

    def _run_with_ticks(self, flush_returns):
        """Drive ``run()`` with a stubbed flush and count publishes.

        ``_async_entry`` is replaced by a coroutine that yields control
        enough times for the loop to take a few ticks, so the real
        tick/flush/publish wiring is exercised without SSH.
        """
        ex = object.__new__(_TestExecutor)
        ex.job = self.job
        ex._log_buffer = []
        ex._chunks_persisted = 0
        ex._cap_marker_emitted = False
        ex._bus_audience_ids = None
        ex.sleep_interval = 0
        ex._loop = None

        calls = {'publish': 0}
        returns = list(flush_returns)

        async def _entry():
            for _ in range(len(returns)):
                pass

        ex._async_entry = _entry
        ex._flush_logs = lambda: returns.pop(0) if returns else 0
        ex._publish_bus = lambda: calls.__setitem__(
            'publish', calls['publish'] + 1,
        )
        ex._check_cancel = lambda: None
        ex.run()
        return calls['publish']

    def test_silent_ticks_publish_only_the_final_drain(self):
        published = self._run_with_ticks([0, 0, 0])
        self.assertEqual(published, 1)

    def test_a_tick_with_output_publishes(self):
        published = self._run_with_ticks([3, 0, 0])
        self.assertGreaterEqual(published, 2)


class TestBroadcastPayload(_BusTestCase):
    """The payload names the target so watchers can self-filter."""

    def _capture(self, job, **kwargs):
        sent = []
        with patch.object(
            type(self.env['res.users']), '_bus_send',
            side_effect=lambda channel, payload, *a, **kw: sent.append(
                (channel, payload),
            ),
        ):
            self.env['cloud.job']._broadcast_job_update(job.id, **kwargs)
        return sent

    def test_host_job_carries_host_and_null_instance(self):
        sent = self._capture(self.job)
        self.assertTrue(sent, "expected at least one bus send")
        channel, payload = sent[0]
        self.assertEqual(channel, 'cloud_jobs')
        self.assertEqual(payload['id'], self.job.id)
        self.assertEqual(payload['host_id'], self.host.id)
        self.assertIsNone(payload['instance_id'])
        self.assertIsNone(payload['project_id'])

    def test_instance_job_carries_instance_and_project(self):
        project = self.env['cloud.project'].create({'name': 'bus-proj'})
        instance = self.env['cloud.instance'].create({
            'name': 'bus-inst',
            'project_id': project.id,
            'host_id': self.host.id,
        })
        job = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'instance_id': instance.id,
            'job_type_id': self.jt.id,
            'name': 'Bus Inst Job',
        })
        _channel, payload = self._capture(job)[0]
        self.assertEqual(payload['instance_id'], instance.id)
        self.assertEqual(payload['project_id'], project.id)

    def test_terminal_state_still_travels(self):
        _channel, payload = self._capture(self.job, state='done')[0]
        self.assertEqual(payload['state'], 'done')

    def test_hidden_job_types_send_nothing(self):
        hidden = self._job_type('host_metrics')
        job = self.env['cloud.job'].sudo().create({
            'host_id': self.host.id,
            'job_type_id': hidden.id,
            'name': 'Hidden',
        })
        self.assertEqual(self._capture(job), [])

    def test_precomputed_audience_is_honoured(self):
        """Callers on a repeating path pass the audience they resolved.

        The executor computes it once per run instead of paying an ACL
        check per user on every chunk flush.
        """
        sent = self._capture(self.job, audience_ids=[])
        self.assertEqual(sent, [])


# post_install: creating res.users at_install trips over the
# autopost_bills not-null constraint account adds later.
@tagged("post_install", "-at_install")
class TestBroadcastAudience(_BusTestCase):
    """Only users who may read the job receive the event."""

    def test_audience_excludes_users_who_cannot_read_the_job(self):
        """Host-level jobs are project-manager-and-above only.

        ``rule_job_member`` scopes jobs through
        ``instance_id.project_id.member_ids``, so a job with no instance
        never matches for a stakeholder. Same predicate the email
        notifier uses, via the shared ``_job_visible_to`` helper.
        """
        stakeholder = self.env['res.users'].create({
            'name': 'Stake Holder',
            'login': 'bus-stakeholder',
            'group_ids': [
                (4, self.env.ref('base.group_user').id),
                (4, self.env.ref('incubacloud.group_cloud_user').id),
            ],
        })
        audience = self.env['cloud.job']._bus_audience(self.job)
        self.assertNotIn(stakeholder, audience)

    def test_audience_is_the_acl_helper_not_a_copy_of_the_domain(self):
        """Guard against re-introducing a duplicated membership domain.

        If someone reimplements visibility inline, this stops matching
        and the test fails — the rule must stay the single source of
        truth.
        """
        Job = self.env['cloud.job']
        with patch.object(
            type(Job), '_job_visible_to', return_value=False,
        ) as visible:
            self.assertEqual(len(Job._bus_audience(self.job)), 0)
        self.assertTrue(visible.called)

    def test_no_audience_means_no_send(self):
        sent = []
        with patch.object(
            type(self.env['cloud.job']), '_bus_audience',
            return_value=self.env['res.users'],
        ), patch.object(
            type(self.env['res.users']), '_bus_send',
            side_effect=lambda *a, **kw: sent.append(a),
        ):
            self.env['cloud.job']._broadcast_job_update(self.job.id)
        self.assertEqual(sent, [])


class TestExecutorAudienceIsResolvedOnce(_BusTestCase):
    """The per-run cache is what keeps the ACL check off the hot path."""

    def test_audience_resolved_once_per_run(self):
        ex = object.__new__(_TestExecutor)
        ex.job = self.job
        ex._bus_audience_ids = None

        Job = type(self.env['cloud.job'])
        with patch.object(
            Job, '_bus_audience',
            return_value=self.env['res.users'].browse([1]),
        ) as audience, patch.object(
            Job, '_broadcast_job_update', return_value=None,
        ):
            ex._publish_bus()
            ex._publish_bus()
            ex._publish_bus()
        self.assertEqual(audience.call_count, 1)


class TestSpecdMocksMatchRuntime(_BusTestCase):
    """The runtime API the audience code leans on really exists."""

    def test_res_users_exposes_bus_send(self):
        user = MagicMock(spec=type(self.env['res.users']))
        user._bus_send('cloud_jobs', {'id': 1})
        user._bus_send.assert_called_once()
