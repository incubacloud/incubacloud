"""How long each job waited for a slot of its channel, measured on the job.

Whether the panel needs to run its jobs without holding a thread each
(the second step of the Odoo.sh plan, ``estudio-fase3-o-drones.md``) is
decided with numbers, not by feel: how long jobs wait for a slot, and how
many ran at once. Four fields of ``cloud.job`` keep them (phase 3, cut 7,
12-nov-2026):

- ``slot_wait_s``: what the channel gate held the job back, written by the
  dispatcher in the write that hands it to queue_job;
- ``slot_load`` and ``slot_channel``: how many of the panel's jobs held a
  slot of its channel then, itself included, and which channel;
- ``return_wait_s``: what a parked job took to come back past the time it
  was due, written in the transaction that stores its ``started``.

A job of a hidden type (a probe, a metrics reading) has none of them; a
job from before they existed has them empty (NULL), one that did not wait
has 0.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields

from ..models import queue_job_ext
from .test_job_channel_gate import _GateCase


class _SlotCase(_GateCase):
    """The gate's fixtures, and the fields as stored."""

    def _stored(self, job):
        """The four fields of *job* as the table holds them: None for NULL.

        :rtype: dict
        """
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT slot_wait_s, slot_load, slot_channel, return_wait_s"
            " FROM cloud_job WHERE id = %s", (job.id,),
        )
        return dict(zip(
            ("slot_wait_s", "slot_load", "slot_channel", "return_wait_s"),
            self.env.cr.fetchone(), strict=True,
        ))

    def _at(self, moment):
        """Freeze ``fields.Datetime.now`` at *moment* for a block."""
        return patch.object(fields.Datetime, "now", return_value=moment)


class TestTheWaitForASlot(_SlotCase):
    """What the dispatcher writes when it hands a job to queue_job."""

    def test_a_job_held_then_dispatched_says_how_long_it_waited(self):
        """Held 95 s by a full ``root.user``: 95, one job in the channel
        once it had its slot, ``root.user``."""
        running = self._running()
        job = self._asked()
        self._assert_held(job)
        since = fields.Datetime.now().replace(microsecond=0)
        job.sudo().channel_wait_since = since
        self.env.cr.execute(
            "UPDATE queue_job SET state = 'done' WHERE uuid = %s",
            (running.queue_job_uuid,),
        )
        running.with_context(ic_skip_state_hook=True).write({"state": "done"})
        self.env.flush_all()
        with self._at(since + timedelta(seconds=95)):
            self.Job._dispatch_next(*job._scope_of(), channel_lock=True)
        self._assert_dispatched(job)
        self.assertEqual(self._stored(job), {
            "slot_wait_s": 95, "slot_load": 1, "slot_channel": "root.user",
            "return_wait_s": None,
        })

    def test_a_job_never_held_waited_0(self):
        """Not empty: 0, the channel and the load all the same."""
        job = self._asked()
        self._assert_dispatched(job)
        stored = self._stored(job)
        self.assertEqual(
            (stored["slot_wait_s"], stored["slot_load"], stored["slot_channel"]),
            (0, 1, "root.user"),
        )

    def test_the_load_counts_the_jobs_that_held_a_slot_beside_it(self):
        """``root.user:2`` with one running: the dispatched one makes 2."""
        self.channels = "root:3,root.user:2,root.bg:1"
        self._running()
        job = self._asked()
        self._assert_dispatched(job)
        self.assertEqual(self._stored(job)["slot_load"], 2)

    def test_a_hidden_job_is_not_measured(self):
        """A probe goes to queue_job outside the queue: nothing written."""
        job = self.Job.browse(self.Job.enqueue(
            self.host.id, self.inst.id, "instance_health",
        ))
        self.assertTrue(job.queue_job_uuid)
        self.assertEqual(self._stored(job), {
            "slot_wait_s": None, "slot_load": None, "slot_channel": None,
            "return_wait_s": None,
        })

    def test_with_the_gate_off_only_the_wait_is_written(self):
        """Nothing counted the channel: no load, no channel; waited 0."""
        job = self.env["cloud.job"].browse(self.env["cloud.job"].enqueue(
            self.host.id, self.other.id, "restart_instance",
        ))
        self._assert_dispatched(job)
        self.assertEqual(
            {k: v for k, v in self._stored(job).items() if k != "return_wait_s"},
            {"slot_wait_s": 0, "slot_load": None, "slot_channel": None},
        )

    def test_the_fields_are_there_for_the_qa(self):
        """The QA reads them by RPC (walk 36, step 12)."""
        known = self.env["cloud.job"].fields_get(
            ["slot_wait_s", "return_wait_s", "slot_load", "slot_channel"], ["type"],
        )
        self.assertEqual({k: v["type"] for k, v in known.items()}, {
            "slot_wait_s": "integer", "return_wait_s": "integer",
            "slot_load": "integer", "slot_channel": "char",
        })


