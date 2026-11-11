"""The dispatcher keeps a parked job's place in its channel.

A job parked between two attempts goes back to ``pending`` with an
``eta``, and queue_job's runner stops counting it: in the 5 s before it
comes back the runner could give its place to another job of the channel,
which the parked job would then wait for. So the panel hands a job to
queue_job only while its channel has room, counting its parked jobs as
running (``cloud.job._channel_admits``); a job held back waits ``queued``
with ``channel_wait_since``, and is served first, by priority and age,
when a place frees.

The gate is open in tests unless a test asks for it (``ic_channel_gate``):
without a runner every job dispatched stays ``pending`` and would fill the
channels of every other test.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import BaseCase

from ..models import cloud_job as cloud_job_module
from ..models._job_channels import (
    channel_loads,
    channel_manager,
    channel_problems,
    full_channel,
)
from .test_job_queue import _QueueCase


class TestChannelLoads(BaseCase):
    """The channels as queue_job's runner reads them, and their load."""

    def test_a_full_child_lets_through_only_its_capacity(self):
        """What waits in a full channel does not reach the one above."""
        manager = channel_manager("root:3,root.user:5,root.bg:1")
        loads = channel_loads(manager, ["root.bg"] * 3)
        self.assertEqual(loads[manager.get_channel_by_name("root.bg")], 3)
        self.assertEqual(loads[manager.get_channel_by_name("root")], 1)
        self.assertIsNone(full_channel(manager, "root.user", ["root.bg"] * 3))

    def test_the_first_full_channel_on_the_way_to_root_says_why(self):
        """Its name, its load and its capacity."""
        manager = channel_manager("root:2,root.user:1,root.bg:3")
        self.assertEqual(
            full_channel(manager, "root.user", ["root.user"]), ("root.user", 1, 1),
        )
        self.assertEqual(
            full_channel(manager, "root.user", ["root.bg", "root.bg"]), ("root", 2, 2),
        )

    def test_a_channel_not_configured_counts_under_its_parent(self):
        """Created under the nearest configured one, without capacity."""
        manager = channel_manager("root:1,root.user:4")
        self.assertIsNone(full_channel(manager, "root.user.x", []))
        self.assertEqual(
            full_channel(manager, "root.user.x", ["root.user.y"]), ("root", 1, 1),
        )

    def test_a_capacity_of_0_is_unlimited(self):
        """As the runner reads it."""
        manager = channel_manager("root:0,root.user:0")
        self.assertIsNone(full_channel(manager, "root.user", ["root.user"] * 9))

    def test_the_channels_that_make_a_parked_job_wait(self):
        """``root.user`` without capacity of its own, and other modules' jobs."""
        self.assertEqual(channel_problems("root:3,root.user:1,root.bg:1"), [])
        problems = channel_problems("root:1", foreign={"root.mail", "root"})
        self.assertEqual(len(problems), 2)
        self.assertIn("root.user no capacity of its own", problems[0])
        self.assertIn("root, root.mail", problems[1])
        self.assertEqual(channel_problems("user:1"), [])


