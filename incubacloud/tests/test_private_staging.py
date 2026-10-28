"""What a job stages on a host is its own: in a directory of its own,
secrets owner-only, and gone with the job (27/28-oct-2026).

A deploy's copier answers hold the database, admin and SMTP passwords,
its ``repos.yaml`` the repositories' tokens, its ``backup.env`` the
bucket's keys; a proxy job's staging holds the host's TLS private key,
the certificate store with every key Traefik obtained, and the proxy's
compose file with the dashboard's password hash (and, where the platform
adds it, a DNS provider's token). They were uploaded to fixed paths in
/tmp with the SFTP server's umask: 0644, readable by every account on
the host for as long as the job ran, left there for good by a run that
ended before the step that moves them — and on a host shared with other
accounts, a fixed path is one somebody else can create first: the
scripts directory, the modules a deploy installs, the purge's
environment, the restore's archive, a certificate the proxy job then
installed because a shell found a file where it looked.

Every job now stages in a directory of its own (``_job_dir``): named at
random, created 0700 only if nothing is at the path, removed when the
run ends. Secrets in it are created 0600 (``_upload_private_files``).
"""
import asyncio
import contextlib
import hashlib
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError

from odoo.addons.incubacloud.models import (
    acme_store,
    cloud_restore_upload_grant,
    restore_upload_key_executors,
)
from odoo.addons.incubacloud.models.abstract_executor import (
    JOB_DIR_PREFIX,
    handoff_archive_path,
    handoff_dir,
)
from odoo.addons.incubacloud.models.backup_create_executor import (
    BackupCreateExecutor,
)
from odoo.addons.incubacloud.models.backup_download_executor import (
    BackupDownloadExecutor,
)
from odoo.addons.incubacloud.models.backup_download_neutralized_executor import (
    BackupDownloadNeutralizedExecutor,
)
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.export_instance_executor import (
    ExportInstanceExecutor,
)
from odoo.addons.incubacloud.models.full_setup_executor import (
    FullSetupExecutor,
)
from odoo.addons.incubacloud.models.purge_archived_executor import (
    PurgeArchivedBackupsExecutor,
)
from odoo.addons.incubacloud.models.push_trusted_proxies_executor import (
    PushTrustedProxiesExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)
from odoo.addons.incubacloud.models.restore_instance_executor import (
    RestoreInstanceExecutor,
)
from odoo.addons.incubacloud.models.restore_upload_key_executors import (
    GrantRestoreUploadKeyExecutor,
    RevokeRestoreUploadKeyExecutor,
    VerifyRestoreUploadExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport

from ._certs import make_pair

_BACKUP_ENV = "AWS_SECRET_ACCESS_KEY=not-for-other-accounts\n"
_STORE = '{"le": {"Certificates": []}}'
_JOB_DIR = re.compile(re.escape(JOB_DIR_PREFIX) + r"[0-9a-f]{16}")
#: Every path a command names under /tmp.
_TMP_PATHS = re.compile(r"/tmp/[^\s'\";]+")


def _bash(command, *_args, **_kwargs):
    """Run *command* with bash here, as the host's shell would run it.

    The side effect of a lab transport: what the executor sends to the
    host runs for real, against a directory tree of the test's own.

    :param str command: the shell command an executor sends
    :return: its stdout and exit status
    :rtype: CommandResult
    """
    done = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, check=False,
    )
    return CommandResult(done.stdout, done.returncode)


async def _stage_one_secret(executor, transport):
    """Stage one private file, as a ``before_execute`` would.

    :param executor: the executor whose job stages it
    :param transport: the job's open transport
    """
    await executor._upload_private_files(
        transport, {executor._job_path("a.env"): "A=1\n"},
    )


class _StagingCase(TransactionCase):
    """A host holding its own certificate, an instance on it, a mocked link."""

    def setUp(self):
        """Create the host, with its certificate pair, and an instance."""
        super().setUp()
        # The run's hooks and Full Setup's status write use their own
        # cursor.
        self.registry_enter_test_mode()
        cert, key = make_pair(("*.staging-secrets.example.com",))
        self.host = self.env["cloud.host"].create({
            "name": "staging-secrets-host",
            "ip_address": "192.0.2.61",
            "user": "ubuntu",
            "wildcard_domain": "staging-secrets.example.com",
            "tls_default_cert": cert,
            "tls_default_key": key,
        })
        project = self.env["cloud.project"].create({"name": "stg-secrets"})
        self.inst = self.env["cloud.instance"].create({
            "name": "stg-secrets-inst", "project_id": project.id,
            "environment": "production", "host_id": self.host.id,
        })

    def _executor(self, cls, code, payload=None):
        """Return *cls* on a new job of type *code* for the host."""
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", code)], limit=1,
        )
        vals = {"name": code, "job_type_id": job_type.id, "host_id": self.host.id}
        if job_type.apply_to == "instance":
            vals["instance_id"] = self.inst.id
        if payload:
            vals["payload"] = payload
        return cls(self.env["cloud.job"].create(vals), self.host)

    def _transport(self):
        """Return the host link: every command answers 0, with no output."""
        transport = MagicMock(spec=SSHTransport)
        transport.run.return_value = CommandResult("", 0)
        transport.execute.return_value = CommandResult("", 0)
        return transport

    def _uploads(self, transport):
        """Split what was uploaded by how it was created.

        :return: ``(private, public)``: ``{path: content}`` of the files
            created 0600, and of the ones created with the default mode
        """
        private, public = {}, {}
        for upload in transport.upload_text_files.await_args_list:
            mode = upload.kwargs.get("mode")
            self.assertIn(mode, (None, 0o600))
            (private if mode == 0o600 else public).update(upload.args[0])
        return private, public

    def _removed(self, transport):
        """Return the paths an ``rm -f`` or ``rm -rf`` on the host named."""
        paths = []
        for run in transport.run.await_args_list:
            words = run.args[0].split()
            if words[:2] in (["rm", "-f"], ["rm", "-rf"]):
                paths += words[2:]
        return paths

    def _gone(self, transport, path):
        """Whether the job's end removed *path*, or a directory holding it."""
        return any(
            path == removed or path.startswith(f"{removed}/")
            for removed in self._removed(transport)
        )

    def assertInJobDir(self, executor, path):
        """Assert *path* is in *executor*'s own working directory."""
        self.assertTrue(
            path.startswith(f"{executor._job_dir()}/"),
            f"{path} is not in {executor._job_dir()}",
        )

    def _run(self, executor, transport, logs_error=False):
        """Run *executor*'s whole job against *transport*.

        :param bool logs_error: the run ends on an error of ours, which
            the executor logs with its traceback
        :return: the exception that ended it, or ``None``
        """
        with contextlib.ExitStack() as stack:
            get_transport = stack.enter_context(patch.object(
                type(self.host), "get_transport", autospec=True,
            ))
            get_transport.return_value.__aenter__.return_value = transport
            if logs_error:
                stack.enter_context(
                    self.assertLogs("AbstractExecutor", level="ERROR"),
                )
            # Not ``assertRaises``: Odoo's wraps the block in a savepoint
            # that the run's own cursors would have to outlive.
            try:
                asyncio.run(executor._async_entry())
            except Exception as exc:
                return exc
        return None


