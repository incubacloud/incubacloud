"""The parts of observability that are wiring, not logic.

Three things here failed silently rather than loudly, which is why they
are worth pinning:

* The shipped dashboards and the SPA's tab list are two hand-maintained
  copies of the same uid list. A mismatch renders an empty Grafana frame
  with no error at all.
* A dashboard panel can filter on a label the pipeline never produces,
  and a blank panel is indistinguishable from "no traffic". Traefik
  samples CAN carry an instance label — the service names are derived
  from the project name the panel itself feeds copier — but only while
  the scrape config actually emits the rules that attach it.
* The per-host label map is a snapshot taken when the agents are
  installed. If nothing re-applies it, every instance deployed after
  Host Setup reports containers no rule can attribute.
"""
import json
import pathlib
import re
from xml.etree import ElementTree

import yaml

from odoo import fields
from odoo.tests.common import BaseCase, TransactionCase

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DASHBOARDS = _ROOT / "ansible" / "files" / "dashboards"
_CENTRAL_PLAYBOOK = _ROOT / "ansible" / "playbooks" / "observability_central.yml"
_METRIC_RULES = _ROOT / "data" / "metric_rule_data.xml"
_MONITORING_JS = (
    _ROOT / "static" / "src" / "components" / "monitoring" / "monitoring.js"
)

