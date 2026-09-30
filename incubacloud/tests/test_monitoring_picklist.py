"""What the Monitoring picker shows for each instance.

The metrics name an instance by its compose name, which is what Grafana
filters on and therefore what the picker must send. Shown bare it meant
nothing — ``ic-tenant-034a549f`` or a dozen ``production`` — so the
picker now shows a label and keeps sending the name.
"""
from odoo.tests.common import TransactionCase


class TestMonitoringPicklist(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "HMon",
            "ip_address": "10.0.0.41",
            "user": "root",
            "wildcard_domain": "hmon.example.com",
        })

    def _instance(self, project_name, name, **vals):
        """Create a deployed production instance in a project of its own."""
        project = self.env["cloud.project"].create({"name": project_name})
        return self.env["cloud.instance"].create({
            "name": name,
            "project_id": project.id,
            "environment": "production",
            "host_id": self.host.id,
            "state": "deployed",
        } | vals)

    def _rows(self):
        """Return the picklist keyed by the value the picker sends."""
        return {
            r["name"]: r
            for r in self.env["cloud.instance"]._monitoring_picklist()
        }

    def test_the_label_names_the_project(self):
        inst = self._instance("Acme", "monacme", running=True)
        self.assertEqual(inst._monitoring_label(), "Acme / monacme")

    def test_a_stopped_instance_says_so(self):
        """Its charts will be empty; the picker is where to say why."""
        inst = self._instance("Acme", "monacme", running=False)
        self.assertEqual(inst._monitoring_label(), "Acme / monacme — Stopped")

    def test_the_value_stays_the_name_grafana_filters_on(self):
        self._instance("Zeta", "monzeta", running=True)
        row = self._rows()["monzeta"]
        self.assertEqual(row["label"], "Zeta / monzeta")
        self.assertEqual(row["host"], "HMon")
        self.assertTrue(row["running"])

    def test_drafts_are_left_out(self):
        """Nothing of theirs runs yet, so no series carries their name."""
        self._instance("Draft", "mondraft", state="draft")
        self.assertNotIn("mondraft", self._rows())

    def test_rows_are_sorted_by_what_the_operator_reads(self):
        self._instance("Beta", "monb", running=True)
        self._instance("alpha", "mona", running=True)
        labels = [
            r["label"]
            for r in self.env["cloud.instance"]._monitoring_picklist()
            if r["name"] in ("mona", "monb")
        ]
        self.assertEqual(labels, ["alpha / mona", "Beta / monb"])