class TestEachJobStagesInItsOwnDirectory(_StagingCase):
    """Named at random, created 0700 by the job, removed with it."""

    def test_its_name_is_random_and_the_job_s_own(self):
        """Nothing outside the panel knows it before it exists."""
        first = self._executor(DeployInstanceExecutor, "deploy_instance")
        second = self._executor(DeployInstanceExecutor, "deploy_instance")
        self.assertRegex(first._job_dir(), f"^{_JOB_DIR.pattern}$")
        self.assertNotEqual(first._job_dir(), second._job_dir())
        self.assertEqual(first._job_dir(), first._job_dir())

    def test_it_is_created_0700_before_anything_lands_in_it(self):
        """``mkdir -m 700`` without ``-p``, once, before the first upload."""
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        transport = self._transport()
        asyncio.run(executor._upload_scripts(transport))  # nothing asked yet
        executor.run_script("deploy.sh")
        asyncio.run(executor._upload_private_files(
            transport, {executor._job_path("a.env"): "A=1\n"},
        ))
        asyncio.run(executor._upload_scripts(transport))
        mkdir = f"mkdir -m 700 {executor._job_dir()}"
        self.assertEqual(transport.mock_calls[0], call.run(mkdir))
        runs = [call.args[0] for call in transport.run.await_args_list]
        self.assertEqual(runs.count(mkdir), 1)
        self.assertInJobDir(executor, executor._script_root())
        for upload in transport.upload_text_files.await_args_list:
            for path in upload.args[0]:
                self.assertInJobDir(executor, path)

    def test_a_path_already_taken_stops_the_job_before_any_upload(self):
        """Something is at the path: the job uploads nothing at all."""
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        transport = self._transport()
        transport.run.return_value = CommandResult("", 1)
        with self.assertRaisesRegex(RuntimeError, re.escape(executor._job_dir())):
            asyncio.run(executor._upload_private_files(
                transport, {executor._job_path("a.env"): "A=1\n"},
            ))
        transport.upload_text_files.assert_not_awaited()

    def test_the_whole_directory_goes_when_the_job_ends(self):
        """A run that fails after staging removes the directory, with
        whatever is in it."""
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        transport = self._transport()
        with patch.object(
            DeployInstanceExecutor, "before_execute", autospec=True,
            side_effect=_stage_one_secret,
        ), patch.object(
            DeployInstanceExecutor, "get_commands", autospec=True,
            return_value=[("Build", "docker compose build")],
        ), patch.object(
            DeployInstanceExecutor, "on_failure", autospec=True,
        ):
            transport.execute.return_value = CommandResult("", 1)
            exc = self._run(executor, transport)
        self.assertIsInstance(exc, RuntimeError)
        self.assertEqual(transport.mock_calls[-1][0], "run")
        self.assertIn(executor._job_dir(), self._removed(transport))

    def test_a_job_that_stages_nothing_creates_nothing(self):
        """No upload, no directory, nothing to remove."""
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        transport = self._transport()
        asyncio.run(executor._discard_staging(transport))
        transport.run.assert_not_awaited()


class TestTheCopierSecretsAreStagedPrivately(_StagingCase):
    """Deploy and rebuild: answers, repos.yaml and ``backup.env``."""

    def _patches(self, backup_env=_BACKUP_ENV):
        """Hold GitHub and the backup backend.

        :param backup_env: what ``_backup_env_content`` answers
        """
        return (
            patch.object(
                DeployInstanceExecutor, "_resync_repo_requirements",
                autospec=True,
            ),
            patch.object(
                DeployInstanceExecutor, "_addons_yaml_content",
                autospec=True, return_value="{}\n",
            ),
            patch.object(
                DeployInstanceExecutor, "_backup_env_content",
                autospec=True, return_value=backup_env,
            ),
        )

    def _staged(self, cls, code, backup_env=_BACKUP_ENV):
        """Run *cls*'s copier upload; return the executor and the link."""
        executor = self._executor(cls, code)
        transport = self._transport()
        resync, addons, backup = self._patches(backup_env)
        with resync, addons, backup:
            asyncio.run(executor._upload_copier_files(transport))
        return executor, transport

    def test_a_deploy_creates_them_owner_only(self):
        """Answers, backup.env and repos.yaml (the repositories' tokens
        ride in its URLs) 0600; the files without a secret as before."""
        executor, transport = self._staged(
            DeployInstanceExecutor, "deploy_instance",
        )
        private, public = self._uploads(transport)
        answers, backup = executor._tmp("answers.yml"), executor._tmp("backup.env")
        repos = executor._tmp("repos.yaml")
        self.assertEqual(set(private), {answers, backup, repos})
        self.assertIn("postgres_password", private[answers])
        self.assertEqual(private[backup], _BACKUP_ENV)
        self.assertEqual(private[repos], executor._repos_yaml_content())
        self.assertEqual(
            set(public),
            {executor._tmp(name) for name in (
                "addons.yaml", "odoo.conf", "pip.txt", "apt.txt",
                "override.yml",
            )},
        )

    def test_everything_a_deploy_stages_is_in_its_directory(self):
        """Files, the module it installs, and every path its steps name."""
        executor, transport = self._staged(
            DeployInstanceExecutor, "deploy_instance",
        )
        private, public = self._uploads(transport)
        for path in (*private, *public):
            self.assertInJobDir(executor, path)
        _local, remote = transport.upload_dir.await_args.args
        self.assertEqual(remote, executor._modules_stage_dir())
        self.assertInJobDir(executor, remote)
        for _label, command, *_opts in executor.get_commands():
            for path in _TMP_PATHS.findall(command):
                self.assertInJobDir(executor, path)

    def test_a_rebuild_creates_them_owner_only_in_its_directory(self):
        """Same upload, every rebuild flavour that inherits it."""
        executor, transport = self._staged(
            RebuildInstanceExecutor, "rebuild_instance",
        )
        private, _public = self._uploads(transport)
        self.assertEqual(
            set(private),
            {executor._tmp(name) for name in (
                "answers.yml", "backup.env", "repos.yaml",
            )},
        )
        for _label, command, *_opts in executor.get_commands():
            for path in _TMP_PATHS.findall(command):
                self.assertInJobDir(executor, path)

    def test_without_backups_there_is_no_backup_env(self):
        """No backup backend: nothing named backup.env, either way."""
        executor, transport = self._staged(
            DeployInstanceExecutor, "deploy_instance", backup_env=None,
        )
        private, public = self._uploads(transport)
        self.assertEqual(
            set(private),
            {executor._tmp("answers.yml"), executor._tmp("repos.yaml")},
        )
        self.assertNotIn(executor._tmp("backup.env"), public)

    def test_the_steps_still_read_them_where_they_were_staged(self):
        """Copier reads the answers, a step moves backup.env into the
        instance, another deletes the answers: the staged paths."""
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        commands = dict(item[:2] for item in executor.get_commands())
        answers, backup = executor._tmp("answers.yml"), executor._tmp("backup.env")
        self.assertIn(f" {answers} ", commands["Deploy with copier"])
        self.assertIn(f"f={backup};", commands["Write backup.env"])
        self.assertIn(".docker/backup.env", commands["Write backup.env"])
        self.assertIn(
            f"mv {executor._tmp('repos.yaml')} ", commands["Write repos.yaml"],
        )
        self.assertEqual(commands["Remove temp files"], f"rm -f {answers}")

    def test_a_run_that_fails_after_the_upload_leaves_none(self):
        """The host's key cannot be read: no step runs, and all go."""
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        transport = self._transport()
        resync, addons, backup = self._patches()
        with resync, addons, backup, patch.object(
            type(self.inst), "_check_template_ref", autospec=True,
        ), patch.object(
            DeployInstanceExecutor, "_ship_secret_key", autospec=True,
            side_effect=RuntimeError("cannot read the key"),
        ), patch.object(
            DeployInstanceExecutor, "on_failure", autospec=True,
        ) as failure:
            exc = self._run(executor, transport, logs_error=True)
        self.assertIsInstance(exc, RuntimeError)
        failure.assert_called_once()
        transport.execute.assert_not_awaited()
        for name in ("answers.yml", "backup.env", "repos.yaml"):
            self.assertTrue(self._gone(transport, executor._tmp(name)), name)

    def test_a_staging_that_fails_half_way_still_cleans_up(self):
        """The third file fails to upload: the two before it were
        created, so all three are on the list the job's end removes."""
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        transport = self._transport()
        transport.upload_text_files.side_effect = OSError("connection lost")
        files = {executor._tmp(f"s{n}.env"): "S=1\n" for n in range(3)}
        with self.assertRaises(OSError):
            asyncio.run(executor._upload_private_files(transport, files))
        asyncio.run(executor._discard_staging(transport))
        removed = self._removed(transport)
        self.assertLessEqual(set(files), set(removed))
        self.assertIn(executor._job_dir(), removed)


