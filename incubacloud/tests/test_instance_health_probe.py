"""Classification of a non-running ``odoo`` by the health probe.

Three distinct situations used to collapse into one ``instance_down``:

* container **running** — healthy path;
* container **present but exited** — an incident in core, but *normal*
  for a layered module that schedules sleep (Sablier), which opts in
  via the ``_odoo_stop_is_expected`` hook;
* container **missing** — never normal (this is how a pruned stack
  presents itself), alerts even when a stop would have been expected.
"""
import asyncio

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.instance_health_executor import (
    InstanceHealthExecutor,
)


class _SleepAwareProbe(InstanceHealthExecutor):
    """Test double for a layered module that schedules sleeps."""

    _job_type = None  # keep out of the executor registry

    def _odoo_stop_is_expected(self):
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

    def _down_alert(self):
        return self.env["cloud.alert"].search([
            ("instance_id", "=", self.instance.id),
            ("code", "=", "instance_down"),
            ("state", "=", "active"),
        ])

    def test_exited_odoo_is_an_incident_by_default(self):
        self._probe(InstanceHealthExecutor, self._states(odoo="exited"))
        self.assertEqual(self.instance.status, "error")
        alert = self._down_alert()
        self.assertEqual(len(alert), 1)
        self.assertIn("not running", alert.message)

    def test_missing_odoo_says_so(self):
        """A pruned stack must not hide behind the generic wording."""
        self._probe(InstanceHealthExecutor, self._states(odoo=None))
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
        self._probe(_SleepAwareProbe, self._states(odoo="exited", db="exited"))
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
        self._probe(_SleepAwareProbe, self._states(odoo=None))
        self.assertEqual(self.instance.status, "error")
        alert = self._down_alert()
        self.assertEqual(len(alert), 1)
        self.assertIn("missing", alert.message)

    def test_expected_sleep_resolves_a_previous_down_alert(self):
        """Wake→sleep cycles must not leave a stale critical behind."""
        self._probe(InstanceHealthExecutor, self._states(odoo=None))
        self.assertTrue(self._down_alert())
        self._probe(_SleepAwareProbe, self._states(odoo="exited"))
        self.assertFalse(self._down_alert())


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
