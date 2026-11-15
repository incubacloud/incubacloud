"""An instance's backup path reaches the hosts as written, never as code.

The path (``custom_backup_dst``, or the one a destination's bucket and
path compute) goes to a host three ways: as an argument of the backup
listing's command, as the ``DST`` line of the purges' environment files
and as the answer the template renders into the compose file. These pin
the grammar a path is written in from now on and, for a path saved
before it, that each of the three carries it as data: quoted in the
command, refused by a file whose line it would break.
"""
import asyncio
import importlib.util
import shlex
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.incubacloud.models.backup_list_executor import BackupListExecutor
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.purge_archived_executor import (
    PurgeArchivedBackupsExecutor,
)
from odoo.addons.incubacloud.models.transport import SSHTransport

#: What a shell, an environment file or a compose file would read as more
#: than a path, each after a valid start.
_PAYLOADS = (
    "boto3+s3://bd-bucket/backups/x;touch /tmp/bd-injected",
    "boto3+s3://bd-bucket/backups/$(touch /tmp/bd-injected)",
    "boto3+s3://bd-bucket/backups/`touch /tmp/bd-injected`",
    'boto3+s3://bd-bucket/backups/x"; touch /tmp/bd-injected; "',
    "boto3+s3://bd-bucket/backups/x'; touch /tmp/bd-injected; '",
    "boto3+s3://bd-bucket/backups/x\nAWS_ENDPOINT_URL=https://elsewhere.example",
    "boto3+s3://bd-bucket/backups/x\r\ntouch /tmp/bd-injected",
)

#: Paths the panel writes and imports find: they stay accepted.
_VALID = (
    "boto3+s3://bd-bucket/backups/bd-proj/bd-inst",
    "boto3+s3://b/p/i",
    "s3://bd-bucket/backups/bd-proj/bd-inst/",
    "s3://minio.example.net:9000/bd-bucket/Old_Path/x.y-z",
    "boto3+s3://bd-bucket",
)

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "1.0.229" / "post-migrate.py"
)
#: The name the migration is loaded under, which its log takes.
_MIGRATION_LOGGER = "ic_1_0_229_post"


class _BackupDstCase(TransactionCase):

    def setUp(self):
        self.registry_enter_test_mode()
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "bd-proj"})
        self.host = self.env["cloud.host"].create({
            "name": "bd-host", "ip_address": "10.0.13.1", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "bd.example.com",
            "status": "compatible", "traefik_deployed": True,
        })
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "bd-backend",
            "backend_type": "s3",
            "s3_bucket": "bd-bucket",
            "s3_path": "backups",
            "s3_access_key_id": "AKIABD",
            "s3_secret_access_key": "bd-secret",
            "s3_endpoint_url": "https://bd.example.net",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "bd-inst", "project_id": self.project.id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed",
            "backup_backend_id": self.backend.id,
        })

    def _saved_before(self, dst, record=None):
        """Store *dst* as a path saved before the grammar, past the ORM.

        :param str dst: the path
        :param record: the instance, ``self.instance`` by default
        """
        record = record or self.instance
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_instance SET custom_backup_dst = %s WHERE id = %s",
            (dst, record.id),
        )
        self.env.invalidate_all()

    def _backend_saved_before(self, path):
        """Store a destination's path saved before the grammar, past the ORM.

        :param str path: the path inside the bucket
        """
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_backup_backend SET s3_path = %s, backup_dst = %s"
            " WHERE id = %s",
            (path, f"boto3+s3://bd-bucket/{path}", self.backend.id),
        )
        self.env.invalidate_all()


