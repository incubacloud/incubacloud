"""The parts of observability that are wiring, not logic.

Two things here failed silently rather than loudly, which is why they
are worth pinning:

* A chart can filter on a label the pipeline never produces, and a
  blank chart is indistinguishable from "no traffic". Traefik
  samples CAN carry an instance label — the service names are derived
  from the project name the panel itself feeds copier — but only while
  the scrape config actually emits the rules that attach it.
* The per-host label map is a snapshot taken when the agents are
  installed. If nothing re-applies it, every instance deployed after
  Host Setup reports containers no rule can attribute.
"""
import ast
import json
import pathlib
import re
from xml.etree import ElementTree

import jinja2
import yaml

from odoo import fields
from odoo.tests.common import BaseCase, TransactionCase


_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CENTRAL_PLAYBOOK = _ROOT / "ansible" / "playbooks" / "observability_central.yml"
_MONITORING_JS = (
    _ROOT / "static" / "src" / "components" / "monitoring" / "monitoring.js"
)

class TestTheChartsAreThePanels(BaseCase):

    def test_monitoring_embeds_no_dashboard_of_another_tool(self):
        """Monitoring draws its own boards since 12-nov-2026."""
        js = _MONITORING_JS.read_text()
        xml = _MONITORING_JS.with_suffix(".xml").read_text()
        self.assertNotIn("<iframe", xml)
        self.assertNotIn("grafana", js.lower())
        self.assertIn("FleetBoard", js)
        self.assertIn("HostBoard", js)

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
        "the agent config produces it", so a panel may: the rule is
        checked whether or not a panel uses it yet, rather than the test
        skipping until one does.
        """
        playbook = (
            _ROOT / "ansible" / "playbooks" / "host_observability.yml"
        ).read_text()
        traefik_block = playbook.split("job_name: traefik", 1)[-1]
        self.assertIn(
            "traefik_prefix", traefik_block,
            "a dashboard filters Traefik by instance but the scrape "
            "config emits no rule that could attach the label",
        )

    def test_the_disk_collector_names_its_instance_label_apart(self):
        """A legend can only name a label the exposed metric carries.

        And it must not name ``instance``: the scraper owns that one and
        overwrites it with the target address, renaming whatever the
        metric called that to ``exported_instance``. A chart keyed on it
        renders every series of a host under the same legend.
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
        self.assertIn("instance_name", exposed)

    def test_no_dashboard_of_another_tool_is_shipped(self):
        """The panel draws every chart; the central runs no Grafana."""
        self.assertFalse(list((_ROOT / "ansible" / "files").glob("**/*.json")))


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
            "traefik_deployed": True,
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
        failures for teardowns that had already finished. The first
        refresh is dispatched at once; the host's queue keeps the second
        waiting behind it, and every further one collapses onto that
        waiting job (the type is ``queue_collapse``), which is safe
        because the playbook renders its label map when it runs.
        """
        first = self.host.refresh_observability_labels(reason="one")
        second = self.host.refresh_observability_labels(reason="two")
        third = self.host.refresh_observability_labels(reason="three")
        fourth = self.host.refresh_observability_labels(reason="four")
        self.assertEqual(
            len(self._queued()), 2,
            "each refresh queued its own install job",
        )
        self.assertNotEqual(second, first)
        self.assertEqual(third, second)
        self.assertEqual(fourth, second)
        self.assertEqual(
            self.env["cloud.job"].browse([first, second]).mapped("state"),
            ["pending", "queued"],
        )

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
            # Prepared: Full Setup has run (it sets this as it ends).
            "traefik_deployed": True,
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
        """Installed by the playbook of this version: nothing to re-apply."""
        from ..models.observability_agents_executor import AGENTS_REVISION

        self.host.write({
            "metrics_agents_state": "installed",
            "metrics_agents_revision": AGENTS_REVISION,
        })
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

    def test_a_host_still_being_set_up_is_not_enrolled(self):
        """A host bought on demand has its key a minute after the purchase,
        long before Full Setup installs Docker; its agents wait for Full
        Setup, which queues them as it ends (2026-10-09)."""
        self.host.write({"traefik_deployed": False})
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
    unprivileged. Writing their configuration owner-only is the intuitive
    "careful" choice and it is the wrong one — the first live deployment
    (nginx, then) answered ``permission denied`` on its own credential
    file, from a file that looked perfect on the host.

    Then the obvious fix broke it a second way: some paths are mounted as
    DIRECTORIES, so tightening the whole tree to 0700 left the containers
    unable to traverse them. Confidentiality therefore lives on the root
    of the tree, which nothing mounts, and everything below it stays
    reachable. Both mistakes are cheap to make and silent from the host,
    so both are pinned here.
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
                f"{path} is mounted into a container as a directory but "
                f"is written {mode}: its process cannot traverse it",
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

    _VAR = re.compile(r"\bic_[a-z0-9_]+")
    _INCLUDE = re.compile(r"include_tasks:\s*(\S+)")
    # A ``set_fact:`` or ``vars:`` mapping, with or without the FQCN
    # prefix, and the indented block that belongs to it.
    _DEFINING_BLOCK = re.compile(
        r"^(\s+)(?:ansible\.builtin\.)?(?:set_fact|vars):\s*\n"
        r"((?:\1\s+.*\n|\s*\n)+)",
        re.MULTILINE,
    )

    def _playbook_files(self, playbook):
        """Every file the play runs, following ``include_tasks``."""
        seen, stack = set(), [playbook]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for include in self._INCLUDE.findall(current.read_text()):
                # Bare names resolve against the including file; the
                # playbook itself writes them as ``tasks/<name>``.
                for candidate in (
                    current.parent / include,
                    playbook.parent / include,
                ):
                    if candidate.exists():
                        stack.append(candidate)
                        break
                else:
                    self.fail(
                        f"{current.name} includes {include!r}, which does "
                        f"not resolve; the walk would silently skip "
                        f"whatever that file needs"
                    )
        return seen

    def _undefined_vars(self, playbook):
        """``ic_*`` names the play reads but never defines for itself.

        Deliberately ignores anything the play gives a default: a var
        with a default cannot raise, so it is the playbook's business.
        What is left is what Ansible would resolve to undefined.
        """
        referenced, defined = set(), set()
        for path in sorted(self._playbook_files(playbook)):
            body = path.read_text()
            for expression in re.findall(r"\{\{(.*?)\}\}", body, re.DOTALL):
                referenced |= set(self._VAR.findall(expression))
            for keyword in ("when", "until", "changed_when", "failed_when"):
                for line in re.findall(rf"^\s*{keyword}:\s*(.+)$", body, re.MULTILINE):
                    referenced |= set(self._VAR.findall(line))
            for keyword in ("register", "loop_var"):
                for name in re.findall(rf"^\s*{keyword}:\s*(\S+)", body, re.MULTILINE):
                    defined |= set(self._VAR.findall(name))
            for _indent, block in self._DEFINING_BLOCK.findall(body):
                defined |= set(re.findall(r"^\s*(ic_[a-z0-9_]+):", block, re.MULTILINE))
        return referenced - defined

    def _extra_vars_keys(self, module, class_name):
        """The keys THIS class's ``get_extra_vars`` returns.

        Read off the override rather than off a live instance on
        purpose: the parent's version is dead code for a subclass that
        does not call ``super()``, and that is exactly the situation
        under test.
        """
        tree = ast.parse((_ROOT / "models" / module).read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.ClassDef)
                    and node.name == class_name):
                continue
            for member in node.body:
                if not (isinstance(member, ast.FunctionDef)
                        and member.name == "get_extra_vars"):
                    continue
                return {
                    key.value
                    for statement in ast.walk(member)
                    if isinstance(statement, ast.Return)
                    and isinstance(statement.value, ast.Dict)
                    for key in statement.value.keys
                    if isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                }
        self.fail(f"{class_name}.get_extra_vars not found in {module}")

    def test_every_executor_supplies_the_vars_its_playbook_needs(self):
        """An override that drops a var reads back as a broken boundary.

        ``MetricsAclSyncExecutor`` overrides ``get_extra_vars`` without
        calling ``super()`` — deliberately, because the parent's version
        announces "Deploying the metrics central", which a sync must not
        claim. The price is two dicts maintained by hand, and the first
        thing that fell out of the child's was ``ic_operator_plain``:
        the boundary probe then authenticated with no password at all,
        vmauth answered 401, and the gateway looked broken rather than
        the caller. Every account minted afterwards stayed ungranted.
        """
        for module, class_name, playbook in (
            ("metrics_acl_sync_executor.py",
             "MetricsAclSyncExecutor", self._SYNC_PLAYBOOK),
            ("observability_central_executor.py",
             "ObservabilityCentralExecutor", _CENTRAL_PLAYBOOK),
        ):
            with self.subTest(executor=class_name):
                missing = self._undefined_vars(playbook) - (
                    self._extra_vars_keys(module, class_name)
                )
                self.assertEqual(
                    missing, set(),
                    f"{class_name}.get_extra_vars does not supply "
                    f"{sorted(missing)}, which {playbook.name} reads and "
                    f"never defines; Ansible resolves those to undefined",
                )


class TestObservabilityCapabilities(TransactionCase):
    """The one definition of what a panel can do with observability.

    Four surfaces used to decide this for themselves — the nav entry read
    the master switch, the Monitoring page read the switch *and* the
    Grafana URL, the Settings tab read who owns the settings, and the
    instance Metrics tab read none of them. Four definitions is four ways
    to contradict each other, and the contradiction that shipped was a
    managed panel offering a Monitoring entry whose only possible message
    was an instruction to open the Settings tab it hides.
    """

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()

    def _caps(self):
        return self.env["cloud.settings"].sudo()._observability_capabilities()

    def test_off_collects_nothing_and_shows_nothing(self):
        self.settings.write({
            "metrics_enabled": False, "metrics_central_url": "",
        })
        self.assertEqual(
            self._caps(),
            {"collect": False, "dashboards": False, "configure": True},
        )

    def test_collecting_without_a_central_offers_no_dashboards(self):
        """Real data on its way, nothing to read it from yet.

        Valid while the central is being deployed. What the panel must
        not do is offer charts with nowhere to read them.
        """
        self.settings.write({
            "metrics_enabled": True, "metrics_central_url": "",
        })
        caps = self._caps()
        self.assertTrue(caps["collect"])
        self.assertFalse(
            caps["dashboards"],
            "a panel with no central reported dashboards, which is how "
            "the Monitoring section became a dead end",
        )

    def test_a_blank_central_url_is_not_a_central(self):
        """Whitespace is what a paste into an empty field leaves behind."""
        self.settings.write({
            "metrics_enabled": True, "metrics_central_url": "   ",
        })
        self.assertFalse(self._caps()["dashboards"])

    def test_a_central_is_all_the_charts_need(self):
        """The panel draws its own charts: no Grafana URL is asked for
        (12-nov-2026), so a managed panel, which never had one, shows
        its own servers."""
        self.settings.write({
            "metrics_enabled": True,
            "metrics_central_url": "https://central.example.com/r",
        })
        self.assertEqual(
            self._caps(),
            {"collect": True, "dashboards": True, "configure": True},
        )

    def test_dashboards_require_collection(self):
        """A central alone is not dashboards: with collection off no
        agent sends series, so every chart would be empty."""
        self.settings.write({
            "metrics_enabled": False,
            "metrics_central_url": "https://central.example.com/r",
        })
        self.assertFalse(self._caps()["dashboards"])

    def test_core_never_claims_someone_else_configures_it(self):
        """``configure`` is core's answer for its own operator, and core
        has no notion of a panel whose settings arrive from elsewhere.
        The layer that injects them says so; core saying it would mean
        core knowing about other panels."""
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
    every managed panel's sidebar.
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
        one who could set it up. A managed panel is neither, and that is the
        case the old single flag could not express."""
        gate = self._nav_gate()
        self.assertIn("observability?.dashboards or", gate)
        self.assertIn("observability?.configure", gate)


