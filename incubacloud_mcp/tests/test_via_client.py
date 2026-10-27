"""What an agent does carries its mark: audit rows and jobs."""

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user

from odoo.addons.incubacloud_mcp.protocol.context import VIA_CONTEXT_KEY

VIA = "https://claude.ai/oauth/claude-code-client-metadata (Claude Code)"


@tagged("post_install", "-at_install")
class TestViaClient(TransactionCase):
    """The ``via_client`` mark of audit rows and jobs.

    ``post_install``: it creates a user and a host, whose tables other
    modules may extend with required columns.
    """

    def setUp(self):
        """A host and a job type to queue jobs on."""
        super().setUp()
        self.host = self.env["cloud.host"].create(
            {
                "name": "via-host",
                "ip_address": "10.0.0.81",
                "user": "ubuntu",
                "wildcard_domain": "via.example.com",
            }
        )
        self.job_type = self.env["cloud.job.type"].create(
            {"name": "Via job", "code": "via_job", "apply_to": "host"}
        )

    def _job(self, **context):
        """Create a job as the platform, with *context*."""
        return (
            self.env["cloud.job"]
            .sudo()
            .with_context(**context)
            .create(
                {"name": "j", "host_id": self.host.id, "job_type_id": self.job_type.id}
            )
        )

    def test_an_audit_row_written_for_an_agent_says_which(self):
        """A row written in an agent's request carries it; the panel lists it."""
        Log = self.env["cloud.audit.log"].sudo()
        row = Log.with_context(**{VIA_CONTEXT_KEY: VIA}).create(
            {"action": "Config changed"}
        )
        self.assertEqual(row.via_client, VIA)
        self.assertEqual(row._format()[0]["via_client"], VIA)
        self.assertFalse(Log.create({"action": "Config changed"}).via_client)

    def test_a_forged_mark_is_dropped_on_create(self):
        """Outside an agent's request, a user cannot write the mark himself."""
        user = new_test_user(
            self.env,
            login="via-manager",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        row = (
            self.env["cloud.audit.log"]
            .with_user(user)
            .create({"action": "Config changed", "via_client": "forged"})
        )
        self.assertFalse(row.via_client)

    def test_nobody_but_the_platform_writes_the_mark_by_hand(self):
        """After a job exists, only the superuser writes its mark: not even
        a manager, who may write the rest of it."""
        user = new_test_user(
            self.env,
            login="via-job-manager",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        job = self._job().with_user(user)
        job.write({"name": "renamed"})
        with self.assertRaises(AccessError):
            job.write({"via_client": "forged"})

    def test_a_job_queued_for_an_agent_says_which(self):
        """A job created in an agent's request carries its mark."""
        self.assertEqual(self._job(**{VIA_CONTEXT_KEY: VIA}).via_client, VIA)
        self.assertFalse(self._job().via_client)