class TestTheBackupPathHasAGrammar(_BackupDstCase):

    def test_a_path_a_shell_or_a_file_would_read_as_more_is_refused(self):
        """Written as the platform too: the grammar is the field's."""
        refused = (
            *_PAYLOADS,
            "boto3+s3://bd-bucket/backups/with space",
            "boto3+s3://bd-bucket/backups/../other-inst",
            "boto3+s3://bd-bucket//other-inst",
            "file:///var/backups/bd-inst",
            "bd-bucket/backups/bd-inst",
        )
        for dst in refused:
            with self.subTest(dst=dst), self.assertRaises(ValidationError):
                self.instance.sudo().write({"custom_backup_dst": dst})

    def test_an_import_is_held_to_it(self):
        """Created already deployed, with the path its answers file gave."""
        for dst in _PAYLOADS:
            with self.subTest(dst=dst), self.assertRaises(ValidationError):
                self.env["cloud.instance"].sudo().create({
                    "name": "bd-imported", "project_id": self.project.id,
                    "environment": "staging", "host_id": self.host.id,
                    "state": "deployed", "custom_backup_dst": dst,
                })

    def test_the_paths_the_panel_writes_are_accepted(self):
        for dst in _VALID:
            with self.subTest(dst=dst):
                self.instance.sudo().write({"custom_backup_dst": dst})
                self.assertEqual(self.instance.custom_backup_dst, dst)

    def test_a_path_saved_before_goes_on_as_it_is(self):
        """Only a write of the path is checked: the rest of the record saves."""
        legacy = "boto3+s3://bd-bucket/backups/with space"
        self._saved_before(legacy)
        self.instance.sudo().write({"auto_rebuild": True})
        self.assertEqual(self.instance.custom_backup_dst, legacy)

    def test_archiving_freezes_a_path_computed_from_a_destination_saved_before(self):
        """The stack is gone by then: the path is kept, and the log says so."""
        self._backend_saved_before("with space")
        computed = self.instance.instance_backup_dst
        self.assertIn("with space", computed)
        with self.assertLogs(
            "odoo.addons.incubacloud.models.cloud_instance", "WARNING",
        ) as logs:
            self.instance._finalize_removal(keep_in_panel=True)
        self.assertEqual(self.instance.custom_backup_dst, computed)
        self.assertIn("outside the field's grammar", "\n".join(logs.output))


