"""The health probe tells when a staging's egress filter is not in force.

``odoo_net_setup`` points odoo's default route at the allowlist proxy
once, when it starts; its healthcheck says whether odoo's route is
still that one. The probe read only ``running`` from the container
listing, so a staging whose odoo had started after the sidecar ran on
the open internet with every light green (a customer's staging,
2026-10-08). The probe now reads the healthcheck's verdict too.
"""
import asyncio

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.instance_health_executor import (
    InstanceHealthExecutor,
)

_CODE = "instance_egress_filter_lost"


class TestEgressFilterProbe(TransactionCase):

    def setUp(self):
        """A staging with an allowlist, so the sidecar is expected."""
        super().setUp()
        # The probe writes alerts on a cursor of its own; without test
        # mode those writes cannot see the records created here.
        self.registry_enter_test_mode()
        project = self.env["cloud.project"].create({"name": "EgressProbe"})
        self.host = self.env["cloud.host"].create({
            "name": "egress-probe-host",
            "ip_address": "192.0.2.91",
            "user": "ubuntu",
            "wildcard_domain": "egress-probe.example.com",
        })
        self.staging = self.env["cloud.instance"].create({
            "name": "egressstag",
            "project_id": project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })
        self.env["cloud.instance.whitelist"].create({
            "instance_id": self.staging.id,
            "hostname": "api.example.com",
            "sequence": 10,
        })
        self.assertIn("odoo_net_setup", self.staging.expected_services())
        self.job_type = self.env["cloud.job.type"].search(
            [("code", "=", "instance_health")], limit=1,
        )

    def _listing(self, sidecar_status, odoo_state="running"):
        """A three-column ``docker compose ps -a`` of the staging.

        :param str sidecar_status: the sidecar's ``Status`` text.
        :param str odoo_state: odoo's ``State``.
        """
        lines = []
        for svc in self.staging.expected_services():
            state = odoo_state if svc == "odoo" else "running"
            status = sidecar_status if svc == "odoo_net_setup" else "Up 2 hours"
            lines.append(f"{svc}\t{state}\t{status}")
        return "\n".join(lines)

    def _probe(self, container_state):
        """Run one health probe against *container_state*."""
        job = self.env["cloud.job"].create({
            "name": "Health",
            "host_id": self.host.id,
            "instance_id": self.staging.id,
            "job_type_id": self.job_type.id,
        })
        executor = InstanceHealthExecutor(job, self.host)
        executor._skipped = False
        results = {
            "container_state": {"stdout": container_state},
            "cpu_mem_snapshot": {"stdout": "0.0\t0.0"},
            "http_health": {"stdout": "exit:0"},
            "error_lines": {"stdout": ""},
        }
        executor.parse_results(results)
        asyncio.run(executor.on_success(results))

    def _alert(self):
        """The staging's active egress alert, if any."""
        return self.env["cloud.alert"].search([
            ("code", "=", _CODE),
            ("instance_id", "=", self.staging.id),
            ("state", "=", "active"),
        ])

    def test_an_unhealthy_sidecar_raises_a_critical_alert(self):
        self._probe(self._listing("Up 15 minutes (unhealthy)"))
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "critical")

    def test_the_alert_closes_once_the_filter_is_back(self):
        self._probe(self._listing("Up 15 minutes (unhealthy)"))
        self.assertTrue(self._alert())
        self._probe(self._listing("Up 1 minute (healthy)"))
        self.assertFalse(self._alert())

    def test_a_starting_healthcheck_is_not_an_alert(self):
        self._probe(self._listing("Up 5 seconds (health: starting)"))
        self.assertFalse(self._alert())

    def test_a_stopped_odoo_reaches_nothing(self):
        self._probe(self._listing("Up 15 minutes (unhealthy)", odoo_state="exited"))
        self.assertFalse(self._alert())

    def test_a_two_column_listing_still_parses(self):
        """Older listings, without the status column, are read as before."""
        self._probe("\n".join(
            f"{svc}\trunning" for svc in self.staging.expected_services()
        ))
        self.assertFalse(self._alert())
