"""Tests for the docker_prune executor's protect-label filter.

The filter is what keeps ``docker system prune -af`` from sweeping
containers that are stopped on purpose (the SaaS warm pool). It was
already designed once and silently lost: a SaaS-side monkey-patch of
``get_commands`` implemented it, and the method stopped being called the
day this executor moved to ``AnsibleExecutor`` — nothing failed, the
prune just went back to unfiltered and wiped a warm instance's
containers between its rebuild and that night's backup.

So these assertions pin the *mechanism* (Python and playbook agreeing on
one label, reaching the actual prune command) rather than any wording.

The play deliberately does not run ``docker system prune``: that prunes
networks too, and a project network can be neither labelled (compose
recreates it and tears the live stack down) nor safely swept (a stopped
container releases its endpoint, so the network counts as unused and
goes — taking nothing but the stack's ability to start). Pruning only
what occupies disk avoids the choice, so the tests below pin the absence
of ``system prune`` as hard as they pin the label.
"""
import os
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import yaml

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models import docker_prune_executor
from odoo.addons.incubacloud.models.abstract_executor import HANDOFF_SWEEP
from odoo.addons.incubacloud.models.docker_prune_executor import (
    NETWORK_ALERT_CODE,
    PROTECT_LABEL,
    SWEPT_ALERT_CODE,
    DockerPruneExecutor,
    _format_size,
    _reclaimed_bytes,
)


class TestDockerPruneProtectFilter(TransactionCase):

    def _play(self):
        """Return the parsed maintenance playbook (single play)."""
        path = os.path.join(
            get_module_path("incubacloud"),
            "ansible", "playbooks", "host_maintenance.yml",
        )
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)[0]

    def _command_tasks(self):
        return [
            task for task in (self._play().get("tasks") or [])
            if "ansible.builtin.command" in task
        ]

    def _prune_cmds(self):
        """Return the prune commands the play actually runs."""
        prune_tasks = [
            task for task in self._command_tasks() if "loop" in task
        ]
        self.assertEqual(
            len(prune_tasks), 1, "the play must run one looped prune task",
        )
        return prune_tasks[0]["loop"]

    def test_the_prune_never_touches_networks(self):
        """The regression this play caused, pinned.

        ``docker system prune`` also prunes networks. A project network
        cannot be protected by the label — labelling it makes compose
        recreate it mid-command — and a stopped stack's network counts
        as unused, so the prune deleted the networks of both ``ready``
        warm spares while their labelled containers survived. Neither
        stack could start afterwards: a container that is started
        rather than recreated keeps the network id it was born with.
        """
        for cmd in self._prune_cmds():
            self.assertNotIn(
                "system prune", cmd,
                "system prune takes networks with it; prune the "
                "resources that actually occupy disk instead",
            )
            self.assertNotIn("network prune", cmd)

    def test_prune_covers_what_actually_occupies_disk(self):
        """Dropping networks must not cost the reclaim it was doing."""
        joined = "\n".join(self._prune_cmds())
        self.assertIn("docker container prune", joined)
        self.assertIn("docker image prune", joined)
        self.assertIn("docker builder prune", joined)

    def test_prune_command_excludes_protected_resources(self):
        """The prunes that can sweep a stopped stack carry the filter.

        Build cache has nothing to protect, so it is exempt; containers
        and images both do.
        """
        for cmd in self._prune_cmds():
            if "builder prune" in cmd:
                continue
            self.assertIn(
                '--filter "label!={{ ic_protect_label }}"', cmd,
            )

    def test_playbook_default_matches_the_python_constant(self):
        """Standalone runs prune exactly what a job run prunes."""
        self.assertEqual(
            self._play()["vars"]["ic_protect_label"], PROTECT_LABEL,
        )

    def test_executor_feeds_the_label_to_the_playbook(self):
        """The values that actually run come from ``PROTECT_LABEL`` and
        the host's sweep: ``TMP_SWEEP`` while no restore waits there."""
        host = self.env["cloud.host"].create({
            "name": "prune-host", "ip_address": "198.51.100.71",
            "user": "root", "wildcard_domain": "prune.example.com",
        })
        job = self.env["cloud.job"].create({
            "name": "docker_prune", "host_id": host.id,
            "job_type_id": self.env.ref("incubacloud.docker_prune").id,
        })
        executor = DockerPruneExecutor(job, host)
        self.assertEqual(
            executor.get_extra_vars(),
            {
                "ic_protect_label": PROTECT_LABEL,
                "ic_tmp_sweep": docker_prune_executor.tmp_sweep(self.env, host),
            },
        )
        self.assertEqual(
            docker_prune_executor.tmp_sweep(self.env, host),
            docker_prune_executor.TMP_SWEEP,
        )

    def test_playbook_inventories_survivors(self):
        """A container inventory runs after the prune and is exported.

        The presence check in ``on_success`` is only as good as this
        fact: without it the executor silently skips the verification.
        """
        cmds = [
            task["ansible.builtin.command"]["cmd"]
            for task in self._command_tasks()
        ]
        inventory = [cmd for cmd in cmds if "docker ps -a" in cmd]
        self.assertEqual(len(inventory), 1)
        self.assertIn("{{.Names}}", inventory[0])
        networks = [cmd for cmd in cmds if "docker network ls" in cmd]
        self.assertEqual(
            len(networks), 1,
            "the network check in on_success is only as good as this fact",
        )
        self.assertIn("{{.Name}}", networks[0])
        stats = [
            task for task in (self._play().get("tasks") or [])
            if "ansible.builtin.set_stats" in task
        ]
        self.assertEqual(len(stats), 1)
        exported = stats[0]["ansible.builtin.set_stats"]["data"]
        self.assertIn("ic_containers_after", exported)
        self.assertIn("ic_prune_stdout", exported)
        self.assertIn("ic_networks_after", exported)