@tagged("post_install", "-at_install")
class TestNoContextLiftsTheGrammar(_BackupDstCase):
    """After every module is loaded: the user it creates gets the partner
    columns the modules loaded after this one require."""

    def test_no_context_lets_an_administrator_s_write_past_it(self):
        """What an RPC call may send along does not lift the check."""
        manager = new_test_user(
            self.env, login="bd-manager",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        inst = self.instance.with_user(manager).with_context(backup_dst_freeze=True)
        with self.assertRaises(ValidationError):
            inst.write({"custom_backup_dst": _PAYLOADS[1]})


class TestADestinationsBucketAndPathHaveTheGrammar(_BackupDstCase):

    def test_a_bucket_or_a_path_outside_it_is_refused(self):
        for vals in (
            {"s3_path": "backups;touch /tmp/bd-injected"},
            {"s3_path": "$(touch /tmp/bd-injected)"},
            {"s3_path": 'x"\nprivileged: true'},
            {"s3_path": "with space"},
            {"s3_path": "../elsewhere"},
            {"s3_bucket": "bd-bucket;id"},
            {"s3_bucket": "bd-bucket`id`"},
        ):
            with self.subTest(vals=vals), self.assertRaises(ValidationError):
                self.backend.write(vals)

    def test_the_paths_destinations_have_are_accepted(self):
        for path in ("backups", "clients/acme", "/backups/", ""):
            with self.subTest(path=path):
                self.backend.write({"s3_path": path})
        self.backend.write({"s3_bucket": "Legacy_Bucket.eu"})


class TestTheBackupListingQuotesThePath(_BackupDstCase):

    def _command(self):
        """The listing's command for ``self.instance``."""
        job = self.env["cloud.job"].create({
            "job_type_id": self.env.ref("incubacloud.backup_list").id,
            "instance_id": self.instance.id,
            "host_id": self.host.id,
        })
        (label, command), = BackupListExecutor(job, self.host).get_commands()
        self.assertEqual(label, "List backups")
        return command

    def _assert_one_literal_argument(self, command, dst):
        """*dst* is the command's last argument, and a shell reads it back as is."""
        self.assertEqual(shlex.split(command)[-2:], ["collection-status", dst])
        quoted = command.split(" dup collection-status ", 1)[1]
        echoed = subprocess.run(
            ["bash", "-c", f"printf %s {quoted}"],
            capture_output=True, check=True,
        ).stdout
        self.assertEqual(echoed.decode(), dst)

    def test_a_path_saved_before_is_one_argument(self):
        for dst in _PAYLOADS:
            with self.subTest(dst=dst):
                self._saved_before(dst)
                self._assert_one_literal_argument(self._command(), dst)

    def test_a_path_computed_from_a_destination_saved_before_is_one_argument(self):
        self._backend_saved_before("backups/$(touch /tmp/bd-injected)")
        dst = self.instance.instance_backup_dst
        self.assertIn("$(touch", dst)
        self._assert_one_literal_argument(self._command(), dst)


class TestThePurgeEnvironmentKeepsItsLines(_BackupDstCase):

    def _purge(self):
        """Archive the instance, delete it; return the purge's executor."""
        self.instance._finalize_removal(keep_in_panel=True)
        job = self.env["cloud.job"].browse(self.instance.delete_archived()[0])
        executor = object.__new__(PurgeArchivedBackupsExecutor)
        executor.job = job
        executor.env = job.env
        executor._log_buffer = []
        executor._scripts_requested = False
        executor._scripts_uploaded = False
        executor._script_overlay_cache = None
        return executor

    def test_a_path_that_breaks_its_line_fails_the_job_before_the_host(self):
        executor = self._purge()
        for dst in _PAYLOADS[-2:]:
            with self.subTest(dst=dst):
                self._saved_before(dst)
                upload = AsyncMock()
                with patch.object(
                    PurgeArchivedBackupsExecutor, "_upload_private_files", upload,
                ), self.assertRaises(ValueError):
                    asyncio.run(executor.before_execute(MagicMock(spec=SSHTransport)))
                upload.assert_not_awaited()

    def test_a_further_prefix_that_breaks_its_line_is_refused_too(self):
        executor = self._purge()
        with patch.object(
            type(self.instance), "_archived_copy_extra_prefixes",
            autospec=True, return_value=[_PAYLOADS[-2]],
        ), self.assertRaises(ValueError):
            executor._env_contents()

    def test_a_shell_s_characters_stay_on_the_one_line_as_data(self):
        """No shell reads the file: the value is the rest of its line."""
        executor = self._purge()
        for dst in _PAYLOADS[:-2]:
            with self.subTest(dst=dst):
                self._saved_before(dst)
                lines = executor._env_content().splitlines()
                self.assertEqual(lines[0], f"DST={dst}")
                self.assertEqual(
                    [line.split("=", 1)[0] for line in lines],
                    ["DST", "PURGE_BEFORE", "AWS_ACCESS_KEY_ID",
                     "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL"],
                )


class TestTheComposeFileGetsNoPathThatBreaksIt(_BackupDstCase):

    def _executor(self):
        """A deploy executor of ``self.instance``."""
        job = self.env["cloud.job"].create({
            "job_type_id": self.env.ref("incubacloud.deploy_instance").id,
            "instance_id": self.instance.id,
            "host_id": self.host.id,
        })
        return DeployInstanceExecutor(job, self.host)

    def test_a_path_saved_before_reaches_the_answers_and_is_refused(self):
        executor = self._executor()
        for dst in (
            'boto3+s3://bd-bucket/backups/x"\n    privileged: true',
            "boto3+s3://bd-bucket/backups/${POSTGRES_PASSWORD}",
            "boto3+s3://bd-bucket/backups/x\\",
        ):
            with self.subTest(dst=dst):
                self._saved_before(dst)
                answer = self.instance._render_copier_answers()["backup_dst"]
                self.assertEqual(answer, dst)
                with self.assertRaises(ValueError):
                    executor._check_backup_dst_renders(answer)

    def test_the_upload_stops_before_anything_reaches_the_host(self):
        executor = self._executor()
        upload = AsyncMock()
        with patch.object(
            DeployInstanceExecutor, "_read_host_cpus", new_callable=AsyncMock,
        ), patch.object(
            DeployInstanceExecutor, "_resync_repo_requirements",
            new_callable=AsyncMock,
        ), patch.object(
            DeployInstanceExecutor, "_build_answers", autospec=True,
            return_value={"backup_dst": "boto3+s3://bd-bucket/x\nprivileged: true"},
        ), patch.object(
            DeployInstanceExecutor, "_upload_private_files", upload,
        ), self.assertRaises(ValueError):
            asyncio.run(executor._upload_copier_files(MagicMock(spec=SSHTransport)))
        upload.assert_not_awaited()

    def test_a_path_of_the_grammar_renders(self):
        executor = self._executor()
        for dst in (*_VALID, "", "boto3+s3://bd-bucket/backups/with space"):
            with self.subTest(dst=dst):
                executor._check_backup_dst_renders(dst)


class TestTheUpdateNamesThePathsSavedBefore(_BackupDstCase):

    def _migrate(self):
        """Run 1.0.229's post-migrate on this cursor, from 1.0.228."""
        spec = importlib.util.spec_from_file_location(_MIGRATION_LOGGER, _MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.migrate(self.env.cr, "19.0.1.0.228")

    def test_they_are_logged_and_kept(self):
        legacy = _PAYLOADS[1]
        self._saved_before(legacy)
        self._backend_saved_before("with space")
        with self.assertLogs(_MIGRATION_LOGGER, "WARNING") as logs:
            self._migrate()
        output = "\n".join(logs.output)
        self.assertIn(f"instance bd-inst (id {self.instance.id})", output)
        self.assertIn(f"backup destination bd-backend (id {self.backend.id})", output)
        self.assertEqual(self.instance.custom_backup_dst, legacy)
        self.assertEqual(self.backend.s3_path, "with space")

    def test_paths_of_the_grammar_are_not_named(self):
        self._saved_before(_VALID[0])
        with self.assertNoLogs(_MIGRATION_LOGGER, "WARNING"):
            self._migrate()
