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
import contextlib
import importlib.util
import json
import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import jinja2
import yaml

from odoo import http as odoo_http
from odoo.exceptions import ValidationError
from odoo.http import Request
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.incubacloud.controllers._data_load import _routes_ops
from odoo.addons.incubacloud.controllers.data_load import CloudDataLoadController
from odoo.addons.incubacloud.models.abstract_executor import JobRefused
from odoo.addons.incubacloud.models.backup_list_executor import BackupListExecutor
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.purge_archived_executor import (
    PurgeArchivedBackupsExecutor,
)
from odoo.addons.incubacloud.models.transport import SSHTransport
from odoo.addons.incubacloud.tests._template import pin_template

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
)

#: The bucket's root: of the grammar, and a path a compose file carries,
#: but every instance's copies are under it.
_ROOT = "boto3+s3://bd-bucket"

_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "1.0.229" / "post-migrate.py"
)
#: The name the migration is loaded under, which its log takes.
_MIGRATION_LOGGER = "ic_1_0_229_post"


@contextlib.contextmanager
def _no_host_reads():
    """Patch the copier upload's reads of the host, which these tests never reach."""
    with patch.object(
        DeployInstanceExecutor, "_read_host_cpus", autospec=True,
    ), patch.object(
        DeployInstanceExecutor, "_resync_repo_requirements", autospec=True,
    ):
        yield


def _docker_env(content):
    """Read an environment file as ``docker run --env-file`` does.

    A line at a time, the rest of the line after the first ``=`` the
    value, and the last of two lines naming a variable the one that wins.

    :param str content: the file
    :return: ``{name: value}``
    :rtype: dict
    """
    env = {}
    for line in content.split("\n"):
        if line and not line.startswith("#"):
            name, _sep, value = line.partition("=")
            env[name] = value
    return env


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
            "boto3+s3://bd-bucket/./backups/bd-inst",
            "boto3+s3://bd-bucket/backups/./bd-inst",
            "s3://minio.example.net:99999/bd-bucket/x",
            "s3://minio.example.net:65536/bd-bucket/x",
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
                with patch.object(
                    PurgeArchivedBackupsExecutor, "_upload_private_files",
                    autospec=True,
                ) as upload, self.assertRaises(ValueError):
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
                answers = self.instance._render_copier_answers()
                self.assertEqual(answers["backup_dst"], dst)
                with self.assertRaises(ValueError):
                    executor._check_backup_dst_renders(answers)

    def test_the_upload_stops_before_anything_reaches_the_host(self):
        executor = self._executor()
        with _no_host_reads(), patch.object(
            DeployInstanceExecutor, "_build_answers", autospec=True,
            return_value={"backup_dst": "boto3+s3://bd-bucket/x\nprivileged: true"},
        ), patch.object(
            DeployInstanceExecutor, "_upload_private_files", autospec=True,
        ) as upload, self.assertRaises(ValueError):
            asyncio.run(executor._upload_copier_files(MagicMock(spec=SSHTransport)))
        upload.assert_not_awaited()

    def test_a_path_of_the_grammar_renders(self):
        executor = self._executor()
        answers = self.instance._render_copier_answers()
        for dst in (*_VALID, _ROOT, "", "boto3+s3://bd-bucket/backups/with space"):
            with self.subTest(dst=dst):
                executor._check_backup_dst_renders(answers | {"backup_dst": dst})


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


