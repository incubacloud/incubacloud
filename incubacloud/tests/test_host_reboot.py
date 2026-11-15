"""The server's restart for its updates (``cloud_host_reboot``, 1.0.229).

The server no longer restarts by itself; the panel checks it, restarts it
inside its window once nothing of the panel runs there, holds every queue
of the server while it does, and waits for it to come back. The
transport is a ``MagicMock(spec=SSHTransport)`` answering each order as
the script would.
"""
import asyncio
import contextlib
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests import tagged

from odoo.addons.queue_job.exception import RetryableJobError

from ..models.abstract_executor import JobParked
from ..models.cloud_host import CloudHost
from ..models.cloud_host_reboot import (
    REBOOT_FAILED_ALERT,
    REBOOT_INCOMPLETE_ALERT,
    REBOOT_PENDING_ALERT,
    REBOOT_PENDING_ALERT_DAYS,
    parse_marks,
)
from ..models.host_reboot_executor import (
    EXIT_VETO,
    RETURN_DEADLINE_S,
    HostRebootCheckExecutor,
    HostRebootExecutor,
)
from ..models.instance_health_executor import InstanceHealthExecutor
from ..models.restart_instance_executor import RestartInstanceExecutor
from ..models.transport import CommandResult, SSHTransport
from .test_cloud_job import _run_after_commit
from .test_job_queue import _QueueCase

_BOOT = "082e0387-f50e-4ec6-86cd-dc52901eb9eb"
_NEW_BOOT = "da1fb0f5-0000-4000-8000-000000000001"
#: What the panel's process reads of its own boot: never the servers'.
_PANEL_BOOT = "panel-boot"


_CLEAN = f"IC_BOOT_ID={_NEW_BOOT}\nIC_REBOOT_REQUIRED=0\nIC_DOCKER=active\n"


def _transport(boot=_BOOT, guard=(0, "IC_VETO=\n"), reboot=(0, "IC_SCHEDULED=1\n"),
               scheduled=False, verify=_CLEAN, quiet="0\n"):
    """A transport answering the orders of the restart.

    :param str boot: what the server says of its boot
    :param tuple guard: ``(exit, stdout)`` of ``host_reboot.sh guard``
    :param tuple reboot: ``(exit, stdout)`` of ``host_reboot.sh reboot``
    :param bool scheduled: the restart is still scheduled on the server
    :param str verify: what ``host_reboot.sh verify`` prints
    :param str quiet: what the count inside an instance prints
    """
    transport = MagicMock(spec=SSHTransport)

    async def _run(command, timeout=None):
        if "boot_id" in command:
            out = boot + "\n" + ("IC_SCHEDULED=1\n" if scheduled else "")
            return CommandResult(out, 0)
        if "move_steps.sh quiet" in command:
            return CommandResult(quiet, 0)
        return CommandResult("", 0)

    async def _execute(command, on_stdout, on_stderr, timeout=None):
        if "host_reboot.sh guard" in command:
            return CommandResult(guard[1], guard[0])
        if "host_reboot.sh reboot" in command:
            return CommandResult(reboot[1], reboot[0])
        if "host_reboot.sh prepare" in command:
            return CommandResult("IC_CONTAINER=proxy\nIC_CONTAINER=queue-inst-db-1\n", 0)
        if "host_reboot.sh verify" in command:
            return CommandResult(verify, 0)
        return CommandResult("", 0)

    transport.run.side_effect = _run
    transport.execute.side_effect = _execute
    return transport


