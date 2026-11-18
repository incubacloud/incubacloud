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
import logging
import re
import secrets
import shlex
import subprocess
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import jinja2
import yaml

from odoo import fields
from odoo import http as odoo_http
from odoo.exceptions import AccessError, ValidationError
from odoo.http import Request
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import SQL

from odoo.addons.incubacloud.controllers._data_load import _routes_ops
from odoo.addons.incubacloud.controllers.data_load import CloudDataLoadController
from odoo.addons.incubacloud.models.abstract_executor import JobRefused
from odoo.addons.incubacloud.models._backup_dst import (
    compose_env_breaks,
    dst_covers,
    dst_is_valid,
    dst_key,
)
from odoo.addons.incubacloud.models.backup_list_executor import BackupListExecutor
from odoo.addons.incubacloud.models.delete_instance_executor import (
    PURGE_ALERT_BY_EXIT,
    DeleteInstanceExecutor,
)
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.purge_archived_executor import (
    PURGE_ARCHIVED_LABEL,
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
    / "migrations" / "1.0.232" / "post-migrate.py"
)
#: The name the migration is loaded under, which its log takes.
_MIGRATION_LOGGER = "ic_1_0_232_post"


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


#: ``$NAME``, ``${NAME}`` or ``$$`` in a value compose interpolates.
_COMPOSE_VAR_RE = re.compile(
    r"\$(?:\$|\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))"
)