class TestTheProxySecretsAreStagedPrivately(_StagingCase):
    """Full Setup and the trusted-proxies push: key, store, compose file."""

    def _prepared(self, cls, code, store=_STORE):
        """Run *cls*'s ``before_execute``; return executor and link.

        :param store: what the certificate store prune stages
        """
        executor = self._executor(cls, code)
        transport = self._transport()
        with patch.object(
            acme_store.AcmeStorePruneMixin, "_prepare_acme_prune",
            autospec=True, return_value=store,
        ):
            asyncio.run(executor.before_execute(transport))
        return executor, transport

    def test_full_setup_creates_its_secrets_owner_only(self):
        """The key, the store and the compose file 0600; the rest as before."""
        executor, transport = self._prepared(FullSetupExecutor, "full_setup")
        stage = executor._job_path
        private, public = self._uploads(transport)
        self.assertEqual(set(private), {
            stage("inverseproxy.yaml"), stage("default.crt"),
            stage("default.key"), executor._acme_local_path(),
        })
        self.assertEqual(private[stage("default.key")], self.host.tls_default_key)
        self.assertEqual(private[executor._acme_local_path()], _STORE)
        salted = re.compile(r"\$\$2y\$\$12\$\$[^\s\"]+")
        self.assertEqual(
            salted.sub("<hash>", private[stage("inverseproxy.yaml")]),
            salted.sub("<hash>", self.host._shipped_inverseproxy_yaml()),
        )
        self.assertFalse(set(private) & set(public))
        self.assertLessEqual(
            {stage("traefik.yml"), stage("config.yml"), stage("tls.yml"),
             stage("whitelist.yaml")},
            set(public),
        )

    def test_the_push_creates_its_secrets_owner_only(self):
        """The key and the store 0600; the documents as before."""
        executor, transport = self._prepared(
            PushTrustedProxiesExecutor, "push_trusted_proxies",
        )
        private, public = self._uploads(transport)
        self.assertEqual(set(private), {
            executor._job_path("default.crt"), executor._job_path("default.key"),
            executor._acme_local_path(),
        })
        self.assertFalse(set(private) & set(public))
        self.assertIn(executor._job_path("traefik.yml"), public)

    def test_everything_they_stage_and_name_is_in_their_directory(self):
        """Uploads and every path a step names under /tmp."""
        for cls, code in ((FullSetupExecutor, "full_setup"),
                          (PushTrustedProxiesExecutor, "push_trusted_proxies")):
            executor, transport = self._prepared(cls, code)
            private, public = self._uploads(transport)
            with self.subTest(job=code):
                for path in (*private, *public):
                    self.assertInJobDir(executor, path)
                for _label, command, *_opts in executor.get_commands():
                    for path in _TMP_PATHS.findall(command):
                        self.assertInJobDir(executor, path)

    def test_a_host_without_its_own_certificate_stages_no_key(self):
        """No pair, no store to prune: only the compose file is private."""
        self.host.write({"tls_default_cert": False, "tls_default_key": False})
        executor, transport = self._prepared(
            FullSetupExecutor, "full_setup", store="",
        )
        private, public = self._uploads(transport)
        self.assertEqual(set(private), {executor._job_path("inverseproxy.yaml")})
        self.assertFalse([p for p in public if p.endswith("default.key")])
        _executor, transport = self._prepared(
            PushTrustedProxiesExecutor, "push_trusted_proxies", store="",
        )
        private, _public = self._uploads(transport)
        self.assertFalse(private)

    def test_what_is_installed_is_what_this_job_staged(self):
        """The branch is chosen from what was uploaded, never from a file
        a shell finds: with a pair the step moves it, without one it only
        removes, and no step tests for a file's presence."""
        for cls, code in ((FullSetupExecutor, "full_setup"),
                          (PushTrustedProxiesExecutor, "push_trusted_proxies")):
            executor, _transport = self._prepared(cls, code)
            commands = dict(item[:2] for item in executor.get_commands())
            install = commands["Install the default certificate"]
            with self.subTest(job=code, pair=True):
                self.assertIn(executor._job_path("default.key"), install)
                self.assertNotIn("[ -s", install)
                self.assertNotIn("[ -s", commands["Require the CDN client certificate"])
        self.host.write({"tls_default_cert": False, "tls_default_key": False})
        for cls, code in ((FullSetupExecutor, "full_setup"),
                          (PushTrustedProxiesExecutor, "push_trusted_proxies")):
            executor, _transport = self._prepared(cls, code, store="")
            commands = dict(item[:2] for item in executor.get_commands())
            install = commands["Install the default certificate"]
            with self.subTest(job=code, pair=False):
                self.assertNotIn("mv ", install)
                self.assertNotIn("ic_put ", install)
                self.assertIn("rm -f ~/traefik/dynamic/tls-default.yml", install)
                self.assertNotIn(executor._job_dir(), install)

    def test_the_steps_still_move_them_from_where_they_were_staged(self):
        """The compose file, the key and the store reach their place from
        the paths they were staged at."""
        executor, _transport = self._prepared(FullSetupExecutor, "full_setup")
        commands = dict(item[:2] for item in executor.get_commands())
        self.assertIn(
            f"mv {executor._job_path('inverseproxy.yaml')}"
            " ~/traefik/inverseproxy.yaml",
            commands["Move traefik config files"],
        )
        self.assertIn(
            f"mv {executor._job_path('default.key')} ~/traefik/certs/default.key",
            commands["Install the default certificate"],
        )
        executor, _transport = self._prepared(
            PushTrustedProxiesExecutor, "push_trusted_proxies",
        )
        commands = dict(item[:2] for item in executor.get_commands())
        self.assertIn(
            f"ic_put {executor._job_path('default.key')}"
            " ~/traefik/certs/default.key",
            commands["Install the default certificate"],
        )
        local = executor._acme_local_path()
        self.assertIn(f"< {local}", acme_store.write_command(local))

    def test_a_full_setup_that_stops_early_leaves_none_of_them(self):
        """The first step times out: nothing was moved, all of it goes."""
        executor = self._executor(FullSetupExecutor, "full_setup")
        transport = self._transport()
        transport.execute.side_effect = TimeoutError("host hung")
        with patch.object(
            acme_store.AcmeStorePruneMixin, "_prepare_acme_prune",
            autospec=True, return_value=_STORE,
        ):
            exc = self._run(executor, transport, logs_error=True)
        self.assertIsInstance(exc, TimeoutError)
        for path in (executor._job_path("inverseproxy.yaml"),
                     executor._job_path("default.key"),
                     executor._acme_local_path()):
            self.assertTrue(self._gone(transport, path), path)

    def test_a_push_that_fails_leaves_none_of_them(self):
        """Every step fails: the key and the store go all the same, and
        only after the steps."""
        executor = self._executor(
            PushTrustedProxiesExecutor, "push_trusted_proxies",
        )
        transport = self._transport()
        transport.execute.return_value = CommandResult("", 1)
        with patch.object(
            acme_store.AcmeStorePruneMixin, "_prepare_acme_prune",
            autospec=True, return_value=_STORE,
        ), patch.object(
            PushTrustedProxiesExecutor, "on_failure", autospec=True,
        ) as failure:
            exc = self._run(executor, transport)
        self.assertIsInstance(exc, RuntimeError)
        failure.assert_called_once()
        self.assertEqual(transport.mock_calls[-1][0], "run")
        for path in (executor._job_path("default.key"),
                     executor._acme_local_path()):
            self.assertTrue(self._gone(transport, path), path)