class _RebootCase(_QueueCase):
    """The queue's host, hardened, restarted by the panel, its window open."""

    def setUp(self):
        super().setUp()
        self.host.write({"hardened": True, "auto_security_updates": True})
        boot = patch(
            "odoo.addons.incubacloud.models.host_reboot_executor.panel_boot_id",
            return_value=_PANEL_BOOT,
        )
        boot.start()
        self.addCleanup(boot.stop)
        # 05:00 UTC: inside the default window (04:00, 3 h, UTC).
        self.inside = datetime(2026, 11, 15, 5, 0)
        self.Reboot = self.env["cloud.host.reboot"]

    def _reboot_job(self, state="started", host=None):
        """A restart job of *host*, in *state*."""
        return self._job("host_reboot", state=state, inst=False, host=host)

    def _hold(self, job, holds_panel=False, shutdown_at=False):
        """The row of *job*'s hold."""
        return self.Reboot.create({
            "job_id": job.id,
            "host_id": job.host_id.id,
            "holds_panel": holds_panel,
            "boot_id": _BOOT,
            "shutdown_at": shutdown_at,
        })

    def _said(self, job, text):
        """Whether *job*'s log has a line with *text*."""
        self.env.flush_all()
        return bool(self.env["cloud.job.log.chunk"].search_count([
            ("job_id", "=", job.id), ("content", "ilike", text),
        ]))

    def _executor(self, cls, job):
        """*cls* bound to *job*."""
        return cls(job_record=job, host_record=job.host_id)

    def _other_host(self):
        """A second host, with an instance of its own."""
        host = self.env["cloud.host"].create({
            "name": "reboot-other",
            "ip_address": "10.0.0.82",
            "user": "ubuntu",
            "wildcard_domain": "other.example.com",
            "status": "compatible",
            "traefik_deployed": True,
        })
        inst = self.env["cloud.instance"].create({
            "name": "reboot-other-inst",
            "project_id": self.project.id,
            "host_id": host.id,
            "environment": "staging",
            "state": "deployed",
        })
        return host, inst


@tagged("post_install", "-at_install")
class TestRebootWindow(_RebootCase):
    """The window is the host's, in its time zone."""

    def test_the_default_window_is_four_to_seven_utc(self):
        host = self.host
        self.assertEqual(host.reboot_window_start, 4.0)
        self.assertEqual(host.reboot_window_hours, 3)
        self.assertEqual(host.reboot_tz, "UTC")
        self.assertFalse(host._reboot_window_open(datetime(2026, 11, 15, 3, 59)))
        self.assertTrue(host._reboot_window_open(datetime(2026, 11, 15, 4, 0)))
        self.assertTrue(host._reboot_window_open(datetime(2026, 11, 15, 6, 59)))
        self.assertFalse(host._reboot_window_open(datetime(2026, 11, 15, 7, 0)))

    def test_a_window_across_midnight(self):
        self.host.reboot_window_start = 23.0
        self.assertTrue(self.host._reboot_window_open(datetime(2026, 11, 15, 1, 30)))
        self.assertTrue(self.host._reboot_window_open(datetime(2026, 11, 15, 23, 0)))
        self.assertFalse(self.host._reboot_window_open(datetime(2026, 11, 15, 2, 0)))

    def test_the_zone_and_its_change_of_clock(self):
        """04:00 in Madrid is 02:00 UTC in summer and 03:00 in winter."""
        self.host.reboot_tz = "Europe/Madrid"
        self.assertTrue(self.host._reboot_window_open(datetime(2026, 7, 1, 2, 0)))
        self.assertFalse(self.host._reboot_window_open(datetime(2026, 7, 1, 5, 0)))
        self.assertFalse(self.host._reboot_window_open(datetime(2026, 1, 15, 2, 0)))
        self.assertTrue(self.host._reboot_window_open(datetime(2026, 1, 15, 3, 0)))
        # The night the clock goes back (25-oct-2026, 03:00 CEST → 02:00
        # CET): 04:00 local is 03:00 UTC.
        self.assertTrue(self.host._reboot_window_open(datetime(2026, 10, 25, 3, 0)))

    def test_only_a_hardened_host_with_its_updates_on_is_restarted_by_the_panel(self):
        self.assertTrue(self.host._reboot_by_panel())
        self.host.auto_security_updates = False
        self.assertFalse(self.host._reboot_by_panel())
        self.host.write({"auto_security_updates": True, "hardened": False})
        self.assertFalse(self.host._reboot_by_panel())

    def test_the_check_is_due_every_pass_only_inside_the_window_with_a_restart_pending(self):
        host = self.host
        self.assertTrue(host._reboot_check_due(self.inside))
        host.reboot_checked_at = self.inside - timedelta(hours=1)
        self.assertFalse(host._reboot_check_due(self.inside))
        host.reboot_required_since = self.inside - timedelta(days=1)
        self.assertTrue(host._reboot_check_due(self.inside))
        self.assertFalse(host._reboot_check_due(datetime(2026, 11, 15, 8, 0)))
        host.auto_security_updates = False
        self.assertFalse(host._reboot_check_due(self.inside))
        host.reboot_checked_at = self.inside - timedelta(hours=6)
        self.assertTrue(host._reboot_check_due(self.inside))

    def test_the_cron_queues_one_check_per_due_host(self):
        with patch.object(
            CloudHost, "_ssh_ready_hosts", autospec=True, return_value=self.host,
        ):
            self.env["cloud.host"].cron_host_reboot_check()
            self.env["cloud.host"].cron_host_reboot_check()
        checks = self.Job.search([
            ("host_id", "=", self.host.id),
            ("job_type_id.code", "=", "host_reboot_check"),
        ])
        self.assertEqual(len(checks), 1)