class TestTheDailySweepOfTmp(TransactionCase):
    """The day's prune also sweeps this account's leftovers in /tmp.

    A handoff whose restore never came holds a production database; it
    was swept only when another handoff or restore started on the same
    host, which may never happen (28-oct-2026). And releases before
    1.0.200 left archives and copier answers at fixed names, 0644, when
    a job ended early: they are this account's, and they go too.
    """

    def _play(self):
        """Return the parsed maintenance playbook (single play)."""
        path = os.path.join(
            get_module_path("incubacloud"),
            "ansible", "playbooks", "host_maintenance.yml",
        )
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)[0]

    def test_the_play_sweeps_first_and_never_fails_on_it(self):
        """Before the prune, so a prune that fails still swept; a sweep
        that cannot finish leaves the rest for the next day."""
        tasks = self._play()["tasks"]
        sweeps = [
            index for index, task in enumerate(tasks)
            if task.get("ansible.builtin.shell", {}).get("cmd")
            == "{{ ic_tmp_sweep }}"
        ]
        self.assertEqual(len(sweeps), 1)
        prune = next(
            index for index, task in enumerate(tasks) if "loop" in task
        )
        self.assertLess(sweeps[0], prune)
        self.assertIs(tasks[sweeps[0]]["failed_when"], False)
        self.assertIs(tasks[sweeps[0]].get("become", False), False)

    def test_playbook_default_matches_the_python_constant(self):
        """Standalone runs sweep exactly what a job run sweeps."""
        self.assertEqual(
            self._play()["vars"]["ic_tmp_sweep"],
            docker_prune_executor.TMP_SWEEP,
        )

    def test_only_this_account_s_and_only_a_day_old(self):
        """Handoffs as the jobs sweep them, and the older releases' names,
        both by this account's uid and never through a link."""
        sweep = docker_prune_executor.TMP_SWEEP
        self.assertTrue(sweep.startswith(HANDOFF_SWEEP + "; "))
        legacy = docker_prune_executor.legacy_tmp_sweep()
        jobs = docker_prune_executor.job_dir_sweep()
        uploads = docker_prune_executor.upload_dir_sweep()
        self.assertTrue(sweep.endswith(f"{legacy}; {jobs}; {uploads}"))
        for part in (HANDOFF_SWEEP, legacy, jobs, uploads):
            self.assertIn('-user "$(id -u)"', part)
            self.assertIn("-mmin +", part)
            self.assertNotIn(" -L ", part)
            self.assertNotIn("-follow", part)
        self.assertIn("-mmin +1440", legacy)
        self.assertIn("-maxdepth 1", legacy)

    def test_what_others_left_goes_through_a_privileged_pass(self):
        """What the account cannot remove: root's (before a hardening
        moved the panel to an operator account) and uid 1000's, the odoo
        container's, which wrote the live dumps and exports (I4). Root
        runs it directly, no search; an operator asks ``sudo -n`` only
        after a search it can do itself found something, so a host
        without sudoers reports no incident each day (M-a). The same
        names and age as its own pass, and it never fails."""
        legacy = docker_prune_executor.legacy_tmp_sweep()
        own, _sep, privileged = legacy.partition("; if ")
        self.assertIn(r"\( -user 0 -o -user 1000 \)", privileged)
        self.assertTrue(
            privileged.startswith('[ "$(id -u)" = 0 ]; then find /tmp'),
            privileged,
        )
        direct, _sep, probe = privileged.partition("; elif ")
        self.assertNotIn("sudo", direct)
        self.assertTrue(probe.startswith("find /tmp"), probe)
        self.assertIn("-print -quit 2>/dev/null | grep -q .; then sudo -n find", probe)
        self.assertTrue(privileged.endswith("fi 2>/dev/null || true"), privileged)
        self.assertEqual(probe.count("sudo"), 1)
        self.assertEqual(
            direct[direct.index("-mmin"):direct.index("-exec")],
            own[own.index("-mmin"):own.index("-exec")],
        )

    def _run_legacy_sweep(self, root, uid, sudo_exit=None):
        """Run the legacy sweep over *root* as an account with *uid*.

        ``id -u`` answers *uid* and ``sudo`` is a stub that logs how it
        was called and, if *sudo_exit* is None, runs its command (after
        its ``-n``) as the test's own account (a real one would be root).

        :return: the sweep's exit status and the stub's calls
        """
        bin_dir = Path(root).parent / "bin"
        bin_dir.mkdir(exist_ok=True)
        log = Path(root).parent / "sudo.log"
        (bin_dir / "id").write_text(
            f"#!/bin/sh\necho {uid}\n", encoding="utf-8",
        )
        failing = (
            "" if sudo_exit is None
            else f"echo 'not in sudoers' >&2; exit {sudo_exit}\n"
        )
        (bin_dir / "sudo").write_text(
            f'#!/bin/sh\necho "$*" >> {log}\n{failing}shift\nexec "$@"\n',
            encoding="utf-8",
        )
        for stub in ("id", "sudo"):
            (bin_dir / stub).chmod(0o755)
        done = subprocess.run(
            ["sh", "-c", docker_prune_executor.legacy_tmp_sweep(str(root))],
            check=False, capture_output=True, text=True,
            env=os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}"},
        )
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return done, calls

    def _old_leftover(self, root):
        """Create a day-old live dump in *root*, owned by the test's account."""
        dump = Path(root, ".incubacloud-backup-prod.zip")
        dump.write_text("x", encoding="utf-8")
        old = time.time() - 2 * 86400
        os.utime(dump, (old, old))
        return dump

    def test_lab_the_odoo_containers_dump_goes_for_an_account_that_is_not_its_owner(self):
        """A live dump the odoo container wrote (uid 1000) is neither the
        operator's nor root's: it goes through ``sudo -n`` for an operator
        account, and directly, no ``sudo``, for root (I4)."""
        if os.getuid() not in (0, 1000):
            self.skipTest("needs a test account that is uid 0 or 1000")
        for uid, expected_calls in ((1001, 1), (0, 0)):
            with self.subTest(account_uid=uid), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp, "tmp")
                root.mkdir()
                dump = self._old_leftover(root)
                done, calls = self._run_legacy_sweep(root, uid)
                self.assertEqual(done.returncode, 0)
                self.assertFalse(dump.exists())
                self.assertEqual(len(calls), expected_calls, calls)

    def test_lab_no_sudo_when_there_is_nothing_to_sweep(self):
        """A host without sudoers must not see ``sudo`` each day: the
        search the account makes itself finds nothing, so it never asks
        (M-a). What is not old, or not named like them, does not count."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "tmp")
            root.mkdir()
            Path(root, ".incubacloud-backup-today.zip").write_text(
                "x", encoding="utf-8",
            )
            unrelated = Path(root, "unrelated.zip")
            unrelated.write_text("x", encoding="utf-8")
            old = time.time() - 2 * 86400
            os.utime(unrelated, (old, old))
            done, calls = self._run_legacy_sweep(root, 1001)
            self.assertEqual(done.returncode, 0)
            self.assertEqual(calls, [])
            self.assertEqual(done.stderr, "")

    def test_lab_a_refused_sudo_does_not_fail_the_sweep(self):
        """Without sudoers the privileged pass is refused: the sweep
        still ends well, quietly, and what it could not remove stays."""
        if os.getuid() not in (0, 1000):
            self.skipTest("needs a test account that is uid 0 or 1000")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "tmp")
            root.mkdir()
            dump = self._old_leftover(root)
            done, calls = self._run_legacy_sweep(root, 1001, sudo_exit=1)
            self.assertEqual(done.returncode, 0)
            self.assertEqual(len(calls), 1)
            self.assertTrue(dump.exists())
            self.assertEqual(done.stderr, "")

    def test_a_job_directory_a_dead_worker_left_goes_after_three_days(self):
        """A job removes its own directory; one whose worker died does
        not. Three days: no job runs that long (a step's commands stop
        after an hour, a download from a link after a day)."""
        jobs = docker_prune_executor.job_dir_sweep()
        self.assertIn("-type d -name '.incubacloud-job-*'", jobs)
        self.assertIn("-mmin +4320", jobs)

    def test_an_upload_a_restore_left_goes_after_three_days(self):
        """What an upload key's directory still holds once its restore is
        over: the panel revokes the grant three days on, directory and
        all; this is for the host where that job never ran. This
        account's grant directories only, under a root of its own."""
        uploads = docker_prune_executor.upload_dir_sweep()
        self.assertIn("find /tmp/incubacloud-restore -mindepth 1 -maxdepth 1"
                      " -type d", uploads)
        self.assertIn(f"-name '{'[0-9a-f]' * 16}'", uploads)
        self.assertIn("-mmin +4320", uploads)
        self.assertIn('[ ! -h /tmp/incubacloud-restore ]', uploads)
        self.assertIn('[ -O /tmp/incubacloud-restore ]', uploads)
        self.assertNotIn("sudo", uploads)

    def test_lab_only_this_account_s_old_uploads_go(self):
        """Real directories: a grant's three days old goes, links in it
        and all, and nothing they point at; a newer one, another name, a
        link in the root, and a root that is itself a link stay."""
        older = time.time() - 4 * 86400
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "incubacloud-restore")
            outside = Path(tmp, "outside")
            root.mkdir(mode=0o700)
            outside.mkdir()
            (outside / "hostfile").write_text("keep", encoding="utf-8")
            gone = root / "0123456789abcdef"
            fresh = root / "fedcba9876543210"
            other = root / "not-a-grant-dir1"
            for directory in (gone, fresh, other):
                directory.mkdir()
                (directory / "restore.zip").write_text("x", encoding="utf-8")
            (gone / "h").symlink_to(outside / "hostfile")
            (gone / "d").symlink_to(outside)
            link = root / "00000000000000aa"
            link.symlink_to(outside)
            for path in (gone, other):
                os.utime(path, (older, older))
            os.utime(link, (older, older), follow_symlinks=False)
            done = subprocess.run(
                ["sh", "-c", docker_prune_executor.upload_dir_sweep(str(root))],
                check=False,
            )
            self.assertEqual(done.returncode, 0)
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                sorted([fresh.name, other.name, link.name]),
            )
            self.assertEqual(
                (outside / "hostfile").read_text(encoding="utf-8"), "keep",
            )
            # A root that is a link leads elsewhere: nothing is read there.
            moved = Path(tmp, "moved")
            root.rename(moved)
            root.symlink_to(moved)
            os.utime(moved / other.name, (older, older))
            (moved / "aaaaaaaaaaaaaaaa").mkdir()
            os.utime(moved / "aaaaaaaaaaaaaaaa", (older, older))
            done = subprocess.run(
                ["sh", "-c", docker_prune_executor.upload_dir_sweep(str(root))],
                check=False,
            )
            self.assertEqual(done.returncode, 0)
            self.assertTrue((moved / "aaaaaaaaaaaaaaaa").exists())
            # And none at all is nothing to do.
            root.unlink()
            done = subprocess.run(
                ["sh", "-c", docker_prune_executor.upload_dir_sweep(str(root))],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_lab_the_leftovers_go_and_nothing_else(self):
        """Real files: what older releases left a day ago goes; what is
        newer, what a job of this release holds, a link and what it points
        at stay."""
        old = time.time() - 2 * 86400
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "tmp")
            outside = Path(tmp, "outside")
            gone = [
                ".incubacloud-handoff-123.zip", ".incubacloud-backup-prod.zip",
                ".incubacloud-bkdl-prod.zip", ".incubacloud-bkneu-77.zip",
                ".incubacloud-export-prod.tar.gz",
                ".incubacloud-shop-answers.yml",
                ".incubacloud-shop-repos.yaml",
                ".incubacloud-shop-backup.env",
                ".incubacloud-shop-incubacloud.env",
                # The deploy's other files, the proxy's keys and the ACME
                # store, the whitelist, the SaaS layer's (I4, 28-oct-2026).
                ".incubacloud-shop-addons.yaml", ".incubacloud-shop-odoo.conf",
                ".incubacloud-shop-pip.txt", ".incubacloud-shop-apt.txt",
                ".incubacloud-shop-override.yml",
                ".incubacloud-shop-extra-env_smtp.env",
                ".incubacloud-acme.json", ".incubacloud-traefik-default.key",
                ".incubacloud-traefik-cdn-ca.pem",
                ".incubacloud-whitelist.yaml",
                ".incubacloud-github-webhook.yml",
                ".icfree-shop-20261001T000000.zip",
                ".icfree-shop-db-20261001T000000.tar.gz",
                ".ic-sablier-theme.html", ".ic-sablier-restart.conf",
            ]
            gone_dirs = [
                ".incubacloud-bkdl-prod", ".incubacloud-bkneu-77",
                ".incubacloud-export-staging-prod",
                ".incubacloud-archived-purge-9",
                ".incubacloud-scripts-1234", ".ic-modules-shop",
                ".ic-sablier-dynamic",
                ".incubacloud-job-fedcba9876543210",
            ]
            kept = [".incubacloud-backup-today.zip", "unrelated.zip"]
            kept_dirs = [
                ".incubacloud-job-0123456789abcdef",
                ".incubacloud-handoff-0123456789abcdef",
                ".incubacloud-allowlist-0123456789ab",
            ]
            links = [".incubacloud-bkneu-link", ".incubacloud-backup-l.zip"]
            root.mkdir()
            outside.mkdir()
            for name in gone + kept:
                (root / name).write_text("x", encoding="utf-8")
            for name in gone_dirs + kept_dirs:
                (root / name).mkdir()
                (root / name / "dump.sql").write_text("x", encoding="utf-8")
            (outside / "hostfile").write_text("keep", encoding="utf-8")
            (root / links[0]).symlink_to(outside)
            (root / links[1]).symlink_to(outside / "hostfile")
            for name in gone + gone_dirs + kept_dirs + ["unrelated.zip"]:
                os.utime(root / name, (old, old))
            # A dead worker's, three days and more: gone.
            older = time.time() - 4 * 86400
            os.utime(root / ".incubacloud-job-fedcba9876543210", (older, older))
            for name in links:
                os.utime(root / name, (old, old), follow_symlinks=False)
            done = subprocess.run(
                ["sh", "-c", "; ".join([
                    docker_prune_executor.legacy_tmp_sweep(str(root)),
                    docker_prune_executor.job_dir_sweep(str(root)),
                ])],
                check=False,
            )
            self.assertEqual(done.returncode, 0)
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                sorted(kept + kept_dirs + links),
            )
            self.assertEqual(
                (outside / "hostfile").read_text(encoding="utf-8"), "keep",
            )


