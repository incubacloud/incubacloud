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

    def test_the_fleet_counter_only_counts_real_instances(self):
        """The same omission as the picker above, one panel over.

        ``instance_id`` is attached by the agent's relabelling, and only
        to containers belonging to an instance — the agents' own stack
        and the proxy never carry it. PromQL does not drop the series
        that lack the grouping label: it collects them into one group
        keyed on the empty string. So the count came out at one more
        than the fleet holds, on every host that reports containers at
        all, and read as a real instance nobody could find. Filtered, an
        empty fleet has no series left to count, hence the fallback.
        """
        fleet = json.loads((_DASHBOARDS / "incubacloud-fleet.json").read_text())
        panel = next(
            p for p in fleet["panels"] if p["title"] == "Instances observed"
        )
        expression = panel["targets"][0]["expr"]
        self.assertIn('instance_id!=""', expression)
        self.assertIn("or vector(0)", expression)

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

    def test_repeated_refreshes_collapse_onto_the_queued_job(self):
        """Five deletions on one host must not queue five installs.

        That stampede is what fed the serialization race of 2026-08-13:
        the losing transactions were rolled back and re-run, and reported
        failures for teardowns that had already finished. Collapsing is
        safe because the playbook renders its label map when it runs, so
        a job still waiting picks up the final state either way.
        """
        first = self.host.refresh_observability_labels(reason="one")
        second = self.host.refresh_observability_labels(reason="two")
        third = self.host.refresh_observability_labels(reason="three")
        self.assertEqual(
            len(self._queued()), 1,
            "each refresh queued its own install job",
        )
        self.assertEqual(second, first)
        self.assertEqual(third, first)

    def test_a_running_install_does_not_absorb_a_later_change(self):
        """A started job already rendered its map; a change after it needs
        its own job or it waits for the reconciliation cron to be seen."""
        self.host.refresh_observability_labels(reason="one")
        queued = self._queued()
        queued.queue_job_id.write({"state": "started"})
        self.host.refresh_observability_labels(reason="two")
        self.assertEqual(len(self._queued()), 2)

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
        """Return the central playbook's task list, includes expanded.

        The tasks that write and mount the credential files now live in
        shared task files, because the account sync applies the same
        boundary and the two must not be able to disagree about it. This
        walks into them the way Ansible does — without it the guards
        below would keep passing while quietly checking a shrinking set,
        which is the failure mode they exist to prevent.
        """
        return self._expand(
            yaml.safe_load(_CENTRAL_PLAYBOOK.read_text())[0]["tasks"],
            _CENTRAL_PLAYBOOK.parent,
        )

    def _expand(self, tasks, base):
        """Return *tasks* with ``include_tasks`` replaced by their contents.

        :param tasks: task list as parsed from YAML
        :param base: directory the includes resolve against
        """
        out = []
        for task in tasks:
            included = task.get("ansible.builtin.include_tasks")
            if not included:
                out.append(task)
                continue
            path = base / str(included)
            out.extend(
                self._expand(yaml.safe_load(path.read_text()), path.parent),
            )
        return out

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


class TestTheAccountSyncSharesTheBoundary(BaseCase):
    """The sync and the deployment must not drift apart.

    They write the same file on the same host. The moment they stop
    doing it from the same tasks, one of them starts producing a
    boundary the other would not recognise — and nothing about a green
    deployment would reveal it.
    """

    _SYNC_PLAYBOOK = _ROOT / "ansible" / "playbooks" / "metrics_acl_sync.yml"
    _TASKS_DIR = _ROOT / "ansible" / "playbooks" / "tasks"

    def _includes(self, playbook):
        return {
            str(task["ansible.builtin.include_tasks"])
            for task in yaml.safe_load(playbook.read_text())[0]["tasks"]
            if task.get("ansible.builtin.include_tasks")
        }

    def test_both_playbooks_write_the_acl_from_the_same_task_file(self):
        shared = self._includes(_CENTRAL_PLAYBOOK) & self._includes(
            self._SYNC_PLAYBOOK,
        )
        self.assertIn("tasks/vmauth_acl_write.yml", shared)
        self.assertIn("tasks/vmauth_acl_apply.yml", shared)

    def test_neither_playbook_writes_the_acl_inline(self):
        """A second copy of the copy task is how they would drift.

        Matched on the rendered document rather than on the file path:
        the compose file legitimately *mounts* ``vmauth/auth.yml``, and
        only one task may *write* it.
        """
        for playbook in (_CENTRAL_PLAYBOOK, self._SYNC_PLAYBOOK):
            body = playbook.read_text()
            self.assertNotIn(
                "{{ ic_vmauth_config }}", body,
                f"{playbook.name} writes the access-control document "
                "itself instead of including the shared task file",
            )

    def test_the_sync_never_touches_the_compose_file(self):
        """Its whole safety argument is that it cannot recreate anything.

        The moment it writes the compose file, granting an account stops
        being a proxy reload and becomes a restart of the shared stack —
        which is the cost that made automating it look unacceptable.
        """
        body = self._SYNC_PLAYBOOK.read_text()
        self.assertNotIn("docker-compose.yaml:", body)
        self.assertNotIn("up -d", body)

    def test_the_dashboard_glob_resolves_from_the_task_file(self):
        """A wrong relative depth here does not fail, it provisions
        organisations with a datasource and no dashboards at all."""
        orgs = self._TASKS_DIR / "grafana_orgs.yml"
        globs = re.findall(r"fileglob',\s*'([^']+)'", orgs.read_text())
        self.assertEqual(len(globs), 1, "the dashboard glob moved or split")
        resolved = (orgs.parent / globs[0]).resolve()
        self.assertEqual(
            resolved.parent, _DASHBOARDS,
            f"the glob resolves to {resolved.parent}, not the shipped "
            f"dashboards directory",
        )
        self.assertTrue(
            list(orgs.parent.glob(globs[0])),
            "the glob matches no dashboard files from its own directory",
        )