@tagged("post_install", "-at_install")
class TestRebootCheck(_RebootCase):
    """What the check reads, and when it queues the restart."""

    def _check(self, required=True, pkgs="linux-image-6.8.0-90-generic", policy="kept",
               now=None):
        """Run the check's success hook on what the script would print."""
        job = self._job("host_reboot_check", inst=False)
        out = (
            f"IC_BOOT_ID={_BOOT}\nIC_REBOOT_REQUIRED={int(required)}\n"
            f"IC_REBOOT_PKGS={pkgs if required else ''}\nIC_POLICY={policy}\n"
        )
        executor = self._executor(HostRebootCheckExecutor, job)
        results = {"Check": {"stdout": out, "exit_status": 0}}
        self.assertEqual(executor.parse_results(results), [])
        with patch.object(fields.Datetime, "now", return_value=now or self.inside):
            asyncio.run(executor.on_success(results))
        executor._flush_logs()
        return job

    def _queued_reboots(self):
        return self.Job.search([
            ("host_id", "=", self.host.id),
            ("job_type_id.code", "=", "host_reboot"),
        ])

    def test_the_marks_are_read(self):
        marks = parse_marks("noise\nIC_BOOT_ID=a\nIC_REBOOT_PKGS=x y\n")
        self.assertEqual(marks["IC_BOOT_ID"], ["a"])
        self.assertEqual(marks["IC_REBOOT_PKGS"], ["x y"])

    def test_a_pending_restart_is_recorded_once_and_cleared(self):
        self.host.reboot_window_start = 12.0  # closed now: nothing queued
        self._check()
        since = self.host.reboot_required_since
        self.assertEqual(since, self.inside)
        self.assertEqual(self.host.reboot_required_pkgs, "linux-image-6.8.0-90-generic")
        self.assertEqual(self.host.reboot_boot_id, _BOOT)
        self._check(now=self.inside + timedelta(hours=6))
        self.assertEqual(self.host.reboot_required_since, since)
        self._check(required=False)
        self.assertFalse(self.host.reboot_required_since)
        self.assertFalse(self.host.reboot_required_pkgs)
        self.assertFalse(self._queued_reboots())

    def test_inside_the_window_a_free_host_gets_its_restart_once(self):
        self._check()
        self.assertEqual(len(self._queued_reboots()), 1)
        self._check()
        self.assertEqual(len(self._queued_reboots()), 1)

    def test_a_busy_host_is_not_restarted_in_this_pass(self):
        self._job("rebuild_instance")
        job = self._check()
        self.assertFalse(self._queued_reboots())
        self.assertTrue(self._said(job, "Not restarted in this pass"))

    def test_a_host_the_panel_does_not_restart_is_never_queued(self):
        self.host.auto_security_updates = False
        self._check()
        self.assertFalse(self._queued_reboots())
        self.host.write({"auto_security_updates": True, "hardened": False})
        self._check()
        self.assertFalse(self._queued_reboots())

    def test_the_server_s_own_restart_turned_off_is_said(self):
        self.host.reboot_window_start = 12.0
        job = self._check(policy="fixed")
        self.assertTrue(self.env["cloud.job.log.chunk"].search_count([
            ("job_id", "=", job.id), ("content", "ilike", "turned off"),
        ]))

    def test_seven_days_pending_alert_even_without_auto_updates(self):
        """The owner's choice: the alert after a week, auto updates or not."""
        self.host.auto_security_updates = False
        Alert = self.env["cloud.alert"]

        def active():
            return Alert.search([
                ("code", "=", REBOOT_PENDING_ALERT), ("host_id", "=", self.host.id),
                ("state", "=", "active"),
            ])

        self._check()
        self.assertFalse(active())
        self._check(now=self.inside + timedelta(days=REBOOT_PENDING_ALERT_DAYS))
        self.assertTrue(active())
        self._check(required=False, now=self.inside + timedelta(days=8))
        self.assertFalse(active())


