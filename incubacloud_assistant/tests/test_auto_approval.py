"""The panel's assistant with an auto-approval rule: no card, and the chat says so.

A rule of the credential ``assistant`` for a person lets their assistant run
what would show a card — the MCP's approvals, the assistant's own rules and
the mark of content others can write — and nothing else does: neither a
rule of one of the person's keys, nor anyone else's (issue #8).
"""

import dataclasses
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud_mcp.models.mcp_auto_approval import NOTICE_VERSION
from odoo.addons.incubacloud_mcp.protocol.context import Principal
from odoo.addons.incubacloud_mcp.tests.common import AutoApprovalAllowedMixin
from odoo.addons.queue_job.tests.common import trap_jobs

from ..models.assistant_prompt import AUTO_APPROVAL_CHARGES, AUTO_APPROVAL_RULE
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

    def _grant(self, credential=ASSISTANT_CREDENTIAL, include_charges=False):
        """Grant the manager's *credential* a rule for a week, with charges
        when *include_charges*."""
        return (
            self.env["incubacloud.mcp.auto.approval"]
            .with_user(self.admin)
            ._grant(
                self.manager.id,
                credential,
                days=7,
                acknowledged=True,
                notice_version=NOTICE_VERSION,
                include_charges=include_charges,
            )
        )

    def _charging(self, name="delete_project"):
        """Declare the tool *name* as one that charges money, for the test."""
        Catalog = type(self.env["incubacloud.mcp.catalog"])
        original = Catalog._tool_specs

        def specs(catalog):
            """Every spec, *name*'s marked as charging."""
            return [
                dataclasses.replace(spec, charges=True) if spec.name == name else spec
                for spec in original(catalog)
            ]

        return patch.object(Catalog, "_tool_specs", specs)

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
        key = (
            self.env["res.users.apikeys"]
            .with_user(self.manager)
            .sudo()
            ._generate("rpc", "k", fields.Datetime.now() + timedelta(days=1))
        )
        _user, _name, key_id = self.env["incubacloud.mcp.auth"]._user_for_api_key(key)
        credential = f"API key #{key_id}"
        with self.enter_registry_test_mode():
            self.env["incubacloud.mcp.credential"]._seen(
                Principal(
                    uid=self.manager.id, credential="API key k", client=credential
                ),
                "test-agent",
            )
        self._grant(credential)
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

    def test_a_rule_without_charges_shows_a_card_for_a_tool_that_charges(self):
        """By default the assistant's rule leaves out charges: a tool that
        charges shows its card, runs nothing, and the rule counts no use."""
        rule = self._grant()
        conversation = self._working(self.manager)
        with self._charging(), trap_jobs():
            self._delete_empty(conversation)
        self.assertTrue(self.empty.exists())
        card = conversation.message_ids.filtered(lambda m: m.role == "card")
        self.assertEqual(card.card_state, "pending")
        rule.invalidate_recordset()
        self.assertEqual(rule.use_count, 0)

    def test_a_rule_with_charges_runs_a_tool_that_charges_without_a_card(self):
        """With charges included, the tool that charges runs, marked auto."""
        rule = self._grant(include_charges=True)
        conversation = self._working(self.manager)
        with self._charging(), trap_jobs():
            self._delete_empty(conversation)
        self.assertFalse(self.empty.exists())
        self.assertFalse(conversation.message_ids.filtered(lambda m: m.role == "card"))
        audit = (
            self.env["cloud.audit.log"]
            .sudo()
            .search([("action", "=", "Agent: delete_project")], limit=1)
        )
        self.assertEqual((audit.approval, audit.approval_rule_id), ("auto", rule))

    def test_a_rule_with_charges_runs_any_other_tool_too(self):
        """Including charges takes nothing away: a tool that does not charge
        runs without a card."""
        self._grant(include_charges=True)
        conversation = self._working(self.manager)
        with trap_jobs():
            self._delete_empty(conversation)
        self.assertFalse(self.empty.exists())
        self.assertFalse(conversation.message_ids.filtered(lambda m: m.role == "card"))

    def test_the_prompt_and_the_tools_say_charges_still_show_a_card(self):
        """Under a rule without charges, the model's rules say actions that
        charge still show a card, and so does the tool's description; with
        charges, neither does."""
        conversation = self._working(self.manager)
        Engine = self.env["incubacloud.assistant.engine"].with_user(self.manager)
        rule = self._grant()
        with self._charging():
            request = Engine._request(conversation.with_user(self.manager), "m")
            self.assertIn(AUTO_APPROVAL_CHARGES, request.system)
            described = {tool.name: tool.description for tool in request.tools}
            self.assertNotIn("without asking", described["delete_project"])
            self.assertIn("does not include charges", described["delete_project"])
            rule._revoke("manual")
            self._grant(include_charges=True)
            request = Engine._request(conversation.with_user(self.manager), "m")
            self.assertNotIn(AUTO_APPROVAL_CHARGES, request.system)
            described = {tool.name: tool.description for tool in request.tools}
            self.assertIn("without asking", described["delete_project"])

    def test_the_assistant_cannot_grant_a_rule(self):
        """The grant route, reached from a tool's request, refuses it."""
        Dispatch = self.env["incubacloud.mcp.dispatch"].with_user(self.admin)
        with tool_request(Dispatch.env, "Assistant (conversation 1)"):
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

    def test_uninstalling_the_assistant_revokes_its_rules_and_keeps_them(self):
        """What the uninstall does with the rules of kind ``assistant``: the
        live one is revoked, as a deleted credential's, and stays as history
        with its audit rows."""
        rule = self._grant()
        selection = self.env["ir.model.fields.selection"].search(
            [
                ("field_id.model", "=", "incubacloud.mcp.auto.approval"),
                ("field_id.name", "=", "kind"),
                ("value", "=", "assistant"),
            ]
        )
        self.assertEqual(len(selection), 1)
        selection._process_ondelete()
        self.assertTrue(rule.exists())
        self.assertEqual(rule.revoked_reason, "credential deleted")
        actions = (
            self.env["cloud.audit.log"]
            .search([("approval_rule_id", "=", rule.id)])
            .mapped("action")
        )
        self.assertIn("Auto-approval rule revoked", actions)