# Load per core is a ratio: there is no unit to give it, and forcing one
# would be worse than none. Everything else has to name its numbers.
_RATIO_PANELS = frozenset({"Load per core, by host", "CPU load per core"})


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

    def test_traefik_panels_are_backed_by_relabelling_rules(self):
        """A Traefik panel may filter by instance — but only because we
        emit the rules that make the label exist.

        This test used to assert the opposite, on the belief that Traefik
        service names were copier-time literals unrelated to anything we
        control. That was wrong, and it came from surveying doodba
        projects scaffolded by hand: the panel runs ``copier copy``
        itself and feeds it the same project name it forces into
        COMPOSE_PROJECT_NAME, so the names are derived. The invariant
        worth pinning is therefore not "never filter by instance" but
        "if a panel filters by it, the agent config must produce it".
        """
        filters_by_instance = any(
            "traefik_" in target.get("expr", "")
            and re.search(r'instance(_id)?\s*=', target.get("expr", ""))
            for path in _dashboard_files()
            for panel in json.loads(path.read_text())["panels"]
            for target in panel.get("targets", [])
        )
        if not filters_by_instance:
            self.skipTest("no Traefik panel filters by instance yet")
        playbook = (
            _ROOT / "ansible" / "playbooks" / "host_observability.yml"
        ).read_text()
        traefik_block = playbook.split("job_name: traefik", 1)[-1]
        self.assertIn(
            "traefik_prefix", traefik_block,
            "a dashboard filters Traefik by instance but the scrape "
            "config emits no rule that could attach the label",
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
                        # ``$1`` and friends are label_replace's capture
                        # references, resolved by PromQL itself — not
                        # dashboard variables, and never declared.
                        if used.isdigit() or used in declared:
                            continue
                        offenders.append(
                            f"{path.name}: {panel['title']} uses ${used}"
                        )
        self.assertFalse(offenders, "undeclared dashboard variables: %s"
                         % offenders)

    def test_the_staleness_panel_still_matches_its_alert(self):
        """The panel and the rule share one expression, by design.

        Whoever "improves" the panel query has to change the rule too, or
        the fleet view and the alert start telling different stories about
        the same host. The threshold is pinned for the same reason: the
        colour break has to fall where the alert fires.
        """
        rule = ElementTree.parse(  # nosec B314 — our own data file, shipped in this repo
            _METRIC_RULES,
        ).getroot().find(".//record[@id='metric_rule_host_down']")
        expression = rule.find("field[@name='expression']").text.strip()
        threshold = float(rule.find("field[@name='threshold']").text)

        fleet = json.loads((_DASHBOARDS / "incubacloud-fleet.json").read_text())
        panel = next(
            p for p in fleet["panels"]
            if p["title"].startswith("Ingest age per host")
        )
        self.assertEqual(panel["targets"][0]["expr"], expression)
        # Lanes, not lines: every agent in the fleet scrapes on the same
        # wall-clock tick, so on a shared axis the healthy hosts drew
        # identical curves and hid one another completely.
        self.assertEqual(panel["type"], "state-timeline")
        steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
        red = [s for s in steps if s["color"] == "red"]
        self.assertEqual([s["value"] for s in red], [threshold])

    def test_the_instance_picker_only_offers_real_instances(self):
        """cAdvisor labels anything it cannot attribute with the raw
        target address — the same literal string on every host. It sorts
        before every real instance, so it was what the dashboard opened
        on, and selecting it adds up containers across hosts."""
        instance = json.loads(
            (_DASHBOARDS / "incubacloud-instance.json").read_text()
        )
        variable = next(
            v for v in instance["templating"]["list"] if v["name"] == "instance"
        )
        self.assertIn('instance_id!=""', variable["definition"])
        self.assertEqual(variable["definition"], variable["query"]["query"])

    def test_instance_panels_are_scoped_to_one_host(self):
        """Container names repeat across hosts and an instance name is
        only unique within its project, so an unscoped filter can add up
        two machines without saying so."""
        instance = json.loads(
            (_DASHBOARDS / "incubacloud-instance.json").read_text()
        )
        for panel in instance["panels"]:
            for target in panel["targets"]:
                self.assertIn(
                    'host=~"$host"', target["expr"],
                    f"{panel['title']} filters by instance without a host",
                )

    def test_the_disk_panel_reads_a_label_the_collector_emits(self):
        """A legend can only name a label the exposed metric carries.

        And it must not name ``instance``: the scraper owns that one and
        overwrites it with the target address, renaming whatever the
        metric called that to ``exported_instance``. A panel keyed on it
        renders every series of a host under the same legend — data
        present, series distinct, and completely unreadable.
        """
        playbook = (
            _ROOT / "ansible" / "playbooks" / "host_observability.yml"
        ).read_text()
        exposed = set(re.findall(
            r"(\w+)=\"%s\"",
            playbook.split("ic_instance_disk_bytes{", 1)[1].split("}", 1)[0],
        ))
        self.assertNotIn(
            "instance", exposed,
            "the collector exposes a label the scraper overwrites",
        )
        host = json.loads((_DASHBOARDS / "incubacloud-host.json").read_text())
        panel = next(
            p for p in host["panels"] if p["title"] == "Disk used per instance"
        )
        used = set(re.findall(r"{{(\w+)}}", panel["targets"][0]["legendFormat"]))
        self.assertTrue(
            used <= exposed,
            f"the panel legends by {used - exposed}, which the collector "
            f"never emits (it emits {exposed})",
        )

    def test_every_chart_panel_says_what_its_numbers_are(self):
        """A byte count rendered as ``10000000000`` is not a reading.

        Ratios are exempt because there is nothing to name; anything else
        declares a unit or, where Grafana has none, an axis label.
        """
        offenders = []
        for path in _dashboard_files():
            for panel in json.loads(path.read_text())["panels"]:
                if panel["type"] not in ("timeseries", "state-timeline"):
                    continue
                if panel["title"] in _RATIO_PANELS:
                    continue
                defaults = panel.get("fieldConfig", {}).get("defaults", {})
                if defaults.get("unit"):
                    continue
                if defaults.get("custom", {}).get("axisLabel"):
                    continue
                offenders.append(f"{path.name}: {panel['title']}")
        self.assertFalse(offenders, "panels with unlabelled numbers: %s"
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
            "metrics_account": "acct_wiring",
        })
        self.host = self.env["cloud.host"].create({
            "name": "HObs",
            "ip_address": "10.0.0.30",
            "user": "root",
            "wildcard_domain": "hobs.example.com",
            "known_hosts_key": "hobs.example.com ssh-ed25519 AAAA",
            # Enrolled: the agents are actually installed here. This used
            # to be expressed as ``last_probed``, which the SSH telemetry
            # job also writes — so with the fallback alive every host
            # looked enrolled and the guard did the opposite of what it
            # claimed.
            "metrics_agents_state": "installed",
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

    def test_a_host_without_agents_is_left_alone(self):
        """Deploying an instance must not silently enrol a host.

        Enrolment is the reconciliation cron's job. Doing it here would
        make monitoring a surprise side effect of an unrelated deploy.
        """
        self.host.metrics_agents_state = "never"
        self.host.refresh_observability_labels(reason="test")
        self.assertFalse(self._queued())

    def test_a_probed_but_unenrolled_host_is_not_mistaken_for_enrolled(self):
        """The regression: ``last_probed`` never meant "has agents".

        The SSH telemetry job stamps it on every host it touches, so a
        guard reading it saw the whole fleet as enrolled.
        """
        self.host.write({
            "metrics_agents_state": "never",
            "last_probed": "2026-07-28 10:00:00",
        })
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


class TestEnrolmentConverges(TransactionCase):
    """Observability applies to every host, without anyone asking for it.

    It used to be installed once, chained to host setup, which left three
    silent ways to end up unmonitored: set up before observability was
    switched on, set up with no remote-write URL, or a single failed
    install nobody retried. The cron replaces that one-shot event with a
    state that converges.
    """

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.write({
            "metrics_enabled": True,
            "metrics_central_url": "http://vm.test:8428",
            "metrics_remote_write_url": "http://vm.test:8428/api/v1/write",
            "metrics_account": "acct_recon",
        })
        self.host = self.env["cloud.host"].create({
            "name": "HRec",
            "ip_address": "10.0.0.31",
            "user": "root",
            "wildcard_domain": "hrec.example.com",
            "known_hosts_key": "hrec.example.com ssh-ed25519 AAAA",
        })

    def _queued(self):
        return self.env["cloud.job"].search([
            ("host_id", "=", self.host.id),
            ("job_type_id.code", "=", "install_observability"),
        ])

    def test_an_unenrolled_host_is_picked_up(self):
        """Turning observability on IS the instruction to enrol."""
        self.env["cloud.host"]._cron_reconcile_observability()
        self.assertTrue(self._queued())

    def test_an_enrolled_host_is_left_alone(self):
        self.host.metrics_agents_state = "installed"
        self.env["cloud.host"]._cron_reconcile_observability()
        self.assertFalse(self._queued())

    def test_nothing_is_queued_while_observability_is_off(self):
        """The master switch is the gate; the cron must respect it."""
        self.settings.metrics_enabled = False
        self.env["cloud.host"]._cron_reconcile_observability()
        self.assertFalse(self._queued())

    def test_a_recent_failure_backs_off_instead_of_hammering(self):
        """An unreachable host must not be retried every single tick."""
        self.host.write({
            "metrics_agents_state": "failed",
            "metrics_agents_attempts": 1,
            "metrics_agents_since": fields.Datetime.now(),
        })
        self.env["cloud.host"]._cron_reconcile_observability()
        self.assertFalse(self._queued())

    def test_an_old_failure_is_retried(self):
        """Backing off must not become giving up."""
        self.host.write({
            "metrics_agents_state": "failed",
            "metrics_agents_attempts": 1,
            "metrics_agents_since": fields.Datetime.subtract(
                fields.Datetime.now(), hours=4,
            ),
        })
        self.env["cloud.host"]._cron_reconcile_observability()
        self.assertTrue(self._queued())

    def test_an_unprepared_host_is_not_enrolled(self):
        """It cannot take agents yet; queuing against it is just noise.

        ``known_hosts_key`` is the marker: the panel only has one once it
        has actually reached the host over SSH.
        """
        self.host.write({"known_hosts_key": ""})
        self.env["cloud.host"]._cron_reconcile_observability()
        self.assertFalse(self._queued())

    def test_failures_are_counted_so_the_backoff_grows(self):
        first = self.host._mark_observability_failed()
        second = self.host._mark_observability_failed()
        self.assertEqual((first, second), (1, 2))
        self.host._mark_observability_installed()
        self.assertEqual(self.host.metrics_agents_attempts, 0)
        self.assertEqual(self.host.metrics_agents_state, "installed")


class TestTheCentralIsReadableByItsOwnContainers(BaseCase):
    """What the central mounts, the container's process must be able to read.

    The processes behind the mounts are not root: vmauth runs
    unprivileged and Grafana runs as uid 472. Writing their configuration
    owner-only is the intuitive "careful" choice and it is the wrong one —
    the first live deployment (nginx, then) answered ``permission denied``
    on its own credential file and left a Grafana in a restart loop, from
    files that looked perfect on the host.

    Then the obvious fix broke it a second way: two of these paths are
    mounted as DIRECTORIES, so tightening the whole tree to 0700 left
    Grafana unable to traverse them. Confidentiality therefore lives on
    the root of the tree, which nothing mounts, and everything below it
    stays reachable. Both mistakes are cheap to make and silent from the
    host, so both are pinned here.
    """

    def _tasks(self):
        """Return the central playbook's task list."""
        return yaml.safe_load(_CENTRAL_PLAYBOOK.read_text())[0]["tasks"]

    def _mounted_paths(self):
        """Return the host paths the compose file bind-mounts, as suffixes.

        :return: set of paths relative to ``ic_central_dir``, e.g.
            ``/vmauth/auth.yml``.
        """
        for task in self._tasks():
            if task.get("name") == "Write the central compose file":
                compose = task["ansible.builtin.copy"]["content"]
                break
        else:
            self.fail("the compose-writing task was renamed or removed")
        return set(re.findall(
            r"-\s*\{\{\s*ic_central_dir\s*\}\}(/\S*?):/", compose,
        ))

    def _declared_modes(self, module):
        """Return ``{path_suffix: mode}`` for one Ansible module's tasks.

        Paths are returned relative to ``ic_central_dir`` so they can be
        compared with the mount list. Looped tasks contribute every item.

        :param module: ``'ansible.builtin.copy'`` or
            ``'ansible.builtin.file'``.
        """
        out = {}
        marker = "{{ ic_central_dir }}"
        for task in self._tasks():
            spec = task.get(module)
            if not isinstance(spec, dict) or "mode" not in spec:
                continue
            targets = [spec.get("dest") or spec.get("path") or ""]
            if "{{ item }}" in targets[0]:
                targets = [str(i) for i in (task.get("loop") or [])]
            for target in targets:
                if marker in target:
                    out[target.split(marker, 1)[1]] = str(spec["mode"])
        return out

    def test_the_root_of_the_tree_is_the_only_gate(self):
        """Nothing mounts it, so it can be — and must be — owner-only.

        Inherited protection is not enough: the parent is the SSH user's
        home, and on a hardened host that is not 0700.
        """
        modes = self._declared_modes("ansible.builtin.file")
        self.assertEqual(
            modes.get(""), "0700",
            "the central directory must be owner-only; it is what keeps "
            "the operator credential away from other users on the host",
        )

    def test_every_mounted_file_is_readable_by_a_non_root_process(self):
        files = self._declared_modes("ansible.builtin.copy")
        checked = 0
        for path in self._mounted_paths():
            mode = files.get(path)
            if mode is None:
                continue  # a directory mount, covered by the next test
            checked += 1
            self.assertIn(
                mode[-1], "4567",
                f"{path} is mounted into a container whose process is not "
                f"root, but is written {mode}: the container will fail to "
                f"read it while the file looks correct on the host",
            )
        self.assertGreaterEqual(
            checked, 2, "the mount list stopped matching the copy tasks",
        )

    def test_every_mounted_directory_can_be_entered(self):
        """A directory mount needs the execute bit, not just read."""
        dirs = self._declared_modes("ansible.builtin.file")
        files = self._declared_modes("ansible.builtin.copy")
        for path in self._mounted_paths():
            if path in files:
                continue
            mode = dirs.get(path)
            self.assertIsNotNone(
                mode, f"{path} is mounted but never created",
            )
            self.assertIn(
                mode[-1], "1357",
                f"{path} is mounted into Grafana as a directory but is "
                f"written {mode}: uid 472 cannot traverse it",
            )