class _GateCase(_QueueCase):
    """The queue fixtures, with the gate on and channels of Odoo.sh's kind."""

    channels = "root:3,root.user:1,root.bg:1"

    def setUp(self):
        """Channels, the gate asked for, and no job of the snapshot running."""
        super().setUp()
        self.env.cr.execute(
            "UPDATE queue_job SET state = 'done'"
            " WHERE state IN ('pending', 'enqueued', 'started')"
        )
        self.Job = self.env["cloud.job"].with_context(ic_channel_gate=True)
        self.startPatcher(patch.object(
            cloud_job_module, "configured_channels",
            side_effect=lambda: self.channels,
        ))
        self.other = self._instance("gate-other")
        self._seq = 0

    def _running(
        self, inst=None, channel="root.user", state="started", eta=None,
        parked=False, code="rebuild_instance",
    ):
        """A job of the panel whose queue.job is in *state*, in *channel*.

        :param parked: give it the checkpoint of a job parked between two
            attempts
        :return: the ``cloud.job``
        """
        self._seq += 1
        qjob = self.env["queue.job"].sudo().create({
            "uuid": f"gate-qj-{self._seq}",
            "name": "gate",
            "state": state,
            "channel": channel,
            "eta": eta,
            "method_name": "execute",
            "model_name": "cloud.job",
            "func_string": "execute()",
        })
        job = self._job(
            code, state="started", inst=inst if inst is not None else self.inst,
            queue_job_uuid=qjob.uuid,
        )
        if parked:
            self.env["cloud.job.checkpoint"].sudo().create({
                "job_id": job.id,
                "queue_job_uuid": qjob.uuid,
                "parked": True,
                "parked_until": fields.Datetime.now() + timedelta(seconds=5),
            })
        self.env.flush_all()
        return job

    def _asked(self, inst=None):
        """``enqueue`` a restart of *inst* (``root.user``) through the gate."""
        inst = inst if inst is not None else self.other
        return self.Job.browse(self.Job.enqueue(
            self.host.id, inst.id, "restart_instance",
        ))

    def _assert_held(self, job):
        """*job* waits for a place: queued, dated, not handed to queue_job."""
        self.assertEqual(job.state, "queued")
        self.assertTrue(job.channel_wait_since)
        self.assertFalse(job.queue_job_uuid)

    def _assert_dispatched(self, job):
        """*job* was handed to queue_job, its wait forgotten."""
        self.assertEqual(job.state, "pending")
        self.assertTrue(job.queue_job_uuid)
        self.assertFalse(job.channel_wait_since)


class TestTheGate(_GateCase):
    """What takes a place in a channel, and what does not."""

    def test_a_free_channel_dispatches(self):
        """Nothing runs: the job goes to queue_job at once."""
        self._assert_dispatched(self._asked())

    def test_a_full_channel_holds_the_job(self):
        """``root.user:1`` taken: the next one waits, queued."""
        self._running()
        self._assert_held(self._asked())

    def test_a_parked_job_keeps_its_place(self):
        """``pending`` with an ``eta`` ahead, and its checkpoint: it counts."""
        self._running(
            state="pending", eta=fields.Datetime.now() + timedelta(seconds=5),
            parked=True,
        )
        self._assert_held(self._asked())

    def test_a_job_that_sleeps_takes_no_place(self):
        """``pending`` with an ``eta`` ahead and not parked: waiting for a lock."""
        self._running(
            state="pending", eta=fields.Datetime.now() + timedelta(minutes=30),
        )
        self._assert_dispatched(self._asked())

    def test_a_job_due_takes_a_place(self):
        """``pending`` with no ``eta``, or one gone by: the runner starts it next."""
        self._running(state="pending")
        self._assert_held(self._asked())

    def test_a_step_that_waits_for_the_one_before_takes_no_place(self):
        """``wait_dependencies``: it takes the place its previous step leaves."""
        self._running(state="wait_dependencies")
        self._assert_dispatched(self._asked())

    def test_probes_count_where_they_run(self):
        """A hidden job in ``root.bg`` fills ``root:1``."""
        self.channels = "root:1,root.user:1,root.bg:1"
        self._running(channel="root.bg", code="instance_health")
        job = self._asked()
        self._assert_held(job)
        self.assertEqual(job._channel_wait()["channel"], "root")

    def test_what_waits_in_a_full_channel_does_not_fill_the_one_above(self):
        """Three in ``root.bg:1`` take one place of ``root``."""
        self.channels = "root:3,root.user:5,root.bg:1"
        for _probe in range(3):
            self._running(channel="root.bg", code="instance_health", inst=False)
        self._assert_dispatched(self._asked())

    def test_the_switch_turns_the_gate_off(self):
        """``[incubacloud] channel_gate = off``: dispatched as before."""
        self._running()
        with patch.object(
            cloud_job_module, "conf_section", return_value={"channel_gate": "off"},
        ):
            self._assert_dispatched(self._asked())

    def test_the_gate_is_open_in_tests_unless_asked_for(self):
        """Without the context key, a full channel dispatches."""
        self._running()
        self.Job = self.env["cloud.job"]
        self._assert_dispatched(self._asked())

    def test_the_fast_path_takes_no_lock_and_the_fresh_dispatcher_does(self):
        """``enqueue`` in the request's transaction; the dispatcher after a commit."""
        Job = type(self.env["cloud.job"])
        original = Job._channel_admits
        with patch.object(
            Job, "_channel_admits", autospec=True, side_effect=original,
        ) as admits:
            first = self._running()
            held = self._asked()
            self.assertFalse(admits.call_args.kwargs.get("lock", False))
            self._assert_held(held)
            self._end(first)
            self.env.flush_all()
            self.assertTrue(cloud_job_module._dispatch_scope_fresh(
                self.env.registry, held._scope_of(),
            ))
            self.assertTrue(admits.call_args.kwargs.get("lock"))
        held.invalidate_recordset()
        self._assert_dispatched(held)

    def test_what_the_held_job_shows(self):
        """Its channel, the load and the capacity, and since when."""
        self._running()
        job = self._asked()
        wait = job._channel_wait()
        self.assertEqual(
            {k: wait[k] for k in ("channel", "load", "capacity")},
            {"channel": "root.user", "load": 1, "capacity": 1},
        )
        formatted = job._format()[0]["channel_wait"]
        self.assertEqual(formatted["channel"], "root.user")
        self.assertEqual(
            formatted["since"], fields.Datetime.to_string(job.channel_wait_since),
        )