@tagged("post_install", "-at_install")
class TestRebootBlockers(_RebootCase):
    """What keeps the panel from restarting a server, one by one."""

    def test_a_free_host_has_none(self):
        self.assertEqual(self.host._reboot_blockers(), [])

    def test_a_job_of_an_instance_on_it(self):
        self._job("rebuild_instance")
        self.assertTrue(self.host._reboot_blockers())

    def test_a_job_of_the_host(self):
        self._job("host_probe", inst=False)
        self.assertTrue(self.host._reboot_blockers())

    def test_the_restart_itself_does_not_count(self):
        job = self._reboot_job()
        self.assertTrue(self.host._reboot_blockers())
        self.assertEqual(self.host._reboot_blockers(exclude_job_id=job.id), [])

    def test_a_job_of_another_host_does_not_count(self):
        other, inst = self._other_host()
        self._job("rebuild_instance", inst=inst, host=other)
        self.assertEqual(self.host._reboot_blockers(), [])
        # The panel's own server waits for the whole fleet.
        self.assertTrue(self.host._reboot_blockers(holds_panel=True))

    def test_a_build_parked_with_the_host_s_lock(self):
        job = self._job("rebuild_instance", state="done")
        self.env["cloud.job.checkpoint"].create({
            "job_id": job.id, "host_id": self.host.id,
            "build_lock_host_id": self.host.id,
        })
        self.assertTrue(self.host._reboot_blockers())

    def test_a_hidden_job_that_works_counts_and_a_probe_does_not(self):
        prune = self._enqueue("docker_prune", inst=False)
        prune.queue_job_id.state = "started"
        self.assertTrue(self.host._reboot_blockers())
        prune.queue_job_id.state = "done"
        metrics = self._enqueue("host_metrics", inst=False)
        metrics.queue_job_id.state = "started"
        self.assertEqual(self.host._reboot_blockers(), [])


