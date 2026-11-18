"""
Unit tests for ``AnsibleExecutor`` — the host-state half of the Phase 3
execution model ("machine state = Ansible, application operation =
versioned script").

Nothing here talks to a host or needs ansible-runner installed: the
tests cover what we own, which is the inventory we hand to Ansible (it
carries the credentials and the host-key pinning), the private data dir
we build for it, and the way playbook output reaches the job log.
"""
import asyncio
import errno
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud import ansible_noshm
from odoo.addons.incubacloud.models import ansible_executor as ansible_executor_module
from odoo.addons.incubacloud.models.abstract_executor import AbstractExecutor
from odoo.addons.incubacloud.models.ansible_executor import AnsibleExecutor

PING_PLAYBOOK = "playbooks/ping.yml"


class _PingExecutor(AnsibleExecutor):
    """Test subject. No ``_job_type`` → never enters the executor registry."""

    _playbook = PING_PLAYBOOK


class AnsibleExecutorCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = MagicMock(spec=type(self.env["cloud.host"]))
        self.host.id = 3
        self.host.name = "host-a"
        self.host.ip_address = "10.0.0.9"
        self.host.port = 2222
        self.host.user = "cloud"
        self.host.login_type = "ssh_key"
        self.host.key_file = "BASE64KEY"
        self.host.password = "s3cret"
        self.host.known_hosts_key = "[10.0.0.9]:2222 ssh-ed25519 AAAAC3Nz"
        self.host._get_ssh_private_key_bytes.return_value = (
            b"-----BEGIN OPENSSH PRIVATE KEY-----"
        )
        # The executor reads the connection fields elevated (the job runs
        # under the enqueuing user's env and those fields are
        # developer-gated), so ``sudo()`` must hand back the same record.
        self.host.sudo.return_value = self.host
        self.executor = self._make_executor()

    def _make_executor(self, cls=_PingExecutor):
        """Build an executor without ``__init__`` (no SSH, no job record)."""
        executor = object.__new__(cls)
        executor.job = MagicMock(spec=type(self.env["cloud.job"]))
        executor.job.id = 11
        executor.env = self.env
        executor._host_record = self.host
        executor.host = self.host.ip_address
        executor._log_buffer = []
        executor._cancel_requested = False
        return executor


class TestInventory(AnsibleExecutorCase):

    def _hostvars(self, key_path="/tmp/k", known_hosts="/tmp/kh"):
        inventory = self.executor.build_inventory(key_path, known_hosts)
        return inventory["all"]["hosts"][self.executor.inventory_hostname()]

    def test_inventory_hostname_is_stable_and_id_scoped(self):
        self.assertEqual(self.executor.inventory_hostname(), "cloud-host-3")

    def test_connection_details_come_from_the_host_record(self):
        hostvars = self._hostvars()
        self.assertEqual(hostvars["ansible_host"], "10.0.0.9")
        self.assertEqual(hostvars["ansible_port"], 2222)
        self.assertEqual(hostvars["ansible_user"], "cloud")

    def test_host_key_checking_stays_on_and_is_pinned(self):
        """Ansible must trust exactly what the SSH transport trusts."""
        hostvars = self._hostvars(known_hosts="/tmp/kh")
        self.assertIn("-o UserKnownHostsFile=/tmp/kh", hostvars["ansible_ssh_common_args"])
        self.assertIn("-o StrictHostKeyChecking=yes", hostvars["ansible_ssh_common_args"])

    def test_key_auth_passes_the_key_file_and_no_password(self):
        hostvars = self._hostvars(key_path="/tmp/k")
        self.assertEqual(hostvars["ansible_ssh_private_key_file"], "/tmp/k")
        self.assertNotIn("ansible_password", hostvars)

    def test_password_auth_falls_back_to_the_host_password(self):
        self.host.login_type = "password"
        hostvars = self._hostvars(key_path=None)
        self.assertEqual(hostvars["ansible_password"], "s3cret")
        self.assertNotIn("ansible_ssh_private_key_file", hostvars)

    def test_password_auth_goes_through_sshpass(self):
        """Not ssh_askpass, ansible-core's default since 2.19: the panel checks for sshpass."""
        self.host.login_type = "password"
        hostvars = self._hostvars(key_path=None)
        self.assertEqual(hostvars["ansible_ssh_password_mechanism"], "sshpass")

    def test_key_auth_names_no_password_mechanism(self):
        self.assertNotIn("ansible_ssh_password_mechanism", self._hostvars())


