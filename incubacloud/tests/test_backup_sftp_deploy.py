"""What a deploy or a rebuild ships for an instance backing up to SFTP.

``backup.env`` without AWS keys and with a weekly full; the key, the
trusted server key and ``~/.ssh/config`` as files of their own (0600, in a
0700 directory, mounted read-only over the cache volume); nothing at all
when the server key is not confirmed or the backup path is not on the
destination's server. And when the instance stops backing up to SFTP, the
directory goes.
"""
import json

import asyncssh
import yaml

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models import sftp_dst, ssh_host_keys
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)


class _SftpDeployCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id", "0",
        )
        self.project = self.env["cloud.project"].create({
            "name": "SftpDeploy", "remote_folder": "proj",
        })
        self.host = self.env["cloud.host"].create({
            "name": "sftp-deploy-host",
            "ip_address": "192.0.2.93",
            "user": "ubuntu",
            "wildcard_domain": "sftp93.example.com",
        })
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "sftp-deploy",
            "backend_type": "sftp",
            "sftp_host": "backup.example.com",
            "sftp_port": 23,
            "sftp_user": "u1",
            "sftp_path": "backups",
            "backup_retention": "14D",
        })
        server_key = asyncssh.generate_private_key("ssh-ed25519")
        self.known_hosts = ssh_host_keys.known_hosts_line(
            "backup.example.com", 23, server_key,
        )
        self.instance = self.env["cloud.instance"].create({
            "name": "sftpinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
            "backup_backend_id": self.backend.id,
        })

    def _trust(self):
        self.backend.sudo().write({"sftp_known_hosts": self.known_hosts})

    def _executor(self, executor_cls=DeployInstanceExecutor, code="deploy_instance"):
        job_type = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        job = self.env["cloud.job"].create({
            "name": f"sftp {code}",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": job_type.id,
        })
        executor = executor_cls(job, self.host)
        executor._host_cpus = 0
        return executor

    def _private_marker(self):
        """A line of the private key that only the key itself holds."""
        return self.backend.sudo().sftp_private_key.splitlines()[1]


class TestSftpBackupEnvAndAnswers(_SftpDeployCase):

    def test_backup_env_has_no_aws_keys_and_a_weekly_full(self):
        self._trust()
        content = self._executor()._backup_env_content()
        self.assertNotIn("AWS_", content)
        self.assertIn(
            "OPTIONS_EXTRA=--metadata-sync-mode partial --full-if-older-than 1W\n",
            content,
        )
        self.assertNotIn("--file-prefix", content)
        self.assertIn("PASSPHRASE=", content)
        self.assertIn("remove-older-than 14D", content)
        self.assertNotIn(self._private_marker(), content)

    def test_answers_carry_the_sftp_path_and_no_aws_keys(self):
        answers = self.instance._render_copier_answers()
        self.assertEqual(
            answers["backup_dst"], "sftp://u1@backup.example.com:23/backups/proj/sftpinst",
        )
        self.assertEqual(answers["backup_aws_access_key_id"], "")
        self.assertEqual(answers["backup_aws_secret_access_key"], "")
        self.assertNotIn(self._private_marker(), json.dumps(answers, default=str))

    def test_s3_answers_unchanged(self):
        s3 = self.env["cloud.backup.backend"].create({
            "name": "s3", "s3_bucket": "b", "s3_access_key_id": "AK",
            "s3_secret_access_key": "SK",
        })
        self.instance.backup_backend_id = s3
        answers = self.instance._render_copier_answers()
        self.assertEqual(answers["backup_aws_access_key_id"], "AK")
        self.assertEqual(answers["backup_aws_secret_access_key"], "SK")


class TestSftpKeyFiles(_SftpDeployCase):

    def test_no_build_without_a_confirmed_server_key(self):
        with self.assertRaisesRegex(UserError, "Confirm the SFTP server's key"):
            self.instance._extra_env_files()

    def test_the_three_files(self):
        self._trust()
        files = self.instance._extra_env_files()
        self.assertEqual(set(files), {
            sftp_dst.SSH_KEY_FILE, sftp_dst.SSH_KNOWN_HOSTS_FILE,
            sftp_dst.SSH_CONFIG_FILE,
        })
        asyncssh.import_private_key(files[sftp_dst.SSH_KEY_FILE])
        self.assertTrue(
            files[sftp_dst.SSH_KNOWN_HOSTS_FILE].startswith("[backup.example.com]:23 "),
        )
        self.assertIn("IdentityFile /root/.ssh/id_ed25519", files[sftp_dst.SSH_CONFIG_FILE])

    def test_a_path_on_another_server_is_refused(self):
        self._trust()
        self.instance.custom_backup_dst = "sftp://u1@other.example.com:23/backups/p/i"
        with self.assertRaisesRegex(UserError, "not on the server"):
            self.instance._extra_env_files()

    def test_a_path_shared_with_other_instances_is_refused(self):
        self._trust()
        self.instance.custom_backup_dst = "sftp://u1@backup.example.com:23/backups"
        with self.assertRaisesRegex(UserError, "folder of its own"):
            self.instance._extra_env_files()

    def test_an_s3_instance_ships_none(self):
        s3 = self.env["cloud.backup.backend"].create({"name": "s3", "s3_bucket": "b"})
        self.instance.backup_backend_id = s3
        self.assertEqual(self.instance._extra_env_files(), {})


class TestSftpBuildSteps(_SftpDeployCase):

    def test_deploy_and_rebuild_ready_the_directory_and_move_the_files(self):
        self._trust()
        for cls, code in (
            (DeployInstanceExecutor, "deploy_instance"),
            (RebuildInstanceExecutor, "rebuild_instance"),
        ):
            with self.subTest(code=code):
                executor = self._executor(cls, code)
                d = "~/projects/x"
                steps = executor._write_extra_env_step(d)
                labels = [s[0] for s in steps]
                self.assertEqual(
                    labels, ["Ready the backup SSH directory", "Write extra env files"],
                )
                self.assertEqual(
                    steps[0][1],
                    f"mkdir -p {d}/.docker/backup-ssh && chmod 700 {d}/.docker/backup-ssh",
                )
                self.assertIn(f"chmod 600 {d}/.docker/backup-ssh/id_ed25519", steps[1][1])
                self.assertNotIn(self._private_marker(), json.dumps(steps))

    def test_without_sftp_the_directory_goes(self):
        s3 = self.env["cloud.backup.backend"].create({"name": "s3", "s3_bucket": "b"})
        self.instance.backup_backend_id = s3
        steps = self._executor()._write_extra_env_step("~/projects/x")
        self.assertEqual(
            steps, (("Remove the backup SSH directory",
                     "rm -rf ~/projects/x/.docker/backup-ssh"),),
        )

    def test_the_backup_service_mounts_it_read_only(self):
        self._trust()
        data = yaml.safe_load(self._executor()._resource_override_content())
        self.assertIn(sftp_dst.SSH_MOUNT, data["services"]["backup"]["volumes"])
        self.assertNotIn(self._private_marker(), json.dumps(data))

    def test_an_s3_instance_mounts_nothing(self):
        s3 = self.env["cloud.backup.backend"].create({"name": "s3", "s3_bucket": "b"})
        self.instance.backup_backend_id = s3
        data = yaml.safe_load(self._executor()._resource_override_content())
        self.assertNotIn(
            sftp_dst.SSH_MOUNT,
            data["services"].get("backup", {}).get("volumes", []),
        )