@tagged("post_install", "-at_install")
class TestRebootHold(_RebootCase):
    """A held server starts nothing; its end serves every queue it held."""

    def test_nothing_of_the_host_starts_while_it_is_held(self):
        reboot = self._reboot_job()
        self._hold(reboot)
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart.state, "queued")
        self.assertTrue(self._said(restart, "restart of the server"))
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "queued")
        self._end(reboot)
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "pending")

    def test_another_host_is_not_held(self):
        self._hold(self._reboot_job())
        other, inst = self._other_host()
        job_id = self.Job.enqueue(other.id, inst.id, "restart_instance")
        self.assertEqual(self.Job.browse(job_id).state, "pending")

    def test_the_panel_s_own_server_holds_every_queue(self):
        self._hold(self._reboot_job(), holds_panel=True)
        other, inst = self._other_host()
        job_id = self.Job.enqueue(other.id, inst.id, "restart_instance")
        self.assertEqual(self.Job.browse(job_id).state, "queued")

    def test_a_probe_is_not_held(self):
        self._hold(self._reboot_job())
        probe = self._enqueue("instance_health")
        self.assertEqual(probe.state, "pending")

    def test_a_hold_ends_with_its_job_however_it_ends(self):
        for state in ("failed", "cancelled", "done"):
            with self.subTest(state=state):
                reboot = self._reboot_job()
                self._hold(reboot)
                self.assertEqual(self.Job._reboot_hold_of([self.host.id]), reboot.id)
                reboot.state = state
                self.assertEqual(self.Job._reboot_hold_of([self.host.id]), 0)

    def test_a_job_dispatched_before_the_hold_waits_when_it_starts(self):
        restart = self._job("restart_instance", state="pending")
        self._hold(self._reboot_job())
        executor = self._executor(RestartInstanceExecutor, restart)
        with self.assertRaises(RetryableJobError):
            executor.pre_run_checks()
        probe = self._job("instance_health", state="pending")
        self._executor(InstanceHealthExecutor, probe)._defer_while_restarting()

    def test_the_probes_of_the_host_s_instances_skip_their_pass(self):
        self.assertIn("host_reboot", self.Job._get_health_blocking_host_job_types())
        self.assertIn("host_reboot_check", self.Job._get_hidden_job_types())


class _RebootJobCase(_RebootCase):
    """Runs the restart's attempts against a transport that answers."""

    def _run_before(self, job, transport):
        """Run the first attempt's orders; return what it raised."""
        executor = self._executor(HostRebootExecutor, job)
        try:
            asyncio.run(executor.before_execute(transport))
        except Exception as exc:  # noqa: BLE001 — the test reads it
            return executor, exc
        return executor, None

    def _row(self, job):
        return self.Reboot.search([("job_id", "=", job.id)])

    @contextlib.contextmanager
    def _connect(self, transport=None, error=None):
        """``get_transport`` yields *transport*, or raises *error*."""

        @contextlib.asynccontextmanager
        async def _open(_host):
            if error is not None:
                raise error
            yield transport

        with patch.object(CloudHost, "get_transport", autospec=True, side_effect=_open):
            yield

    def _wait(self, job, seconds, transport=None, error=None):
        """Run a waiting attempt *seconds* after the order; return what it raised."""
        row = self._hold(job, shutdown_at=self.inside)
        executor = self._executor(HostRebootExecutor, job)
        values = executor._row_values()
        self.assertEqual(values["id"], row.id)
        now = self.inside + timedelta(seconds=seconds)
        with self._connect(transport, error), patch.object(
            fields.Datetime, "now", return_value=now,
        ):
            try:
                asyncio.run(executor._wait_for_return(values))
            except Exception as exc:  # noqa: BLE001 — the test reads it
                return executor, exc
            finally:
                # The hooks wrote on a cursor of their own.
                self.env.invalidate_all()
        return executor, None

    def _failed_alert(self):
        return self.env["cloud.alert"].search([
            ("code", "=", REBOOT_FAILED_ALERT), ("host_id", "=", self.host.id),
            ("state", "=", "active"),
        ])