def _compose_env(content):
    """Read an environment file as compose reads a service's ``env_file``.

    A stand-in for compose's own reader (compose-go's ``dotenv``), which
    the tests cannot run: the rules the panel's files meet, each checked
    against ``docker compose`` v5.7.0 with the lines these tests use.
    ``NAME=value``; a value that opens with a quote ends at the closing
    one, and what follows on the line is read as another ``NAME=value``;
    an unquoted value ends at `` #`` and loses the spaces around it; in
    both but a single-quoted one, ``$NAME`` and ``${NAME}`` are replaced
    by an earlier variable of the file, or by nothing, and ``$$`` is
    ``$``. Escapes inside double quotes are not read: no value the
    panel writes holds one.

    :param str content: the file
    :return: ``{name: value}`` as the container gets it
    :rtype: dict
    """
    env = {}

    def interpolate(value):
        """Replace the variables of *value* as compose does."""
        return _COMPOSE_VAR_RE.sub(
            lambda m: "$" if m.group(0) == "$$"
            else env.get(m.group(1) or m.group(2), ""),
            value,
        )

    rest = content
    while rest.strip():
        rest = rest.lstrip()
        if rest.startswith("#"):
            rest = rest.partition("\n")[2]
            continue
        name, _sep, rest = rest.partition("=")
        name = name.strip()
        if rest[:1] in ("'", '"'):
            quote = rest[0]
            end = rest.index(quote, 1)
            value, rest = rest[1:end], rest[end + 1:]
            env[name] = value if quote == "'" else interpolate(value)
        else:
            line, _nl, rest = rest.partition("\n")
            env[name] = interpolate(line.split(" #", 1)[0].strip())
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

    def _dated(self, record, **columns):
        """Write an instance's dates as they would be, past the ORM.

        ``create_date`` is the transaction's start for every record a
        test creates, and ``archived_at`` the moment it archives.

        :param record: the ``cloud.instance``
        :param columns: ``create_date``, ``archived_at`` or
            ``purge_cutoff_at``, each a naive UTC datetime
        """
        self.env.flush_all()
        for column, value in columns.items():
            self.env.cr.execute(SQL(
                "UPDATE cloud_instance SET %s = %s WHERE id = %s",
                SQL.identifier(column), value, record.id,
            ))
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

    def test_an_sftp_path_an_sftp_destination_writes_is_accepted(self):
        """The grammar takes ``sftp://`` as ``sftp_dst.build`` writes it."""
        for dst in (
            "sftp://u123@backup.example.net/backups/bd-proj/bd-inst",
            "sftp://u123-sub1@backup.example.net:23//srv/backups/bd-proj/bd-inst",
            "sftp://u@10.0.0.5/bd-proj/bd-inst",
        ):
            with self.subTest(dst=dst):
                self.assertTrue(dst_is_valid(dst))
                self.instance.sudo().write({"custom_backup_dst": dst})
                self.assertEqual(self.instance.custom_backup_dst, dst)

    def test_an_sftp_path_a_shell_or_a_file_would_read_as_more_is_refused(self):
        for dst in (
            "sftp://backup.example.net/bd-proj/bd-inst",
            "sftp://u:pw@backup.example.net/bd-proj/bd-inst",
            "sftp://u@backup.example.net;touch/bd-proj/bd-inst",
            "sftp://u@backup.example.net/bd-proj/$(touch x)",
            "sftp://u@backup.example.net/bd-proj/../other",
            "sftp://u@backup.example.net:0/bd-proj/bd-inst",
            "sftp://u@backup.example.net/bd-proj/bd-inst?x=1",
            "sftp://u@[::1]/bd-proj/bd-inst",
            "sftp://u@backup.example.net/bd-proj/bd-inst\nDST=x",
        ):
            with self.subTest(dst=dst):
                self.assertFalse(dst_is_valid(dst))
                with self.assertRaises(ValidationError):
                    self.instance.sudo().write({"custom_backup_dst": dst})

    def test_an_sftp_path_is_compared_on_its_server_as_duplicity_reads_it(self):
        """A relative path is under the home: never the absolute one of the same name."""
        root = dst_key("sftp://u@Backup.example.net/backups")
        self.assertEqual(root, ("sftp://u@backup.example.net:22", ("backups",)))
        inst = dst_key("sftp://u@backup.example.net:22/backups/p/i")
        self.assertTrue(dst_covers(root, inst))
        self.assertFalse(dst_covers(root, dst_key("sftp://u@backup.example.net//backups/p/i")))
        self.assertFalse(dst_covers(root, dst_key("sftp://u@backup.example.net:23/backups/p/i")))
        self.assertFalse(dst_covers(root, dst_key("sftp://v@backup.example.net/backups/p/i")))
        self.assertIsNone(dst_key("sftp://backup.example.net/backups"))

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
        self.assertEqual(
            shlex.split(command)[-4:],
            ["collection-status", dst, "</dev/null", "2>&1"],
        )
        quoted = command.split(" dup collection-status ", 1)[1].removesuffix(
            " </dev/null 2>&1",
        )
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
        """Run 1.0.232's post-migrate on this cursor, from 1.0.228."""
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
        """Not this instance's: a base may hold other records it names."""
        self._saved_before(_VALID[0])
        with self.assertLogs(_MIGRATION_LOGGER, "WARNING") as logs:
            logging.getLogger(_MIGRATION_LOGGER).warning("the update ran")
            self._migrate()
        self.assertNotIn(f"(id {self.instance.id}", "\n".join(logs.output))

    def test_a_path_two_records_share_is_named_alerted_and_kept(self):
        self.instance._finalize_removal(keep_in_panel=True)
        path = self.instance.custom_backup_dst
        live = self.env["cloud.instance"].create({
            "name": "bd-inst",
            "project_id": self.env["cloud.project"].create({"name": "bd-proj-2"}).id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
        })
        self._saved_before(path, live)
        with self.assertLogs(_MIGRATION_LOGGER, "WARNING") as logs:
            self._migrate()
        output = "\n".join(logs.output)
        self.assertIn(f"the backup path {path} is held by", output)
        self.assertIn(f"bd-inst (id {self.instance.id}, archived)", output)
        self.assertIn(f"bd-inst (id {live.id}, live)", output)
        alert = self.env["cloud.alert"].search([
            ("code", "=", "backup_path_shared"), ("state", "=", "active"),
            ("message", "like", path),
        ])
        self.assertEqual(len(alert), 1)
        self.assertEqual(live.instance_backup_dst, path)
        self.assertFalse(live.backup_path_suffix)

    def test_the_alert_resolves_once_one_record_holds_the_path(self):
        """At the daily check of archived copies, compared as a purge reads it.

        A copy of a panel's database may hold alerts of its own, one of
        them resolvable by the first check: that check runs first, so the
        counts below are this test's paths alone.
        """
        Instance = self.env["cloud.instance"]
        Instance._resolve_unshared_backup_paths()
        self.instance._finalize_removal(keep_in_panel=True)
        path = self.instance.custom_backup_dst
        live = self.env["cloud.instance"].create({
            "name": "bd-inst",
            "project_id": self.env["cloud.project"].create({"name": "bd-proj-2"}).id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
        })
        self._saved_before(path, live)
        with self.assertLogs(_MIGRATION_LOGGER, "WARNING"):
            self._migrate()
        alert = self.env["cloud.alert"].search([
            ("code", "=", "backup_path_shared"), ("state", "=", "active"),
            ("dedup_key", "=", path),
        ])
        self.assertEqual(len(alert), 1)
        self.assertEqual(Instance._resolve_unshared_backup_paths(), 0)
        self.assertEqual(alert.state, "active")
        self._saved_before(f"{path}/", live)
        self.assertEqual(Instance._resolve_unshared_backup_paths(), 0)
        self._saved_before(f"{path}-{live.id}", live)
        with patch.object(
            type(Instance), "_probe_archived_copy", autospec=True,
            return_value=("present", 0.0),
        ):
            Instance._cron_verify_archived_copies()
        self.assertEqual(alert.state, "dismissed")


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
        text = jinja2.Environment(  # noqa: S701  # nosec B701 — YAML, never HTML
            autoescape=False,
        ).from_string(self._TEMPLATE).render(**answers)
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

    def test_its_own_path_and_one_of_its_own_are_accepted(self):
        for dst in (self.own, "boto3+s3://elsewhere/bd-inst", f"{self.own}-imported"):
            with self.subTest(dst=dst):
                self.instance.sudo().write({"custom_backup_dst": dst})

    def test_the_path_of_one_of_the_same_name_is_refused(self):
        """Live, or archived a while before: its chain would be continued."""
        self.instance.sudo().write({"custom_backup_dst": f"{self.own}-imported"})
        for archived in (False, True):
            with self.subTest(archived=archived):
                if archived:
                    self.instance._finalize_removal(keep_in_panel=True)
                    self._dated(
                        self.instance,
                        archived_at=fields.Datetime.now() - timedelta(days=2),
                    )
                with self.assertRaises(ValidationError) as caught:
                    self.env["cloud.instance"].create({
                        "name": "bd-inst", "project_id": self.elsewhere.id,
                        "environment": "staging", "host_id": self.host.id,
                        "state": "deployed", "backup_backend_id": self.backend.id,
                        "custom_backup_dst": f"{self.own}-imported",
                    })
                self.assertIn("bd-inst", str(caught.exception))

    def _purge(self):
        """Archive the instance, delete it; return the purge's executor."""
        self.instance._finalize_removal(keep_in_panel=True)
        job = self.env["cloud.job"].browse(self.instance.delete_archived()[0])
        return PurgeArchivedBackupsExecutor(job, self.host)

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
        successor = self.env["cloud.instance"].create({
            "name": "bd-inst", "project_id": self.elsewhere.id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
        })
        # A pair from before successors had a path of their own.
        self._saved_before(self.instance.custom_backup_dst, successor)
        now = fields.Datetime.now()
        self._dated(
            self.instance, create_date=now - timedelta(days=9),
            archived_at=now - timedelta(days=2),
        )
        self._dated(successor, create_date=now - timedelta(days=1))
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