class TestTheArchivesAndTheirCredentialsStayInIt(_StagingCase):
    """The purge's environment and the restore's archive."""

    def test_the_purge_environment_is_created_owner_only_in_it(self):
        """No directory of a predictable name, no unchecked ``install -d``:
        the environment is a private file in the job's directory, and
        the script is told that path."""
        executor = self._executor(
            PurgeArchivedBackupsExecutor, "purge_archived_backups",
        )
        transport = self._transport()
        with patch.object(
            PurgeArchivedBackupsExecutor, "_env_content", autospec=True,
            return_value=_BACKUP_ENV,
        ):
            asyncio.run(executor.before_execute(transport))
        private, public = self._uploads(transport)
        self.assertEqual(private, {executor._env_path(): _BACKUP_ENV})
        self.assertFalse(public)
        self.assertInJobDir(executor, executor._env_path())
        transport.execute.assert_not_awaited()
        (step,) = executor.get_commands()
        self.assertIn(f" {executor._env_path()} ", step[1])

    def test_an_archive_this_job_brings_lands_in_it(self):
        """Uploaded, relayed or downloaded by the job: in its directory,
        which exists before the transfer starts."""
        for mode in ("browser", "from_job", "from_url"):
            executor = self._executor(
                RestoreInstanceExecutor, "restore_instance",
                payload={"mode": mode, "url": "https://93.184.216.34/a.zip"},
            )
            with self.subTest(mode=mode):
                self.assertEqual(
                    executor._remote_path(), executor._job_path("restore.zip"),
                )
        transport = self._transport()
        asyncio.run(executor.before_execute(transport))
        transport.run.assert_any_await(f"mkdir -m 700 {executor._job_dir()}")
        for _label, command, *_opts in executor.get_commands():
            for path in _TMP_PATHS.findall(command):
                self.assertInJobDir(executor, path)

    def test_a_refused_restore_makes_nothing_on_the_host(self):
        """The checks come first: a restore refused for its payload has
        created no directory before saying so."""
        executor = self._executor(
            RestoreInstanceExecutor, "restore_instance",
            payload={"mode": "from_job"},
        )
        transport = self._transport()
        with self.assertRaisesRegex(ValueError, "source_job_id"):
            asyncio.run(executor.before_execute(transport))
        transport.run.assert_not_awaited()

    def _rsync_restore(self):
        """Return a restore of an archive copied to the agreed path."""
        return self._executor(
            RestoreInstanceExecutor, "restore_instance",
            payload={"mode": "rsync"},
        )

    def test_an_archive_placed_before_the_job_is_taken_into_it(self):
        """``rsync`` names a path agreed before the job existed, in /tmp,
        that any account could have created: only a regular file of this
        account's is taken from there, into the job's directory, and it
        is checked again there before anything hands it to ``sudo``."""
        executor = self._rsync_restore()
        agreed = shlex.quote(f"/tmp/incubacloud-restore-{self.inst.id}.zip")
        local = executor._job_path("restore.zip")
        self.assertEqual(executor._remote_path(), local)
        transport = self._transport()
        asyncio.run(executor.before_execute(transport))
        runs = [c.args[0] for c in transport.run.await_args_list]
        self.assertEqual(runs, [
            f"mkdir -m 700 {executor._job_dir()}",
            f"test -f {agreed} && ! test -L {agreed} && test -O {agreed}"
            f" && mv -fT -- {agreed} {local}",
            f"test -f {local} && ! test -L {local}",
        ])
        for _label, command, *_opts in executor.get_commands():
            self.assertNotIn(agreed, command)

    def test_an_archive_of_another_account_is_refused(self):
        """Not taken, so nothing after it runs — ``verify-file``'s ``sudo``
        least of all."""
        executor = self._rsync_restore()
        transport = self._transport()
        transport.run.side_effect = lambda command, *_a, **_kw: CommandResult(
            "", 1 if " mv -fT " in command else 0,
        )
        with self.assertRaisesRegex(RuntimeError, "not a regular file of"):
            asyncio.run(executor.before_execute(transport))
        runs = [c.args[0] for c in transport.run.await_args_list]
        self.assertEqual(len(runs), 2)
        self.assertIn(" mv -fT ", runs[-1])

    def test_lab_the_agreed_archive_goes_into_the_job_and_back(self):
        """Real files: taken while it is a regular file of this account's,
        given back when the restore does not get to its end; a link or a
        directory at the agreed path is left where it is."""
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            RestoreInstanceExecutor, "_rsync_path", autospec=True,
            return_value=f"{tmp}/agreed.zip",
        ):
            agreed = Path(tmp, "agreed.zip")
            transport = MagicMock(spec=SSHTransport)
            transport.run.side_effect = _bash
            agreed.write_bytes(b"PK-ok")
            executor = self._rsync_restore()
            executor._job_dir_path = f"{tmp}/job"
            asyncio.run(executor.before_execute(transport))
            self.assertFalse(agreed.exists())
            self.assertEqual(Path(tmp, "job/restore.zip").read_bytes(), b"PK-ok")
            asyncio.run(executor._discard_staging(transport))
            self.assertEqual(agreed.read_bytes(), b"PK-ok")
            self.assertFalse(Path(tmp, "job").exists())
            agreed.rename(f"{tmp}/hostfile")
            agreed.symlink_to(f"{tmp}/hostfile")
            executor = self._rsync_restore()
            executor._job_dir_path = f"{tmp}/job2"
            with self.assertRaisesRegex(RuntimeError, "not a regular file of"):
                asyncio.run(executor.before_execute(transport))
            self.assertTrue(agreed.is_symlink())
            self.assertFalse(Path(tmp, "job2/restore.zip").exists())
            agreed.unlink()
            agreed.mkdir()
            executor = self._rsync_restore()
            executor._job_dir_path = f"{tmp}/job3"
            with self.assertRaisesRegex(RuntimeError, "not a regular file of"):
                asyncio.run(executor.before_execute(transport))
            self.assertTrue(agreed.is_dir())


class TestWhatTheHostProducesStaysInTheJobDirectory(_StagingCase):
    """Dumps and exports: built in the job's own directory, never in /tmp.

    A dump of the customer's database was written to a fixed path in /tmp
    by the container that took it, 0644 (the image's umask): readable by
    every account on the host, at a name another account could create
    first — a directory of its own, for one, that the copy then filled.
    And the container saw the host's whole /tmp.
    """

    def setUp(self):
        """A deployed staging instance beside the production one."""
        super().setUp()
        self.inst.with_context(cloud_state_transition=True).write(
            {"state": "deployed"},
        )
        self.staging = self.env["cloud.instance"].create({
            "name": "stg-secrets-copy", "project_id": self.inst.project_id.id,
            "environment": "staging", "host_id": self.host.id,
        })
        self.staging.with_context(cloud_state_transition=True).write(
            {"state": "deployed"},
        )

    def _on(self, cls, code, inst, payload=None):
        """Return *cls* on a job of type *code* for *inst*."""
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", code)], limit=1,
        )
        job = self.env["cloud.job"].create({
            "name": code, "job_type_id": job_type.id,
            "host_id": self.host.id, "instance_id": inst.id,
            "payload": payload or {},
        })
        return cls(job, self.host)

    def _runs(self, transport):
        """Return the commands run on the host, in order."""
        return [c.args[0] for c in transport.run.await_args_list]

    def _assert_only_job_paths(self, executor, commands):
        """Assert every /tmp path the steps name is in the job's directory."""
        for _label, command, *_opts in commands:
            for path in _TMP_PATHS.findall(command):
                self.assertInJobDir(executor, path)

    def test_a_manual_backup_is_dumped_into_the_drop_directory(self):
        """The container writes into a directory of the job's, made for
        it before the dump, and sees nothing else of the host's /tmp."""
        executor = self._on(BackupCreateExecutor, "backup_create", self.staging)
        transport = self._transport()
        asyncio.run(executor.before_execute(transport))
        drop = executor._drop_dir()
        self.assertInJobDir(executor, drop)
        self.assertEqual(self._runs(transport), [
            f"mkdir -m 700 {executor._job_dir()}", f"mkdir -m 777 {drop}",
        ])
        ((_label, command, _opts),) = executor.get_commands()
        self.assertIn(f" {drop}/backup.zip ", command)
        self._assert_only_job_paths(executor, executor.get_commands())

    def test_a_production_backup_stages_nothing(self):
        """Production asks its backup container: nothing on the host."""
        executor = self._on(BackupCreateExecutor, "backup_create", self.inst)
        transport = self._transport()
        asyncio.run(executor.before_execute(transport))
        transport.run.assert_not_awaited()

    def test_only_a_regular_file_is_downloaded(self):
        """A link left where the archive should be would be followed by
        the download, to whatever the host's account can read."""
        executor = self._on(BackupCreateExecutor, "backup_create", self.staging)
        transport = self._transport()
        archive = executor._remote_tmp()
        checked = f"test -f {archive} && ! test -L {archive}"
        transport.run.side_effect = lambda command, *_a, **_kw: (
            CommandResult("", 1 if command == checked else 0)
        )
        with self.assertRaisesRegex(RuntimeError, re.escape(archive)):
            asyncio.run(executor.after_commands(transport, {}))
        transport.download_file.assert_not_awaited()
        transport.run.side_effect = None
        asyncio.run(executor.after_commands(transport, {}))
        transport.download_file.assert_awaited_once()
        runs = self._runs(transport)
        self.assertLess(runs.index(checked, 1), runs.index(f"rm -f {archive}"))

    def test_a_download_is_built_in_the_job_directory(self):
        """Live or from the backup store, user download or not."""
        for inst, payload in (
            (self.staging, {"time": "live", "download_type": "all"}),
            (self.inst, {"time": "live", "download_type": "dump"}),
            (self.inst, {"time": "latest", "download_type": "all"}),
            (self.inst, {"time": "latest", "download_type": "dump"}),
        ):
            executor = self._on(
                BackupDownloadExecutor, "backup_download", inst, payload,
            )
            transport = self._transport()
            asyncio.run(executor.before_execute(transport))
            with self.subTest(**payload, env=inst.environment):
                self.assertInJobDir(executor, executor._tmp_archive())
                self.assertInJobDir(executor, executor._tmp_dir())
                self.assertIn(
                    f"mkdir -m 700 {executor._job_dir()}", self._runs(transport),
                )
                self._assert_only_job_paths(executor, executor.get_commands())

    def test_a_handoff_is_built_in_the_job_directory_too(self):
        """Only its handover leaves the job's directory, at the end, into
        a directory of its own named with the panel's secret."""
        for payload in (
            {"time": "live", "download_type": "all", "handoff": "host"},
            {"time": "latest", "download_type": "all", "handoff": "host"},
        ):
            executor = self._on(
                BackupDownloadExecutor, "backup_download", self.inst, payload,
            )
            with self.subTest(time=payload["time"]):
                self.assertInJobDir(executor, executor._tmp_archive())
                self.assertInJobDir(executor, executor._tmp_dir())
                *_steps, (label, command, _opts) = executor.get_commands()
                self.assertEqual(label, "Hand the archive over")
                self.assertIn(handoff_dir(self.env, executor.job.id), command)
                self.assertNotEqual(
                    handoff_archive_path(self.env, executor.job.id),
                    f"/tmp/.incubacloud-handoff-{executor.job.id}.zip",
                )

    def test_a_neutralized_copy_is_built_in_the_job_directory(self):
        """Its source dump (production's, before neutralizing) included."""
        for inst in (self.inst, self.staging):
            executor = self._on(
                BackupDownloadNeutralizedExecutor,
                "backup_download_neutralized", inst,
                {"time": "live" if inst == self.staging else "latest",
                 "with_filestore": True},
            )
            transport = self._transport()
            asyncio.run(executor.before_execute(transport))
            with self.subTest(env=inst.environment):
                self.assertInJobDir(executor, executor._host_tmp_dir())
                self.assertInJobDir(executor, executor._host_tmp_archive())
                self.assertIn(
                    f"mkdir -m 700 {executor._job_dir()}", self._runs(transport),
                )
                self._assert_only_job_paths(executor, executor.get_commands())

    def test_an_export_is_copied_and_packed_in_the_job_directory(self):
        """The copy holds the project before its secrets are stripped."""
        executor = self._on(ExportInstanceExecutor, "export_instance", self.inst)
        transport = self._transport()
        transport.run.return_value = CommandResult("SIZE:1K", 0)
        asyncio.run(executor.before_execute(transport))
        sanitize = next(c for c in self._runs(transport) if "export_sanitize.sh" in c)
        for path in _TMP_PATHS.findall(sanitize):
            self.assertInJobDir(executor, path)
        self.assertInJobDir(executor, executor._remote_tmp())
        transport.download_file.assert_awaited_once()
        self.assertEqual(
            transport.download_file.await_args.args[0], executor._remote_tmp(),
        )


