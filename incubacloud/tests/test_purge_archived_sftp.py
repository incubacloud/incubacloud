"""Deleting an archived instance whose copy is on an SFTP server.

The job uploads the destination's key and trusted server key beside the
environment (no AWS keys in it), and only after checking that every
prefix is one instance's folder on the destination's own server: a
frozen path that names another server never gets the key.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import asyncssh

from odoo.exceptions import UserError

from odoo.addons.incubacloud.models import ssh_host_keys
from odoo.addons.incubacloud.models.purge_archived_executor import (
    SFTP_KEY_NAME,
    SFTP_KNOWN_HOSTS_NAME,
)

from .test_purge_archived import _ArchivedPurgeBase


class TestArchivedSftpPurge(_ArchivedPurgeBase):

    def setUp(self):
        super().setUp()
        self.sftp = self.env["cloud.backup.backend"].create({
            "name": "pa-sftp",
            "backend_type": "sftp",
            "sftp_host": "backup.example.com",
            "sftp_user": "u1",
            "sftp_path": "backups",
        })
        key = asyncssh.generate_private_key("ssh-ed25519").convert_to_public()
        self.sftp.sudo().write({
            "sftp_known_hosts": ssh_host_keys.known_hosts_line(
                "backup.example.com", 22, key,
            ),
        })
        self.instance.backup_backend_id = self.sftp
        self.instance.project_id.remote_folder = "proj"

    def _job_executor(self):
        inst = self._archive()
        job_id = inst.delete_archived()[0]
        return self._executor(self.env["cloud.job"].browse(job_id))

    def test_the_environment_carries_no_aws_keys(self):
        executor = self._job_executor()
        (content,) = executor._env_contents().values()
        self.assertEqual(content.splitlines()[0],
                         "DST=sftp://u1@backup.example.com/backups/proj/pa-inst")
        self.assertNotIn("AWS_", content)

    def test_the_key_files_go_beside_the_environment(self):
        executor = self._job_executor()
        uploaded = {}

        async def upload(_transport, files):
            uploaded.update(files)

        with patch.object(type(executor), "_upload_private_files",
                          AsyncMock(side_effect=upload)):
            asyncio.run(executor.before_execute(None))
        names = {path.rsplit("/", 1)[-1] for path in uploaded}
        self.assertEqual(names, {"purge.env", SFTP_KEY_NAME, SFTP_KNOWN_HOSTS_NAME})
        key_path = next(p for p in uploaded if p.endswith(SFTP_KEY_NAME))
        asyncssh.import_private_key(uploaded[key_path])

    def test_a_frozen_path_on_another_server_never_gets_the_key(self):
        inst = self._archive()
        inst.sudo().write({
            "custom_backup_dst": "sftp://u1@elsewhere.example.com/backups/proj/pa-inst",
        })
        job_id = inst.delete_archived()[0]
        executor = self._executor(self.env["cloud.job"].browse(job_id))
        upload = AsyncMock()
        with patch.object(type(executor), "_upload_private_files", upload):
            with self.assertRaises(UserError):
                asyncio.run(executor.before_execute(None))
        upload.assert_not_awaited()