class TestALateReturn(_SlotCase):
    """What a parked job took to come back, stored with its ``started``."""

    def _parked_until(self, job, moment):
        """Park *job*'s checkpoint until *moment*, as a park leaves it."""
        self.env["cloud.job.checkpoint"].sudo().search(
            [("job_id", "=", job.id)],
        ).write({"parked": True, "parked_until": moment})
        self.env.flush_all()

    def _starts(self, job, moment):
        """queue_job stores *job*'s ``started`` at *moment*."""
        qjob = self.env["queue.job"].sudo().search(
            [("uuid", "=", job.queue_job_uuid)],
        )
        qjob.write({"state": "pending"})
        with self._at(moment):
            qjob.write({"state": "started"})
        return qjob

    def test_each_late_return_adds_its_delay(self):
        """40 s late, then 5 s: 45."""
        job = self._running(state="pending", parked=True)
        due = fields.Datetime.now().replace(microsecond=0)
        self._parked_until(job, due)
        self._starts(job, due + timedelta(seconds=40))
        self.assertEqual(self._stored(job)["return_wait_s"], 40)
        self._parked_until(job, due + timedelta(minutes=5))
        self._starts(job, due + timedelta(minutes=5, seconds=5))
        self.assertEqual(self._stored(job)["return_wait_s"], 45)

    def test_a_start_that_is_no_return_waited_0(self):
        """A first start, or a checkpoint not parked: 0, not empty."""
        job = self._running(state="pending")
        self._starts(job, fields.Datetime.now())
        self.assertEqual(self._stored(job)["return_wait_s"], 0)

    def test_back_before_it_was_due_counts_nothing(self):
        """Taken up early (a worker that died): never below 0."""
        job = self._running(state="pending", parked=True)
        due = fields.Datetime.now().replace(microsecond=0)
        self._parked_until(job, due + timedelta(seconds=30))
        self._starts(job, due)
        self.assertEqual(self._stored(job)["return_wait_s"], 0)

    def test_a_hidden_job_is_not_measured(self):
        job = self._running(state="pending", parked=True, code="instance_health")
        self._starts(job, fields.Datetime.now() + timedelta(minutes=1))
        self.assertIsNone(self._stored(job)["return_wait_s"])

    def test_a_measure_that_fails_does_not_keep_the_job_from_starting(self):
        """In a savepoint: the job is ``started`` all the same."""
        job = self._running(state="pending", parked=True)
        with patch.object(
            type(self.env["queue.job"]), "_late_return_s", autospec=True,
            side_effect=ValueError("boom"),
        ), self.assertLogs(queue_job_ext.__name__, level="WARNING"):
            self._starts(job, fields.Datetime.now() + timedelta(minutes=1))
        job.invalidate_recordset()
        self.assertEqual(job.state, "started")
        self.assertIsNone(self._stored(job)["return_wait_s"])