class TestGrafanaIdentityIsNotInTheComposeFile(BaseCase):
    """Why adding a tenant stopped recreating Grafana.

    The organisation map names every account, so it changed with every
    tenant — and it lived in the compose file, whose every change makes
    ``up -d`` recreate the container. One new account dropped every open
    session in the fleet, which is what made automating the grant look
    unacceptable in the first place.

    Measured (lab, Grafana 11.2.0): the SSO settings API writes to
    Grafana's database, overrides the env vars (``source`` flips from
    ``system`` to ``database``) and applies with the restart count still
    at zero.
    """

    _TASKS_DIR = _ROOT / "ansible" / "playbooks" / "tasks"

    def _sso_task(self):
        for task in yaml.safe_load(
            (self._TASKS_DIR / "grafana_orgs.yml").read_text(),
        ):
            if "sso-settings" in str(task.get("ansible.builtin.uri", {})):
                return task
        self.fail("the SSO settings task was renamed or removed")

    def test_the_compose_file_carries_no_oauth_configuration(self):
        """Two sources for one block, with the database silently
        winning, is worse than either alone."""
        self.assertNotIn(
            "GF_AUTH_GENERIC_OAUTH",
            _CENTRAL_PLAYBOOK.read_text(),
            "the OAuth config is back in the compose file, so every new "
            "account recreates Grafana again — and the database copy "
            "written by the API would override it anyway",
        )

    def test_the_whole_provider_block_is_sent(self):
        """The API replaces the provider, it does not patch a field.

        Anything omitted here silently reverts to Grafana's default.
        ``orgAttributePath`` is the one that already cost us: with
        ``groupsAttributePath`` instead, org_mapping falls back to the
        default organisation and every login lands in "Main Org." while
        the token carries the right claim.
        """
        settings = self._sso_task()["ansible.builtin.uri"]["body"]["settings"]
        for key in (
            "enabled", "clientId", "clientSecret", "authUrl", "tokenUrl",
            "apiUrl", "scopes", "usePkce", "orgMapping", "orgAttributePath",
            "roleAttributePath", "allowAssignGrafanaAdmin", "autoLogin",
        ):
            self.assertIn(
                key, settings,
                f"{key} is missing from the SSO settings body; the PUT "
                f"replaces the whole provider, so it would silently "
                f"revert to Grafana's default",
            )
        self.assertEqual(settings["orgAttributePath"], "groups")

    def test_it_is_skipped_without_an_identity_provider(self):
        """The self-hosted case has none, and the playbook wires
        auth.proxy instead — enabling OAuth there would deploy a Grafana
        redirecting to a provider that will refuse it."""
        self.assertIn("ic_grafana_oidc", str(self._sso_task().get("when", "")))

    def test_it_goes_through_the_operator_door(self):
        """One door, not a second one to keep track of: vmauth
        authenticates the operator and swaps in Grafana's admin
        credential, so the panel never holds Grafana's password."""
        uri = self._sso_task()["ansible.builtin.uri"]
        self.assertIn("/gadmin/", uri["url"])
        self.assertEqual(uri["url_username"], "operator")