class TestTheBackendRefusesDeletionsWithoutTheKey(BaseCase):
    """VictoriaMetrics starts with the deletion key whenever there is one.

    Without ``-deleteAuthKey`` it deletes for anyone who can reach it,
    and measured (lab, 2026-09-27) an account could: through ``/r/`` or
    ``/w/`` it deleted its own series, the forced filter being the only
    thing keeping it to its own. The flag is rendered into the compose
    file, so it is checked here the way Ansible renders it.

    The key itself travels in a file since 12-nov-2026: as the flag's
    value it was in the container's command, which ``docker inspect``
    shows to the host's docker group.
    """

    _FILE = "/etc/victoriametrics/delete_auth_key"

    def _tasks(self):
        """Return the central playbook's tasks."""
        return yaml.safe_load(_CENTRAL_PLAYBOOK.read_text())[0]["tasks"]

    def _vm_service(self, key):
        """Return the VictoriaMetrics service the playbook would write.

        :param key: the ``ic_delete_auth_key`` value to render with.
        :return: the ``victoriametrics`` service of the compose file.
        """
        import jinja2  # noqa: PLC0415 — only this test renders a template

        task = next(
            t for t in self._tasks()
            if t.get("name") == "Write the central compose file"
        )
        content = task["ansible.builtin.copy"]["content"]
        # Ansible's own defaults: trim_blocks on, lstrip_blocks off.
        rendered = jinja2.Environment(  # nosec B701 — renders YAML as Ansible does, never HTML
            trim_blocks=True,
        ).from_string(
            content,
        ).render(
            ic_delete_auth_key=key, ic_retention_days=90,
            ic_central_dir="/root/observability-central",
        )
        return yaml.safe_load(rendered)["services"]["victoriametrics"]

    def _vm_command(self, key):
        """Return the VictoriaMetrics command the playbook would write."""
        return self._vm_service(key)["command"]

    def test_the_key_becomes_the_flag_through_a_file(self):
        command = self._vm_command("s3cr3t")
        self.assertIn(f"-deleteAuthKey=file://{self._FILE}", command)
        self.assertNotIn("s3cr3t", "\n".join(command))

    def test_the_file_s_directory_is_mounted_read_only(self):
        """The directory, not the file: a replaced file is a new inode."""
        self.assertIn(
            "/root/observability-central/victoriametrics:/etc/victoriametrics:ro",
            self._vm_service("s3cr3t")["volumes"],
        )

    def test_the_file_is_written_owner_only_and_never_logged(self):
        task = next(
            t for t in self._tasks()
            if t.get("name") == "Write the backend's deletion key"
        )
        copy = task["ansible.builtin.copy"]
        self.assertTrue(copy["dest"].endswith("/victoriametrics/delete_auth_key"))
        self.assertEqual(copy["content"], "{{ ic_delete_auth_key }}")
        self.assertEqual(copy["mode"], "0600")
        self.assertIs(task["no_log"], True)
        names = [t.get("name") for t in self._tasks()]
        self.assertLess(
            names.index("Write the backend's deletion key"),
            names.index("Bring the central stack up"),
        )

    def test_no_key_renders_no_flag(self):
        """An empty flag would be a key of '' — the lock left open."""
        service = self._vm_service("")
        command = service["command"]
        self.assertFalse(
            [arg for arg in command if arg.startswith("-deleteAuthKey")],
        )
        self.assertIn("-retentionPeriod=90d", command)
        self.assertEqual(service["volumes"], ["vmdata:/vmdata"])


