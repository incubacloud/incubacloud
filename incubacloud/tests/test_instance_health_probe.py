"""Classification of a non-running ``odoo`` by the health probe.

Three distinct situations used to collapse into one ``instance_down``:

* container **running** — healthy path;
* container **present but exited** — an incident in core, but *normal*
  for a layered module that schedules sleep (Sablier), which opts in
  via the ``_odoo_stop_is_expected`` hook;
* container **missing** — never normal (this is how a pruned stack
  presents itself), alerts even when a stop would have been expected.

A layer that parks a whole stack on purpose opts in through the wider
``_stack_stop_is_expected`` hook: there the companions are stopped too,
so nothing is graded at all.
"""
import asyncio
import shutil
import subprocess
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.instance_health_executor import (
    InstanceHealthExecutor,
)


class _SleepAwareProbe(InstanceHealthExecutor):
    """Test double for a layered module that schedules sleeps."""

    _job_type = None  # keep out of the executor registry

    def _odoo_stop_is_expected(self):
        return True


class _ParkedProbe(InstanceHealthExecutor):
    """Test double for a layered module that parks whole stacks."""

    _job_type = None  # keep out of the executor registry

    def _stack_stop_is_expected(self):
        return True


class TestHealthProbeClassification(TransactionCase):

    def setUp(self):
        super().setUp()
        # The probe writes its alerts on a cursor of its own so they
        # outlive a failing job; without test mode that cursor cannot
        # see the records created here and the alert's foreign key to
        # the instance fails. Test mode wraps this transaction instead,
        # so the alert is really written and really rolled back.
        self.registry_enter_test_mode()
        self.project = self.env["cloud.project"].create({"name": "Probe Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "probe-host",
            "ip_address": "192.0.2.63",
            "user": "ubuntu",
            "wildcard_domain": "probe.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "probeinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self.job_type = self.env["cloud.job.type"].search(
            [("code", "=", "instance_health")], limit=1,
        )

    def _states(self, **overrides):
        """Render a ``docker compose ps -a`` payload for this instance.

        Every service the instance expects is reported ``running``
        unless overridden; ``svc=None`` drops it from the listing
        entirely (how a pruned container presents itself). Derived from
        ``expected_services()`` rather than hard-coded so these
        assertions stay about ``odoo``'s classification instead of
        about which optional services (``backup``, ``smtp``) the
        database under test happens to enable — a hard-coded
        ``odoo``/``db`` pair grades the absent ones as companions down
        and turns a healthy sleep into a warning.
        """
        states = dict.fromkeys(self.instance.expected_services(), "running")
        for svc, state in overrides.items():
            if state is None:
                states.pop(svc, None)
            else:
                states[svc] = state
        return "\n".join(f"{svc}\t{state}" for svc, state in states.items())

    def _probe(self, executor_cls, container_state, http_exit=1):
        job = self.env["cloud.job"].create({
            "name": "Health",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.job_type.id,
        })
        executor = executor_cls(job, self.host)
        executor._skipped = False
        results = {
            "container_state": {"stdout": container_state},
            "cpu_mem_snapshot": {"stdout": "0.0\t0.0"},
            "http_health": {"stdout": f"exit:{http_exit}"},
            "error_lines": {"stdout": ""},
        }
        executor.parse_results(results)
        asyncio.run(executor.on_success(results))
        return executor

    def _confirmed_probe(self, executor_cls, container_state, http_exit=1):
        """Probe twice in a row: ``instance_down`` and the companion
        alerts are measurements, so ``cloud.alert.policy`` keeps the
        first sighting as a suspicion and the second one raises."""
        self._probe(executor_cls, container_state, http_exit=http_exit)
        return self._probe(executor_cls, container_state, http_exit=http_exit)

    def _down_alert(self):
        return self.env["cloud.alert"].search([
            ("instance_id", "=", self.instance.id),
            ("code", "=", "instance_down"),
            ("state", "=", "active"),
        ])

    def test_one_probe_is_a_suspicion_not_an_incident(self):
        """A deploy, a rebuild or a wake-up restarts odoo for seconds; a
        probe landing inside that window used to mail the customer a
        critical outage. The status still says error, the alert waits."""
        self._probe(InstanceHealthExecutor, self._states(odoo="exited"))
        self.assertEqual(self.instance.status, "error")
        self.assertFalse(self._down_alert())
        suspicion = self.env["cloud.alert.suspicion"].search([
            ("code", "=", "instance_down"),
            ("instance_id", "=", self.instance.id),
        ])
        self.assertEqual(len(suspicion), 1)

    def test_a_recovery_between_probes_starts_the_count_over(self):
        self._probe(InstanceHealthExecutor, self._states(odoo="exited"))
        self._probe(InstanceHealthExecutor, self._states())
        self._probe(InstanceHealthExecutor, self._states(odoo="exited"))
        self.assertFalse(self._down_alert())

    def test_exited_odoo_is_an_incident_by_default(self):
        self._confirmed_probe(InstanceHealthExecutor, self._states(odoo="exited"))
        self.assertEqual(self.instance.status, "error")
        alert = self._down_alert()
        self.assertEqual(len(alert), 1)
        self.assertIn("not running", alert.message)

    def test_missing_odoo_says_so(self):
        """A pruned stack must not hide behind the generic wording."""
        self._confirmed_probe(InstanceHealthExecutor, self._states(odoo=None))
        alert = self._down_alert()
        self.assertEqual(len(alert), 1)
        self.assertIn("missing", alert.message)

    def test_expected_sleep_is_healthy(self):
        self._probe(_SleepAwareProbe, self._states(odoo="exited"))
        self.assertEqual(self.instance.status, "ok")
        self.assertFalse(self.instance.running)
        self.assertFalse(self._down_alert())

    def test_expected_sleep_still_grades_companions(self):
        """Sablier stops only ``odoo`` — a dead ``db`` is an issue."""
        self._confirmed_probe(_SleepAwareProbe, self._states(odoo="exited", db="exited"))
        self.assertEqual(self.instance.status, "warning")
        self.assertFalse(self._down_alert())
        svc_alert = self.env["cloud.alert"].search([
            ("instance_id", "=", self.instance.id),
            ("code", "=", "instance_service_db_down"),
            ("state", "=", "active"),
        ])
        self.assertEqual(len(svc_alert), 1)

    def test_missing_odoo_alerts_even_when_sleep_is_expected(self):
        """Slept and pruned are different things: absent always alerts."""
        self._confirmed_probe(_SleepAwareProbe, self._states(odoo=None))
        self.assertEqual(self.instance.status, "error")
        alert = self._down_alert()
        self.assertEqual(len(alert), 1)
        self.assertIn("missing", alert.message)

    def test_expected_sleep_resolves_a_previous_down_alert(self):
        """Wake→sleep cycles must not leave a stale critical behind."""
        self._confirmed_probe(InstanceHealthExecutor, self._states(odoo=None))
        self.assertTrue(self._down_alert())
        self._probe(_SleepAwareProbe, self._states(odoo="exited"))
        self.assertFalse(self._down_alert())

    def _all_stopped(self):
        """Render a listing where every expected service has exited."""
        return self._states(
            **dict.fromkeys(self.instance.expected_services(), "exited"),
        )

    def _service_alerts(self):
        return self.env["cloud.alert"].search([
            ("instance_id", "=", self.instance.id),
            ("code", "=like", "instance_service_%_down"),
            ("state", "=", "active"),
        ])

    def test_parked_stack_is_healthy(self):
        """Stopped companions are part of the parking, not an issue."""
        self._probe(_ParkedProbe, self._all_stopped())
        self.assertEqual(self.instance.status, "ok")
        self.assertFalse(self.instance.running)
        self.assertFalse(self._down_alert())
        self.assertFalse(self._service_alerts())

    def test_parked_stack_resolves_what_was_standing(self):
        """Parking closes the alerts raised before, or mid-stop."""
        self._confirmed_probe(InstanceHealthExecutor, self._states(odoo="exited"))
        self._confirmed_probe(_SleepAwareProbe, self._states(odoo="exited", db="exited"))
        self._confirmed_probe(InstanceHealthExecutor, self._states(odoo="exited"))
        self.assertTrue(self._down_alert())
        self.assertTrue(self._service_alerts())
        self._probe(_ParkedProbe, self._all_stopped())
        self.assertFalse(self._down_alert())
        self.assertFalse(self._service_alerts())

    def test_stopped_stack_is_an_incident_by_default(self):
        self._confirmed_probe(InstanceHealthExecutor, self._all_stopped())
        self.assertEqual(self.instance.status, "error")
        self.assertEqual(len(self._down_alert()), 1)

    def test_missing_odoo_alerts_even_when_parked(self):
        """Parked and pruned are different things: absent always alerts."""
        self._confirmed_probe(_ParkedProbe, self._states(odoo=None))
        self.assertEqual(self.instance.status, "error")
        alert = self._down_alert()
        self.assertEqual(len(alert), 1)
        self.assertIn("missing", alert.message)


class TestUnresponsiveHysteresis(TransactionCase):
    """``instance_unresponsive`` needs two failures, and dies with the container.

    A running container whose Odoo is not listening yet is what every
    single boot looks like from the probe's side (``curl`` exits 7 or
    56). Free tenants boot several times a day — Sablier wakes them, a
    core release rebuilds them — so alerting on the first failed probe,
    at *critical*, made "starting" indistinguishable from "down".

    The second half matters more. The alert used to survive the
    instance going back to sleep, because the sleep branch returned
    early after resolving only ``instance_down``. A tenant caught
    mid-wake and then slept for the night kept a critical alert open
    the whole time: ten hours and forty minutes, measured on a Free
    tenant that behaved exactly as designed.
    """

    def setUp(self):
        super().setUp()
        self.registry_enter_test_mode()
        self.project = self.env["cloud.project"].create({"name": "Hyst Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "hyst-host",
            "ip_address": "192.0.2.64",
            "user": "ubuntu",
            "wildcard_domain": "hyst.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "hystinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self.job_type = self.env["cloud.job.type"].search(
            [("code", "=", "instance_health")], limit=1,
        )

    def _states(self, **overrides):
        states = dict.fromkeys(self.instance.expected_services(), "running")
        for svc, state in overrides.items():
            if state is None:
                states.pop(svc, None)
            else:
                states[svc] = state
        return "\n".join(f"{svc}\t{state}" for svc, state in states.items())

    def _probe(self, executor_cls, container_state, http_exit):
        job = self.env["cloud.job"].create({
            "name": "Health",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.job_type.id,
        })
        executor = executor_cls(job, self.host)
        executor._skipped = False
        results = {
            "container_state": {"stdout": container_state},
            "cpu_mem_snapshot": {"stdout": "0.0\t0.0"},
            "http_health": {"stdout": f"exit:{http_exit}"},
            "error_lines": {"stdout": ""},
        }
        executor.parse_results(results)
        asyncio.run(executor.on_success(results))
        return executor

    def _alert(self):
        return self.env["cloud.alert"].search([
            ("instance_id", "=", self.instance.id),
            ("code", "=", "instance_unresponsive"),
            ("state", "=", "active"),
        ])

    def test_one_failed_probe_does_not_alert(self):
        self._probe(InstanceHealthExecutor, self._states(), http_exit=7)
        self.assertFalse(self._alert())
        self.assertEqual(self.instance.http_fail_streak, 1)
        # Still visible in the panel — just not worth waking anyone.
        self.assertEqual(self.instance.status, "error")

    def test_second_consecutive_failure_alerts(self):
        self._probe(InstanceHealthExecutor, self._states(), http_exit=7)
        self._probe(InstanceHealthExecutor, self._states(), http_exit=56)
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "critical")
        self.assertIn("2 consecutive", alert.message)
        self.assertEqual(self.instance.http_fail_streak, 2)

    def test_success_resolves_and_resets_the_streak(self):
        self._probe(InstanceHealthExecutor, self._states(), http_exit=7)
        self._probe(InstanceHealthExecutor, self._states(), http_exit=7)
        self.assertTrue(self._alert())
        self._probe(InstanceHealthExecutor, self._states(), http_exit=0)
        self.assertFalse(self._alert())
        self.assertEqual(self.instance.http_fail_streak, 0)

    def test_going_to_sleep_resolves_an_open_alert(self):
        """The ten-hour critical. A stopped container is not unresponsive."""
        self._probe(InstanceHealthExecutor, self._states(), http_exit=7)
        self._probe(InstanceHealthExecutor, self._states(), http_exit=7)
        self.assertTrue(self._alert())
        self._probe(
            _SleepAwareProbe, self._states(odoo="exited"), http_exit=1,
        )
        self.assertFalse(self._alert())
        self.assertEqual(self.instance.http_fail_streak, 0)
        self.assertEqual(self.instance.status, "ok")

    def test_container_down_resolves_it_too(self):
        """``instance_down`` owns that outage; reporting it twice helps nobody."""
        self._probe(InstanceHealthExecutor, self._states(), http_exit=7)
        self._probe(InstanceHealthExecutor, self._states(), http_exit=7)
        self.assertTrue(self._alert())
        for _ in range(2):  # ``instance_down`` confirms over two probes
            self._probe(
                InstanceHealthExecutor, self._states(odoo="exited"), http_exit=1,
            )
        self.assertFalse(self._alert())
        down = self.env["cloud.alert"].search([
            ("instance_id", "=", self.instance.id),
            ("code", "=", "instance_down"),
            ("state", "=", "active"),
        ])
        self.assertEqual(len(down), 1)

    def test_alert_carries_the_shared_dedup_rule(self):
        """``_inst_alert`` must not re-implement dedup: one row, refreshed.

        It used to be a second search-then-create, and the copy that
        drifted was this one — it never got the unique-index savepoint
        the model-level rule has.
        """
        for _ in range(4):
            self._probe(InstanceHealthExecutor, self._states(), http_exit=7)
        alerts = self.env["cloud.alert"].search([
            ("instance_id", "=", self.instance.id),
            ("code", "=", "instance_unresponsive"),
        ])
        self.assertEqual(len(alerts), 1)
        self.assertIn("4 consecutive", alerts.message)


class TestRunningWhileMetricsOwnIt(TransactionCase):
    """An odoo the probe sees answering is running, whoever owns the flag.

    While the metrics readings are fresh the metrics cron owns
    ``running`` and the probe leaves it alone. Their reading can lag what
    the probe sees: on 22-oct-2026 (QA) a production that answered, every
    container up and its status ok, read "Stopped" without its Connect
    button for four minutes after a backup. Odoo running and answering is
    direct evidence, so the probe writes it. The other way round does not
    change: a stopped odoo is the metrics' to report while they own the
    flag.
    """

    def setUp(self):
        """A production whose metrics were read just now."""
        super().setUp()
        self.registry_enter_test_mode()
        self.env["cloud.settings"].sudo()._get_system().write({
            "metrics_enabled": True,
        })
        self.project = self.env["cloud.project"].create({"name": "Own Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "own-host",
            "ip_address": "192.0.2.66",
            "user": "ubuntu",
            "wildcard_domain": "own.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "owninst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
            "metrics_last_seen": fields.Datetime.now(),
        })
        self.job_type = self.env["cloud.job.type"].search(
            [("code", "=", "instance_health")], limit=1,
        )

    def _states(self, **overrides):
        """Render a ``docker compose ps -a`` payload for this instance.

        :param overrides: a state per service; ``None`` drops it
        :return: one ``service<TAB>state`` line per service, every
            expected one ``running`` unless overridden
        :rtype: str
        """
        states = dict.fromkeys(self.instance.expected_services(), "running")
        for svc, state in overrides.items():
            if state is None:
                states.pop(svc, None)
            else:
                states[svc] = state
        return "\n".join(f"{svc}\t{state}" for svc, state in states.items())

    def _probe(self, container_state, http_exit,
               executor_cls=InstanceHealthExecutor, skipped=False):
        """Run one probe cycle on this instance with these readings.

        :param str container_state: what ``docker compose ps -a`` printed
        :param int http_exit: exit status of the HTTP check
        :param executor_cls: the probe class to run
        :param bool skipped: whether an intrusive job skips the cycle
        """
        job = self.env["cloud.job"].create({
            "name": "Health",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.job_type.id,
        })
        executor = executor_cls(job, self.host)
        executor._skipped = skipped
        results = {
            "container_state": {"stdout": container_state},
            "cpu_mem_snapshot": {"stdout": "0.0\t0.0"},
            "http_health": {"stdout": f"exit:{http_exit}"},
            "error_lines": {"stdout": ""},
        }
        executor.parse_results(results)
        asyncio.run(executor.on_success(results))

    def test_metrics_own_the_flag_here(self):
        """The premise of every test below."""
        self.assertTrue(self.instance._liveness_covered_by_metrics())

    def test_odoo_up_and_answering_is_running(self):
        """Direct evidence overrules the metrics' False."""
        self.instance.write({"running": False})
        self._probe(self._states(), http_exit=0)
        self.assertTrue(
            self.instance.running,
            "an odoo that answered stayed stopped on the metrics' word",
        )

    def test_odoo_up_but_not_answering_is_left_to_the_metrics(self):
        """A booting odoo is not evidence enough to overrule them."""
        self.instance.write({"running": False})
        self._probe(self._states(), http_exit=7)
        self.assertFalse(self.instance.running)

    def test_odoo_stopped_is_left_to_the_metrics(self):
        """A stopped odoo is theirs to report."""
        self.instance.write({"running": True})
        self._probe(self._states(odoo="exited"), http_exit=1)
        self.assertTrue(self.instance.running)

    def test_odoo_missing_is_left_to_the_metrics(self):
        """So is a missing one."""
        self.instance.write({"running": True})
        self._probe(self._states(odoo=None), http_exit=1)
        self.assertTrue(self.instance.running)

    def test_an_expected_sleep_is_left_to_the_metrics(self):
        """A sleep the layer above expects writes nothing either."""
        self.instance.write({"running": True})
        self._probe(
            self._states(odoo="exited"), http_exit=1,
            executor_cls=_SleepAwareProbe,
        )
        self.assertTrue(self.instance.running)

    def test_a_parked_stack_is_left_to_the_metrics(self):
        """Nor does a stack parked whole."""
        self.instance.write({"running": True})
        self._probe(
            self._states(
                **dict.fromkeys(self.instance.expected_services(), "exited"),
            ),
            http_exit=1, executor_cls=_ParkedProbe,
        )
        self.assertTrue(self.instance.running)

    def test_a_skipped_cycle_touches_nothing(self):
        """An intrusive job in flight: nothing read means nothing written."""
        self.instance.write({"running": False})
        self._probe(self._states(), http_exit=0, skipped=True)
        self.assertFalse(self.instance.running)

    def test_stale_metrics_hand_the_flag_back(self):
        """Without fresh metrics the probe owns it, as before: odoo up is
        running, answering or not, and odoo stopped is not."""
        self.instance.write({
            "running": False,
            "metrics_last_seen": fields.Datetime.now() - timedelta(hours=1),
        })
        self._probe(self._states(), http_exit=7)
        self.assertTrue(self.instance.running)
        self._probe(self._states(odoo="exited"), http_exit=1)
        self.assertFalse(self.instance.running)


class TestJobsThatStopTheDatabase(TransactionCase):
    """What a job that stops the database on purpose leaves is not read.

    It cuts every connection Odoo holds, and Odoo logs each one as an
    ERROR: the job runner retrying, the cron worker exiting, the server
    refused while ``db`` shuts down. The probe skipped its pass while the
    job ran, but the first pass after it read from the previous check,
    less five minutes of clock-skew grace, and found them all: alerts
    3202 and 3203 after a rebuild of the instance (October 2026). Alert
    3201 came from a job of the instance's host, the daily backup,
    which did not even skip a pass.
    """

    #: Alert 3202: the check before the rebuild, the rebuild's end, and
    #: what the log held around it.
    LAST_CHECK = "2026-10-23 01:24:10"
    REBUILD_DONE = "2026-10-23 01:34:10"
    REBUILD_LOG = (
        "2026-10-23 01:33:10,803 44 ERROR ? "
        "odoo.addons.queue_job.jobrunner.runner: exception: sleeping 5s "
        "and retrying\n"
        "2026-10-23 01:33:11,375 1 ERROR prod odoo.service.server: "
        'connection to server at "db" (172.25.0.2), port 5432 failed: '
        "FATAL:  the database system is shutting down\n"
        "2026-10-23 01:36:00,000 37 ERROR prod odoo.addons.sale: "
        "after the rebuild\n"
    )
    #: Alert 3201: the check before the host's backup, the backup's end,
    #: and what the log held around it.
    HOST_CHECK = "2026-10-22 15:13:49"
    BACKUP_DONE = "2026-10-22 15:15:21"
    BACKUP_LOG = (
        "2026-10-22 15:14:29,209 29 ERROR ? odoo.service.server: Worker "
        "WorkerCron (29) Exception occurred, exiting...\n"
        "Traceback (most recent call last):\n"
        "psycopg2.OperationalError: server closed the connection "
        "unexpectedly\n"
        "2026-10-22 15:18:00,000 37 ERROR prod odoo.addons.sale: "
        "after the backup\n"
    )
    #: A host job type that an addon lists as stopping the instances.
    HOST_STOP = "test_stop_host_databases"

    def setUp(self):
        """An instance on a host, another host, and a log directory."""
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Stop Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "stop-host",
            "ip_address": "192.0.2.68",
            "user": "ubuntu",
            "wildcard_domain": "stop.example.com",
        })
        self.other_host = self.env["cloud.host"].create({
            "name": "stop-other-host",
            "ip_address": "192.0.2.69",
            "user": "ubuntu",
            "wildcard_domain": "stop-other.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "stopinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self.job_type = self.env["cloud.job.type"].search(
            [("code", "=", "instance_health")], limit=1,
        )
        self.env["cloud.job.type"].create({
            "name": "Stop the host's databases",
            "code": self.HOST_STOP,
            "apply_to": "host",
        })
        self.root = tempfile.mkdtemp(prefix="ic-stop-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (Path(self.root) / "logs").mkdir()

    def _listed(self):
        """Have an addon list ``HOST_STOP`` among the host jobs that stop
        the instances, while the returned patcher is active."""
        return patch.object(
            type(self.env["cloud.job"]),
            "_get_health_blocking_host_job_types",
            autospec=True,
            return_value={self.HOST_STOP},
        )

    def _job(self, code, host, instance=None, date_done=None):
        """Create a job of *code*, running or, with *date_done*, ended.

        An ended job is linked to a queue.job that ended done then, which
        is where its ``date_done`` comes from on a real run.

        :param str code: the job type's code
        :param host: the job's ``cloud.host``
        :param instance: its ``cloud.instance``; a host job without
        :param str date_done: when it ended; running when omitted
        :return: the ``cloud.job``
        """
        vals = {
            "name": code,
            "host_id": host.id,
            "instance_id": instance.id if instance else False,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", code)], limit=1,
            ).id,
        }
        if date_done:
            qjob = self.env["queue.job"].sudo().create({
                "uuid": uuid.uuid4().hex,
                "name": code,
                "state": "done",
                "date_done": date_done,
                "method_name": "execute",
                "model_name": "cloud.job",
                "func_string": "execute()",
            })
            vals["queue_job_uuid"] = qjob.uuid
        else:
            vals["state"] = "started"
        job = self.env["cloud.job"].sudo().create(vals)
        if date_done:
            self.assertEqual(str(job.date_done), date_done)
        return job

    def _executor(self):
        """Return the probe of the instance, on a job of its own."""
        job = self.env["cloud.job"].create({
            "name": "Health",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.job_type.id,
        })
        return InstanceHealthExecutor(job, self.host)

    def _read(self, last_check, log):
        """Run this cycle's error scrape on a log file holding *log*.

        :param str last_check: the instance's previous check
        :param str log: the content of ``logs/odoo.log``
        :return: the samples the cycle would report, one per group
        :rtype: list
        """
        self.instance.write({"last_health_check": last_check})
        (Path(self.root) / "logs" / "odoo.log").write_text(log, encoding="utf-8")
        executor = self._executor()
        executor._skipped = False
        with patch.object(
            InstanceHealthExecutor, "_inst_dir",
            autospec=True, return_value=self.root,
        ):
            cmd = dict(executor.get_commands())["error_lines"]
        proc = subprocess.run(
            ["sh", "-c", cmd], capture_output=True, timeout=60, check=False,
        )
        out = proc.stdout.decode("utf-8", "replace")
        return [
            group["samples"][0] for group in executor._dedupe_error_lines(out)
        ]

    def test_the_window_used_to_reach_into_the_rebuild(self):
        """The premise: without the job, its tail is in the window."""
        samples = self._read(self.LAST_CHECK, self.REBUILD_LOG)
        self.assertEqual(len(samples), 3, samples)

    def test_the_pass_after_a_rebuild_reads_from_its_end(self):
        """From the rebuild's end plus the margin, and on."""
        self._job("rebuild_instance", self.host, self.instance,
                  date_done=self.REBUILD_DONE)
        samples = self._read(self.LAST_CHECK, self.REBUILD_LOG)
        self.assertEqual(len(samples), 1, samples)
        self.assertIn("after the rebuild", samples[0])

    def test_the_margin_is_a_minute(self):
        """The window opens a minute after the job's ``date_done``."""
        self._job("rebuild_instance", self.host, self.instance,
                  date_done=self.REBUILD_DONE)
        self.instance.write({"last_health_check": self.LAST_CHECK})
        executor = self._executor()
        executor._skipped = False
        cmd = dict(executor.get_commands())["error_lines"]
        self.assertIn("2026-10-23 01:35:10", cmd)
        self.assertIn("--since '2026-10-23T01:35:10Z'", cmd)

    def test_a_job_that_does_not_stop_the_database_moves_nothing(self):
        """A backup of the instance stops nothing: the window stays."""
        self._job("backup_create", self.host, self.instance,
                  date_done=self.REBUILD_DONE)
        samples = self._read(self.LAST_CHECK, self.REBUILD_LOG)
        self.assertEqual(len(samples), 3, samples)

    def test_a_host_job_listed_by_an_addon_moves_it_too(self):
        """Alert 3201: the host's backup, not the instance's own job."""
        self._job(self.HOST_STOP, self.host, date_done=self.BACKUP_DONE)
        with self._listed():
            samples = self._read(self.HOST_CHECK, self.BACKUP_LOG)
        self.assertEqual(len(samples), 1, samples)
        self.assertIn("after the backup", samples[0])

    def test_a_host_job_on_another_host_moves_nothing(self):
        """Only the jobs of the instance's own host count."""
        self._job(self.HOST_STOP, self.other_host, date_done=self.BACKUP_DONE)
        with self._listed():
            samples = self._read(self.HOST_CHECK, self.BACKUP_LOG)
        self.assertEqual(len(samples), 2, samples)

    def _skips(self):
        """Whether this cycle's probe skips its pass.

        :rtype: bool
        """
        executor = self._executor()
        asyncio.run(executor.before_execute(None))
        return executor.get_commands() == [("noop", "true")]

    def test_a_host_job_listed_by_an_addon_running_skips_the_pass(self):
        """While it runs, the instances of its host are not probed."""
        self._job(self.HOST_STOP, self.host)
        with self._listed():
            self.assertTrue(self._skips())

    def test_a_host_job_running_on_another_host_does_not(self):
        """Those of another host are."""
        self._job(self.HOST_STOP, self.other_host)
        with self._listed():
            self.assertFalse(self._skips())

    def _unresponsive(self):
        """Open ``instance_unresponsive`` on the instance, its streak at two.

        :return: the alert
        """
        self.instance.http_fail_streak = 2
        return self.env["cloud.alert"].sudo().raise_alert(
            "instance_unresponsive", "Odoo does not answer.",
            level="critical", instance=self.instance,
        )

    def _skip_a_pass(self):
        """Run a cycle that skips its pass, its outcome included."""
        self.registry_enter_test_mode()
        executor = self._executor()
        asyncio.run(executor.before_execute(None))
        self.assertEqual(executor.get_commands(), [("noop", "true")])
        asyncio.run(executor.on_success({}))
        # The alerts are closed on a cursor of their own.
        self.env.invalidate_all()

    def test_a_host_job_skipping_the_pass_leaves_the_alert_open(self):
        """The host's daily backup stops the instance and changes nothing
        in it: an Odoo that did not answer still has to, its streak and
        its critical alert stand (they were closed, and opened again with
        a new notice 10 to 15 minutes later)."""
        alert = self._unresponsive()
        self._job(self.HOST_STOP, self.host)
        with self._listed():
            self._skip_a_pass()
        self.assertEqual(alert.state, "active")
        self.assertEqual(self.instance.http_fail_streak, 2)

    def test_a_job_of_the_instance_skipping_the_pass_still_resets_them(self):
        """A rebuild of the instance replaces what they measured."""
        alert = self._unresponsive()
        self._job("rebuild_instance", self.host, self.instance)
        self._skip_a_pass()
        self.assertEqual(alert.state, "dismissed")
        self.assertEqual(self.instance.http_fail_streak, 0)

    def test_a_host_job_beside_one_of_the_instance_still_resets_them(self):
        """Both running at once: the instance's own job decides, as when alone."""
        alert = self._unresponsive()
        self._job(self.HOST_STOP, self.host)
        self._job("rebuild_instance", self.host, self.instance)
        with self._listed():
            self._skip_a_pass()
        self.assertEqual(alert.state, "dismissed")
        self.assertEqual(self.instance.http_fail_streak, 0)

    def test_a_host_job_nobody_lists_skips_nothing(self):
        """Core lists none: a host job skips nothing unless an addon
        says it stops the instances, as before."""
        self.assertNotIn(
            self.HOST_STOP,
            self.env["cloud.job"]._get_health_blocking_host_job_types(),
        )
        self._job(self.HOST_STOP, self.host)
        self.assertFalse(self._skips())