class TestObservabilityCapabilities(TransactionCase):
    """The one definition of what a panel can do with observability.

    Four surfaces used to decide this for themselves — the nav entry read
    the master switch, the Monitoring page read the switch *and* the
    Grafana URL, the Settings tab read who owns the settings, and the
    instance Metrics tab read none of them. Four definitions is four ways
    to contradict each other, and the contradiction that shipped was a
    tenant panel offering a Monitoring entry whose only possible message
    was an instruction to open the Settings tab it hides.
    """

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()

    def _caps(self):
        return self.env["cloud.settings"].sudo()._observability_capabilities()

    def test_off_collects_nothing_and_shows_nothing(self):
        self.settings.write({
            "metrics_enabled": False, "grafana_base_url": "",
        })
        self.assertEqual(
            self._caps(),
            {"collect": False, "dashboards": False, "configure": True},
        )

    def test_collecting_without_grafana_offers_no_dashboards(self):
        """The state the fix is about: real data, nothing to embed.

        Perfectly valid — the panel still enrols hosts, evaluates rules
        and reads metrics. What it must not do is offer dashboards.
        """
        self.settings.write({
            "metrics_enabled": True, "grafana_base_url": "",
        })
        caps = self._caps()
        self.assertTrue(caps["collect"])
        self.assertFalse(
            caps["dashboards"],
            "a panel with no Grafana URL reported dashboards, which is "
            "how the Monitoring section became a dead end",
        )

    def test_a_blank_grafana_url_is_not_a_grafana_url(self):
        """Whitespace is what a paste into an empty field leaves behind,
        and it builds an embed URL that loads the panel inside itself."""
        self.settings.write({
            "metrics_enabled": True, "grafana_base_url": "   ",
        })
        self.assertFalse(self._caps()["dashboards"])

    def test_fully_configured_offers_everything(self):
        self.settings.write({
            "metrics_enabled": True,
            "grafana_base_url": "https://grafana.example.com",
        })
        self.assertEqual(
            self._caps(),
            {"collect": True, "dashboards": True, "configure": True},
        )

    def test_dashboards_require_collection(self):
        """A URL alone is not dashboards: with collection off there are
        no series behind them, so the embed would render empty panels."""
        self.settings.write({
            "metrics_enabled": False,
            "grafana_base_url": "https://grafana.example.com",
        })
        self.assertFalse(self._caps()["dashboards"])

    def test_core_never_claims_someone_else_configures_it(self):
        """``configure`` is core's answer for its own operator, and core
        has no notion of a panel whose settings arrive from elsewhere.
        The layer that injects them says so; core saying it would mean
        core knowing about tenants."""
        for enabled in (True, False):
            self.settings.metrics_enabled = enabled
            self.assertTrue(self._caps()["configure"])

    def test_the_descriptor_carries_exactly_these_axes(self):
        """Pins the contract the SPA and the layers above consume.

        A key added here without its consumer, or renamed under one, is
        the class of drift this descriptor exists to end.
        """
        self.assertEqual(
            set(self._caps()), {"collect", "dashboards", "configure"},
        )


class TestNavGateConsultsEveryAxis(BaseCase):
    """The Monitoring nav entry must not regress to a single flag.

    Structural rather than behavioural because the gate lives in a QWeb
    attribute, and the bug it guards against is precisely the one that
    reads *plausible*: ``observability`` alone looks like it means "is
    observability on", and that reading is what put a dead-end entry in
    every tenant's sidebar.
    """

    def _nav_gate(self):
        app_xml = (_ROOT / "static" / "src" / "app" / "app.xml").read_text()
        match = re.search(
            r"<t t-if=\"([^\"]*can_view_metrics[^\"]*)\">\s*"
            r"<li[^>]*state\.route === 'monitoring'",
            app_xml,
        )
        self.assertIsNotNone(
            match, "could not find the Monitoring nav gate in app.xml",
        )
        return match.group(1)

    def test_it_reads_all_three_axes(self):
        gate = self._nav_gate()
        for axis in ("dashboards", "collect", "configure"):
            self.assertIn(
                f"observability?.{axis}", gate,
                f"the nav gate ignores the '{axis}' axis; a panel that "
                f"cannot act on what the section says would be offered "
                f"it anyway",
            )

    def test_it_offers_dashboards_or_a_way_to_set_them_up(self):
        """Either there is something to look at, or this operator is the
        one who could set it up. A tenant is neither, and that is the
        case the old single flag could not express."""
        gate = self._nav_gate()
        self.assertIn("observability?.dashboards or", gate)
        self.assertIn("observability?.configure", gate)