class TestTheSshAllowlistDirectory(_StagingCase):
    """The allowlist change stages its scripts in a directory of its own."""

    def _change(self, transport):
        """Run an allowlist change through *transport*."""
        self.host.hardened = True

        @contextlib.asynccontextmanager
        async def _open(_host):
            """Yield *transport* as the host's connection."""
            yield transport

        with patch.object(type(self.host), "get_transport", _open):
            return self.host._ssh_allowlist_change("add", ["203.0.113.9"])

    def test_it_is_created_whole_or_not_at_all(self):
        """``mkdir -m 700`` without ``-p``: a path someone else created
        first stops the change before anything is uploaded."""
        transport = self._transport()
        transport.run.return_value = CommandResult("", 1)
        with self.assertRaisesRegex(UserError, "already at that path"):
            self._change(transport)
        first = transport.run.await_args_list[0].args[0]
        self.assertRegex(
            first, r"^mkdir -m 700 /tmp/\.incubacloud-allowlist-[0-9a-f]+ "
            r"/tmp/\.incubacloud-allowlist-[0-9a-f]+/lib$",
        )
        transport.upload_text_files.assert_not_awaited()
        self.assertEqual(len(transport.run.await_args_list), 1)


class TestAnUploadThroughTheTemporaryKey(_StagingCase):
    """``ssh_upload``: what the grant's holder put on the host is theirs —
    its name, its kind — until the restore takes it into the job's own
    directory, under a name of its own, and checks it is a file.

    Its name is read from a listing the host prints, so it is only ever
    one safe component (28-oct-2026: a name carrying a newline wrote the
    last line of the listing itself, ``..`` included, and the restore
    moved whatever that named)."""

    def setUp(self):
        """A grant whose upload was checked, and a restore consuming it."""
        super().setUp()
        self.grant, _private = self.env["cloud.restore.upload.grant"]._open(
            self.inst,
        )
        self.grant.write({
            "received_filename": "restore.zip",
            "received_bytes": 5,
            "received_sha256": hashlib.sha256(b"PK-ok").hexdigest(),
        })
        self.env.flush_all()

    def _transport(self):
        """Return the host link; the archive's digest is the one checked."""
        transport = super()._transport()
        digest = self.grant.received_sha256
        transport.run.side_effect = lambda command, *_a, **_kw: CommandResult(
            f"{digest}  -\n" if command.startswith("sha256sum ") else "", 0,
        )
        return transport

    def _restore(self, expected=None):
        """Return the restore executor for the grant.

        :param expected: the digest the restore names, the one the check
            recorded by default
        """
        return self._executor(
            RestoreInstanceExecutor, "restore_instance",
            payload={
                "mode": "ssh_upload", "grant_id": self.grant.id,
                "expected_sha256": (
                    self.grant.received_sha256 if expected is None else expected
                ),
            },
        )

    def _verify(self):
        """Return the executor that lists what arrived through the grant."""
        return self._executor(
            VerifyRestoreUploadExecutor, "verify_restore_upload",
            payload={"grant_id": self.grant.id},
        )

    @contextlib.contextmanager
    def _lab(self):
        """Put the grants' root in a directory of the test's own.

        :return: the temporary directory, holding ``restore/`` (the root)
            and, once a restore made it, ``job/``
        """
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cloud_restore_upload_grant, "UPLOAD_ROOT", f"{tmp}/restore",
        ), patch.object(
            restore_upload_key_executors, "UPLOAD_ROOT", f"{tmp}/restore",
        ):
            Path(self.grant._directory()).mkdir(mode=0o700, parents=True)
            yield tmp

    def _lab_transport(self):
        """Return a host link whose commands run here, for real."""
        transport = MagicMock(spec=SSHTransport)
        transport.run.side_effect = _bash
        return transport

    def _inspect(self, executor):
        """Run the listing step for real and hand its output to the executor.

        :param executor: a ``VerifyRestoreUploadExecutor``
        """
        ((label, command),) = executor.get_commands()
        asyncio.run(executor.on_success(
            {label: {"stdout": _bash(command).stdout, "exit_status": 0}},
        ))

    def test_the_upload_is_taken_into_the_job_directory(self):
        """Checked where it is, moved, checked again; every step reads the
        job's copy."""
        executor = self._restore()
        transport = self._transport()
        asyncio.run(executor.before_execute(transport))
        local = executor._job_path("restore.zip")
        self.assertEqual(executor._remote_path(), local)
        runs = [c.args[0] for c in transport.run.await_args_list]
        granted = shlex.quote(self.grant._remote_path("restore.zip"))
        received = executor._job_path("upload.received")
        move = next(c for c in runs if "mv " in c)
        self.assertEqual(
            move,
            f'[ "$(realpath -e -- {granted})" = {granted} ]'
            f" && mv -fT -- {granted} {received}"
            f" && test -f {received} && ! test -L {received}"
            f" && {{ cp --reflink=auto -- {received} {local}"
            f" || {{ rm -f -- {local}; mv -fT -- {received} {local}; false; }}; }}"
            f" && rm -f -- {received}",
        )
        self.assertLess(
            runs.index(f"mkdir -m 700 {executor._job_dir()}"), runs.index(move),
        )
        self.assertEqual(
            runs[runs.index(move) + 1], f"test -f {local} && ! test -L {local}",
        )

    def test_a_restore_takes_only_the_archive_its_operator_saw(self):
        """The digest the restore names must be the one the grant's last
        check recorded: another check since, or none named, and the
        restore stops before the host is asked anything, its key still
        open."""
        for expected in ("", "cd" * 32):
            with self.subTest(expected=expected):
                transport = self._transport()
                with self.assertRaisesRegex(ValueError, "Check what arrived"):
                    asyncio.run(self._restore(expected).before_execute(transport))
                self.assertFalse(transport.method_calls)
                self.assertEqual(self.grant.state, "granted")

    def test_a_link_uploaded_as_the_archive_is_refused(self):
        """``sudo chown`` would follow it to whatever it names."""
        executor = self._restore()
        transport = self._transport()
        transport.run.side_effect = lambda command, *_a, **_kw: CommandResult(
            "", 1 if command.startswith("test -f ") else 0,
        )
        with self.assertRaisesRegex(RuntimeError, "not a regular file"):
            asyncio.run(executor.before_execute(transport))

    def test_a_restore_that_fails_puts_the_upload_back(self):
        """The next attempt finds it where the key wrote it — if the
        grant's directory is still where its name says."""
        executor = self._restore()
        transport = self._transport()
        asyncio.run(executor.before_execute(transport))
        transport.run.reset_mock()
        asyncio.run(executor._discard_staging(transport))
        local = executor._job_path("restore.zip")
        granted = shlex.quote(self.grant._remote_path("restore.zip"))
        directory = shlex.quote(self.grant._directory())
        runs = [c.args[0] for c in transport.run.await_args_list]
        self.assertEqual(
            runs[0],
            f"test -f {local} && ! test -L {local}"
            f' && {{ test -O {local} || sudo -n chown "$(id -u):$(id -g)"'
            f" -- {local}; }}"
            f' && [ "$(realpath -e -- {directory})" = {directory} ]'
            f" && mv -fT -- {local} {granted}",
        )
        self.assertIn(executor._job_dir(), runs[-1])

    def test_a_name_that_is_not_one_safe_component_is_never_used(self):
        """Whatever reached the record — a listing of an older release, a
        write by hand — a name with a path, a shell word or a line break
        in it names no file: the restore stops before the host is told
        anything, and nothing is given back on its way out."""
        for name in (
            "../escape.zip", "..", ".", ".restore.zip", "-rf",
            "x;touch pwned;#.zip", "a b.zip", "a\nb.zip", "a/b.zip",
            "é.zip", "a" * 256,
        ):
            with self.subTest(name=name):
                self.grant.write({"received_filename": name})
                executor = self._restore()
                self.assertIsNone(executor._granted_path())
                transport = self._transport()
                with self.assertRaisesRegex(ValueError, "name"):
                    asyncio.run(executor.before_execute(transport))
                transport.run.assert_not_awaited()
                asyncio.run(executor._discard_staging(transport))
                transport.run.assert_not_awaited()

    def test_lab_a_listing_cannot_be_written_by_a_file_name(self):
        """Real files on a real shell. A name carrying a newline used to
        print the listing's last line itself — here ``..``, the root of
        every grant — and a name the restore cannot use is never listed:
        the operator is told one arrived."""
        forged_digest = hashlib.sha256(b"x").hexdigest()
        with self._lab():
            directory = Path(self.grant._directory())
            (directory / "restore.zip").write_bytes(b"PK-ok")
            (directory / f"zz\n1 {forged_digest} ..").write_bytes(b"x")
            (directory / "my backup (1).zip").write_bytes(b"x")
            (directory / "zzz.zip").symlink_to("/etc/passwd")
            self.grant.write({"received_filename": False})
            executor = self._verify()
            self._inspect(executor)
        self.assertEqual(self.grant.received_filename, "restore.zip")
        self.assertEqual(self.grant.received_bytes, 5)
        self.assertEqual(
            self.grant.received_sha256, hashlib.sha256(b"PK-ok").hexdigest(),
        )
        self.assertIn(
            "2 file(s) arrived with a name that cannot be restored",
            "".join(line for line, _source in executor._log_buffer),
        )

    def test_lab_a_forged_name_alone_records_nothing(self):
        """Without the archive beside it, the forged line is not taken for
        one either."""
        with self._lab():
            directory = Path(self.grant._directory())
            (directory / f"a\n5 {'0' * 64} ..").write_bytes(b"x")
            self.grant.write({"received_filename": False})
            self._inspect(self._verify())
        self.assertFalse(self.grant.received_filename)

    def test_lab_the_archive_goes_into_the_job_and_back(self):
        """Taken where the key wrote it, checked, and given back when the
        restore does not get to its end."""
        with self._lab() as tmp:
            executor = self._restore()
            executor._job_dir_path = f"{tmp}/job"
            granted = Path(self.grant._remote_path("restore.zip"))
            granted.write_bytes(b"PK-ok")
            transport = self._lab_transport()
            asyncio.run(executor.before_execute(transport))
            self.assertFalse(granted.exists())
            self.assertEqual(Path(tmp, "job/restore.zip").read_bytes(), b"PK-ok")
            asyncio.run(executor._discard_staging(transport))
            self.assertEqual(granted.read_bytes(), b"PK-ok")
            self.assertFalse(Path(tmp, "job").exists())

    def test_lab_a_second_name_of_the_upload_cannot_change_what_is_restored(self):
        """A copy, not the uploaded file itself: a hard link the uploader
        sent beside it (``rsync -H``) still names the uploaded file, and
        the key stays open until its revocation runs, after the restore.
        Whatever it writes through that name, in place, the job's copy
        is another file, and the digest checked is the copy's."""
        with self._lab() as tmp:
            executor = self._restore()
            executor._job_dir_path = f"{tmp}/job"
            granted = Path(self.grant._remote_path("restore.zip"))
            granted.write_bytes(b"PK-ok")
            second = granted.parent / "h"
            os.link(granted, second)
            asyncio.run(executor.before_execute(self._lab_transport()))
            with second.open("r+b") as written_in_place:
                written_in_place.write(b"XX")
            local = Path(tmp, "job/restore.zip")
            self.assertEqual(local.read_bytes(), b"PK-ok")
            self.assertEqual(local.stat().st_nlink, 1)
            self.assertNotEqual(local.stat().st_ino, second.stat().st_ino)
            self.assertFalse(granted.exists())
            self.assertEqual(
                [p.name for p in Path(tmp, "job").iterdir()], ["restore.zip"],
            )

    def test_lab_an_upload_that_is_not_a_file_is_never_read(self):
        """A pipe in place of the archive would hold the copy forever: it is
        refused before anything reads it."""
        with self._lab() as tmp:
            executor = self._restore()
            executor._job_dir_path = f"{tmp}/job"
            os.mkfifo(self.grant._remote_path("restore.zip"))
            transport = MagicMock(spec=SSHTransport)
            transport.run.side_effect = lambda command, *_a, **_kw: (
                lambda done: CommandResult(done.stdout, done.returncode)
            )(subprocess.run(
                ["bash", "-c", command], capture_output=True, text=True,
                check=False, timeout=30,
            ))
            with self.assertRaisesRegex(RuntimeError, "could not be taken"):
                asyncio.run(executor.before_execute(transport))
            self.assertFalse(Path(tmp, "job/restore.zip").exists())

    def test_a_grant_of_another_instance_is_refused_before_the_host_is_asked(self):
        """A job names its grant by id, in a payload its caller wrote: a
        restore queued for this instance with another's grant — another
        customer's upload on the same host — takes nothing."""
        project = self.env["cloud.project"].create({"name": "stg-other"})
        other = self.env["cloud.instance"].create({
            "name": "stg-other-inst", "project_id": project.id,
            "environment": "production", "host_id": self.host.id,
        })
        theirs, _private = self.env["cloud.restore.upload.grant"]._open(other)
        theirs.write({
            "received_filename": "restore.zip",
            "received_sha256": self.grant.received_sha256,
        })
        self.env.flush_all()
        executor = self._executor(
            RestoreInstanceExecutor, "restore_instance",
            payload={"mode": "ssh_upload", "grant_id": theirs.id},
        )
        transport = self._transport()
        with self.assertRaisesRegex(ValueError, "not one of this job's"):
            asyncio.run(executor.before_execute(transport))
        self.assertFalse(transport.method_calls)
        self.assertEqual(theirs.state, "granted")

    def test_a_grant_the_panel_did_not_shape_reaches_no_command(self):
        """What a job puts in a command is checked against what ``_open``
        writes, whatever the record holds."""
        forged = (
            {"token": "../../root"},
            {"token": "0123456789abcdef; reboot"},  # gitleaks:allow
            {"public_key": "ssh-ed25519 AAAA\nssh-ed25519 BBBB other"},
            {"public_key": 'ssh-ed25519 AAAA" x'},
            {"public_key": "ssh-rsa AAAAB3NzaC1yc2E"},
        )
        executors = (
            (GrantRestoreUploadKeyExecutor, "grant_restore_upload_key"),
            (VerifyRestoreUploadExecutor, "verify_restore_upload"),
            (RevokeRestoreUploadKeyExecutor, "revoke_restore_upload_key"),
        )
        shape = {"token": self.grant.token, "public_key": self.grant.public_key}
        for values in forged:
            self.grant.write(values)
            for cls, code in executors:
                with self.subTest(values=values, job=code):
                    executor = self._executor(
                        cls, code, payload={"grant_id": self.grant.id},
                    )
                    with self.assertRaisesRegex(ValueError, "panel opened"):
                        executor.get_commands()
            with self.subTest(values=values, job="restore_instance"):
                transport = self._transport()
                with self.assertRaisesRegex(ValueError, "panel opened"):
                    asyncio.run(self._restore().before_execute(transport))
                self.assertFalse(transport.method_calls)
            self.grant.write(shape)

    def test_lab_a_grant_directory_that_is_a_link_is_not_followed(self):
        """A link in place of the grant's directory, or of the archive,
        names a file somewhere else: nothing is moved, and the restore
        stops."""
        with self._lab() as tmp:
            directory = Path(self.grant._directory())
            elsewhere = Path(tmp, "elsewhere")
            elsewhere.mkdir()
            (elsewhere / "restore.zip").write_bytes(b"not-the-upload")
            directory.rmdir()
            directory.symlink_to(elsewhere)
            executor = self._restore()
            executor._job_dir_path = f"{tmp}/job"
            with self.assertRaisesRegex(RuntimeError, "could not be taken"):
                asyncio.run(executor.before_execute(self._lab_transport()))
            self.assertTrue((elsewhere / "restore.zip").exists())
            self.assertFalse(Path(tmp, "job/restore.zip").exists())
            directory.unlink()
            directory.mkdir()
            (directory / "restore.zip").symlink_to(elsewhere / "restore.zip")
            executor = self._restore()
            executor._job_dir_path = f"{tmp}/job2"
            with self.assertRaisesRegex(RuntimeError, "could not be taken"):
                asyncio.run(executor.before_execute(self._lab_transport()))
            self.assertTrue((directory / "restore.zip").is_symlink())

    def test_the_grant_s_directory_is_this_account_s_alone(self):
        """No ``mkdir -p`` over a directory another account made first:
        the root must be this account's, 0700, and the grant's directory
        is created in it."""
        executor = self._executor(
            GrantRestoreUploadKeyExecutor, "grant_restore_upload_key",
            payload={"grant_id": self.grant.id},
        )
        command = dict(item[:2] for item in executor.get_commands())[
            "Prepare the upload directory"
        ]
        self.assertNotIn("mkdir -p", command)
        self.assertIn("-O", command)
        self.assertIn("chmod 700", command)
        self.assertIn(f"mkdir -m 700 {self.grant._directory()}", command)

    def test_lab_a_root_that_is_not_this_account_s_says_how_to_free_it(self):
        """A root left by another account — root itself, after a hardening
        moved the panel to an operator account — stops every grant; the
        message says what to remove."""
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            cloud_restore_upload_grant, "UPLOAD_ROOT", f"{tmp}/restore",
        ), patch.object(
            restore_upload_key_executors, "UPLOAD_ROOT", f"{tmp}/restore",
        ):
            Path(tmp, "theirs").mkdir()
            Path(tmp, "restore").symlink_to(f"{tmp}/theirs")
            executor = self._executor(
                GrantRestoreUploadKeyExecutor, "grant_restore_upload_key",
                payload={"grant_id": self.grant.id},
            )
            command = dict(item[:2] for item in executor.get_commands())[
                "Prepare the upload directory"
            ]
            done = subprocess.run(
                ["bash", "-c", command], capture_output=True, text=True,
                check=False,
            )
            self.assertNotEqual(done.returncode, 0)
            self.assertIn(f"rm -rf {tmp}/restore", done.stderr)
            self.assertIn("ask for the upload key again", done.stderr)
            self.assertFalse(list(Path(tmp, "theirs").iterdir()))

    def test_the_job_s_copy_is_the_one_that_was_checked(self):
        """The key stays open until its revocation runs, after the
        restore: what the uploader sends after the check is not what the
        operator confirmed. The copy taken into the job's directory —
        out of the key's reach — must carry the digest that was shown
        (N9, 28-oct-2026)."""
        executor = self._restore()
        transport = self._transport()
        asyncio.run(executor.before_execute(transport))
        local = executor._job_path("restore.zip")
        runs = [c.args[0] for c in transport.run.await_args_list]
        move = next(c for c in runs if "mv " in c)
        digest = f"sha256sum -- {local}"
        self.assertIn(digest, runs)
        self.assertLess(runs.index(move), runs.index(digest))

    def test_lab_an_archive_changed_after_its_check_is_not_restored(self):
        with self._lab() as tmp:
            executor = self._restore()
            executor._job_dir_path = f"{tmp}/job"
            granted = Path(self.grant._remote_path("restore.zip"))
            granted.write_bytes(b"PK-changed-after-the-check")
            transport = self._lab_transport()
            with self.assertRaisesRegex(RuntimeError, "changed"):
                asyncio.run(executor.before_execute(transport))
            asyncio.run(executor._discard_staging(transport))
            self.assertEqual(granted.read_bytes(), b"PK-changed-after-the-check")

    def test_lab_a_check_that_finds_nothing_usable_forgets_the_last(self):
        """What an earlier check recorded is not what is there now: it is
        forgotten, and the operator is told nothing can be restored (N8,
        28-oct-2026)."""
        for names in ([], ["my backup (1).zip"]):
            with self.subTest(names=names), self._lab():
                self.grant.write({
                    "received_filename": "restore.zip", "received_bytes": 5,
                    "received_sha256": hashlib.sha256(b"PK-ok").hexdigest(),
                })
                directory = Path(self.grant._directory())
                for name in names:
                    (directory / name).write_bytes(b"x")
                executor = self._verify()
                self._inspect(executor)
                self.assertFalse(self.grant.received_filename)
                self.assertFalse(self.grant.received_bytes)
                self.assertFalse(self.grant.received_sha256)
                logged = "".join(line for line, _src in executor._log_buffer)
                self.assertIn("Nothing that can be restored has arrived", logged)

    def test_the_upload_check_reports_no_link(self):
        """A link is not a file that arrived."""
        ((_label, command),) = self._verify().get_commands()
        self.assertIn('[ -L "$f" ]', command)