class TestTheEnvironmentFilesKeepEveryLine(_BackupDstCase):
    """Every line of the purges' and the backup container's files, not just DST.

    ``docker run --env-file`` takes the last of two lines naming one
    variable: a line break in the endpoint or a key would name ``DST``
    again, and the purge would empty the destination's root.
    """

    #: An endpoint that would name the purge's DST and cutoff again.
    _ENDPOINT_PAYLOAD = (
        "https://bd.example.net\nDST=boto3+s3://bd-bucket/backups\nPURGE_BEFORE="
    )
    #: A key that would add a job the backup container runs in a shell.
    _KEY_PAYLOAD = "AKIABD\nJOB_900_WHAT=touch /tmp/bd-injected"

    def test_a_destination_refuses_an_endpoint_or_a_key_that_breaks_its_line(self):
        for vals in (
            {"s3_endpoint_url": self._ENDPOINT_PAYLOAD},
            {"s3_endpoint_url": "https://bd.example.net/x y"},
            {"s3_endpoint_url": "ftp://bd.example.net"},
            {"s3_endpoint_url": "https://bd.example.net:70000"},
            {"s3_endpoint_url": "https://user@bd.example.net"},
            {"s3_endpoint_url": "https://bd.example.net/?x=$(id)"},
            {"s3_access_key_id": self._KEY_PAYLOAD},
            {"s3_access_key_id": "AKIA BD"},
            {"s3_secret_access_key": "bd\rJOB_900_WHAT=id"},
            {"passphrase": "bd\nJOB_900_WHAT=id"},
            {"backup_retention": "3M; touch /tmp/bd-injected"},
        ):
            with self.subTest(vals=vals), self.assertRaises(ValidationError):
                self.backend.write(vals)

    def test_the_endpoints_and_keys_destinations_have_are_accepted(self):
        for url in (
            "https://s3.eu-central-1.amazonaws.com",
            "http://10.0.0.5:9000",
            "https://[2001:db8::5]:9000/minio",
            "https://abc.r2.cloudflarestorage.com",
            "",
        ):
            with self.subTest(url=url):
                self.backend.write({"s3_endpoint_url": url})
        self.backend.write({
            "s3_access_key_id": "AKIA/BD+x=",
            "s3_secret_access_key": "wJalr/K7MDENG+bPxRfiCY=",
            "passphrase": "a passphrase with spaces",
        })

    def _saved_before(self, column, value):
        """Store *value* in the destination's *column*, past its checks.

        A secret stored as plain text reads back as it is
        (``decrypt_value``), as one saved before encryption does.

        :param str column: the column
        :param str value: the value
        """
        self.env.flush_all()
        self.env.cr.execute(
            f"UPDATE cloud_backup_backend SET {column} = %s WHERE id = %s",  # noqa: S608
            (value, self.backend.id),
        )
        self.env.invalidate_all()

    def _purge(self):
        """Archive the instance, delete it; return the purge's executor."""
        self.instance._finalize_removal(keep_in_panel=True)
        job = self.env["cloud.job"].browse(self.instance.delete_archived()[0])
        return PurgeArchivedBackupsExecutor(job, self.host)

    def test_the_purge_s_file_names_each_variable_once(self):
        """Read as docker reads it, the DST is the instance's own."""
        content = self._purge()._env_content()
        names = [line.split("=", 1)[0] for line in content.splitlines()]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(_docker_env(content)["DST"], self.instance.custom_backup_dst)

    def test_a_value_saved_before_that_would_name_dst_again_fails_the_purge(self):
        """The last DST would win: the job fails before anything is uploaded."""
        executor = self._purge()
        for column, value in (
            ("s3_endpoint_url", self._ENDPOINT_PAYLOAD),
            ("s3_access_key_id", "AKIABD\nDST=boto3+s3://bd-bucket/backups"),
            ("s3_secret_access_key", "bd\nPURGE_BEFORE="),
        ):
            with self.subTest(column=column):
                self._saved_before(column, value)
                with patch.object(
                    PurgeArchivedBackupsExecutor, "_upload_private_files",
                    autospec=True,
                ) as upload, self.assertRaises(ValueError):
                    asyncio.run(executor.before_execute(MagicMock(spec=SSHTransport)))
                upload.assert_not_awaited()
                self._saved_before(column, "bd-clean")

    def _deploy(self):
        """A deploy executor of ``self.instance``."""
        job = self.env["cloud.job"].create({
            "job_type_id": self.env.ref("incubacloud.deploy_instance").id,
            "instance_id": self.instance.id,
            "host_id": self.host.id,
        })
        return DeployInstanceExecutor(job, self.host)

    def test_backup_env_refuses_a_value_saved_before_that_adds_a_job(self):
        """``JOB_900_WHAT`` would run in the backup container's shell."""
        executor = self._deploy()
        for column in ("s3_access_key_id", "s3_secret_access_key", "passphrase",
                       "s3_endpoint_url"):
            with self.subTest(column=column):
                self._saved_before(column, self._KEY_PAYLOAD)
                with self.assertRaises(ValueError):
                    executor._backup_env_content()
                self._saved_before(column, "bd-clean")

    def test_backup_env_names_each_variable_once(self):
        content = self._deploy()._backup_env_content()
        names = [line.split("=", 1)[0] for line in content.splitlines()]
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("JOB_900_WHAT", _docker_env(content))