class TestTeardownStopsTheAgents(BaseCase):
    """A retired host stops pushing with this panel's credential.

    The panel deletes a retired host's series. Agents left running on a
    machine that outlives its record (a BYOH server) would recreate them
    on the next scrape, for ever, with a credential the panel no longer
    accounts for.
    """

    _TEARDOWN = _ROOT / "ansible" / "playbooks" / "host_teardown.yml"
    _INSTALL = _ROOT / "ansible" / "playbooks" / "host_observability.yml"

    def _tasks(self, playbook):
        """Return the task list of *playbook*."""
        return yaml.safe_load(playbook.read_text())[0]["tasks"]

    def test_the_agents_stack_is_brought_down(self):
        install_project = re.search(
            r"docker compose -p (\S+)", self._INSTALL.read_text(),
        ).group(1)
        commands = [
            t["ansible.builtin.command"]["cmd"]
            for t in self._tasks(self._TEARDOWN)
            if "ansible.builtin.command" in t
        ]
        self.assertIn(
            f"docker compose -p {install_project} -f docker-compose.yaml down --volumes",
            commands,
        )

    def test_the_disk_collector_cron_is_removed_by_its_install_name(self):
        """``cron`` removes by name: a different name removes nothing."""
        installed = {
            t["ansible.builtin.cron"]["name"]
            for t in self._tasks(self._INSTALL)
            if "ansible.builtin.cron" in t
        }
        removed = {
            t["ansible.builtin.cron"]["name"]
            for t in self._tasks(self._TEARDOWN)
            if t.get("ansible.builtin.cron", {}).get("state") == "absent"
        }
        self.assertTrue(installed)
        self.assertEqual(installed, removed)

    def test_the_directory_holding_the_credential_is_removed(self):
        removed = [
            t["ansible.builtin.file"]["path"]
            for t in self._tasks(self._TEARDOWN)
            if t.get("ansible.builtin.file", {}).get("state") == "absent"
        ]
        self.assertIn("{{ ic_obs_dir }}", removed)