class TestPruneSummary(TransactionCase):
    """Pure parsing of the prune stdout into log lines."""

    def _executor(self):
        executor = DockerPruneExecutor.__new__(DockerPruneExecutor)
        executor._log_buffer = []
        return executor

    def _logged(self, executor):
        return [msg for msg, _src in executor._log_buffer]

    def test_sections_are_counted_and_networks_named(self):
        executor = self._executor()
        executor._summarize_prune_output(
            "Deleted Containers:\nabc123\ndef456\n\n"
            "Deleted Networks:\nic-tenant-x_default\n\n"
            "Deleted Images:\nuntagged: foo\ndeleted: sha256:bar\n\n"
            "Total reclaimed space: 5.9GB"
        )
        logged = "\n".join(self._logged(executor))
        self.assertIn("2 containers", logged)
        self.assertIn("1 networks", logged)
        self.assertIn("2 images", logged)
        self.assertIn("ic-tenant-x_default", logged)

    def test_empty_output_logs_nothing(self):
        executor = self._executor()
        executor._summarize_prune_output("Total reclaimed space: 0B")
        self.assertEqual(self._logged(executor), [])


class TestReclaimedTotal(TransactionCase):
    """The play prints one total per prune; the log must show the sum."""

    def test_three_totals_are_added_up(self):
        """Reading the output backwards would report only the last one."""
        stdout = (
            "Total reclaimed space: 0B\n"
            "Total reclaimed space: 11.2GB\n"
            "Total reclaimed space: 1.66GB\n"
        )
        self.assertEqual(
            _format_size(_reclaimed_bytes(stdout)), "12.86GB",
            "the sum, not the build cache's line alone",
        )

    def test_units_are_si_like_docker(self):
        self.assertEqual(_reclaimed_bytes("Total reclaimed space: 938.4kB"),
                         938400)
        self.assertEqual(_reclaimed_bytes("Total reclaimed space: 5B"), 5)

    def test_no_total_is_zero_not_a_crash(self):
        self.assertEqual(_reclaimed_bytes("nothing here"), 0)
        self.assertEqual(_format_size(0), "0B")