@tagged("post_install", "-at_install")
class TestRebootJob(_RebootJobCase):
    """The restart's attempts, against a transport that answers."""

    def test_a_free_host_is_held_restarted_and_the_job_parks(self):
        job = self._reboot_job()
        transport = _transport()
        executor, raised = self._run_before(job, transport)
        self.assertIsInstance(raised, JobParked)
        row = self._row(job)
        self.assertTrue(row.shutdown_at)
        self.assertEqual(row.boot_id, _BOOT)
        self.assertEqual(row._containers(), ["proxy", "queue-inst-db-1"])
        self.assertFalse(row.holds_panel)
        orders = [c.args[0] for c in transport.execute.call_args_list]
        self.assertTrue(any("host_reboot.sh guard" in o for o in orders))
        self.assertTrue(any("host_reboot.sh reboot" in o for o in orders))
        # Past its point of no return: a stop is refused from here.
        self.assertTrue(self.env["cloud.job.no.return"].search([("job_id", "=", job.id)]))

    def test_the_server_s_veto_ends_the_job_without_a_restart(self):
        job = self._reboot_job()
        transport = _transport(guard=(EXIT_VETO, "IC_VETO=pg_dump is running\n"))
        executor, raised = self._run_before(job, transport)
        self.assertIsNone(raised)
        self.assertEqual(executor._verdict[0], "skipped")
        self.assertIn("pg_dump", executor._verdict[1])
        orders = [c.args[0] for c in transport.execute.call_args_list]
        self.assertFalse(any("host_reboot.sh reboot" in o for o in orders))
        self.assertFalse(self._row(job).shutdown_at)
        self.assertFalse(self.env["cloud.job.no.return"].search([("job_id", "=", job.id)]))
        self.assertEqual(executor.parse_results({}), [])

    def test_a_busy_host_is_not_held(self):
        job = self._reboot_job()
        self._job("rebuild_instance")
        executor, raised = self._run_before(job, _transport())
        self.assertIsNone(raised)
        self.assertEqual(executor._verdict[0], "skipped")
        self.assertFalse(self._row(job))

    def test_the_panel_s_own_server_holds_every_queue(self):
        job = self._reboot_job()
        with patch(
            "odoo.addons.incubacloud.models.host_reboot_executor.panel_boot_id",
            return_value=_BOOT,
        ):
            self._run_before(job, _transport())
        self.assertTrue(self._row(job).holds_panel)

    def test_a_restart_not_scheduled_fails_and_lets_go(self):
        job = self._reboot_job()
        executor, raised = self._run_before(job, _transport(reboot=(1, "")))
        self.assertIsInstance(raised, RuntimeError)
        self.assertFalse(self._row(job).shutdown_at)

    def test_back_with_a_new_boot_ends_the_job(self):
        job = self._reboot_job()
        executor, raised = self._wait(job, 95, _transport(boot=_NEW_BOOT))
        self.assertIsNone(raised)
        self.assertEqual(executor._verdict[0], "back")
        self.assertEqual(self.host.reboot_boot_id, _NEW_BOOT)
        self.assertTrue(self.Job.search([
            ("host_id", "=", self.host.id),
            ("job_type_id.code", "=", "host_reboot_check"),
        ]))
        self.assertFalse(self._failed_alert())

    def test_a_server_that_does_not_answer_yet_is_waited_for(self):
        job = self._reboot_job()
        _executor, raised = self._wait(job, 95, error=ConnectionRefusedError())
        self.assertIsInstance(raised, JobParked)
        self.assertFalse(self._failed_alert())

    def test_a_server_that_never_comes_back_fails_with_its_alert(self):
        job = self._reboot_job()
        _executor, raised = self._wait(
            job, RETURN_DEADLINE_S, error=ConnectionRefusedError(),
        )
        self.assertIsInstance(raised, RuntimeError)
        alert = self._failed_alert()
        self.assertTrue(alert)
        self.assertEqual(alert.level, "critical")

    def test_the_same_boot_after_the_restart_fails(self):
        job = self._reboot_job()
        _executor, raised = self._wait(job, 120, _transport(boot=_BOOT))
        self.assertIsInstance(raised, RuntimeError)
        self.assertTrue(self._failed_alert())

    def test_the_same_boot_with_the_restart_still_scheduled_waits(self):
        job = self._reboot_job()
        _executor, raised = self._wait(job, 95, _transport(boot=_BOOT, scheduled=True))
        self.assertIsInstance(raised, JobParked)

    def test_a_stop_before_the_order_ends_the_job_with_no_restart(self):
        job = self._reboot_job()
        job.with_context(ic_skip_state_hook=True).write({"state": "cancelled"})
        self.env.flush_all()
        transport = _transport()
        _executor, raised = self._run_before(job, transport)
        self.assertEqual(type(raised).__name__, "JobCancelled")
        orders = [c.args[0] for c in transport.execute.call_args_list]
        self.assertFalse(any("host_reboot.sh reboot" in o for o in orders))
        self.assertFalse(self._row(job).shutdown_at)