class TestTheDefaultOrganisationIsNotAPrivilegedFallback(BaseCase):
    """Grafana's default org must hold nothing worth landing in.

    It is where Grafana drops a login whose ``groups`` claim matches no
    entry in ``orgMapping`` — so whatever lives there is what an
    unrecognised user reads. File provisioning carries no orgId, which
    means a datasource provisioned from a file lands exactly there, and
    the one that used to be provisioned pointed at vmauth's operator
    path: no account filter, the whole fleet.

    Structural because the failure is silent from every angle. The
    tenant sees a dashboard that works, the operator sees an org nobody
    uses, and the only difference between the two readings is which
    session rendered the iframe.
    """

    _SYNC_PLAYBOOK = _ROOT / "ansible" / "playbooks" / "metrics_acl_sync.yml"
    _DATASOURCE_FILE = "provisioning/datasources"

    def _tasks(self, playbook):
        return yaml.safe_load(playbook.read_text())[0]["tasks"]

    def test_no_task_writes_a_datasource_provisioning_file(self):
        """Writing one at all is the bug: it can only land in org 1."""
        for task in self._tasks(_CENTRAL_PLAYBOOK):
            copy = task.get("ansible.builtin.copy") or {}
            self.assertNotIn(
                self._DATASOURCE_FILE, str(copy.get("dest", "")),
                f"'{task.get('name')}' provisions a datasource from a "
                f"file, and file provisioning has no orgId: it lands in "
                f"the default organisation, which is the fallback for "
                f"every unmapped login",
            )

    def test_the_provisioned_datasource_is_removed(self):
        """Not writing it is not enough on an already-deployed central."""
        removals = [
            task for task in self._tasks(_CENTRAL_PLAYBOOK)
            if self._DATASOURCE_FILE
            in str((task.get("ansible.builtin.file") or {}).get("path", ""))
        ]
        self.assertEqual(
            len(removals), 1,
            "exactly one task should account for the datasource "
            "provisioning file",
        )
        self.assertEqual(
            removals[0]["ansible.builtin.file"].get("state"), "absent",
        )

    def test_the_datasource_already_in_grafana_is_deleted(self):
        """The file is not the datasource.

        Removing the provisioning file stops a *new* Grafana from making
        one; the row an earlier deployment created lives in Grafana's
        database and outlives every file. Without this task the fix
        would read as applied and change nothing on the only centrals
        that have the problem.
        """
        tasks = self._tasks(_CENTRAL_PLAYBOOK)
        deletes = [
            i for i, task in enumerate(tasks)
            if (task.get("ansible.builtin.uri") or {}).get("method") == "DELETE"
            and "api/datasources/name/"
            in str((task.get("ansible.builtin.uri") or {}).get("url", ""))
        ]
        self.assertEqual(len(deletes), 1, "the datasource delete is missing")
        deleted = tasks[deletes[0]]["ansible.builtin.uri"]
        self.assertIn(
            404, deleted.get("status_code", []),
            "a central deployed after this change never had one; absent "
            "must be a success, not a failed deployment",
        )
        # Grafana's current org is a persistent property of the
        # authenticated user, not a per-request header. A delete that
        # does not immediately follow its own switch deletes whatever
        # the previous switch left current — which, in a playbook that
        # loops over every account, is somebody's real datasource.
        before = tasks[deletes[0] - 1].get("ansible.builtin.uri") or {}
        self.assertEqual(before.get("method"), "POST")
        self.assertTrue(
            str(before.get("url", "")).endswith("/gadmin/api/user/using/1"),
            "the delete is not immediately preceded by a switch into the "
            "default organisation",
        )

    def test_the_organisation_map_is_rewritten_on_every_sync(self):
        """``orgMapping`` names every account, so it cannot be gated.

        The sync used to include this only when it had new accounts to
        grant. The organisations are indeed the only thing that needs a
        new account — but the map is rewritten whole, and skipping it
        leaves an account minted and unmapped until the next full
        deployment of the central. Its user lands in the default
        organisation for the entire window.
        """
        includes = [
            task for task in self._tasks(self._SYNC_PLAYBOOK)
            if task.get("ansible.builtin.include_tasks")
            == "tasks/grafana_orgs.yml"
        ]
        self.assertEqual(len(includes), 1)
        self.assertNotIn(
            "when", includes[0],
            "the sync gates the organisation map on there being new "
            "accounts; a revocation-only sync would then leave the map "
            "naming an account that no longer exists, and a grant whose "
            "org already existed would never refresh it",
        )