class TestTheComposeAnswersKeepTheirShape(_BackupDstCase):
    """The image tag, the time zone and the addresses the template renders.

    ``image: …:{{ backup_image_version }}`` unquoted, the rest between
    double quotes of the backup service's ``environment:``: a line break
    there adds YAML of its own, such as ``privileged: true``.
    """

    #: The lines of the template's backup service these answers reach
    #: (``common.yaml.jinja``).
    _TEMPLATE = (
        "services:\n"
        "  backup:\n"
        "    image: ghcr.io/tecnativa/docker-duplicity-postgres-s3:"
        "{{ backup_image_version }}\n"
        "    environment:\n"
        '      DST: "{{ backup_dst }}"\n'
        '      EMAIL_FROM: "{{ backup_email_from }}"\n'
        '      EMAIL_TO: "{{ backup_email_to }}"\n'
        '      TZ: "{{ backup_tz }}"\n'
    )

    def _render(self, answers):
        """The backup service as the template renders it from *answers*.

        :param dict answers: the copier answers
        :return: the service, read back as YAML
        :rtype: dict
        """
        text = jinja2.Environment(autoescape=False).from_string(  # noqa: S701
            self._TEMPLATE,
        ).render(**answers)
        return yaml.safe_load(text)["services"]["backup"]

    def test_a_destination_refuses_an_answer_that_adds_yaml(self):
        for vals in (
            {"backup_image_version": "latest\n    privileged: true\n    pid: host"},
            {"backup_image_version": "latest privileged"},
            {"backup_image_version": "-latest"},
            {"backup_tz": 'UTC"\n    privileged: true\n    x: "'},
            {"backup_tz": "Mars/Olympus"},
            {"email_from": "ops@example.net\n    privileged: true"},
            {"email_to": 'ops@example.net"\n    privileged: true\n    x: "'},
            {"email_to": "ops@example.net, other@example.net"},
            {"email_from": "not an address"},
        ):
            with self.subTest(vals=vals), self.assertRaises(ValidationError):
                self.backend.write(vals)

    def test_the_answers_destinations_have_are_accepted(self):
        self.backend.write({
            "backup_image_version": "4.0.0",
            "backup_tz": "Europe/Madrid",
            "email_from": "Backups <backups@example.net>",
            "email_to": "ops@example.net",
        })
        service = self._render(self.instance._render_copier_answers())
        self.assertEqual(service["environment"]["TZ"], "Europe/Madrid")
        self.assertEqual(
            service["environment"]["EMAIL_FROM"], "Backups <backups@example.net>",
        )
        self.assertNotIn("privileged", service)

    def _saved_before(self, column, value):
        """Store *value* in the destination's *column*, past its checks.

        :param str column: the column
        :param str value: the value
        """
        self.env.flush_all()
        self.env.cr.execute(
            f"UPDATE cloud_backup_backend SET {column} = %s WHERE id = %s",  # noqa: S608
            (value, self.backend.id),
        )
        self.env.invalidate_all()

    def test_a_quoted_answer_saved_before_renders_as_its_own_text(self):
        """Escaped for the double quotes: the service gains nothing."""
        for column, field in (
            ("backup_tz", "TZ"), ("email_from", "EMAIL_FROM"),
            ("email_to", "EMAIL_TO"),
        ):
            with self.subTest(column=column):
                payload = 'x"\n    privileged: true\n    pid: host\n    y: "\\'
                self._saved_before(column, payload)
                answers = self.instance._render_copier_answers()
                self.assertEqual(
                    answers[f"backup_{column.removeprefix('backup_')}"],
                    json.dumps(payload)[1:-1],
                )
                service = self._render(answers)
                self.assertEqual(service["environment"][field], payload)
                self.assertNotIn("privileged", service)
                self.assertNotIn("pid", service)
                self._saved_before(column, "UTC" if column == "backup_tz" else "")

    def _deploy(self):
        """A deploy executor of ``self.instance``."""
        job = self.env["cloud.job"].create({
            "job_type_id": self.env.ref("incubacloud.deploy_instance").id,
            "instance_id": self.instance.id,
            "host_id": self.host.id,
        })
        return DeployInstanceExecutor(job, self.host)

    def test_an_image_tag_saved_before_that_adds_yaml_stops_the_upload(self):
        """Unquoted in the template: refused before anything reaches the host."""
        self._saved_before(
            "backup_image_version", "latest\n    privileged: true\n    pid: host",
        )
        executor = self._deploy()
        with self.assertRaises(ValueError):
            executor._check_backup_dst_renders(self.instance._render_copier_answers())
        with _no_host_reads(), patch.object(
            DeployInstanceExecutor, "_upload_private_files", autospec=True,
        ) as upload, self.assertRaises(ValueError):
            asyncio.run(executor._upload_copier_files(MagicMock(spec=SSHTransport)))
        upload.assert_not_awaited()

    def test_a_key_saved_before_that_breaks_backup_env_is_refused(self):
        """The template writes each key as a line of backup.env too."""
        executor = self._deploy()
        for column in ("s3_access_key_id", "s3_secret_access_key", "passphrase"):
            with self.subTest(column=column):
                self._saved_before(column, "bd\nJOB_900_WHAT=touch /tmp/bd-injected")
                with self.assertRaises(ValueError):
                    executor._check_backup_dst_renders(
                        self.instance._render_copier_answers(),
                    )
                self._saved_before(column, "bd-clean")

    def test_the_answers_a_destination_holds_render(self):
        self._deploy()._check_backup_dst_renders(self.instance._render_copier_answers())


