"""Tests for the chain/archive split on cloud.instance.backup.

Two semantics used to share the model by convention only; the ``kind``
field plus its constraints make the split structural, and the
``backup_list`` sync must never prune archive rows (they are absent
from the duplicity listing by definition).
"""
from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.backup_list_executor import (
    BackupListExecutor,
)


class TestBackupKind(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Kind Proj"})
        self.host = self.env["cloud.host"].create(
            {
                "name": "kind-host",
                "ip_address": "192.0.2.30",
                "user": "ubuntu",
                "wildcard_domain": "kind.example.com",
            }
        )
        self.instance = self.env["cloud.instance"].create(
            {
                "name": "kindinst",
                "project_id": self.project.id,
                "environment": "production",
                "host_id": self.host.id,
            }
        )
        self.Backup = self.env["cloud.instance.backup"]

    def _row(self, **kw):
        return self.Backup.create(
            {
                "instance_id": self.instance.id,
                "backup_type": "Full",
                "backup_time": fields.Datetime.now(),
            }
            | kw
        )

    def test_chain_row_refuses_an_attachment(self):
        att = self.env["ir.attachment"].create(
            {"name": "z.zip", "type": "binary", "datas": "eA=="}
        )
        with self.assertRaises(ValidationError):
            self._row(kind="chain", attachment_id=att.id)

    def test_archive_row_refuses_a_chain_start(self):
        with self.assertRaises(ValidationError):
            self._row(kind="archive", chain_start=fields.Datetime.now())

    def test_archive_row_survives_attachment_expiry(self):
        att = self.env["ir.attachment"].create(
            {"name": "z.zip", "type": "binary", "datas": "eA=="}
        )
        row = self._row(kind="archive", attachment_id=att.id)
        att.unlink()
        self.assertEqual(row.kind, "archive")
        self.assertFalse(row.attachment_id)

    def test_sync_prunes_stale_chains_but_never_archives(self):
        """The stale-pruning of the duplicity sync is chain-scoped."""
        archive = self._row(kind="archive")
        stale_chain = self._row(
            kind="chain", chain_start=fields.Datetime.now()
        )
        job = self.env["cloud.job"].create(
            {
                "job_type_id": self.env.ref("incubacloud.backup_list").id,
                "instance_id": self.instance.id,
                "host_id": self.host.id,
            }
        )
        executor = BackupListExecutor(job, self.host)
        executor._sync_backup_records({"chains": []})
        self.assertFalse(stale_chain.exists())
        self.assertTrue(archive.exists())