#: A stand-in for the rrsync of rsync 3.2.4 and later: Python, and it
#: knows ``-munge`` and ``-no-del``.
_MODERN_RRSYNC = """#!/usr/bin/env python3
import argparse
parser = argparse.ArgumentParser(prog='rrsync', add_help=False)
parser.add_argument('-wo', action='store_true')
parser.add_argument('-munge', action='store_true')
parser.add_argument('-no-del', action='store_true')
parser.add_argument('-help', '-h', action='help')
parser.add_argument('dir')
parser.parse_args()
"""

#: A stand-in for the Perl rrsync before it: ``-help`` is taken for its
#: directory, and its usage knows only ``-ro`` and ``-wo``.
_OLD_RRSYNC = """#!/bin/sh
echo "$0: Restricted directory does not exist!" >&2
echo "Use 'command=\\"$0 [-ro|-wo] SUBDIR\\"'" >&2
exit 1
"""


class TestTheUploadKeyNeedsAModernRrsync(_StagingCase):
    """The key's forced command is ``rrsync -wo -munge -no-del``, and it is
    installed only behind an rrsync that knows both options (28-oct-2026).

    With ``-wo`` alone, one session stored a link to any directory of the
    account and the next wrote through it — ``~/.ssh/authorized_keys``
    included: a shell as the account the panel connects as, root on the
    platform's hosts. The Perl rrsync of rsync before 3.2.4 has neither
    option, and the job that resolves the wrapper used to install it from
    the package's documentation.
    """

    def setUp(self):
        """A grant for the instance."""
        super().setUp()
        self.grant, _private = self.env["cloud.restore.upload.grant"]._open(
            self.inst,
        )

    def _wrapper_lab(self, tmp, target=None, sources=(), sudo=True,
                     root=False):
        """Run the step that resolves the wrapper against a tree of *tmp*.

        ``sudo`` is a stand-in that records what it was asked and runs it,
        or refuses it; ``id`` says the account is root, or is not.

        :param str tmp: the test's directory
        :param str target: content of the wrapper already in place, if any
        :param sources: ``(name, content)`` of the copies the package ships
        :param bool sudo: whether ``sudo -n`` lets the account act as root
        :param bool root: whether the account is root itself
        :return: the finished process, and what ``sudo`` was asked
        """
        bin_dir = Path(tmp, "bin")
        bin_dir.mkdir()
        stand_in = bin_dir / "sudo"
        stand_in.write_text(
            '#!/bin/sh\necho "$*" >> "$SUDO_LOG"\n[ "$1" = -n ] && shift\n'
            'exec "$@"\n' if sudo else
            '#!/bin/sh\necho "$*" >> "$SUDO_LOG"\n'
            'echo "sudo: a password is required" >&2\nexit 1\n',
            encoding="utf-8",
        )
        stand_in.chmod(0o755)
        account = bin_dir / "id"
        account.write_text(
            f'#!/bin/sh\n[ "$1" = -u ] && {{ echo {0 if root else 1000}; exit 0; }}\n'
            'exec /usr/bin/id "$@"\n', encoding="utf-8",
        )
        account.chmod(0o755)
        wrapper = Path(tmp, "usr-bin-rrsync")
        if target is not None:
            wrapper.write_text(target, encoding="utf-8")
            wrapper.chmod(0o755)
        paths = []
        for name, content in sources:
            path = Path(tmp, name)
            if name.endswith(".gz"):
                subprocess.run(
                    ["gzip", "-c"], input=content.encode(), check=True,
                    stdout=path.open("wb"),
                )
            else:
                path.write_text(content, encoding="utf-8")
            paths.append(str(path))
        command = restore_upload_key_executors._ensure_rrsync(
            str(wrapper), tuple(paths) or (f"{tmp}/none",),
        )
        done = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True,
            check=False, env={
                "PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin",
                "SUDO_LOG": f"{tmp}/sudo.log",
            },
        )
        asked = Path(tmp, "sudo.log")
        return done, wrapper, asked.read_text() if asked.exists() else ""

    def test_the_key_line_munges_links_and_deletes_nothing(self):
        """``-munge``: a link it receives leads nowhere; ``-no-del``: no
        session removes what another sent."""
        line = self.grant._authorized_keys_line()
        self.assertIn(
            f'command="/usr/bin/rrsync -wo -munge -no-del '
            f'{self.grant._directory()}",restrict,',
            line,
        )

    def test_lab_a_modern_wrapper_in_place_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            done, wrapper, asked = self._wrapper_lab(tmp, _MODERN_RRSYNC)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.split()[-1], str(wrapper))
        self.assertEqual(asked, "")

    def test_lab_an_old_wrapper_in_place_is_refused(self):
        """Left there by an earlier release, or by the host's owner."""
        with tempfile.TemporaryDirectory() as tmp:
            done, _wrapper, _asked = self._wrapper_lab(tmp, _OLD_RRSYNC)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout.split()[-1], "OLD")
        self.assertIn("3.2.4", done.stderr)

    def test_lab_an_old_copy_is_never_installed(self):
        """The documentation of an older package carries the Perl one,
        plain or compressed: it is not put where the key would run it."""
        with tempfile.TemporaryDirectory() as tmp:
            done, wrapper, asked = self._wrapper_lab(
                tmp, None, [("old-rrsync", _OLD_RRSYNC),
                            ("old-rrsync.gz", _OLD_RRSYNC)],
            )
            self.assertFalse(wrapper.exists())
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout.split()[-1], "OLD")
        self.assertEqual(asked, "")

    def test_lab_a_modern_copy_is_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            done, wrapper, asked = self._wrapper_lab(
                tmp, None, [("old-rrsync", _OLD_RRSYNC),
                            ("rrsync.gz", _MODERN_RRSYNC)],
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.split()[-1], str(wrapper))
            self.assertEqual(wrapper.read_text(), _MODERN_RRSYNC)
            self.assertTrue(wrapper.stat().st_mode & 0o111)
        self.assertTrue(asked)

    def test_lab_an_account_without_sudo_is_told_so(self):
        """A modern copy the account cannot install is not an old rrsync:
        the status and the message name the missing ``sudo``."""
        with tempfile.TemporaryDirectory() as tmp:
            done, wrapper, asked = self._wrapper_lab(
                tmp, None, [("rrsync", _MODERN_RRSYNC)], sudo=False,
            )
            self.assertFalse(wrapper.exists())
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout.split()[-1], "NOSUDO")
        self.assertIn("sudo", done.stderr)
        self.assertNotIn("older", done.stderr)
        self.assertTrue(asked)

    def test_lab_root_installs_it_without_sudo(self):
        """The platform's hosts connect as root, and may have no ``sudo``."""
        with tempfile.TemporaryDirectory() as tmp:
            done, wrapper, asked = self._wrapper_lab(
                tmp, None, [("rrsync", _MODERN_RRSYNC)], sudo=False, root=True,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.split()[-1], str(wrapper))
            self.assertEqual(wrapper.read_text(), _MODERN_RRSYNC)
        self.assertEqual(asked, "")

    def test_lab_no_wrapper_at_all_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            done, _wrapper, _asked = self._wrapper_lab(tmp)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(done.stdout.split()[-1], "MISSING")

    def test_no_key_is_installed_behind_a_refused_wrapper(self):
        """The steps before the key stop the run when they fail: no line
        is added, and the grant is closed."""
        executor = self._executor(
            GrantRestoreUploadKeyExecutor, "grant_restore_upload_key",
            payload={"grant_id": self.grant.id},
        )
        steps = {item[0]: item for item in executor.get_commands()}
        for label in ("Prepare the upload directory",
                      "Resolve the restricted rsync wrapper"):
            self.assertTrue(steps[label][2].get("stop_on_failure"), label)
        transport = self._transport()
        transport.execute.side_effect = lambda command, *_a, **_kw: (
            CommandResult("OLD\n", 1) if "-no-del" in command
            and "authorized_keys" not in command else CommandResult("", 0)
        )
        self._run(executor, transport)
        ran = [c.args[0] for c in transport.execute.await_args_list]
        self.assertFalse([c for c in ran if "authorized_keys" in c])
        self.grant.invalidate_recordset(["state"])
        self.assertEqual(self.grant.state, "revoked")