class TestAPurgeReachesOnlyItsOwnCopy(_BackupDstCase):
    """A path that is a destination's, or holds another instance's, is refused.

    The purge deletes everything under ``prefix/`` older than its cutoff,
    which protects only what was written after it.
    """

    def setUp(self):
        super().setUp()
        # A staging: a project has one production.
        self.other = self.env["cloud.instance"].create({
            "name": "bd-other", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
        })
        self.elsewhere = self.env["cloud.project"].create({"name": "bd-proj-2"})
        self.own = self.instance.instance_backup_dst
        self.theirs = self.other.instance_backup_dst
        self.folder = self.own.rsplit("/", 1)[0]

    def test_a_path_written_over_another_s_is_refused_and_names_it(self):
        for dst in (self.theirs, f"{self.theirs}/sub", self.folder):
            with self.subTest(dst=dst), self.assertRaises(ValidationError) as caught:
                self.instance.sudo().write({"custom_backup_dst": dst})
            self.assertIn("bd-other", str(caught.exception))

    def test_a_destination_s_path_or_its_bucket_s_root_is_refused(self):
        for dst in (_ROOT, "boto3+s3://bd-bucket/backups", "s3://bd-bucket/backups/"):
            with self.subTest(dst=dst), self.assertRaises(ValidationError):
                self.instance.sudo().write({"custom_backup_dst": dst})

    def test_its_own_path_one_of_its_own_and_a_successor_s_are_accepted(self):
        for dst in (self.own, "boto3+s3://elsewhere/bd-inst", f"{self.own}-imported"):
            with self.subTest(dst=dst):
                self.instance.sudo().write({"custom_backup_dst": dst})
        # One of the same name, in another project: the path of a successor.
        successor = self.env["cloud.instance"].create({
            "name": "bd-inst", "project_id": self.elsewhere.id,
            "environment": "staging", "host_id": self.host.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
            "custom_backup_dst": f"{self.own}-imported",
        })
        self.assertEqual(successor.custom_backup_dst, f"{self.own}-imported")

    def _purge(self):
        """Archive the instance, delete it; return the purge's executor."""
        self.instance._finalize_removal(keep_in_panel=True)
        job = self.env["cloud.job"].browse(self.instance.delete_archived()[0])
        return PurgeArchivedBackupsExecutor(job, self.host)

    def _saved_before(self, dst):
        """Store *dst* as the archived instance's path, past its checks."""
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_instance SET custom_backup_dst = %s WHERE id = %s",
            (dst, self.instance.id),
        )
        self.env.invalidate_all()

    def test_a_purge_that_would_reach_another_s_copy_is_refused_with_an_alert(self):
        executor = self._purge()
        for dst, named in (
            (self.theirs, "bd-other"),
            (self.folder, "bd-other"),
            ("boto3+s3://bd-bucket/backups", "bd-backend"),
            (_ROOT, "would empty"),
        ):
            with self.subTest(dst=dst):
                self._saved_before(dst)
                with patch.object(
                    PurgeArchivedBackupsExecutor, "_upload_private_files",
                    autospec=True,
                ) as upload, self.assertRaises(JobRefused) as caught:
                    asyncio.run(executor.before_execute(MagicMock(spec=SSHTransport)))
                upload.assert_not_awaited()
                self.assertIn(named, str(caught.exception))
        with patch.object(PurgeArchivedBackupsExecutor, "_sys", autospec=True):
            asyncio.run(executor.on_abort(caught.exception))
        alert = self.env["cloud.alert"].search([
            ("code", "=", "archived_purge_failed"),
            ("instance_id", "=", self.instance.id), ("state", "=", "active"),
        ])
        self.assertEqual(len(alert), 1)
        self.assertIn("stopped before it started", alert.message)
        self.assertTrue(self.instance.exists())

    def test_a_purge_where_a_successor_of_the_same_name_backs_up_goes_on(self):
        """The cutoff protects what the successor wrote since."""
        executor = self._purge()
        self.env["cloud.instance"].create({
            "name": "bd-inst", "project_id": self.elsewhere.id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
            "custom_backup_dst": self.instance.custom_backup_dst,
        })
        with patch.object(
            PurgeArchivedBackupsExecutor, "_upload_private_files", autospec=True,
        ) as upload:
            asyncio.run(executor.before_execute(MagicMock(spec=SSHTransport)))
        upload.assert_awaited_once()

    def test_an_archived_instance_s_path_is_protected_too(self):
        """``active_test=False``: an archived copy is still in the bucket."""
        self.other.with_context(ic_archive_instance=True).write({"active": False})
        with self.assertRaises(ValidationError):
            self.instance.sudo().write({"custom_backup_dst": self.theirs})


