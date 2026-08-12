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

    def _probe(self, executor_cls, container_state):
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
            "http_health": {"stdout": "exit:1"},
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
