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
import contextlib
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.sql_db import Callbacks
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

    def test_a_channel_created_during_a_count_does_not_break_it(self):
        """With workers = 0 the threads share a manager: one may create a
        channel (``resolve_channel``) while another walks the children."""
        manager = channel_manager("root:3,root.user:1,root.race:1")
        race = manager.get_channel_by_name("root.race")
        Channel = type(race)

        class _Racing(Channel):
            """A channel whose capacity, read, has another thread create one."""

            @property
            def capacity(self):
                """Create a sibling, as another request would, then answer."""
                manager.get_channel_by_name("root.late", parent_fallback=True)
                return self.__dict__["capacity"]

        race.__class__ = _Racing
        try:
            loads = channel_loads(manager, ["root.user", "root.race"])
        finally:
            race.__class__ = Channel
        self.assertEqual(loads[manager.get_channel_by_name("root")], 2)

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
        parked=False, code="rebuild_instance", next_uuid=None,
    ):
        """A job of the panel whose queue.job is in *state*, in *channel*.

        :param parked: give it the checkpoint of a job parked between two
            attempts
        :param str next_uuid: the queue job of the step of its chain that
            waits for it
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
            "dependencies": {
                "depends_on": [],
                "reverse_depends_on": [next_uuid] if next_uuid else [],
            },
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

    def _held(self, inst, since):
        """A job held by the gate since *since*."""
        return self._job(
            "restart_instance", state="queued", inst=inst, channel_wait_since=since,
        )

    @contextlib.contextmanager
    def _committed(self):
        """Gather what the block registers for after the commit, then run it.

        The test's cursor never commits: its ``postcommit`` is swapped for
        callbacks of its own, run once the block ends, with the fresh
        dispatcher recorded instead of run.

        :return: the queues served, in order, filled once the block ends
        """
        cr = self.env.cr
        original = cr.postcommit
        cr.postcommit = armed = Callbacks()
        served = []
        try:
            yield served
        finally:
            cr.postcommit = original
        self.env.flush_all()
        with patch.object(
            cloud_job_module, "_dispatch_scope_fresh",
            side_effect=lambda _registry, scope, refused=None:
                served.append(scope) or False,
        ):
            armed.run()


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

    @contextlib.contextmanager
    def _unseen_by_fresh_reads(self):
        """Hide from the gate's fresh reads the queue jobs the block creates.

        In registry test mode a fresh cursor rides on the test's
        transaction and sees what it wrote; a real one does not see what
        the transaction has not committed. Renamed for the read, they are
        not found, as there.
        """
        self.env.flush_all()
        self.env.cr.execute("SELECT coalesce(max(id), 0) FROM queue_job")
        since = self.env.cr.fetchone()[0]
        Job = type(self.env["cloud.job"])
        original = Job._fresh_env

        @contextlib.contextmanager
        def _fresh_env(job):
            """The fresh environment, without this transaction's queue jobs."""
            with original(job) as env:
                env.cr.execute(
                    "UPDATE queue_job SET uuid = 'unseen-' || uuid"
                    " WHERE id > %s RETURNING id", (since,),
                )
                hidden = tuple(row[0] for row in env.cr.fetchall())
                try:
                    yield env
                finally:
                    if hidden:
                        env.cr.execute(
                            "UPDATE queue_job SET uuid = substr(uuid, 8)"
                            " WHERE id IN %s", (hidden,),
                        )

        with patch.object(Job, "_fresh_env", _fresh_env):
            yield

    def test_two_jobs_asked_in_one_transaction_take_one_slot(self):
        """The second counts the first, which no fresh cursor sees yet."""
        third = self._instance("gate-third")
        with self._unseen_by_fresh_reads():
            first = self._asked(self.other)
            second = self._asked(third)
        self._assert_dispatched(first)
        self._assert_held(second)

    def _probes(self, count=3, state="pending"):
        """*count* probes in ``root.bg``, due and not started."""
        for _probe in range(count):
            self._running(
                channel="root.bg", code="instance_health", inst=False, state=state,
            )

    def _held_since(self, minutes):
        """A restart held *minutes* ago by the gate, then served again."""
        job = self._asked()
        self._assert_held(job)
        job.with_context(ic_skip_state_hook=True).write({
            "channel_wait_since": fields.Datetime.now() - timedelta(minutes=minutes),
        })
        self.env.flush_all()
        self.Job._dispatch_next(*job._scope_of(), channel_lock=True)
        job.invalidate_recordset()
        return job

    def test_held_two_minutes_a_job_stops_waiting_for_the_probes_due(self):
        """``root:1``: past the patience, probes not started do not count."""
        self.channels = "root:1,root.user:1,root.bg:1"
        self._probes()
        self._assert_dispatched(self._held_since(3))

    def test_within_the_patience_the_probes_due_count(self):
        """Under two minutes it still waits for them."""
        self.channels = "root:1,root.user:1,root.bg:1"
        self._probes()
        self._assert_held(self._held_since(1))

    def test_past_the_patience_a_probe_that_runs_still_counts(self):
        """Only the probes not started stop counting."""
        self.channels = "root:1,root.user:1,root.bg:1"
        self._probes(count=1, state="started")
        self._assert_held(self._held_since(3))

    def test_past_the_patience_a_parked_job_keeps_its_place(self):
        """Its place is never given up."""
        self.channels = "root:1,root.user:1,root.bg:1"
        self._probes()
        self._running(
            state="pending", eta=fields.Datetime.now() + timedelta(seconds=5),
            parked=True,
        )
        self._assert_held(self._held_since(3))

    def test_a_list_of_held_jobs_counts_the_channels_once(self):
        """One fresh read for the whole list, however many are held."""
        self._running()
        held = self._asked()
        Job = type(self.env["cloud.job"])

        def _reads(jobs):
            """Fresh reads ``_format`` and ``_format_history`` make for *jobs*."""
            original = Job._fresh_env
            with patch.object(
                Job, "_fresh_env", autospec=True, side_effect=original,
            ) as fresh:
                jobs._format()
                jobs._format_history()
            return fresh.call_count

        one = _reads(held)
        for name in ("gate-a", "gate-b", "gate-c"):
            held |= self._asked(self._instance(name))
        self.assertEqual(_reads(held), one)

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