class TestPostPruneNetworkGuard(TransactionCase):
    """The post-prune presence check on panel-managed networks.

    This is the check that was missing on 2026-08-20: the containers
    were all present, the guard said every stack was intact, and both
    warm spares were unstartable because their network was gone.
    """

    def setUp(self):
        super().setUp()
        # Same reason as the container guard below: the alert is written
        # on a cursor of its own and would not see this transaction's
        # host without test mode.
        self.registry_enter_test_mode()
        self.project = self.env["cloud.project"].create({"name": "Net Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "net-host",
            "ip_address": "192.0.2.63",
            "user": "ubuntu",
            "wildcard_domain": "net.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "netinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
            "state": "deployed",
        })
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "docker_prune")], limit=1,
        )
        self.job = self.env["cloud.job"].create({
            "name": "Prune",
            "host_id": self.host.id,
            "job_type_id": job_type.id,
        })
        self.executor = DockerPruneExecutor(self.job, self.host)

    def _alert(self):
        return self.env["cloud.alert"].search([
            ("code", "=", NETWORK_ALERT_CODE),
            ("host_id", "=", self.host.id),
            ("state", "=", "active"),
        ])

    def test_surviving_network_raises_nothing(self):
        self.executor._check_managed_networks(
            f"bridge\nhost\n{self.instance.doodba_project_name}_default",
        )
        self.assertFalse(self._alert())

    def test_missing_network_raises_critical(self):
        """The production shape: containers fine, network gone."""
        self.executor._check_managed_networks("bridge\nhost\nnone")
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "critical")
        self.assertIn(
            f"{self.instance.doodba_project_name}_default", alert.message,
        )

    def test_recovered_network_resolves_the_alert(self):
        self.executor._check_managed_networks("bridge")
        self.assertTrue(self._alert())
        self.executor._check_managed_networks(
            f"{self.instance.doodba_project_name}_default",
        )
        self.assertFalse(self._alert())

    def test_busy_instance_is_exempt(self):
        """A rebuild drops the network on its way down; that is normal."""
        Job = self.env["cloud.job"]
        with patch.object(
            type(Job), "search_count", autospec=True, return_value=1,
        ):
            self.executor._check_managed_networks("bridge")
        self.assertFalse(self._alert())