@tagged("post_install", "-at_install")
class TestRebootComesBack(_RebootJobCase):
    """What the restart checks once the server is back (cut 2)."""

    def _incomplete_alert(self):
        return self.env["cloud.alert"].search([
            ("code", "=", REBOOT_INCOMPLETE_ALERT), ("host_id", "=", self.host.id),
            ("state", "=", "active"),
        ])

    def test_work_inside_an_instance_holds_the_restart(self):
        job = self._reboot_job()
        CloudInstance = type(self.env["cloud.instance"])
        with patch.object(
            CloudInstance, "_move_quiet_sql", autospec=True, return_value="SELECT 2",
        ):
            transport = _transport(quiet="2\n")
            executor, raised = self._run_before(job, transport)
        self.assertIsNone(raised)
        self.assertEqual(executor._verdict[0], "skipped")
        self.assertIn("queue-inst", executor._verdict[1])
        orders = [c.args[0] for c in transport.execute.call_args_list]
        self.assertFalse(any("host_reboot.sh reboot" in o for o in orders))

    def test_a_count_inside_that_cannot_be_read_holds_it_too(self):
        job = self._reboot_job()
        CloudInstance = type(self.env["cloud.instance"])
        with patch.object(
            CloudInstance, "_move_quiet_sql", autospec=True, return_value="SELECT 1",
        ):
            executor, _raised = self._run_before(job, _transport(quiet="ERROR\n"))
        self.assertEqual(executor._verdict[0], "skipped")

    def test_everything_back_clears_the_pending_restart_and_probes_the_instances(self):
        self.assertTrue(self.inst.deployed)
        self.host.write({
            "reboot_required_since": self.inside - timedelta(days=1),
            "reboot_required_pkgs": "linux-image",
        })
        job = self._reboot_job()
        _executor, raised = self._wait(job, 95, _transport(boot=_NEW_BOOT))
        self.assertIsNone(raised)
        self.assertFalse(self.host.reboot_required_since)
        self.assertFalse(self._incomplete_alert())
        self.assertTrue(self.Job.search([
            ("instance_id", "=", self.inst.id),
            ("job_type_id.code", "=", "instance_health"),
        ]))

    def test_a_container_that_did_not_come_back_is_named(self):
        job = self._reboot_job()
        verify = _CLEAN + "IC_MISSING=proxy\nIC_UNCLEAN=queue-inst-db-1\n"
        _executor, raised = self._wait(job, 95, _transport(boot=_NEW_BOOT, verify=verify))
        self.assertIsNone(raised)
        alert = self._incomplete_alert()
        self.assertTrue(alert)
        self.assertEqual(alert.level, "warning")
        self.assertIn("proxy", alert.message)
        self.assertFalse(self._failed_alert())

    def test_an_update_still_pending_after_the_restart_stays_recorded(self):
        self.host.reboot_required_since = self.inside - timedelta(days=1)
        job = self._reboot_job()
        verify = _CLEAN.replace("IC_REBOOT_REQUIRED=0", "IC_REBOOT_REQUIRED=1")
        self._wait(job, 95, _transport(boot=_NEW_BOOT, verify=verify))
        self.assertTrue(self.host.reboot_required_since)