class TestTheKeyLineReachesTheFileWhole(_StagingCase):
    """The key's line goes into ``authorized_keys`` exactly as built, and
    its revocation removes exactly that line (28-oct-2026).

    The line holds double quotes (``command="…"``, ``expiry-time="…"``):
    written between double quotes, the host's shell split it into
    several, none of them a key sshd accepts, and every upload was
    refused.
    """

    #: Lines of the account's own, which neither step may touch.
    OPERATOR = (
        'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOperatorsOwnKey operator@laptop',
        'from="192.0.2.1",command="/usr/local/bin/backup" ssh-rsa AAAAB3 bk',
    )

    def setUp(self):
        """A grant for the instance, and an account's home of the test's own."""
        super().setUp()
        self.grant, _private = self.env["cloud.restore.upload.grant"]._open(
            self.inst,
        )
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.home)], check=False)
        self.keys = self.home / ".ssh" / "authorized_keys"

    def _step(self, executor_cls, code, label, payload=None):
        """Return the command of step *label* of a job of type *code*."""
        executor = self._executor(
            executor_cls, code, payload=payload or {"grant_id": self.grant.id},
        )
        return dict(item[:2] for item in executor.get_commands())[label]

    def _sh(self, command):
        """Run *command* with bash, its ``$HOME`` the test's own."""
        done = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True,
            check=False, env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_lab_the_line_is_written_exactly_once(self):
        """Quotes included, beside the account's own keys; a retry leaves
        one line, not two."""
        self.keys.parent.mkdir(mode=0o700)
        self.keys.write_text("\n".join(self.OPERATOR) + "\n", encoding="utf-8")
        install = self._step(
            GrantRestoreUploadKeyExecutor, "grant_restore_upload_key",
            "Install the one-use key",
        )
        self._sh(install)
        self._sh(install)
        line = self.grant._authorized_keys_line()
        self.assertIn('"', line)
        self.assertEqual(
            self.keys.read_text(encoding="utf-8").splitlines(),
            [*self.OPERATOR, line],
        )

    def test_lab_the_line_is_written_into_a_new_file(self):
        """An account with no ``authorized_keys`` yet gets one, 0600."""
        self._sh(self._step(
            GrantRestoreUploadKeyExecutor, "grant_restore_upload_key",
            "Install the one-use key",
        ))
        self.assertEqual(
            self.keys.read_text(encoding="utf-8"),
            self.grant._authorized_keys_line() + "\n",
        )
        self.assertEqual(self.keys.stat().st_mode & 0o777, 0o600)

    def test_lab_the_revocation_removes_exactly_that_line(self):
        """Another grant's line and the account's own keys stay. The
        pieces an older install left when the shell split its line go
        too: none carried the grant's marker."""
        other, _private = self.env["cloud.restore.upload.grant"]._open(
            self.inst,
        )
        self.keys.parent.mkdir(mode=0o700)
        self.keys.write_text("\n".join([
            self.OPERATOR[0],
            "command=/usr/bin/rrsync", "-wo", "-munge", "-no-del",
            f"{self.grant._directory()},restrict,expiry-time=20261028223734"
            f" {self.grant.public_key} {self.grant._key_comment()}",
            self.grant._authorized_keys_line(),
            other._authorized_keys_line(),
            self.OPERATOR[1],
        ]) + "\n", encoding="utf-8")
        self._sh(self._step(
            RevokeRestoreUploadKeyExecutor, "revoke_restore_upload_key",
            "Remove the one-use key",
        ))
        self.assertEqual(
            self.keys.read_text(encoding="utf-8").splitlines(),
            [self.OPERATOR[0], other._authorized_keys_line(), self.OPERATOR[1]],
        )