class TestPrivateDataDir(AnsibleExecutorCase):

    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="ic-ansible-test-")
        self.addCleanup(self._rmtree)

    def _rmtree(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def _build(self):
        self.executor._build_private_data_dir(self.root)

    def test_project_dir_contains_the_playbook(self):
        self._build()
        self.assertTrue(
            os.path.isfile(os.path.join(self.root, "project", PING_PLAYBOOK)),
        )

    def test_inventory_is_written_as_valid_yaml(self):
        self._build()
        with open(os.path.join(self.root, "inventory", "hosts.yml")) as fh:
            inventory = yaml.safe_load(fh)
        self.assertIn("cloud-host-3", inventory["all"]["hosts"])

    def test_credentials_are_written_0600(self):
        """The key and known_hosts hold trust material: owner-only."""
        self._build()
        for name in ("ssh_key", "known_hosts"):
            path = os.path.join(self.root, name)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, name)

    def test_known_hosts_holds_the_trusted_entry(self):
        self._build()
        with open(os.path.join(self.root, "known_hosts")) as fh:
            self.assertEqual(fh.read().strip(), self.host.known_hosts_key)

    def test_password_host_gets_no_key_file(self):
        self.host.login_type = "password"
        self._build()
        self.assertFalse(Path(self.root, "ssh_key").exists())

    def test_untrusted_host_is_refused(self):
        """No TOFU capture yet → refuse rather than disable host checking."""
        self.host.known_hosts_key = False
        with self.assertRaises(RuntimeError):
            self._build()

    def test_missing_playbook_is_reported_with_the_addon_chain(self):
        class _MissingPlaybook(_PingExecutor):
            _playbook = "playbooks/nope.yml"

        executor = self._make_executor(_MissingPlaybook)
        with self.assertRaises(RuntimeError) as caught:
            executor._build_private_data_dir(self.root)
        self.assertIn("incubacloud", str(caught.exception))


class TestOutputStreaming(AnsibleExecutorCase):

    def test_each_line_becomes_a_log_chunk(self):
        self.executor._emit("TASK [ping]\nok: [cloud-host-3]\n")
        self.assertEqual(
            [line for line, _source in self.executor._log_buffer],
            ["TASK [ping]", "ok: [cloud-host-3]"],
        )

    def test_blank_lines_are_dropped(self):
        self.executor._emit("\n\n  \n")
        self.assertEqual(self.executor._log_buffer, [])

    def test_none_is_tolerated(self):
        self.executor._emit(None)
        self.assertEqual(self.executor._log_buffer, [])

    def test_failure_lines_are_classified_as_errors(self):
        self.executor._emit("fatal: [cloud-host-3]: FAILED! => ERROR boom")
        self.assertEqual(self.executor._log_buffer[0][1], "stderr")

    def test_ordinary_lines_are_not_classified_as_errors(self):
        self.executor._emit("ok: [cloud-host-3]")
        self.assertEqual(self.executor._log_buffer[0][1], "stdout")

    def test_runner_event_hook_streams_stdout(self):
        self.executor._on_runner_event({"stdout": "PLAY RECAP"})
        self.assertEqual(self.executor._log_buffer[0][0], "PLAY RECAP")


class TestCancellation(AnsibleExecutorCase):

    def test_cancel_flags_the_running_playbook_and_still_raises(self):
        """The worker thread only stops if _check_cancel leaves a mark."""
        self.executor.job.env.registry.cursor.side_effect = Exception("cancelled")
        with self.assertRaises(Exception):
            self.executor._check_cancel()
        self.assertTrue(self.executor._cancel_requested)


class TestContract(AnsibleExecutorCase):

    def test_get_commands_is_empty(self):
        """This executor drives ansible-runner, not the SSH command loop."""
        self.assertEqual(self.executor.get_commands(), [])