class TestWhatTheListsShow(_GateCase):
    """A parked job says when the panel reconnects, in the lists and the history."""

    def test_a_parked_job_on_its_server(self):
        """Its step runs apart: on the server, and the time it comes back."""
        job = self._running(state="pending", parked=True)
        row = self.env["cloud.job.checkpoint"].sudo().search([("job_id", "=", job.id)])
        row.detached_started = True
        until = fields.Datetime.to_string(row.parked_until)
        for formatted in (job._format()[0], job._format_history()[0]):
            self.assertEqual(formatted["state"], "started")
            self.assertTrue(formatted["on_server"])
            self.assertEqual(formatted["parked_until"], until)

    def test_parked_between_two_steps_is_not_on_the_server(self):
        """Nothing runs apart: only the time it comes back."""
        job = self._running(state="pending", parked=True)
        formatted = job._format()[0]
        self.assertFalse(formatted["on_server"])
        self.assertTrue(formatted["parked_until"])

    def test_a_job_that_runs_or_a_closed_row_is_not_parked(self):
        """Taken up again, or kept by a closing hook: no time to show."""
        running = self._running()
        kept = self._running(state="pending", parked=True)
        self.env["cloud.job.checkpoint"].sudo().search(
            [("job_id", "=", kept.id)],
        ).closed = True
        for job in running | kept:
            formatted = job._format()[0]
            self.assertFalse(formatted["on_server"])
            self.assertIsNone(formatted["parked_until"])

    def test_the_history_shows_the_held_job_too(self):
        """Its channel, as the lists show it."""
        self._running()
        held = self._asked()
        self._assert_held(held)
        self.assertEqual(held._format_history()[0]["channel_wait"]["channel"], "root.user")


class TestTheWakeUp(_GateCase):
    """When a place frees, the jobs held wait no longer than the sweep."""

    def test_the_held_are_served_by_priority_then_age(self):
        """Production first, as queue_job's priorities order them."""
        prod = self._instance("gate-prod", environment="production")
        now = fields.Datetime.now()
        older = self._held(self.other, now - timedelta(minutes=5))
        urgent = self._held(prod, now - timedelta(minutes=1))
        newer = self._held(self.inst, now)
        with self._committed() as served:
            self.Job._wake_channel_waiters()
        self.assertEqual(
            served, [urgent._scope_of(), older._scope_of(), newer._scope_of()],
        )

    def test_a_job_held_while_the_ending_one_ran_is_served(self):
        """Looked for once the end commits: the runner's snapshot is older."""
        running = self._running()
        with self._committed() as served:
            self._end(running)
            late = self._held(self.other, fields.Datetime.now())
        self.assertIn(late._scope_of(), served)

    def test_the_held_go_before_the_queue_of_the_job_that_ended(self):
        """A staging's next job does not take a slot a production waits for."""
        prod = self._instance("gate-prod", environment="production")
        running = self._running()
        self._job("restart_instance", state="queued", inst=self.inst)
        held = self._held(prod, fields.Datetime.now() - timedelta(minutes=1))
        with self._committed() as served:
            self._end(running)
        self.assertEqual(served[:2], [held._scope_of(), running._scope_of()])

    def test_once_a_channel_refuses_its_other_held_jobs_are_not_tried(self):
        """``root.user`` full: the first held finds no slot, the rest wait."""
        self._running()
        now = fields.Datetime.now()
        for name in ("gate-a", "gate-b", "gate-c"):
            self._held(self._instance(name), now)
        self.env.flush_all()
        Job = type(self.env["cloud.job"])
        original = Job._dispatch_next
        # The fresh dispatcher's environment does not carry the test's
        # ``ic_channel_gate``.
        with patch.object(
            Job, "_dispatch_next", autospec=True, side_effect=original,
        ) as dispatch, patch.object(Job, "_channel_gate_on", return_value=True):
            cloud_job_module._serve_held_fresh(self.env.registry)
        self.assertEqual(dispatch.call_count, 1)

    def test_a_step_that_hands_its_slot_to_the_next_wakes_nobody(self):
        """Done, the next step of its chain takes the slot it leaves."""
        self._running(state="wait_dependencies", inst=self.other)
        first = self._running(next_uuid=f"gate-qj-{self._seq}")
        Job = type(self.env["cloud.job"])
        with patch.object(Job, "_wake_channel_waiters", autospec=True) as wake:
            self._end(first)
        wake.assert_not_called()

    def test_a_step_whose_next_is_elsewhere_or_that_failed_wakes_them(self):
        """Another channel, or no next step to run: the slot is free."""
        self._running(state="wait_dependencies", inst=self.other, channel="root.bg")
        elsewhere = self._running(next_uuid=f"gate-qj-{self._seq}")
        self._running(state="wait_dependencies", inst=self.other)
        failed = self._running(next_uuid=f"gate-qj-{self._seq}")
        Job = type(self.env["cloud.job"])
        for job, state in ((elsewhere, "done"), (failed, "failed")):
            with patch.object(Job, "_wake_channel_waiters", autospec=True) as wake:
                self._end(job, state=state)
            wake.assert_called()

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
