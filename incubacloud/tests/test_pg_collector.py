"""The database collector, the sizes of the disk collector and the agents' revision.

Three things travel with the agents' install since 12-nov-2026:

* ``collect_pg.sh``, root's, every minute: its install must never fail the
  job that runs at the end of every deploy, it lives where the connecting
  user cannot change it, a setting removes it, and the teardown removes it;
* the disk collector measures each database and filestore, the filestore
  once an hour on the small servers;
* the host records the revision of the install it last completed, and the
  reconciliation cron re-applies, a few per pass, the hosts on an older one.

The shell of ``collect_pg.sh`` is tested by ``tests/shell/collect_pg.bats``.
"""
import asyncio
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import timedelta
from unittest.mock import MagicMock, patch

import jinja2
import yaml

from odoo import fields
from odoo.http import Request
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud
from odoo.addons.incubacloud.models import cloud_host_observability
from odoo.addons.incubacloud.models.observability_agents_executor import (
    AGENTS_REVISION,
    ObservabilityAgentsExecutor,
)
from odoo.addons.incubacloud.tests.test_observability_executors import (
    ObservabilityExecutorCase,
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_INSTALL = _ROOT / "ansible" / "playbooks" / "host_observability.yml"
_TEARDOWN = _ROOT / "ansible" / "playbooks" / "host_teardown.yml"
_COLLECTOR = _ROOT / "ansible" / "playbooks" / "files" / "collect_pg.sh"


def _play(path):
    """Return the only play of the playbook at *path*, as Ansible parses it."""
    return yaml.safe_load(path.read_text())[0]


def _all_tasks(tasks):
    """Yield every task of *tasks*, those inside blocks and rescues too."""
    for task in tasks:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _all_tasks(task.get(key, ()))


def _task(play, name):
    """Return the task called *name*, wherever it is nested."""
    for task in _all_tasks(play["tasks"]):
        if task.get("name") == name:
            return task
    raise AssertionError(f"no task {name!r}: renamed or removed")


def _render(template, **values):
    """Render *template* as Ansible does (trim_blocks on, its filters)."""
    env = jinja2.Environment(  # nosec B701 — renders shell and YAML as Ansible does, never HTML
        trim_blocks=True, undefined=jinja2.StrictUndefined,
    )
    env.filters["regex_replace"] = lambda value, pattern, repl: re.sub(
        pattern, repl, value,
    )
    env.filters["int"] = int
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")
    return env.from_string(template).render(**values)


class TestTheCollectorCannotStopThePlaybook(BaseCase):
    """A3 of the review of 12-nov-2026, as the owner changed it the same
    day: whatever the collectors need (cron, sudo, apt, a package) goes
    wrong in a rescue, so the agents still come up; the facts say what
    failed, and the executor fails the job with what to do."""

    def setUp(self):
        super().setUp()
        self.play = _play(_INSTALL)
        self.block = _task(self.play, "Install the database collector")

    def test_its_install_is_a_block_with_a_rescue(self):
        self.assertIn("rescue", self.block)
        self.assertEqual(self.block["when"], "ic_collect_pg | bool")
        rescue = self.block["rescue"][0]["ansible.builtin.set_fact"]
        self.assertEqual(rescue["ic_pg_collector"], "failed")
        self.assertIn("ansible_failed_result", rescue["ic_pg_collector_error"])

    def test_every_step_that_needs_root_is_in_the_block(self):
        """No ``become`` outside a block that rescues it."""
        inside = {id(t) for t in _all_tasks(self.block["block"])}
        for name in ("Remove the database collector, switched off",
                     "Install cron when the server has none"):
            block = _task(self.play, name)
            self.assertIn("rescue", block, name)
            inside |= {id(t) for t in _all_tasks(block["block"])}
        for task in _all_tasks(self.play["tasks"]):
            if "become" in task:
                self.assertIn(id(task), inside, task["name"])

    def test_root_is_asked_for_only_when_not_connected_as_root(self):
        """``become`` as root still calls sudo, which a minimal server lacks."""
        for task in _all_tasks(self.block["block"]):
            if "become" in task:
                self.assertEqual(task["become"], "{{ not (ic_as_root | bool) }}", task["name"])

    def test_the_client_is_installed_only_when_missing_and_waits_for_apt_briefly(self):
        apt_task = _task(self.play, "Install the PostgreSQL client")
        self.assertEqual(apt_task["when"], "ic_psql.rc != 0")
        apt = apt_task["ansible.builtin.apt"]
        self.assertEqual(apt["name"], "postgresql-client")
        self.assertEqual(apt["lock_timeout"], 60)
        look = _task(self.play, "Look for a PostgreSQL client")
        self.assertEqual(look["failed_when"], False)

    def test_the_outcome_is_exported_and_shown_on_the_board(self):
        stats = _task(self.play, "Export the agent facts")["ansible.builtin.set_stats"]["data"]
        self.assertIn("ic_pg_collector", stats)
        self.assertIn("ic_pg_collector_error", stats)
        board = _task(self.play, "Say on the server's board whether the collector runs")
        for state, value in (("failed", "0"), ("installed", "1")):
            content = _render(
                board["ansible.builtin.copy"]["content"], ic_pg_collector=state,
            )
            self.assertIn(f"ic_pg_collector {value}", content.splitlines())

    def test_the_agents_are_up_whatever_became_of_the_collector(self):
        """The collector comes before the agents start, and stops nothing."""
        names = [t.get("name") for t in self.play["tasks"]]
        self.assertLess(
            names.index("Install the database collector"),
            names.index("Bring the agents up"),
        )

    def test_cron_is_installed_before_anything_is_scheduled(self):
        """Debian 13 ships without cron: the project's image had none and
        the disk collector's schedule failed the install (12-nov-2026)."""
        names = [t.get("name") for t in self.play["tasks"]]
        self.assertLess(
            names.index("Install cron when the server has none"),
            names.index("Install the database collector"),
        )
        self.assertLess(
            names.index("Install cron when the server has none"),
            names.index("Schedule the disk collector"),
        )
        check = _task(self.play, "Check that cron is there to run it")
        self.assertEqual(check["failed_when"], "not ic_crontab_now.stat.exists")

    def test_without_cron_the_disk_collector_is_not_scheduled(self):
        """M-a of the final review (12-nov-2026): cron's own install may
        fail (apt's lock, sudo asking a password), and the cron module
        fails without crontab. The playbook goes on and says so; the
        executor fails the job (``TestTheInstallRecordsItsRevision``)."""
        names = [t.get("name") for t in self.play["tasks"]]
        look = _task(self.play, "Look for cron before scheduling the disk collector")
        self.assertEqual(look["ansible.builtin.stat"]["path"], "/usr/bin/crontab")
        self.assertEqual(look["register"], "ic_crontab_disk")
        self.assertLess(
            names.index("Look for cron before scheduling the disk collector"),
            names.index("Schedule the disk collector"),
        )
        self.assertEqual(
            _task(self.play, "Schedule the disk collector")["when"],
            "ic_crontab_disk.stat.exists",
        )
        rescue = _task(self.play, "Note cron not installed, and why")["ansible.builtin.set_fact"]
        self.assertIn("ansible_failed_result", rescue["ic_cron_error"])
        note = _task(self.play, "Note the disk collector not scheduled")
        self.assertEqual(note["when"], "not ic_crontab_disk.stat.exists")
        self.assertEqual(note["ansible.builtin.set_fact"]["ic_disk_collector"], "failed")
        first = _task(self.play, "Note whether this run connects as root")
        self.assertEqual(first["ansible.builtin.set_fact"]["ic_disk_collector"], "installed")
        stats = _task(self.play, "Export the agent facts")["ansible.builtin.set_stats"]["data"]
        self.assertEqual(stats["ic_disk_collector"], "{{ ic_disk_collector }}")
        self.assertEqual(stats["ic_cron_error"], "{{ ic_cron_error }}")

    def test_every_cron_task_waits_for_crontab(self):
        """No ``cron`` module call where crontab may be missing."""
        for path in (_INSTALL, _TEARDOWN):
            for task in _all_tasks(_play(path)["tasks"]):
                if "ansible.builtin.cron" in task:
                    self.assertIn("stat.exists", str(task.get("when")), f"{path.name}: {task['name']}")


class TestTheCollectorIsRoots(BaseCase):
    """M4: what root runs, the connecting user cannot change."""

    def setUp(self):
        super().setUp()
        self.play = _play(_INSTALL)
        self.vars = self.play["vars"]

    def test_script_list_and_schedule_live_outside_the_home(self):
        self.assertEqual(self.vars["ic_lib_dir"], "/usr/local/lib/incubacloud")
        self.assertEqual(self.vars["ic_pg_cron_file"], "/etc/cron.d/incubacloud-collect-pg")
        for name in (
            "Create the collector's directory", "Install the collector",
            "Write the databases the collector reads",
            "Schedule the collector every minute, as root",
        ):
            task = _task(self.play, name)
            module = task.get("ansible.builtin.copy") or task.get("ansible.builtin.file")
            self.assertEqual((module["owner"], module["group"]), ("root", "root"), name)
            self.assertNotIn("ic_obs_dir", module.get("dest", module.get("path")), name)

    def test_the_installed_script_is_the_tested_one(self):
        copy = _task(self.play, "Install the collector")["ansible.builtin.copy"]
        self.assertEqual(
            (_INSTALL.parent / "files" / copy["src"]).resolve(), _COLLECTOR.resolve(),
        )
        self.assertTrue(os.access(_COLLECTOR, os.X_OK))

    def test_the_schedule_runs_every_minute_as_root(self):
        content = _render(
            _task(self.play, "Schedule the collector every minute, as root")
            ["ansible.builtin.copy"]["content"],
            ic_lib_dir="/usr/local/lib/incubacloud", ic_obs_dir="/home/ops/observability",
        )
        line = next(l for l in content.splitlines() if l.startswith("*"))
        self.assertEqual(
            line,
            "* * * * * root /usr/local/lib/incubacloud/collect_pg.sh "
            "/usr/local/lib/incubacloud/collect_pg.instances "
            "/home/ops/observability/textfile >/dev/null 2>&1",
        )
        self.assertIn("PATH=", content)

    def test_the_list_names_each_database_as_the_script_reads_it(self):
        content = _render(
            _task(self.play, "Write the databases the collector reads")
            ["ansible.builtin.copy"]["content"],
            ic_instances=[{
                "instance_id": "7", "instance": "acme", "compose_project": "f-acme",
                "dbname": "prod", "dbuser": "odoo",
            }],
        )
        rows = [l for l in content.splitlines() if l and not l.startswith("#")]
        self.assertEqual(rows, ["7 acme f-acme prod odoo"])
        self.assertIn(
            "<instance_id> <instance_name> <compose project> <dbname> <dbuser>",
            _COLLECTOR.read_text(),
        )


class TestTheCollectorComesOff(BaseCase):
    """M6: the setting and the teardown remove what the install put in."""

    def test_switched_off_removes_its_schedule_and_files(self):
        play = _play(_INSTALL)
        off = _task(play, "Remove the database collector, switched off")
        self.assertEqual(off["when"], "not (ic_collect_pg | bool)")
        self.assertIn("rescue", off)
        removed = _task(play, "Remove the collector's schedule and files")
        self.assertEqual(removed["ansible.builtin.file"]["state"], "absent")
        self.assertEqual(removed["loop"], [
            "{{ ic_pg_cron_file }}",
            "{{ ic_lib_dir }}/collect_pg.sh",
            "{{ ic_lib_dir }}/collect_pg.instances",
        ])
        readings = _task(play, "Remove the collector's last readings")
        self.assertEqual(readings["loop"], ["ic_pg.prom", "ic_pg_collector.prom"])

    def test_the_teardown_removes_roots_schedule_and_directory(self):
        install = _play(_INSTALL)["vars"]
        teardown = _play(_TEARDOWN)
        for var in ("ic_lib_dir", "ic_pg_cron_file"):
            self.assertEqual(teardown["vars"][var], install[var], var)
        task = _task(teardown, "Remove the database collector, its schedule first")
        self.assertEqual(task["become"], "{{ ansible_user_id != 'root' }}")
        self.assertEqual(task["loop"], ["{{ ic_pg_cron_file }}", "{{ ic_lib_dir }}"])
        self.assertEqual(task["ansible.builtin.file"]["state"], "absent")
        names = [t["name"] for t in teardown["tasks"]]
        self.assertLess(
            names.index("Remove the database collector"),
            names.index("Remove the agents' directory"),
        )

    def test_the_teardown_of_a_server_without_cron_or_sudo_still_ends(self):
        """M-b of the final review (12-nov-2026): ``delete_host`` failed on a
        Debian 13 without cron, and a sudo that no longer works must not
        stop it either: what is left is said in the job's log."""
        teardown = _play(_TEARDOWN)
        names = [t["name"] for t in teardown["tasks"]]
        look = _task(teardown, "Look for cron")
        self.assertEqual(look["ansible.builtin.stat"]["path"], "/usr/bin/crontab")
        removal = _task(teardown, "Remove the disk usage collector")
        self.assertEqual(removal["when"], "ic_crontab.stat.exists")
        self.assertEqual(removal["ansible.builtin.cron"]["state"], "absent")
        self.assertLess(names.index("Look for cron"), names.index("Remove the disk usage collector"))
        said = _task(teardown, "Say there is no schedule to remove")
        self.assertEqual(said["when"], "not ic_crontab.stat.exists")
        self.assertIn("cron is not installed", said["ansible.builtin.debug"]["msg"])
        block = _task(teardown, "Remove the database collector")
        self.assertIn("rescue", block)
        self.assertIn("ic_pg_collector_files", block["when"])
        inside = {id(t) for t in _all_tasks(block["block"])}
        for task in _all_tasks(teardown["tasks"]):
            if "become" in task:
                self.assertIn(id(task), inside, task["name"])
        said = block["rescue"][0]["ansible.builtin.debug"]["msg"]
        self.assertIn("ansible_failed_result", said)
        self.assertIn("{{ ic_pg_cron_file }}", said)


class TestTheDiskCollectorMeasuresDatabaseAndFilestore(BaseCase):
    """M7: the database's size every ten minutes, the filestore under
    ``nice`` inside its container, once an hour on the small servers.

    The script is rendered as Ansible renders it and run against a docker
    stub that answers ``ps`` and ``exec`` and records each call.
    """

    def setUp(self):
        super().setUp()
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "obs" / "textfile").mkdir(parents=True)
        (self.tmp / "inst" / "acme").mkdir(parents=True)
        (self.tmp / "inst" / "acme" / "f").write_text("x" * 100)
        (self.tmp / "bin").mkdir()
        docker = self.tmp / "bin" / "docker"
        docker.write_text(
            "#!/usr/bin/env bash\n"
            'echo "$*" >> "$STUB_LOG"\n'
            'case "$1" in\n'
            '  ps) printf "f-acme odoo o1\\nf-acme db d1\\n" ;;\n'
            '  exec) case "$*" in *psql*) echo 123456 ;; *du*) printf "7890\\t/x\\n" ;; esac ;;\n'
            "esac\n"
        )
        docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
        self.script = self.tmp / "collect_disk.sh"

    def _write(self, memory_mb):
        """Render the collector for one instance on a server of *memory_mb*."""
        play = _play(_INSTALL)
        task = _task(play, "Install the per-instance disk usage collector")
        self.script.write_text(_render(
            task["ansible.builtin.copy"]["content"],
            ic_obs_dir=str(self.tmp / "obs"),
            ansible_env={"HOME": str(self.tmp)},
            ansible_memtotal_mb=memory_mb,
            ic_filestore_hourly_below_mb=play["vars"]["ic_filestore_hourly_below_mb"],
            ic_instances=[{
                "instance_id": "7", "instance": "acme", "compose_project": "f-acme",
                "dbname": "prod", "dbuser": "odoo", "dir": "~/inst/acme",
            }],
        ))
        self.script.chmod(0o755)

    def _run(self):
        """Run the collector once; return its docker calls."""
        log = self.tmp / "docker.log"
        if log.exists():
            log.unlink()
        env = dict(os.environ, PATH=f"{self.tmp / 'bin'}:{os.environ['PATH']}", STUB_LOG=str(log))
        subprocess.run(["bash", str(self.script)], check=True, env=env, timeout=60)  # noqa: S603,S607 — a fixed local script
        return log.read_text() if log.exists() else ""

    def _prom(self, name):
        """Return the textfile *name* the collector wrote."""
        return (self.tmp / "obs" / "textfile" / name).read_text()

    def test_it_measures_the_directory_the_database_and_the_filestore(self):
        self._write(8000)
        calls = self._run()
        disk = self._prom("ic_instance_disk.prom")
        self.assertRegex(disk, r'ic_instance_disk_bytes\{instance_id="7",instance_name="acme"\} \d+')
        self.assertIn('ic_pg_db_size_bytes{instance_id="7",instance_name="acme"} 123456', disk)
        self.assertIn(
            'ic_instance_filestore_bytes{instance_id="7",instance_name="acme"} 7890',
            self._prom("ic_instance_filestore.prom"),
        )
        self.assertIn("default_transaction_read_only=on", calls)
        self.assertIn("pg_database_size(current_database())", calls)

    def test_nice_and_ionice_run_inside_the_container_with_a_timeout(self):
        self._write(8000)
        calls = self._run()
        du_call = calls[calls.index("exec o1 sh -c"):]
        self.assertIn("timeout 120 nice -n 19 ionice -c 3 du -sb", du_call)
        self.assertTrue(du_call.rstrip().endswith("sh prod"), du_call)

    def _age_filestore(self, minutes):
        """Make the last filestore reading *minutes* older."""
        filestore = self.tmp / "obs" / "textfile" / "ic_instance_filestore.prom"
        then = filestore.stat().st_mtime - minutes * 60
        os.utime(filestore, (then, then))

    def test_a_large_server_measures_the_filestore_every_pass(self):
        """Its cron runs every ten minutes: each pass finds a reading that old."""
        self._write(8000)
        self._run()
        self._age_filestore(10)
        self.assertIn("du -sb", self._run())

    def test_a_small_server_measures_it_once_an_hour(self):
        self._write(3800)
        self.assertIn("du -sb", self._run())
        self._age_filestore(10)
        self.assertNotIn("du -sb", self._run())
        self._age_filestore(50)
        self.assertIn("du -sb", self._run())

    def test_an_instance_asleep_keeps_its_directory_and_has_no_database_figure(self):
        self._write(8000)
        docker = self.tmp / "bin" / "docker"
        docker.write_text('#!/usr/bin/env bash\necho "$*" >> "$STUB_LOG"\n')
        self._run()
        disk = self._prom("ic_instance_disk.prom")
        self.assertIn("ic_instance_disk_bytes{", disk)
        self.assertNotIn("ic_pg_db_size_bytes{", disk)

    def test_a_pass_still_running_makes_the_next_one_exit(self):
        self._write(8000)
        lock = self.tmp / "obs" / ".collect_disk.lock"
        holder = subprocess.Popen(  # noqa: S603,S607 — flock(1), fixed arguments
            ["flock", str(lock), "sleep", "30"],
        )
        self.addCleanup(holder.kill)
        for _ in range(50):
            if lock.exists():
                break
            subprocess.run(["sleep", "0.1"], check=True)  # noqa: S603,S607
        self.assertEqual(self._run(), "")