class TestEarlyGuardsLogBeforeRaising(AnsibleExecutorCase):
    """A job that cannot even start must still leave a line in its log.

    These two guards fire before any other output; a bare raise here was
    the one failure path in the pipeline that produced a failed job with
    an empty log page (seen live when a panel image lacked
    ansible-runner — the reason only reached ``queue_job.exc_message``).
    """

    def test_missing_ansible_runner_is_logged_before_raising(self):
        """No ansible-runner → RuntimeError, with the reason in the log."""
        with patch.object(ansible_executor_module, "ansible_runner", None):
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(self.executor._async_entry())
        self.assertIn("ansible-runner is not installed", str(caught.exception))
        self.assertTrue(self.executor._log_buffer)
        line, source = self.executor._log_buffer[0]
        self.assertIn("ansible-runner is not installed", line)
        self.assertEqual(source, "system")

    def test_missing_playbook_is_logged_before_raising(self):
        """No ``_playbook`` → RuntimeError, with the reason in the log."""

        class _NoPlaybook(_PingExecutor):
            _playbook = None

        executor = self._make_executor(_NoPlaybook)
        # Presence sentinel only — the guard checks ``is None``, nothing
        # calls into it before the playbook guard raises.
        with patch.object(ansible_executor_module, "ansible_runner", object()):
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(executor._async_entry())
        self.assertIn("does not define a playbook", str(caught.exception))
        self.assertTrue(executor._log_buffer)
        self.assertIn("does not define a playbook", executor._log_buffer[0][0])

    def test_a_password_host_without_sshpass_says_so_before_running(self):
        """The panel has no sshpass: the log says that, not a bare auth failure."""
        self.host.login_type = "password"
        self.host._password_login_available.return_value = False
        with patch.object(ansible_executor_module, "ansible_runner", object()):
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(self.executor._async_entry())
        self.assertIn("sshpass", str(caught.exception))
        self.assertIn("host-a", str(caught.exception))
        self.assertTrue(self.executor._log_buffer)
        line, source = self.executor._log_buffer[0]
        self.assertIn("sshpass", line)
        self.assertEqual(source, "system")

    def test_a_password_host_with_sshpass_runs_on(self):
        self.host.login_type = "password"
        self.host._password_login_available.return_value = True
        with (
            patch.object(ansible_executor_module, "ansible_runner", object()),
            patch.object(
                _PingExecutor, "_build_private_data_dir", autospec=True,
                side_effect=RuntimeError("past the guards"),
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(self.executor._async_entry())
        self.assertEqual(str(caught.exception), "past the guards")

    def test_a_key_host_without_sshpass_runs_on(self):
        self.host.login_type = "ssh_key"
        self.host._password_login_available.return_value = False
        with (
            patch.object(ansible_executor_module, "ansible_runner", object()),
            patch.object(
                _PingExecutor, "_build_private_data_dir", autospec=True,
                side_effect=RuntimeError("past the guards"),
            ),
        ):
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(self.executor._async_entry())
        self.assertEqual(str(caught.exception), "past the guards")


class TestRunnerEnvironment(AnsibleExecutorCase):
    """ansible-runner looks ``ansible-playbook`` up in the ``PATH`` it is given."""

    def test_the_path_handed_to_ansible_has_its_tilde_expanded(self):
        """Odoo.sh's ``PATH`` holds ``~/.local/bin``, where pip put ansible-playbook."""
        if ansible_executor_module.ansible_runner is None:
            self.skipTest("ansible-runner is not installed")
        runner_module = MagicMock(spec=ansible_executor_module.ansible_runner)
        runner_module.run.return_value = MagicMock(
            spec=ansible_executor_module.ansible_runner.Runner, rc=0, status="successful",
        )
        with (
            patch.object(ansible_executor_module, "ansible_runner", runner_module),
            patch.dict(os.environ, {"HOME": "/home/odoo", "PATH": "~/.local/bin:/usr/bin"}),
            tempfile.TemporaryDirectory() as root,
        ):
            self.assertEqual(self.executor._run_playbook(root), (0, "successful"))
        envvars = runner_module.run.call_args.kwargs["envvars"]
        self.assertEqual(envvars["PATH"], "/home/odoo/.local/bin:/usr/bin")
        self.assertEqual(envvars["ANSIBLE_NOCOLOR"], "True")


#: A server whose /dev/shm is read-only, as Odoo.sh's: put in PYTHONPATH,
#: it makes every Python started there fail to create a semaphore or a
#: shared memory block with EROFS, as sem_open and shm_open do there
#: (tests/ansible_noshm/test_ansible_noshm.py has the same).
SITECUSTOMIZE = """\
import errno
import multiprocessing.synchronize

import _posixshmem


def _read_only(*args, **kwargs):
    raise OSError(errno.EROFS, "Read-only file system")


multiprocessing.synchronize.SemLock.__init__ = _read_only
_posixshmem.shm_open = _read_only
"""

READ_ONLY = OSError(errno.EROFS, "Read-only file system")

#: A playbook on the panel's own server (``_LocalExecutor``).
LOCAL_PLAYBOOK = [{
    "name": "Without /dev/shm",
    "hosts": "all",
    "gather_facts": False,
    "tasks": [
        {"name": "Say", "ansible.builtin.command": "echo {{ item }}",
         "loop": [1, 2], "changed_when": False},
        {"name": "Wait", "ansible.builtin.command": "sleep {{ ic_sleep | default(0) }}",
         "changed_when": False},
        {"name": "Export", "ansible.builtin.set_stats": {
            "aggregate": False, "data": {"ic_ran": True}}},
    ],
}]


class _LocalExecutor(_PingExecutor):
    """Runs ``LOCAL_PLAYBOOK`` on the panel's own server, with no SSH."""

    def build_inventory(self, key_path, known_hosts_path):
        """Return one local host, run by the panel's own Python."""
        return {"all": {"hosts": {self.inventory_hostname(): {
            "ansible_connection": "local",
            "ansible_python_interpreter": sys.executable,
        }}}}


class _RunnerCase(AnsibleExecutorCase):
    """``_run_playbook`` against ansible-runner, a mock or the real one."""

    def setUp(self):
        super().setUp()
        if ansible_executor_module.ansible_runner is None:
            self.skipTest("ansible-runner is not installed")
        self.root = tempfile.mkdtemp(prefix="ic-ansible-test-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def _mock_runner(self, status="successful", rc=0, during=None):
        """Patch ansible-runner with a mock whose ``run`` returns *status*.

        :param during: called with ``run``'s keyword arguments while it runs
        :return: the mocked module
        """
        real = ansible_executor_module.ansible_runner
        runner_module = MagicMock(spec=real)

        def run(**kwargs):
            if during:
                during(kwargs)
            return MagicMock(spec=real.Runner, rc=rc, status=status)

        runner_module.run.side_effect = run
        patcher = patch.object(ansible_executor_module, "ansible_runner", runner_module)
        patcher.start()
        self.addCleanup(patcher.stop)
        return runner_module

    def _read_only_shm(self):
        """Make the Pythons ansible-runner starts see a read-only /dev/shm."""
        site = Path(self.root, "site")
        site.mkdir()
        (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
        patcher = patch.dict(os.environ, {"PYTHONPATH": str(site)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _local(self, extra_vars=None):
        """An executor whose ``LOCAL_PLAYBOOK`` is in ``self.root``."""
        executor = self._make_executor(_LocalExecutor)
        executor._facts = {}
        executor.get_extra_vars = lambda: dict(extra_vars or {})
        executor._build_private_data_dir(self.root)
        with open(os.path.join(self.root, "project", "playbooks", "ic_local.yml"), "w") as fh:
            yaml.safe_dump(LOCAL_PLAYBOOK, fh, sort_keys=False)
        executor._playbook = "playbooks/ic_local.yml"
        return executor

    def _log(self, executor):
        """The executor's log so far, one string."""
        return "\n".join(line for line, _source in executor._log_buffer)


class TestWithWorkingSemaphores(_RunnerCase):
    """A server with a usable /dev/shm runs ansible-playbook as before."""

    def test_ansible_playbook_runs_with_the_playbook_as_before(self):
        runner = self._mock_runner()
        self.assertEqual(self.executor._run_playbook(self.root), (0, "successful"))
        kwargs = runner.run.call_args.kwargs
        self.assertEqual(kwargs["playbook"], PING_PLAYBOOK)
        self.assertIsNone(kwargs["cmdline"])
        self.assertNotIn("binary", kwargs)
        self.assertFalse(Path(self.root, "bin").exists())

    def test_check_mode_is_the_same_option_as_before(self):
        runner = self._mock_runner()
        self.executor._check_mode = True
        self.executor._run_playbook(self.root)
        self.assertEqual(runner.run.call_args.kwargs["cmdline"], "--check")
        self.assertNotIn("binary", runner.run.call_args.kwargs)


class TestWithoutDevShm(_RunnerCase):
    """Odoo.sh mounts /dev/shm read-only: ``ansible_noshm`` runs the playbook."""

    def test_the_panels_python_starts_the_launcher_with_the_playbook(self):
        seen = {}

        def during(kwargs):
            launcher = os.path.join(self.root, "bin", "ansible-playbook")
            seen["mode"] = os.stat(launcher).st_mode & 0o777
            with open(launcher, "rb") as fh, open(ansible_noshm.__file__, "rb") as own:
                seen["same"] = fh.read() == own.read()

        runner = self._mock_runner(during=during)
        self.executor._run_playbook(self.root, READ_ONLY)
        kwargs = runner.run.call_args.kwargs
        launcher = os.path.join(self.root, "bin", "ansible-playbook")
        self.assertEqual(kwargs["binary"], sys.executable)
        self.assertEqual(kwargs["cmdline"], f"{launcher} {PING_PLAYBOOK}")
        self.assertNotIn("playbook", kwargs)
        self.assertEqual(seen, {"mode": 0o600, "same": True})
        self.assertEqual(kwargs["envvars"]["ANSIBLE_HOST_KEY_CHECKING"], "True")

    def test_check_mode_reaches_the_launcher(self):
        runner = self._mock_runner()
        self.executor._check_mode = True
        self.executor._run_playbook(self.root, READ_ONLY)
        self.assertTrue(runner.run.call_args.kwargs["cmdline"].endswith(
            f"{PING_PLAYBOOK} --check"
        ))

    def _entry(self, error, status="successful", rc=0):
        """Run ``_async_entry`` with the probe answering *error*.

        :return: the private directory the run had, and the results the
            closing hooks were given
        """
        seen = {}
        self._mock_runner(
            status=status, rc=rc,
            during=lambda kwargs: seen.update(root=kwargs["private_data_dir"]),
        )
        with (
            patch.object(ansible_noshm, "semaphore_error", return_value=error),
            patch.object(
                AbstractExecutor, "_dispatch_outcome", autospec=True,
            ) as outcome,
        ):
            asyncio.run(self.executor._async_entry())
        return seen["root"], outcome.call_args.args[1]

    def test_the_log_names_the_read_only_dev_shm_and_the_run_leaves_nothing(self):
        root, _results = self._entry(READ_ONLY)
        self.assertIn(
            "/dev/shm is not writable on this server (EROFS: Read-only file "
            "system): Ansible runs with its locks over pipes.",
            self._log(self.executor),
        )
        self.assertFalse(Path(root).exists())

    def test_with_working_semaphores_the_log_says_nothing_of_dev_shm(self):
        self._entry(None)
        self.assertNotIn("/dev/shm", self._log(self.executor))

    def test_ansible_fails_as_on_odoo_sh_without_the_launcher(self):
        """The fault itself: what the panel ran before, on a read-only /dev/shm."""
        self._read_only_shm()
        executor = self._local()
        rc, status = executor._run_playbook(self.root)
        self.assertEqual(status, "failed", self._log(executor))
        self.assertIn("lack of access to /dev/shm", self._log(executor))

    def test_a_playbook_runs_through_the_launcher_on_a_read_only_dev_shm(self):
        self._read_only_shm()
        executor = self._local()
        rc, status = executor._run_playbook(self.root, READ_ONLY)
        self.assertEqual((rc, status), (0, "successful"), self._log(executor))
        self.assertEqual(executor.playbook_facts(), {"ic_ran": True})
        self.assertIn("Say", self._log(executor))


def _patch_limit(case, seconds):
    """Patch the server's ``limit_time_real`` for the rest of *case*'s test."""
    patcher = patch.object(ansible_executor_module, "config", {"limit_time_real": seconds})
    patcher.start()
    case.addCleanup(patcher.stop)


class TestPlaybookTimeLimit(AnsibleExecutorCase):
    """A playbook ends before the request's ``limit_time_real`` does."""

    def _limit(self, seconds):
        """Patch the server's ``limit_time_real``."""
        _patch_limit(self, seconds)

    def test_the_playbook_gets_what_the_request_has_left_less_the_margin(self):
        self._limit(900)
        self.executor._attempt_started = time.monotonic() - 100
        timeout = self.executor._playbook_timeout()
        margin = ansible_executor_module.PLAYBOOK_LIMIT_MARGIN_S
        self.assertLessEqual(timeout, 900 - 100 - margin)
        self.assertGreaterEqual(timeout, 900 - 102 - margin)

    def test_a_short_limit_gives_up_a_quarter_at_most(self):
        """Odoo's default ``limit_time_real`` of 120 s keeps 90 s, not 60."""
        self._limit(120)
        self.executor._attempt_started = time.monotonic()
        timeout = self.executor._playbook_timeout()
        self.assertLessEqual(timeout, 90)
        self.assertGreaterEqual(timeout, 88)
        self.assertEqual(ansible_executor_module.playbook_limit_margin(120), 30)
        self.assertEqual(ansible_executor_module.playbook_limit_margin(240), 60)
        self.assertEqual(ansible_executor_module.playbook_limit_margin(900), 60)

    def test_a_configured_budget_does_not_shorten_it(self):
        """``attempt_budget_seconds`` = 60 stands for a 100 s limit; the server's is 900."""
        self._limit(900)
        self.executor._attempt_started = time.monotonic()
        self.executor._attempt_limit = 100
        margin = ansible_executor_module.PLAYBOOK_LIMIT_MARGIN_S
        self.assertGreaterEqual(self.executor._playbook_timeout(), 900 - 2 - margin)

    def test_without_a_limit_or_a_clock_nothing_cuts_it(self):
        self._limit(0)
        self.executor._attempt_started = time.monotonic()
        self.assertIsNone(self.executor._playbook_timeout())
        self._limit(900)
        self.executor._attempt_started = None
        self.assertIsNone(self.executor._playbook_timeout())

    def test_a_request_with_nothing_left_still_gives_a_second(self):
        self._limit(900)
        self.executor._attempt_started = time.monotonic() - 2000
        self.assertEqual(self.executor._playbook_timeout(), 1)

class TestPlaybookStopped(_RunnerCase):
    """ansible-runner stops a playbook past its time; the job fails."""

    def _limit(self, seconds):
        """Patch the server's ``limit_time_real``."""
        _patch_limit(self, seconds)

    def test_the_time_limit_reaches_ansible_runner(self):
        runner = self._mock_runner()
        self._limit(900)
        self.executor._attempt_started = time.monotonic()
        self.executor._run_playbook(self.root)
        self.assertGreater(runner.run.call_args.kwargs["timeout"], 700)
        self._limit(0)
        self.executor._run_playbook(self.root, READ_ONLY)
        self.assertIsNone(runner.run.call_args.kwargs["timeout"])

    def test_a_playbook_past_its_time_is_stopped_and_fails_the_job(self):
        # A 4 s limit leaves 3 s after its quarter of margin.
        self._limit(4)
        executor = self._local({"ic_sleep": 60})
        executor._attempt_started = time.monotonic()
        started = time.monotonic()
        rc, status = executor._run_playbook(self.root)
        self.assertEqual((rc, status), (254, "timeout"), self._log(executor))
        self.assertLess(time.monotonic() - started, 30)

    def test_a_stopped_playbook_says_why_and_closes_through_on_failure(self):
        self._limit(900)
        with (
            patch.object(ansible_noshm, "semaphore_error", return_value=None),
            patch.object(AbstractExecutor, "_dispatch_outcome", autospec=True) as outcome,
        ):
            self._mock_runner(status="timeout", rc=254)
            asyncio.run(self.executor._async_entry())
        self.assertIn("The playbook was stopped", self._log(self.executor))
        results = outcome.call_args.args[1]
        self.assertEqual(results[PING_PLAYBOOK]["exit_status"], 254)


class TestPasswordHostRun(_RunnerCase):
    """A password host's run, on a read-only /dev/shm as Odoo.sh's.

    ``ssh_askpass`` hands the password over in a shared memory block,
    which a read-only /dev/shm refuses; sshpass needs none. Nothing
    listens on the host's port, so the run ends unreachable, refused by
    the server and not by the panel's own /dev/shm.
    """

    def test_a_password_host_is_reached_through_sshpass(self):
        if not shutil.which("sshpass"):
            self.fail("sshpass is not installed here, as the panel's image has it")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.host.login_type = "password"
        self.host.ip_address = "127.0.0.1"
        self.host.port = port
        executor = self._make_executor()
        self._read_only_shm()
        executor._build_private_data_dir(self.root)
        rc, status = executor._run_playbook(self.root, READ_ONLY)
        log = self._log(executor)
        self.assertEqual(status, "failed", log)
        self.assertIn("Connection refused", log)
        self.assertNotIn("Read-only file system", log)