@tagged("post_install", "-at_install")
class TestAnImportWithABackupValueRefused(_BackupDstCase):
    """An import whose host's files hold a value the panel refuses goes on without it.

    It said «An internal error occurred» instead.
    """

    def _import(self, copier, backup_env=""):
        """Import ``/srv/bd-imported`` whose files hold *copier* and *backup_env*.

        :param dict copier: the ``.copier-answers.yml``
        :param str backup_env: ``.docker/backup.env``
        :return: what the route answers
        """
        pin_template(self.env, "v10.0.8")
        # A project of its own: one production each.
        copier = {"project_name": "bd-imp", "odoo_version": "19.0", **copier}
        reads = [(yaml.safe_dump(copier), "")] + [("", "")] * 7 + [
            (backup_env, ""),
        ] + [("", "")] * 4
        fake_request = MagicMock(spec=Request)
        fake_request.env = self.env
        # odoo.http's too: the controller's ``env``, which ``_()`` reads.
        with patch.object(_routes_ops, "request", fake_request), patch.object(
            odoo_http, "request", fake_request,
        ), patch.object(
            CloudDataLoadController, "_sec", autospec=True,
            return_value=self.env["cloud.security.mixin"],
        ), patch.object(
            _routes_ops.OpsMixin, "_ssh_run_multi", autospec=True,
            return_value=reads,
        ):
            return CloudDataLoadController().cloud_import_host_instance(
                self.host.id, "/srv/bd-imported",
            )

    def test_a_backup_path_outside_the_grammar_is_left_out_and_told(self):
        answer = self._import({"backup_dst": "sftp://backup.example.net/bd"})
        self.assertTrue(answer["ok"], answer)
        instance = self.env["cloud.instance"].browse(answer["instance_id"])
        self.assertFalse(instance.custom_backup_dst)
        self.assertEqual(len(answer["warnings"]), 1)
        self.assertIn("sftp://backup.example.net/bd", answer["warnings"][0])
        self.assertIn("is invalid", answer["warnings"][0])

    def test_a_backup_path_over_another_instance_s_is_left_out_and_told(self):
        answer = self._import({"backup_dst": self.instance.instance_backup_dst})
        self.assertTrue(answer["ok"], answer)
        self.assertFalse(
            self.env["cloud.instance"].browse(answer["instance_id"]).custom_backup_dst,
        )
        self.assertIn("bd-inst", answer["warnings"][0])

    def test_a_destination_answer_that_adds_yaml_is_not_imported(self):
        answer = self._import(
            {
                "backup_dst": "boto3+s3://bd-import/backups/bd-proj/bd-imported",
                "backup_image_version": "latest\n    privileged: true",
            },
            "AWS_ACCESS_KEY_ID=AKIAIMPORT\nAWS_SECRET_ACCESS_KEY=bd\nPASSPHRASE=bd\n",
        )
        self.assertTrue(answer["ok"], answer)
        instance = self.env["cloud.instance"].browse(answer["instance_id"])
        self.assertFalse(instance.backup_backend_id)
        self.assertFalse(
            self.env["cloud.backup.backend"].search([("name", "=", "S3 bd-imp")]),
        )
        self.assertEqual(len(answer["warnings"]), 1)
        self.assertIn("backup destination was not imported", answer["warnings"][0])

    def test_a_clean_import_says_nothing_more(self):
        answer = self._import(
            {"backup_dst": "boto3+s3://bd-import/backups/bd-proj/bd-imported"},
            "AWS_ACCESS_KEY_ID=AKIAIMPORT\nAWS_SECRET_ACCESS_KEY=bd\nPASSPHRASE=bd\n",
        )
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["warnings"], [])
        instance = self.env["cloud.instance"].browse(answer["instance_id"])
        self.assertEqual(instance.backup_backend_id.s3_bucket, "bd-import")
