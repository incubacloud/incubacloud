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
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models import ansible_executor as ansible_executor_module
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
    an empty log page (seen live when a tenant image lacked
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