class TestTheWakeUp(_GateCase):
    """When a place frees, the jobs held wait no longer than the sweep."""

    def _held(self, inst, since):
        """A job held by the gate since *since*."""
        return self._job(
            "restart_instance", state="queued", inst=inst, channel_wait_since=since,
        )

    def test_the_held_are_served_by_priority_then_age(self):
        """Production first, as queue_job's priorities order them."""
        prod = self._instance("gate-prod", environment="production")
        now = fields.Datetime.now()
        older = self._held(self.other, now - timedelta(minutes=5))
        urgent = self._held(prod, now - timedelta(minutes=1))
        newer = self._held(self.inst, now)
        Job = type(self.env["cloud.job"])
        with patch.object(Job, "_schedule_dispatch", autospec=True) as schedule:
            self.Job._wake_channel_waiters()
        self.assertEqual(
            list(schedule.call_args.args[1]),
            [urgent._scope_of(), older._scope_of(), newer._scope_of()],
        )

    def test_the_sweep_serves_them_in_the_same_order(self):
        """``_cron_dispatch_queued`` goes through the queues in that order."""
        prod = self._instance("gate-prod", environment="production")
        now = fields.Datetime.now()
        older = self._held(self.other, now - timedelta(minutes=5))
        urgent = self._held(prod, now - timedelta(minutes=1))
        served = []
        with patch.object(
            cloud_job_module, "_dispatch_scope_fresh",
            side_effect=lambda _registry, scope: served.append(scope) or False,
        ):
            self.Job._cron_dispatch_queued()
        self.assertEqual(served[:2], [urgent._scope_of(), older._scope_of()])

    def test_the_end_of_a_job_wakes_them(self):
        """Whatever ended: the place it held is free."""
        running = self._running()
        Job = type(self.env["cloud.job"])
        with patch.object(Job, "_wake_channel_waiters", autospec=True) as wake:
            self._end(running)
        wake.assert_called()

    def test_a_postpone_that_is_not_a_park_wakes_them(self):
        """A job that sleeps leaves its place; a parked one keeps it."""
        sleeping = self._running()
        parked = self._running(inst=self.other, parked=True)
        Job = type(self.env["cloud.job"])
        with patch.object(Job, "_wake_channel_waiters", autospec=True) as wake:
            parked.queue_job_id.write({
                "state": "pending",
                "eta": fields.Datetime.now() + timedelta(seconds=5),
            })
            wake.assert_not_called()
            sleeping.queue_job_id.write({
                "state": "pending",
                "eta": fields.Datetime.now() + timedelta(minutes=30),
            })
            wake.assert_called()


