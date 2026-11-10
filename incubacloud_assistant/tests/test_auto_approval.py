"""The panel's assistant with an auto-approval rule: no card, and the chat says so.

A rule of the credential ``assistant`` for a person lets their assistant run
what would show a card — the MCP's approvals, the assistant's own rules and
the mark of content others can write — and nothing else does: neither a
rule of one of the person's keys, nor anyone else's (issue #8).
"""

from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud_mcp.models.mcp_auto_approval import NOTICE_VERSION
from odoo.addons.incubacloud_mcp.protocol.context import Principal
from odoo.addons.incubacloud_mcp.tests.common import AutoApprovalAllowedMixin
from odoo.addons.queue_job.tests.common import trap_jobs

from ..models.assistant_prompt import AUTO_APPROVAL_RULE
from ..models.mcp_auto_approval import ASSISTANT_CREDENTIAL
from ..request_scope import tool_request
from .common import EngineMixin, answer


@tagged("post_install", "-at_install")
class TestAssistantAutoApproval(AutoApprovalAllowedMixin, EngineMixin, TransactionCase):
    """What a rule of the assistant lets through, and what it does not."""

    def setUp(self):
        """A manager who uses the assistant, an Administrator, a project to delete."""
        super().setUp()
        self._panel_fixture()
        self.manager = self._cloud_user("asis-aa-mgr", group="group_cloud_manager")
        self.admin = self._cloud_user("asis-aa-admin", group="group_cloud_manager")
        self._configure(users=[self.manager])
        self.empty = self.env["cloud.project"].create(
            {"name": "Asis AA Empty", "remote_folder": "asisaaempty"}
        )

    def _grant(self, credential=ASSISTANT_CREDENTIAL):
        """Grant the manager's *credential* a rule for a week."""
        return (
            self.env["incubacloud.mcp.auto.approval"]
            .with_user(self.admin)
            ._grant(
                self.manager.id,
                credential,
                days=7,
                acknowledged=True,
                notice_version=NOTICE_VERSION,
            )
        )

    def _delete_empty(self, conversation):
        """Make the model delete the empty project in the next step."""
        self._provider(
            answer("", [("c1", "delete_project", {"project_id": self.empty.id})]),
            answer("Deleted."),
        )
        self._step(conversation)

    def test_the_assistant_is_offered_to_a_person_who_uses_it(self):
        """The candidates of a user of the assistant include it."""
        kinds = [
            item["kind"]
            for item in self.env["incubacloud.mcp.auto.approval"]._candidates_for(
                self.manager
            )
        ]
        self.assertIn("assistant", kinds)
        self.assertNotIn(
            "assistant",
            [
                item["kind"]
                for item in self.env["incubacloud.mcp.auto.approval"]._candidates_for(
                    self.admin
                )
            ],
        )

    def test_with_a_rule_there_is_no_card_and_the_chat_says_so(self):
        """The deletion runs; its line says it ran without asking; audited auto."""
        rule = self._grant()
        self.assertEqual(rule.kind, "assistant")
        conversation = self._working(self.manager)
        with trap_jobs():
            self._delete_empty(conversation)
        self.assertFalse(self.empty.exists())
        self.assertFalse(conversation.message_ids.filtered(lambda m: m.role == "card"))
        line = conversation.message_ids.filtered(
            lambda m: m.tool_name == "delete_project"
        )
        self.assertTrue(line.auto_approved)
        self.assertIn("ran without asking", line._format()["label"])
        audit = (
            self.env["cloud.audit.log"]
            .sudo()
            .search([("action", "=", "Agent: delete_project")], limit=1)
        )
        self.assertEqual((audit.approval, audit.approval_rule_id), ("auto", rule))

    def test_after_reading_a_log_it_still_runs_and_the_line_says_so(self):
        """The mark stays; it no longer makes a card; the line names it."""
        self._grant()
        Tools = type(self.env["incubacloud.mcp.tools"])
        self._provider(
            answer("", [("c1", "read_logs", {"instance_id": self.staging.id})]),
            answer("", [("c2", "delete_project", {"project_id": self.empty.id})]),
            answer("Done."),
        )
        conversation = self._working(self.manager, "What do the logs say?")
        with (
            patch.object(
                Tools,
                "_tool_read_logs",
                autospec=True,
                return_value={"ok": True, "lines": ["ERROR boom"]},
            ),
            trap_jobs(),
        ):
            self._step(conversation)
            self.assertTrue(conversation.tainted)
            self._step(conversation)
        self.assertFalse(self.empty.exists())
        self.assertTrue(conversation.tainted)
        line = conversation.message_ids.filtered(
            lambda m: m.tool_name == "delete_project"
        )
        self.assertTrue(line.auto_after_untrusted)
        self.assertIn("other people can write", line._format()["label"])

    def test_revoked_mid_task_the_next_change_is_a_card_with_the_notice(self):
        """The mark was kept: the card says the task read content others write."""
        rule = self._grant()
        Tools = type(self.env["incubacloud.mcp.tools"])
        self._provider(
            answer("", [("c1", "read_logs", {"instance_id": self.staging.id})]),
            answer("", [("c2", "delete_project", {"project_id": self.empty.id})]),
        )
        conversation = self._working(self.manager, "What do the logs say?")
        with (
            patch.object(
                Tools,
                "_tool_read_logs",
                autospec=True,
                return_value={"ok": True, "lines": ["ERROR boom"]},
            ),
            trap_jobs(),
        ):
            self._step(conversation)
            rule.with_user(self.manager)._revoke_by_person()
            self._step(conversation)
        self.assertTrue(self.empty.exists())
        card = conversation.message_ids.filtered(lambda m: m.role == "card")
        self.assertIn("read content", card.content)

    def test_a_rule_of_a_key_does_not_cover_the_assistant(self):
        """The person's API key has a rule; their assistant still shows a card."""
        with self.enter_registry_test_mode():
            self.env["incubacloud.mcp.credential"]._seen(
                Principal(
                    uid=self.manager.id, credential="API key k", client="API key #77"
                ),
                "test-agent",
            )
        self._grant("API key #77")
        conversation = self._working(self.manager)
        self._delete_empty(conversation)
        self.assertTrue(self.empty.exists())
        card = conversation.message_ids.filtered(lambda m: m.role == "card")
        self.assertEqual(card.card_state, "pending")

    def test_the_prompt_and_the_tools_say_there_is_no_card(self):
        """With a rule, the rules of the model and the tools' descriptions say so."""
        conversation = self._working(self.manager)
        Engine = self.env["incubacloud.assistant.engine"].with_user(self.manager)
        request = Engine._request(conversation.with_user(self.manager), "m")
        self.assertNotIn(AUTO_APPROVAL_RULE, request.system)
        self._grant()
        request = Engine._request(conversation.with_user(self.manager), "m")
        self.assertIn(AUTO_APPROVAL_RULE, request.system)
        described = {tool.name: tool.description for tool in request.tools}
        self.assertIn("without asking", described["delete_project"])

    def test_the_assistant_cannot_grant_a_rule(self):
        """The grant route, reached from a tool's request, refuses it."""
        Dispatch = self.env["incubacloud.mcp.dispatch"].with_user(self.admin)
        with (
            patch.object(self, "http_request_allow_all", True),
            tool_request(Dispatch.env, "Assistant (conversation 1)"),
        ):
            with self.assertRaises(AccessError):
                Dispatch._call(
                    "/cloud/mcp/auto_approval/grant",
                    {
                        "user_id": self.manager.id,
                        "credential": ASSISTANT_CREDENTIAL,
                        "days": 7,
                        "acknowledged": True,
                        "notice_version": NOTICE_VERSION,
                    },
                )
        self.assertFalse(
            self.env["incubacloud.mcp.auto.approval"].search(
                [("user_id", "=", self.manager.id)]
            )
        )