class TestTheAgentsAreNotReachableFromTheInstances(BaseCase):
    """The host's instances share ``inverseproxy_shared``; the agents must not.

    That network is flat: every instance's Odoo, stopped ones included,
    and the proxy see each other on it. vmagent used to join it to scrape
    Traefik, and listened on ``:8429`` there, so any process running in
    an instance could push series that vmagent forwards to the central
    under the host's own credential — activity that never happened,
    enough to keep an idle instance from being suspended. Nothing reads
    vmagent's port from another container, so it listens on its own
    loopback, and it reaches Traefik over the proxy's ``public`` network,
    which no instance joins.
    """

    _PLAYBOOK = _ROOT / "ansible" / "playbooks" / "host_observability.yml"

    def _play(self):
        """Return the playbook's only play, as Ansible parses it."""
        return yaml.safe_load(self._PLAYBOOK.read_text())[0]

    def _task(self, name):
        """Return the task called *name*, failing if it was renamed."""
        for task in self._play()["tasks"]:
            if task.get("name") == name:
                return task
        self.fail(f"the task {name!r} was renamed or removed")

    def _render(self, name, **variables):
        """Render the file one ``copy`` task writes, as Ansible renders it.

        :param str name: the task's name.
        :param variables: the play's variables to override.
        :return: the rendered file, parsed.
        :rtype: dict
        """
        values = dict(self._play()["vars"])
        values.update(
            ic_obs_dir="/home/ops/observability",
            ic_remote_write_url="https://metrics.example.test/w/api/v1/write",
            # What the play settles on after its try of the direct path.
            ic_write_url="https://metrics.example.test/w/api/v1/write",
            ic_logs_url="",
            ic_remote_write_user="acct",
            ic_logs_write_url="",
            ic_host_name="host-a",
            ic_host_id="7",
            ic_account="acct",
            ic_instances=[],
            ic_has_traefik=True,
            # Set by the play: whether this server sends its security logs.
            ic_fluent_bit=False,
        )
        values.update(variables)
        content = self._task(name)["ansible.builtin.copy"]["content"]
        # Ansible's own defaults: trim_blocks on, lstrip_blocks off.
        env = jinja2.Environment(  # nosec B701 — renders YAML as Ansible does, never HTML
            trim_blocks=True, undefined=jinja2.StrictUndefined,
        )
        # Ansible's filter, which the playbook uses.
        env.filters["to_json"] = lambda value, **kw: json.dumps(value, **kw)
        return yaml.safe_load(env.from_string(content).render(**values))

    def _vmagent(self, **variables):
        """Return vmagent's service in the agents' compose file."""
        compose = self._render("Write the agents compose file", **variables)
        return compose, compose["services"]["vmagent"]

    def test_vmagent_listens_on_its_own_loopback_only(self):
        _compose, vmagent = self._vmagent()
        self.assertIn("-httpListenAddr=127.0.0.1:8429", vmagent["command"])

    def test_vmagent_reaches_traefik_over_the_proxy_own_network(self):
        compose, vmagent = self._vmagent()
        self.assertEqual(vmagent["networks"], ["default", "traefik"])
        self.assertEqual(
            compose["networks"]["traefik"],
            {"external": True, "name": "inverseproxy_public"},
        )

    def test_no_agent_joins_the_instances_network(self):
        compose, _vmagent = self._vmagent()
        self.assertNotIn(
            "inverseproxy_shared", yaml.safe_dump(compose),
            "an agent on the instances' network is reachable from every "
            "instance on the host",
        )

    def test_the_scrape_is_enabled_by_the_network_it_uses(self):
        """Detected on the network vmagent joins, not on another one."""
        detect = self._task("Detect whether Traefik runs on this host")
        self.assertEqual(
            detect["ansible.builtin.command"],
            "docker network inspect inverseproxy_public",
        )

    def test_a_host_without_traefik_joins_no_proxy_network(self):
        """Must not change: an external network that does not exist
        stops the whole stack from starting."""
        compose, vmagent = self._vmagent(ic_has_traefik=False)
        self.assertEqual(vmagent["networks"], ["default"])
        self.assertNotIn("traefik", compose["networks"])

    def test_traefik_is_still_scraped_by_its_service_name(self):
        """Must not change: the target and the job name are what the
        relabelling and every Traefik panel are keyed on."""
        scrape = self._render("Write the vmagent scrape configuration")
        jobs = {job["job_name"]: job for job in scrape["scrape_configs"]}
        self.assertEqual(
            jobs["traefik"]["static_configs"], [{"targets": ["proxy:8082"]}],
        )
        self.assertNotIn(
            "traefik",
            {
                job["job_name"]
                for job in self._render(
                    "Write the vmagent scrape configuration",
                    ic_has_traefik=False,
                )["scrape_configs"]
            },
        )

    def test_vmagent_still_pushes_with_the_host_credential(self):
        """Must not change: the remote write and its credential file."""
        _compose, vmagent = self._vmagent()
        self.assertIn(
            "-remoteWrite.url=https://metrics.example.test/w/api/v1/write",
            vmagent["command"],
        )
        self.assertIn(
            "-remoteWrite.basicAuth.passwordFile=/etc/remote_write_token",
            vmagent["command"],
        )


class TestMonitoringExplainsAnEmptyFleet(BaseCase):
    """2-nov-2026: a panel without a server saw every chart read "No data"."""

    _TEMPLATE = _ROOT / "static" / "src" / "components" / "monitoring" / "monitoring.xml"

    def test_the_empty_fleet_is_explained_before_any_board_is_drawn(self):
        page = ElementTree.parse(  # nosec B314 — our own template, shipped in this repo
            self._TEMPLATE,
        ).find(".//t[@t-name='incubacloud.Monitoring']/div")
        branches = [
            el for el in page
            if el.tag == "t" and ({"t-if", "t-elif", "t-else"} & set(el.attrib))
        ]
        empty = next(i for i, el in enumerate(branches) if el.get("t-elif") == "!hasHosts")
        board = next(i for i, el in enumerate(branches) if el.find(".//FleetBoard") is not None)
        self.assertLess(empty, board)
        self.assertIn("No servers yet", ElementTree.tostring(branches[empty], encoding="unicode"))