class TestAParkedJobThatDoesNotComeBack(_GateCase):
    """Its place is kept; past 600 s it says so, and counts as slow."""

    def setUp(self):
        """A rebuild parked 11 minutes ago, its queue.job still waiting."""
        super().setUp()
        self.parked = self._running(
            state="pending", eta=fields.Datetime.now() - timedelta(minutes=11),
            parked=True,
        )
        self._row().write({
            "parked_until": fields.Datetime.now() - timedelta(minutes=11),
        })
        self.env.flush_all()

    def _row(self):
        """The parked job's checkpoint."""
        return self.env["cloud.job.checkpoint"].sudo().search(
            [("job_id", "=", self.parked.id)],
        )

    def _lines(self):
        """The parked job's log lines about the wait."""
        return [
            line for line in self.env["cloud.job.log.chunk"].search(
                [("job_id", "=", self.parked.id)],
            ).mapped("content")
            if "waiting for a free slot" in line
        ]

    def test_it_still_holds_its_place(self):
        """No expiry (10-nov-2026): the next job waits all the same."""
        self._assert_held(self._asked())

    def test_it_says_once_that_it_waits_and_raises_the_slow_job_warning(self):
        """One line in its log; the alert names the channel and the time."""
        self.Job._cron_watch_long_running()
        self.Job._cron_watch_long_running()
        self.assertEqual(len(self._lines()), 1)
        self.assertIn("root.user", self._lines()[0])
        alert = self.env["cloud.alert"].sudo().search([
            ("code", "=", self.Job._LONG_RUNNING_ALERT_CODE),
            ("state", "=", "active"),
            ("job_id", "=", self.parked.id),
        ])
        self.assertEqual(len(alert), 1)
        self.assertIn("waits for a free slot in root.user since", alert.message)

    def test_back_in_time_says_nothing(self):
        """Within 600 s of its return it is not late."""
        self._row().write({
            "parked_until": fields.Datetime.now() - timedelta(minutes=5),
        })
        self.Job._cron_watch_long_running()
        self.assertEqual(self._lines(), [])


class TestTheAttemptIsWatched(_GateCase):
    """The slow job warning measures the attempt against the server's limit."""

    def _attempt(self, minutes):
        """A job whose current attempt started *minutes* ago."""
        job = self._running()
        job.queue_job_id.sudo().write({
            "date_started": fields.Datetime.now() - timedelta(minutes=minutes),
        })
        self.env.flush_all()
        return job

    def _alert(self, job):
        """The slow job alert of *job*, if active."""
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", self.Job._LONG_RUNNING_ALERT_CODE),
            ("state", "=", "active"),
            ("job_id", "=", job.id),
        ])

    def test_three_quarters_of_the_limit(self):
        """900 s: past 675 s of the attempt; the text names the limit."""
        slow = self._attempt(12)
        fine = self._attempt(10)
        Job = type(self.env["cloud.job"])
        with patch.object(Job, "_server_limit_time_real", return_value=900):
            self.Job._cron_watch_long_running()
        self.assertTrue(self._alert(slow))
        self.assertFalse(self._alert(fine))
        self.assertIn("900 s", self._alert(slow).message)

    def test_a_parked_job_s_attempt_is_not_measured_from_its_creation(self):
        """Between two attempts there is no attempt to measure."""
        job = self._running(
            state="pending", eta=fields.Datetime.now() + timedelta(seconds=5),
            parked=True,
        )
        job.flush_recordset()
        self.env.cr.execute(
            "UPDATE cloud_job SET create_date = create_date - interval '3 hours'"
            " WHERE id = %s", (job.id,),
        )
        self.Job.invalidate_model(["create_date"])
        Job = type(self.env["cloud.job"])
        with patch.object(Job, "_server_limit_time_real", return_value=900):
            self.Job._cron_watch_long_running()
        self.assertFalse(self._alert(job))


class TestOtherModulesInTheQueue(_GateCase):
    """The channels other modules' queue jobs ran in, for the startup warning."""

    def test_their_channels_are_found(self):
        """A queue.job of no cloud job, lately."""
        self.env["queue.job"].sudo().create({
            "uuid": "gate-foreign", "name": "foreign", "state": "done",
            "method_name": "x", "model_name": "res.partner", "func_string": "x()",
        })
        self._running()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE queue_job SET channel = 'root.mail',"
            " date_created = (now() at time zone 'UTC') WHERE uuid = 'gate-foreign'"
        )
        foreign = self.env["cloud.settings"]._foreign_job_channels()
        self.assertIn("root.mail", foreign)
        self.assertNotIn("root.user", foreign)