class TestTheAgentsCarryTheCollectorsVariables(ObservabilityExecutorCase):

    def test_each_instance_names_its_database_and_user(self):
        project = self.env["cloud.project"].create({"name": "P"})
        instance = self.env["cloud.instance"].create({
            "name": "dbx", "project_id": project.id,
            "environment": "production", "host_id": self.host.id,
            "odoo_version": "19.0",
            "postgres_dbname": "main", "postgres_username": "dbx_user",
        })
        labels = self._make(ObservabilityAgentsExecutor)._instance_labels(instance)
        self.assertEqual((labels["dbname"], labels["dbuser"]), ("main", "dbx_user"))

    def test_the_setting_switches_the_collector(self):
        self.assertTrue(self.settings.metrics_collect_pg)
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertIs(extra["ic_collect_pg"], True)
        self.settings.metrics_collect_pg = False
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertIs(extra["ic_collect_pg"], False)


class TestTheInstallRecordsItsRevision(ObservabilityExecutorCase):
    """A2: ``on_success`` writes the executor's revision on the host."""

    def _succeed(self, **facts):
        """Run ``on_success`` with *facts*; return what it logged."""
        executor = self._make(ObservabilityAgentsExecutor)
        lines = []
        executor._sys = lines.append
        executor._facts = {"ic_agents_running": "3", "ic_instances_labelled": "0", **facts}
        asyncio.run(executor.on_success({}))
        return "\n".join(lines)

    def test_a_good_install_records_the_revision(self):
        self.assertEqual(self.host.metrics_agents_revision, 0)
        self._succeed(ic_pg_collector="installed")
        self.assertEqual(self.host.metrics_agents_revision, AGENTS_REVISION)
        self.assertEqual(self.host.metrics_agents_state, "installed")

    def _errors(self, **facts):
        """Return what ``parse_results`` makes of a playbook that ended
        well and exported *facts*."""
        executor = self._make(ObservabilityAgentsExecutor)
        executor._facts = {"ic_agents_running": "3", **facts}
        return executor.parse_results({executor._playbook: {"exit_status": 0}})

    def test_a_collector_that_failed_fails_the_job_with_what_to_do(self):
        """The owner, 12-nov-2026: fixed now, not found a month later."""
        errors = self._errors(
            ic_pg_collector="failed",
            ic_pg_collector_error="Check that sudo asks no password: non-zero return code",
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("database collector could not be installed", errors[0])
        self.assertIn("Collect database figures", errors[0])
        self.assertIn("Reason: Check that sudo asks no password", errors[0])

    def test_one_that_could_not_be_removed_says_so(self):
        executor = self._make(ObservabilityAgentsExecutor)
        executor._sent_collect_pg = False
        executor._facts = {"ic_agents_running": "3", "ic_pg_collector": "failed"}
        errors = executor.parse_results({executor._playbook: {"exit_status": 0}})
        self.assertIn("could not be removed", errors[0])

    def test_without_cron_the_job_fails_saying_to_install_it(self):
        errors = self._errors(
            ic_pg_collector="failed", ic_disk_collector="failed",
            ic_cron_error="Install cron: Could not get lock /var/lib/dpkg/lock-frontend",
        )
        self.assertEqual(len(errors), 1, "cron missing is said once, for both collectors")
        self.assertTrue(errors[0].startswith(
            "cron is not installed on the server and could not be installed: "
            "install it (apt install cron) or use the IncubaCloud image."
        ))
        self.assertIn("Could not get lock", errors[0])

    def test_both_collectors_in_place_fail_nothing(self):
        self.assertEqual(self._errors(ic_pg_collector="installed", ic_disk_collector="installed"), [])
        self.assertEqual(self._errors(ic_pg_collector="off"), [])
        # A playbook from before the facts existed.
        self.assertEqual(self._errors(), [])

    def test_a_switch_moved_during_the_install_leaves_the_revision(self):
        """B-e of the final review: the install applied the old value."""
        executor = self._make(ObservabilityAgentsExecutor)
        executor._sent_collect_pg = True
        self.settings.metrics_collect_pg = False
        lines = []
        executor._sys = lines.append
        executor._facts = {"ic_agents_running": "3", "ic_pg_collector": "installed"}
        asyncio.run(executor.on_success({}))
        self.assertEqual(self.host.metrics_agents_revision, 0)
        self.assertEqual(self.host.metrics_agents_state, "installed")
        self.assertIn("switch moved", "\n".join(lines))

    def test_the_value_sent_is_the_one_compared(self):
        executor = self._make(ObservabilityAgentsExecutor)
        extra = executor.get_extra_vars()
        self.assertIs(extra["ic_collect_pg"], executor._sent_collect_pg)

    def test_switched_off_is_said(self):
        self.assertIn("switched off", self._succeed(ic_pg_collector="off"))


class TestTheFleetConvergesOnTheRevision(TransactionCase):
    """A2: installed hosts on an older revision are re-applied, a few per pass."""

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.write({
            "metrics_enabled": True,
            "metrics_central_url": "http://vm.test:8428",
            "metrics_remote_write_url": "http://vm.test:8428/api/v1/write",
            "metrics_account": "acct_rev",
        })
        # A copy of production holds hosts of its own: on the current
        # revision, they stay out of the counts.
        self.env["cloud.host"].search([]).write({"metrics_agents_revision": AGENTS_REVISION})
        self.hosts = self.env["cloud.host"].create([{
            "name": f"HRev{i}",
            "ip_address": f"10.0.9.{i + 1}",
            "user": "root",
            "wildcard_domain": f"hrev{i}.example.com",
            "known_hosts_key": f"hrev{i}.example.com ssh-ed25519 AAAA",
            "traefik_deployed": True,
            "metrics_agents_state": "installed",
            "metrics_agents_since": fields.Datetime.now() - timedelta(hours=1),
        } for i in range(cloud_host_observability._REAPPLY_PER_PASS + 2)])

    def _queued(self, hosts=None):
        """Return the hosts with an agents' install queued."""
        return self.env["cloud.job"].search([
            ("host_id", "in", (hosts or self.hosts).ids),
            ("job_type_id.code", "=", "install_observability"),
        ]).host_id

    def test_a_new_host_starts_below_every_revision(self):
        """What the column holds for hosts installed before it existed."""
        self.assertEqual(self.hosts[0].metrics_agents_revision, 0)
        self.assertGreater(AGENTS_REVISION, 0)

    def test_a_few_per_pass_oldest_first(self):
        per_pass = cloud_host_observability._REAPPLY_PER_PASS
        self.hosts[-1].metrics_agents_revision = AGENTS_REVISION
        self.assertEqual(self.env["cloud.host"]._reconcile_agents_revision(), per_pass)
        self.assertEqual(set(self._queued().ids), set(self.hosts[:per_pass].ids))

    def test_the_cron_runs_it(self):
        self.env["cloud.host"]._cron_reconcile_observability()
        self.assertTrue(self._queued())

    def test_an_empty_column_counts_as_the_oldest(self):
        """A row the column reached empty (``NULL``) is no revision at all."""
        self.hosts[1:].write({"metrics_agents_revision": AGENTS_REVISION})
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_host SET metrics_agents_revision = NULL WHERE id = %s",
            (self.hosts[0].id,),
        )
        self.hosts.invalidate_recordset()
        self.env["cloud.host"]._reconcile_agents_revision()
        self.assertEqual(self._queued(), self.hosts[0])

    def test_a_host_on_the_current_revision_is_left_alone(self):
        self.hosts.write({"metrics_agents_revision": AGENTS_REVISION})
        self.assertEqual(self.env["cloud.host"]._reconcile_agents_revision(), 0)
        self.assertFalse(self._queued())

    def test_a_host_whose_install_waits_is_not_counted(self):
        self.hosts[0]._enqueue_observability("test")
        self.env["cloud.host"]._reconcile_agents_revision()
        queued = self._queued()
        self.assertEqual(len(queued), cloud_host_observability._REAPPLY_PER_PASS + 1)
        self.assertEqual(
            self.env["cloud.job"].search_count([
                ("host_id", "=", self.hosts[0].id),
                ("job_type_id.code", "=", "install_observability"),
            ]),
            1,
        )

    def test_a_host_that_cannot_take_agents_is_skipped(self):
        self.hosts[0].traefik_deployed = False
        self.env["cloud.host"]._reconcile_agents_revision()
        self.assertNotIn(self.hosts[0], self._queued())

    def test_nothing_while_observability_is_off(self):
        self.settings.metrics_enabled = False
        self.assertEqual(self.env["cloud.host"]._reconcile_agents_revision(), 0)
        self.assertFalse(self._queued())

    def test_switching_the_collector_re_applies_every_server(self):
        self.hosts.write({"metrics_agents_revision": AGENTS_REVISION})
        self.settings.metrics_collect_pg = False
        self.assertEqual(set(self.hosts.mapped("metrics_agents_revision")), {0})
        self.hosts.write({"metrics_agents_revision": AGENTS_REVISION})
        self.settings.metrics_collect_pg = False
        self.assertEqual(
            set(self.hosts.mapped("metrics_agents_revision")), {AGENTS_REVISION},
            "a write that changes nothing re-applies nothing",
        )

    def test_no_burst_at_upgrade(self):
        """No migration queues an install: the cron's pace is the only one."""
        for script in (_ROOT / "migrations").glob("*/*.py"):
            text = script.read_text()
            for call in ("install_observability", "refresh_observability_labels",
                         "_reapply_observability_everywhere"):
                self.assertNotIn(call, text, script)


class TestTheSwitchOnScreen(TransactionCase):
    """Settings → Monitoring reads and saves *Collect database figures*."""

    def _call(self, method, **kwargs):
        """Call the controller method *method* with a request on this env."""
        controller = _routes_crud.CrudMixin()
        controller._sec = lambda: self.env["cloud.security.mixin"]
        fake = MagicMock(spec=Request)
        fake.env = self.env
        with patch.object(_routes_crud, "request", fake):
            return getattr(controller, method)(**kwargs)

    def test_it_is_read_and_saved(self):
        settings = self.env["cloud.settings"].sudo()._get()
        self.assertIs(self._call("cloud_get_general_settings")["metrics_collect_pg"], True)
        self.assertEqual(
            self._call("cloud_save_general_settings", metrics_collect_pg=False),
            {"ok": True},
        )
        self.assertFalse(settings.metrics_collect_pg)
        self.assertIs(self._call("cloud_get_general_settings")["metrics_collect_pg"], False)

    def test_a_caller_that_does_not_send_it_leaves_it(self):
        settings = self.env["cloud.settings"].sudo()._get()
        settings.metrics_collect_pg = False
        self._call("cloud_save_general_settings", metrics_retention_days=30)
        self.assertFalse(settings.metrics_collect_pg)
