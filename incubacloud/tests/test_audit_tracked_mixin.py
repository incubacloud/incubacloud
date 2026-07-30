"""Tests for the shared 'Config changed' audit trail (audit tracked mixin).

The three private copies of this block had already diverged once: the
multi-record ``display_name`` fix existed on project only, while the
instance/host copies would break on an x2many with several records.
These tests pin the unified behavior on all three models.
"""
from odoo.tests.common import TransactionCase, tagged


# post_install: creating res.users at_install trips over the
# ``res_partner.autopost_bills`` NOT NULL column (account loads later)
# — same constraint as test_alert_notify.
@tagged("post_install", "-at_install")
class TestAuditTrackedMixin(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Audit Proj"})
        self.audit = self.env["cloud.audit.log"].sudo()

    def _last_entry(self, **domain_kv):
        domain = [("action", "=", "Config changed")]
        domain += [(k, "=", v) for k, v in domain_kv.items()]
        return self.audit.search(domain, order="id desc", limit=1)

    def test_project_multi_member_change_does_not_crash(self):
        """The original divergence bug: two members in an x2many diff.

        ``display_name`` raises on a multi-record set; the shared
        ``_audit_display`` must join names instead, on every model.
        """
        users = self.env["res.users"]
        u1 = users.create({"name": "Aud One", "login": "aud1@example.com"})
        u2 = users.create({"name": "Aud Two", "login": "aud2@example.com"})
        self.project.write({"member_ids": [(6, 0, [u1.id, u2.id])]})
        entry = self._last_entry(project_id=self.project.id)
        self.assertTrue(entry)
        self.assertIn("Aud One", entry.details)
        self.assertIn("Aud Two", entry.details)

    def test_host_change_logs_against_the_host(self):
        host = self.env["cloud.host"].create(
            {
                "name": "audit-host",
                "ip_address": "192.0.2.10",
                "user": "ubuntu",
                "wildcard_domain": "audit.example.com",
            }
        )
        host.write({"port": 2222})
        entry = self._last_entry(host_id=host.id)
        self.assertTrue(entry)
        self.assertIn("2222", entry.details)

    def test_instance_change_logs_against_the_instance(self):
        inst = self.env["cloud.instance"].create(
            {
                "name": "auditinst",
                "project_id": self.project.id,
                "environment": "staging",
            }
        )
        inst.write({"odoo_version": "18.0"})
        entry = self._last_entry(instance_id=inst.id)
        self.assertTrue(entry)
        self.assertIn("18.0", entry.details)

    def test_untracked_field_writes_no_entry(self):
        before = self.audit.search_count([("action", "=", "Config changed")])
        self.project.write({"odoo_commit_sha": "0" * 40})
        after = self.audit.search_count([("action", "=", "Config changed")])
        self.assertEqual(before, after)