class TestPostPruneGuard(TransactionCase):
    """The post-prune presence check on panel-managed containers."""

    def setUp(self):
        super().setUp()
        # The guard raises/resolves its alert on a cursor of its own, so
        # the row survives a job that later fails. A plain
        # TransactionCase leaves that cursor unable to see the host and
        # instance created here, and the alert's foreign keys blow up.
        # Test mode makes the registry hand out cursors wrapping this
        # test's transaction, keeping the write real and rolled back.
        self.registry_enter_test_mode()
        self.project = self.env["cloud.project"].create({"name": "Guard Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "guard-host",
            "ip_address": "192.0.2.62",
            "user": "ubuntu",
            "wildcard_domain": "guard.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "guardinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
            # ``deployed`` is derived — seeding the lifecycle state is
            # the supported way to import an already-running instance.
            "state": "deployed",
        })
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "docker_prune")], limit=1,
        )
        self.job = self.env["cloud.job"].create({
            "name": "Prune",
            "host_id": self.host.id,
            "job_type_id": job_type.id,
        })
        self.executor = DockerPruneExecutor(self.job, self.host)

    def _survivors(self, *names):
        return "\n".join(names)

    def _alert(self):
        return self.env["cloud.alert"].search([
            ("code", "=", SWEPT_ALERT_CODE),
            ("host_id", "=", self.host.id),
            ("state", "=", "active"),
        ])

    def test_intact_stack_raises_nothing(self):
        project = self.instance.doodba_project_name
        self.executor._check_managed_containers(
            self._survivors(f"{project}-odoo-1", f"{project}-db-1"),
        )
        self.assertFalse(self._alert())

    def test_missing_managed_container_raises_critical(self):
        project = self.instance.doodba_project_name
        # db survived (running while asleep), odoo was swept — the
        # exact shape of a pruned Sablier-slept tenant.
        self.executor._check_managed_containers(
            self._survivors(f"{project}-db-1"),
        )
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "critical")
        self.assertIn(f"{project}-odoo-1", alert.message)

    def test_recovered_stack_resolves_the_alert(self):
        project = self.instance.doodba_project_name
        self.executor._check_managed_containers(self._survivors())
        self.assertTrue(self._alert())
        self.executor._check_managed_containers(
            self._survivors(f"{project}-odoo-1", f"{project}-db-1"),
        )
        self.assertFalse(self._alert())

    def test_busy_instance_is_exempt(self):
        """A rebuild's ``compose down`` phase must not false-alarm."""
        Job = self.env["cloud.job"]
        with patch.object(
            type(Job), "search_count", autospec=True, return_value=1,
        ):
            self.executor._check_managed_containers(self._survivors())
        self.assertFalse(self._alert())

    def test_undeployed_instance_is_ignored(self):
        """A draft instance has no containers to demand from the host."""
        # Its own project: a project holds at most one production
        # instance (``cloud_instance_one_production_per_project``).
        draft_project = self.env["cloud.project"].create(
            {"name": "Guard Draft Proj"},
        )
        self.env["cloud.instance"].create({
            "name": "guarddraft",
            "project_id": draft_project.id,
            "environment": "production",
            "host_id": self.host.id,
            # default state 'draft' → deployed is False
        })
        project = self.instance.doodba_project_name
        self.executor._check_managed_containers(
            self._survivors(f"{project}-odoo-1", f"{project}-db-1"),
        )
        self.assertFalse(self._alert())
