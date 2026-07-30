"""The parts of observability that are wiring, not logic.

Three things here failed silently rather than loudly, which is why they
are worth pinning:

* The shipped dashboards and the SPA's tab list are two hand-maintained
  copies of the same uid list. A mismatch renders an empty Grafana frame
  with no error at all.
* A dashboard panel can filter on a label the pipeline never produces.
  Two Traefik panels did exactly that: the ``instance`` label is set by
  cAdvisor relabelling, and the Traefik job has no way to produce it,
  so the panels were permanently blank and looked like "no traffic".
* The per-host label map is a snapshot taken when the agents are
  installed. If nothing re-applies it, every instance deployed after
  Host Setup reports containers no rule can attribute.
"""
import json
import pathlib
import re

from odoo.tests.common import BaseCase, TransactionCase

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DASHBOARDS = _ROOT / "ansible" / "files" / "dashboards"
_MONITORING_JS = (
    _ROOT / "static" / "src" / "components" / "monitoring" / "monitoring.js"
)


def _dashboard_files():
    return sorted(_DASHBOARDS.glob("*.json"))


class TestDashboardsMatchTheSpa(BaseCase):

    def test_every_shipped_dashboard_is_offered_in_the_panel(self):
        """uid + filename must line up with the SPA's tab list."""
        declared = dict(re.findall(
            r'uid:\s*"([^"]+)",\s*slug:\s*"([^"]+)"',
            _MONITORING_JS.read_text(),
        ))
        shipped = {
            json.loads(path.read_text())["uid"]: path.stem
            for path in _dashboard_files()
        }
        self.assertEqual(
            shipped, declared,
            "the dashboards on disk and the tabs in the SPA disagree; a "
            "wrong uid renders an empty Grafana frame with no error",
        )

    def test_no_panel_filters_traefik_by_an_instance_label(self):
        """Traefik samples can never carry ``instance``/``instance_id``.

        Its service names are copier-time literals from each customer's
        own prod.yaml and bear no relation to COMPOSE_PROJECT_NAME, the
        key every other relabelling rule joins on. A panel filtering
        Traefik by instance is therefore always empty — indistinguishable
        from an instance that genuinely serves no traffic.
        """
        offenders = []
        for path in _dashboard_files():
            for panel in json.loads(path.read_text())["panels"]:
                for target in panel.get("targets", []):
                    expression = target.get("expr", "")
                    if "traefik_" not in expression:
                        continue
                    if re.search(r'instance(_id)?\s*=', expression):
                        offenders.append(f"{path.name}: {panel['title']}")
        self.assertFalse(
            offenders,
            "Traefik panels filtered by an instance label they can never "
            "carry: %s" % offenders,
        )

    def test_every_template_variable_used_is_declared(self):
        """``$foo`` with no matching variable silently matches nothing."""
        offenders = []
        for path in _dashboard_files():
            dashboard = json.loads(path.read_text())
            declared = {
                variable["name"]
                for variable in dashboard["templating"]["list"]
            }
            for panel in dashboard["panels"]:
                for target in panel.get("targets", []):
                    for used in re.findall(r'\$(\w+)', target.get("expr", "")):
                        if used not in declared:
                            offenders.append(
                                f"{path.name}: {panel['title']} uses ${used}"
                            )
        self.assertFalse(offenders, "undeclared dashboard variables: %s"
                         % offenders)


class TestLabelMapFollowsTheInstanceSet(TransactionCase):
    """Deploy/move/remove must re-apply the host's scrape config."""

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.write({
            "metrics_enabled": True,
            "metrics_central_url": "http://vm.test:8428",
            "metrics_remote_write_url": "http://vm.test:8428/api/v1/write",
        })
        self.host = self.env["cloud.host"].create({
            "name": "HObs",
            "ip_address": "10.0.0.30",
            "user": "root",
            "wildcard_domain": "hobs.example.com",
            "last_probed": "2026-07-28 10:00:00",
        })

    def _queued(self):
        return self.env["cloud.job"].search([
            ("host_id", "=", self.host.id),
            ("job_type_id.code", "=", "install_observability"),
        ])

    def test_a_reporting_host_is_refreshed(self):
        self.host.refresh_observability_labels(reason="test")
        self.assertTrue(
            self._queued(),
            "the label map was never re-applied, so instances deployed "
            "after Host Setup report unattributed container metrics",
        )

    def test_a_host_the_backend_never_saw_is_left_alone(self):
        """Deploying an instance must not silently enrol a host."""
        self.host.last_probed = False
        self.host.refresh_observability_labels(reason="test")
        self.assertFalse(self._queued())

    def test_nothing_happens_when_observability_is_off(self):
        self.settings.metrics_enabled = False
        self.host.refresh_observability_labels(reason="test")
        self.assertFalse(self._queued())

    def test_nothing_happens_without_a_remote_write_url(self):
        """Agents with nowhere to push are worse than no agents."""
        self.settings.metrics_remote_write_url = ""
        self.host.refresh_observability_labels(reason="test")
        self.assertFalse(self._queued())

    def test_the_lifecycle_executors_ask_for_a_refresh(self):
        """Pin the call sites: the helper is useless if nobody calls it.

        Checked structurally because reaching these ``on_success`` bodies
        for real needs a live SSH transport.
        """
        models = _ROOT / "models"
        expected = {
            "deploy_instance_executor.py",
            "delete_instance_executor.py",
            "move_cutover_executor.py",
        }
        missing = {
            name for name in expected
            if "refresh_observability_labels"
            not in (models / name).read_text()
        }
        self.assertFalse(
            missing,
            "these executors change a host's instance set without "
            "refreshing its label map: %s" % sorted(missing),
        )