class TestComposeReadsTheEnvironmentFilesAsWritten(_BackupDstCase):
    """``backup.env`` is the backup service's ``env_file``: compose reads it.

    Compose closes a value that opens with a quote and reads the rest of
    the line as another variable, interpolates ``$`` and cuts at `` #``:
    a ``JOB_9xx_WHAT`` the job runner starts in a shell, or a passphrase
    other than the one saved, which no restore could then read.
    """

    #: ``(column, value)``: what compose would read as more than the value.
    _PAYLOADS = (
        ("passphrase", '"x" JOB_900_WHAT=touch /tmp/bd-injected'),
        ("s3_access_key_id", '"AKIA"JOB_901_WHAT=id'),
        ("s3_secret_access_key", "'SK' JOB_902_WHAT=id"),
        ("passphrase", "pass$HOME"),
        ("passphrase", "pass #word"),
        ("passphrase", " padded"),
        ("passphrase", "back\\slash"),
        ("passphrase", "back`id`quote"),
    )

    def test_compose_reads_a_value_that_opens_with_a_quote_as_two(self):
        """The re-review's two lines, read as compose read them."""
        self.assertEqual(
            _compose_env('PASSPHRASE="x" JOB_900_WHAT=touch /tmp/pwned\n'),
            {"PASSPHRASE": "x", "JOB_900_WHAT": "touch /tmp/pwned"},
        )
        self.assertEqual(
            _compose_env('AWS_ACCESS_KEY_ID="AKIA"JOB_901_WHAT=id\n'),
            {"AWS_ACCESS_KEY_ID": "AKIA", "JOB_901_WHAT": "id"},
        )
        self.assertEqual(_compose_env("PASSPHRASE=pa$X\n"), {"PASSPHRASE": "pa"})

    def test_a_destination_refuses_a_value_compose_would_read_as_more(self):
        for column, value in self._PAYLOADS:
            with self.subTest(column=column, value=value), \
                    self.assertRaises(ValidationError):
                self.backend.write({column: value})

    def test_the_passphrase_the_panel_generates_and_inner_spaces_are_accepted(self):
        generated = self.env["cloud.backup.backend"].create({
            "name": "bd-generated", "backend_type": "s3", "s3_bucket": "bd-gen",
        })
        self.assertTrue(generated.sudo().passphrase)
        self.assertFalse(compose_env_breaks(generated.sudo().passphrase))
        for _n in range(200):
            self.assertFalse(compose_env_breaks(secrets.token_urlsafe(20)))
        self.backend.write({"passphrase": "a passphrase with spaces"})

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

    def _deploy(self):
        """A deploy executor of ``self.instance``."""
        job = self.env["cloud.job"].create({
            "job_type_id": self.env.ref("incubacloud.deploy_instance").id,
            "instance_id": self.instance.id,
            "host_id": self.host.id,
        })
        return DeployInstanceExecutor(job, self.host)

    def test_a_value_saved_before_stops_backup_env_and_the_answers(self):
        """Both writers of backup.env: the panel's file and the template's."""
        executor = self._deploy()
        for column, value in self._PAYLOADS:
            with self.subTest(column=column, value=value):
                self._saved_before(column, value)
                with self.assertRaises(ValueError):
                    executor._backup_env_content()
                with self.assertRaises(ValueError):
                    executor._check_backup_dst_renders(
                        self.instance._render_copier_answers(),
                    )
                self._saved_before(column, "bd-clean")

    def test_backup_env_reaches_the_container_as_written(self):
        """Read as compose reads it: each value as saved, and no job added."""
        self.backend.write({"retention_owner": "cron", "backup_retention": "7D"})
        content = self._deploy()._backup_env_content()
        env = _compose_env(content)
        secret = self.backend.sudo()
        self.assertEqual(env, {
            "AWS_ACCESS_KEY_ID": secret.s3_access_key_id,
            "AWS_SECRET_ACCESS_KEY": secret.s3_secret_access_key,
            "AWS_ENDPOINT_URL": secret.s3_endpoint_url,
            "PASSPHRASE": secret.passphrase,
            "JOB_800_WHAT": "dup --force remove-older-than 7D $DST",
        })

    def test_the_update_names_a_passphrase_compose_would_read_otherwise(self):
        """Without its value, and kept."""
        self._saved_before("passphrase", "pass$word")
        spec = importlib.util.spec_from_file_location(_MIGRATION_LOGGER, _MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with self.assertLogs(_MIGRATION_LOGGER, "WARNING") as logs:
            module.migrate(self.env.cr, "19.0.1.0.228")
        output = "\n".join(logs.output)
        self.assertIn("passphrase (with a quote, a $", output)
        self.assertNotIn("pass$word", output)
        self.assertEqual(self.backend.sudo().passphrase, "pass$word")


class TestTheEndpointTakesItsUsualSpellings(_BackupDstCase):

    def test_an_upper_case_scheme_is_stored_in_lower_case(self):
        self.backend.write({"s3_endpoint_url": "HTTPS://S3.Example.net/Path"})
        self.assertEqual(self.backend.s3_endpoint_url, "https://S3.Example.net/Path")
        created = self.env["cloud.backup.backend"].create({
            "name": "bd-upper", "backend_type": "s3", "s3_bucket": "bd-upper",
            "s3_endpoint_url": "Http://10.0.0.5:9000",
        })
        self.assertEqual(created.s3_endpoint_url, "http://10.0.0.5:9000")

    def test_a_container_s_name_and_a_fully_qualified_name_are_accepted(self):
        for url in ("http://minio_1:9000", "https://s3.example.net.",
                    "https://s3.example.net.:443/bucket"):
            with self.subTest(url=url):
                self.backend.write({"s3_endpoint_url": url})
                self.assertEqual(self.backend.s3_endpoint_url, url)


class TestDeletingALiveInstanceReachesOnlyItsCopy(_BackupDstCase):
    """The delete empties its prefix whole, with no cutoff.

    A path written before another instance came to live under it, or
    saved before the check, would take that one's copies with it.
    """

    def setUp(self):
        super().setUp()
        self.folder = self.instance.instance_backup_dst.rsplit("/", 1)[0]
        # Its project's folder, while no other instance is in it.
        self.instance.sudo().write({"custom_backup_dst": self.folder})
        # A staging: a project has one production. Its path is computed,
        # under the folder: no check reads it.
        self.other = self.env["cloud.instance"].create({
            "name": "bd-other", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
        })

    def _delete(self, payload=None):
        """A delete executor of ``self.instance``."""
        job = self.env["cloud.job"].create({
            "job_type_id": self.env.ref("incubacloud.delete_instance").id,
            "instance_id": self.instance.id,
            "host_id": self.host.id,
            "payload": payload or {},
        })
        return DeleteInstanceExecutor(job, self.host)

    def test_another_s_copy_under_its_path_refuses_it_before_any_step(self):
        self.assertTrue(
            self.other.instance_backup_dst.startswith(f"{self.folder}/"),
        )
        executor = self._delete()
        with patch.object(
            DeleteInstanceExecutor, "_durable_env", autospec=True,
        ) as durable, self.assertRaises(JobRefused) as caught:
            asyncio.run(executor.before_execute(MagicMock(spec=SSHTransport)))
        durable.assert_not_called()
        self.assertIn("bd-other", str(caught.exception))
        self.assertEqual(self.instance.state, "deployed")
        with patch.object(DeleteInstanceExecutor, "_sys", autospec=True):
            asyncio.run(executor.on_abort(caught.exception))
        alert = self.env["cloud.alert"].search([
            ("code", "=", PURGE_ALERT_BY_EXIT[22][0]),
            ("instance_id", "=", self.instance.id), ("state", "=", "active"),
        ])
        self.assertEqual(len(alert), 1)
        self.assertIn("bd-other", alert.message)
        self.assertIn("was not deleted", alert.message)
        self.assertEqual(self.instance.state, "deployed")
        self.assertTrue(self.instance.exists())

    def test_its_own_path_goes_on(self):
        self.instance.sudo().write({"custom_backup_dst": False})
        executor = self._delete()
        self.assertEqual(
            executor._purge_prefixes(self.instance),
            [self.instance.instance_backup_dst],
        )
        executor._refuse_shared_prefix(self.instance)

    def test_archiving_it_purges_nothing_and_is_not_refused(self):
        executor = self._delete({"keep_in_panel": True})
        self.assertEqual(executor._purge_prefixes(self.instance), [])
        executor._refuse_shared_prefix(self.instance)


class TestDeletingASuccessorKeepsTheArchivedCopy(_BackupDstCase):
    """The delete of a successor empties only what it stored since it was created.

    An archived instance of the same name keeps its copies in the same
    prefix (``custom_backup_dst``, frozen when it was archived), which
    the prefix check lets through: the delete's purge is bounded to what
    came after the successor's creation (``PURGE_SINCE``).
    """

    def setUp(self):
        super().setUp()
        self.instance._finalize_removal(keep_in_panel=True)
        self.archived_path = self.instance.custom_backup_dst
        elsewhere = self.env["cloud.project"].create({"name": "bd-proj-2"})
        self.successor = self.env["cloud.instance"].create({
            "name": "bd-inst", "project_id": elsewhere.id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
        })
        # A pair from before successors had a path of their own.
        self._saved_before(self.archived_path, self.successor)
        now = fields.Datetime.now()
        self._dated(
            self.instance, create_date=now - timedelta(days=9),
            archived_at=now - timedelta(days=2),
        )
        self._dated(self.successor, create_date=now - timedelta(days=1))

    def _purge_args(self, instance):
        """The arguments the delete of *instance* gives ``backup_purge.sh``.

        :param instance: the live ``cloud.instance`` to delete
        :return: the arguments after the script's path
        :rtype: list
        """
        job = self.env["cloud.job"].create({
            "job_type_id": self.env.ref("incubacloud.delete_instance").id,
            "instance_id": instance.id,
            "host_id": self.host.id,
            "payload": {},
        })
        executor = DeleteInstanceExecutor(job, self.host)
        executor._refuse_shared_prefix(instance)
        ((_label, command, _opts),) = executor._purge_step(instance, "~/d")
        argv = shlex.split(command)
        script = next(
            i for i, arg in enumerate(argv) if arg.endswith("/backup_purge.sh")
        )
        return argv[script + 1:]

    def test_the_successor_s_purge_starts_at_its_creation(self):
        self.assertEqual(self.successor.instance_backup_dst, self.archived_path)
        since = self.successor.create_date.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        self.assertEqual(self._purge_args(self.successor), ["~/d", since])

    def test_without_an_archived_instance_it_empties_its_prefix_whole(self):
        lone = self.env["cloud.instance"].create({
            "name": "bd-lone",
            "project_id": self.env["cloud.project"].create({"name": "bd-proj-3"}).id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
        })
        self.assertEqual(self._purge_args(lone), ["~/d"])

    def test_another_name_or_another_path_bounds_nothing(self):
        other = self.env["cloud.instance"].create({
            "name": "bd-third", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
        })
        self.assertEqual(other._backup_purge_since(other.instance_backup_dst), "")
        self.assertEqual(
            self.successor._backup_purge_since(f"{self.archived_path}-pitr"), "",
        )


class TestASharedPathNeedsDatesThatProveASuccession(_BackupDstCase):
    """Two instances of the same name in one path are a successor's only by dates.

    One archived, and the other created at least a minute after: the
    purges' bounds then keep each one's objects apart. Any other pair
    sharing a path is refused as another's copy (15-nov-2026).
    """

    def setUp(self):
        super().setUp()
        self.path = self.instance.instance_backup_dst
        self.namesake = self.env["cloud.instance"].create({
            "name": "bd-inst",
            "project_id": self.env["cloud.project"].create({"name": "bd-proj-2"}).id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
        })
        self._saved_before(self.path, self.namesake)
        self.now = fields.Datetime.now()
        self._dated(self.instance, create_date=self.now - timedelta(days=9))

    def _archived(self, at):
        """Archive ``self.instance`` *at* that instant (None: without the date)."""
        if self.instance.active:
            self.instance._finalize_removal(keep_in_panel=True)
        self._dated(self.instance, archived_at=at)

    def _delete(self, instance):
        """A delete executor of *instance*."""
        job = self.env["cloud.job"].create({
            "job_type_id": self.env.ref("incubacloud.delete_instance").id,
            "instance_id": instance.id, "host_id": self.host.id, "payload": {},
        })
        return DeleteInstanceExecutor(job, self.host)

    def test_two_live_ones_are_refused_both_ways(self):
        for inst, other in (
            (self.instance, self.namesake), (self.namesake, self.instance),
        ):
            with self.subTest(deleting=inst.id):
                self.assertEqual(
                    inst._backup_prefix_conflict(self.path), (None, other),
                )
                with self.assertRaises(JobRefused) as caught:
                    self._delete(inst)._refuse_shared_prefix(inst)
                self.assertIn(f"id {other.id}", str(caught.exception))

    def test_an_archive_without_its_date_proves_nothing(self):
        self._archived(None)
        self._dated(self.namesake, create_date=self.now - timedelta(days=1))
        with self.assertRaises(JobRefused):
            self._delete(self.namesake)._refuse_shared_prefix(self.namesake)

    def test_two_that_lived_at_the_same_time_are_refused(self):
        """The namesake was created before the archive, or within the minute."""
        for created in (
            self.now - timedelta(days=3), self.now - timedelta(days=2, seconds=-59),
        ):
            with self.subTest(created=created):
                self._archived(self.now - timedelta(days=2))
                self._dated(self.namesake, create_date=created)
                self.assertEqual(
                    self.namesake._backup_prefix_conflict(self.path),
                    (None, self.instance),
                )
                self.assertEqual(
                    self.instance._backup_prefix_conflict(self.path),
                    (None, self.namesake),
                )

    def test_one_created_a_minute_after_the_archive_is_a_successor(self):
        self._archived(self.now - timedelta(days=2))
        self._dated(
            self.namesake, create_date=self.now - timedelta(days=2, seconds=-60),
        )
        self.assertIsNone(self.namesake._backup_prefix_conflict(self.path))
        self.assertIsNone(self.instance._backup_prefix_conflict(self.path))
        self._delete(self.namesake)._refuse_shared_prefix(self.namesake)

    def test_the_archived_purge_of_a_pair_that_proves_nothing_is_refused(self):
        self._archived(self.now - timedelta(days=2))
        self._dated(self.namesake, create_date=self.now - timedelta(days=3))
        job = self.env["cloud.job"].browse(self.instance.delete_archived()[0])
        executor = PurgeArchivedBackupsExecutor(job, self.host)
        with patch.object(
            PurgeArchivedBackupsExecutor, "_upload_private_files", autospec=True,
        ) as upload, self.assertRaises(JobRefused) as caught:
            asyncio.run(executor.before_execute(MagicMock(spec=SSHTransport)))
        upload.assert_not_awaited()
        self.assertIn(f"id {self.namesake.id}", str(caught.exception))


class TestTheArchivedPurgeEndsWhereTheSuccessorBegan(_BackupDstCase):
    """``PURGE_BEFORE`` is the delete's cutoff or the successor's creation, the earlier.

    A delete decided after the successor's first copy (a retry of one
    refused, a rebuild that left the archived one behind) would otherwise
    take what the successor stored until then.
    """

    def setUp(self):
        super().setUp()
        self.now = fields.Datetime.now()
        self.instance._finalize_removal(keep_in_panel=True)
        self.path = self.instance.custom_backup_dst
        self._dated(
            self.instance, create_date=self.now - timedelta(days=30),
            archived_at=self.now - timedelta(days=20),
        )
        self.successor = self.env["cloud.instance"].create({
            "name": "bd-inst",
            "project_id": self.env["cloud.project"].create({"name": "bd-proj-2"}).id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
        })
        self._saved_before(self.path, self.successor)
        self.born = self.now - timedelta(days=19)
        self._dated(self.successor, create_date=self.born)

    def _purge_before(self):
        """What the purge's environment gives as ``PURGE_BEFORE``."""
        job = self.env["cloud.job"].browse(self.instance.delete_archived()[0])
        executor = PurgeArchivedBackupsExecutor(job, self.host)
        executor._refuse_shared_prefix()
        return _docker_env(executor._env_content())["PURGE_BEFORE"]

    def test_a_delete_decided_after_the_successor_was_created_ends_at_its_creation(self):
        self._dated(self.instance, purge_cutoff_at=self.now)
        self.assertEqual(self._purge_before(), self.born.isoformat())

    def test_a_delete_decided_before_keeps_its_cutoff(self):
        decided = self.now - timedelta(days=19, hours=1)
        self._dated(self.instance, purge_cutoff_at=decided)
        self.assertEqual(self._purge_before(), decided.isoformat())

    def test_a_successor_archived_in_turn_keeps_its_copy(self):
        self.successor.with_context(ic_archive_instance=True).write({"active": False})
        self._dated(self.successor, archived_at=self.now - timedelta(days=5))
        self._dated(self.instance, purge_cutoff_at=self.now)
        self.assertEqual(self._purge_before(), self.born.isoformat())

    def test_a_namesake_in_another_path_or_older_bounds_nothing(self):
        self._saved_before("boto3+s3://bd-bucket/backups/elsewhere/bd-inst", self.successor)
        self.assertEqual(
            self.instance._backup_purge_before(self.path, self.now), self.now,
        )
        self._saved_before(self.path, self.successor)
        self._dated(self.successor, create_date=self.now - timedelta(days=31))
        self.assertEqual(
            self.instance._backup_purge_before(self.path, self.now), self.now,
        )

    def test_a_chain_the_successor_continued_keeps_the_record_with_its_alert(self):
        self._dated(self.instance, purge_cutoff_at=self.now)
        job = self.env["cloud.job"].browse(self.instance.delete_archived()[0])
        executor = PurgeArchivedBackupsExecutor(job, self.host)
        results = {PURGE_ARCHIVED_LABEL: {"stdout": "", "exit_status": 23}}
        with patch.object(PurgeArchivedBackupsExecutor, "_sys", autospec=True):
            asyncio.run(executor.on_failure(results, ["exit 23"]))
        alert = self.env["cloud.alert"].search([
            ("code", "=", "archived_purge_chain_continued"),
            ("instance_id", "=", self.instance.id), ("state", "=", "active"),
        ])
        self.assertEqual(len(alert), 1)
        self.assertIn("continued its backup chain", alert.message)
        self.assertTrue(self.instance.exists())


class TestAnArchivedSuccessorsPurgeStartsAtItsCreation(_BackupDstCase):
    """The purge of a successor archived in turn keeps its predecessor's copy.

    Both archived in one path: the successor's purge carries
    ``PURGE_SINCE``, its own creation, so what the predecessor stored
    before stays for that one's purge; the predecessor's carries none
    (``_backup_purge_since`` counts only namesakes created before).
    """

    def setUp(self):
        super().setUp()
        self.now = fields.Datetime.now()
        self.instance._finalize_removal(keep_in_panel=True)
        self.path = self.instance.custom_backup_dst
        self._dated(
            self.instance, create_date=self.now - timedelta(days=30),
            archived_at=self.now - timedelta(days=20),
        )
        self.successor = self.env["cloud.instance"].create({
            "name": "bd-inst",
            "project_id": self.env["cloud.project"].create({"name": "bd-proj-2"}).id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
        })
        self.born = self.now - timedelta(days=19)
        self._dated(self.successor, create_date=self.born)
        self.successor.with_context(ic_archive_instance=True).write({"active": False})
        self._saved_before(self.path, self.successor)
        self._dated(self.successor, archived_at=self.now - timedelta(days=5))

    def _purge_env(self, instance):
        """The environment of the purge of archived *instance*'s copy.

        :param instance: an archived ``cloud.instance``
        :return: ``{name: value}``
        :rtype: dict
        """
        job = self.env["cloud.job"].browse(instance.delete_archived()[0])
        executor = PurgeArchivedBackupsExecutor(job, self.host)
        executor._refuse_shared_prefix()
        (content,) = executor._env_contents().values()
        return _docker_env(content)

    def test_the_successor_s_purge_starts_at_its_creation(self):
        env = self._purge_env(self.successor)
        self.assertEqual(env["DST"], self.path)
        self.assertEqual(
            env["PURGE_SINCE"], self.born.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        )
        self.assertEqual(
            env["PURGE_BEFORE"], self.successor.purge_cutoff_at.isoformat(),
        )

    def test_the_predecessor_s_purge_has_no_start(self):
        env = self._purge_env(self.instance)
        self.assertNotIn("PURGE_SINCE", env)
        self.assertEqual(env["PURGE_BEFORE"], self.born.isoformat())
        self.assertEqual(self.instance._backup_purge_since(self.path), "")

    def test_without_a_namesake_in_its_path_the_line_is_not_there(self):
        self._saved_before("boto3+s3://bd-bucket/backups/elsewhere/bd-inst")
        self.assertNotIn("PURGE_SINCE", self._purge_env(self.successor))


@tagged("post_install", "-at_install")
class TestASuccessorGetsABackupPathOfItsOwn(_BackupDstCase):
    """An instance that takes another's name computes a path no record holds.

    ``-<id>`` after the name when another record already holds a path that
    ends as its own would (``_claim_own_backup_path``): its first copy is
    then a full of its own, and no purge of the other reaches it.
    """

    def setUp(self):
        super().setUp()
        self.instance._finalize_removal(keep_in_panel=True)
        self.archived_path = self.instance.custom_backup_dst
        # Its project's folder freed for the new one, as starting over does.
        self.project.remote_folder = "bd-proj-archived"
        self.again = self.env["cloud.project"].create({
            "name": "bd-proj-again", "remote_folder": "bd-proj",
        })

    def _create(self, name="bd-inst", **vals):
        """A staging of *name* in the project that took the folder."""
        return self.env["cloud.instance"].create({
            "name": name, "project_id": self.again.id,
            "environment": "staging", "host_id": self.host.id,
            "state": "draft", "backup_backend_id": self.backend.id,
        } | vals)

    def test_the_successor_s_path_is_its_own(self):
        successor = self._create()
        self.assertEqual(successor.backup_path_suffix, f"-{successor.id}")
        self.assertEqual(
            successor.instance_backup_dst, f"{self.archived_path}-{successor.id}",
        )
        self.assertIsNone(
            self.instance._backup_prefix_conflict(self.archived_path),
        )
        self.assertEqual(
            successor._backup_purge_since(successor.instance_backup_dst), "",
        )

    def test_a_name_nobody_holds_keeps_the_path_the_destination_computes(self):
        other = self._create("bd-new")
        self.assertFalse(other.backup_path_suffix)
        self.assertEqual(
            other.instance_backup_dst, "boto3+s3://bd-bucket/backups/bd-proj/bd-new",
        )

    def test_two_of_a_name_without_a_project_get_two_paths(self):
        # No host: the hostname it would give both is unique.
        first, second = self.env["cloud.instance"].create([{
            "name": "bd-free", "environment": "staging",
            "backup_backend_id": self.backend.id,
        }] * 2)
        self.assertFalse(first.backup_path_suffix)
        self.assertEqual(second.backup_path_suffix, f"-{second.id}")
        self.assertNotEqual(first.instance_backup_dst, second.instance_backup_dst)

    def test_a_rename_or_a_move_claims_it_and_a_form_s_save_does_not(self):
        other = self._create("bd-other")
        other.write({"name": "bd-inst"})
        self.assertEqual(other.backup_path_suffix, f"-{other.id}")
        other.write({"name": "bd-renamed"})
        self.assertFalse(other.backup_path_suffix)
        # A pair from before: the path shared, a save that sends the name
        # back moves nothing.
        old = self._create("bd-old")
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_instance SET name = %s WHERE id = %s",
            (json.dumps({"en_US": "bd-inst"}), old.id),
        )
        self.env.invalidate_all()
        self.assertEqual(old.instance_backup_dst, self.archived_path)
        old.write({"name": "bd-inst", "project_id": self.again.id})
        self.assertFalse(old.backup_path_suffix)
        self.assertEqual(old.instance_backup_dst, self.archived_path)

    def test_a_name_that_spells_another_s_suffixed_path_gets_its_own(self):
        successor = self._create()
        spelled = self._create(f"bd-inst-{successor.id}")
        self.assertEqual(spelled.backup_path_suffix, f"-{spelled.id}")
        self.assertNotEqual(spelled.instance_backup_dst, successor.instance_backup_dst)

    def test_an_instance_with_a_path_of_its_own_gets_no_suffix(self):
        own = self._create(custom_backup_dst="boto3+s3://bd-bucket/imported/bd-inst")
        self.assertFalse(own.backup_path_suffix)

    def test_emptying_its_own_path_claims_the_computed_one(self):
        own = self._create(custom_backup_dst="boto3+s3://bd-bucket/imported/bd-inst")
        own.write({"custom_backup_dst": False})
        self.assertEqual(own.backup_path_suffix, f"-{own.id}")
        self.assertEqual(
            own.instance_backup_dst, f"{self.archived_path}-{own.id}",
        )

    def test_emptying_the_path_of_a_suffixed_one_keeps_its_own(self):
        """Archived and revived, its path written is its own computed one.

        With nobody left at the path without the suffix (the archived one
        purged), a claim would take that path, away from its chain.
        """
        successor = self._create()
        path = successor.instance_backup_dst
        successor.sudo().custom_backup_dst = path
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_instance SET custom_backup_dst = %s WHERE id = %s",
            ("boto3+s3://bd-bucket/elsewhere/bd-inst", self.instance.id),
        )
        self.env.invalidate_all()
        successor.write({"custom_backup_dst": False})
        self.assertEqual(successor.backup_path_suffix, f"-{successor.id}")
        self.assertEqual(successor.instance_backup_dst, path)

    def test_moving_the_project_s_folder_claims_its_instances_paths(self):
        moved = self.env["cloud.project"].create({
            "name": "bd-proj-moved", "remote_folder": "bd-proj-moved",
        })
        inst = self._create(project_id=moved.id)
        self.assertFalse(inst.backup_path_suffix)
        self.again.remote_folder = "bd-proj-again"
        moved.remote_folder = "bd-proj"
        self.assertEqual(inst.backup_path_suffix, f"-{inst.id}")
        self.assertEqual(
            inst.instance_backup_dst, f"{self.archived_path}-{inst.id}",
        )

    def test_archiving_the_successor_freezes_its_own_path(self):
        successor = self._create()
        path = successor.instance_backup_dst
        successor._finalize_removal(keep_in_panel=True)
        self.assertEqual(successor.custom_backup_dst, path)
        self.assertNotEqual(path, self.archived_path)

    def test_nobody_but_the_platform_writes_it(self):
        successor = self._create()
        user = new_test_user(
            self.env, login="bd-suffix-admin",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        with self.assertRaises(AccessError) as caught:
            successor.with_user(user).write({"backup_path_suffix": False})
        self.assertIn("backup_path_suffix", str(caught.exception))
