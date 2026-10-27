"""How a tool presents itself in tools/list, and who a call came from."""

import dataclasses

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.protocol.context import CallContext, Principal, Reply
from odoo.addons.incubacloud_mcp.protocol.tool_spec import (
    ToolSpec,
    boolean,
    integer,
    obj,
    string,
)


class TestToolSpec(BaseCase):
    def _spec(self, **kw):
        """A tool spec with defaults, *kw* applied."""
        values = {
            "name": "delete_instance",
            "description": "Delete an instance.",
            "min_group": "group_cloud_consultant",
            "input_schema": obj(
                {"instance_id": integer("The instance.")}, required=("instance_id",)
            ),
            "route": "/cloud/delete_instance",
        }
        values.update(kw)
        return ToolSpec(**values)

    def test_the_listing_says_the_minimum_role(self):
        listed = self._spec().listed()
        self.assertEqual(listed["name"], "delete_instance")
        self.assertIn("Minimum role: consultant.", listed["description"])
        self.assertEqual(listed["inputSchema"]["required"], ["instance_id"])
        self.assertFalse(listed["inputSchema"]["additionalProperties"])

    def test_a_destructive_tool_says_so_and_that_it_asks(self):
        listed = self._spec(destructive=True, confirm="always").listed()
        self.assertIn("Destructive.", listed["description"])
        self.assertIn("asks the user to approve it", listed["description"])
        self.assertEqual(
            listed["annotations"],
            {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
        )

    def test_a_read_tool_is_hinted_read_only_and_untrusted_output_is_announced(self):
        listed = self._spec(read_only=True, untrusted=True).listed()
        self.assertTrue(listed["annotations"]["readOnlyHint"])
        self.assertFalse(listed["annotations"]["destructiveHint"])
        self.assertIn("untrusted data", listed["description"])

    def test_the_stakeholder_role_is_named_as_the_panel_names_it(self):
        self.assertIn(
            "Minimum role: stakeholder.",
            self._spec(min_group="group_cloud_user").listed()["description"],
        )

    def test_schema_helpers(self):
        self.assertEqual(
            string("x", max_length=3),
            {"type": "string", "description": "x", "maxLength": 3},
        )
        self.assertEqual(boolean("y"), {"type": "boolean", "description": "y"})
        self.assertNotIn("required", obj({}))

    def test_a_spec_is_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self._spec().name = "x"


class TestCallContext(BaseCase):
    def test_the_audit_names_the_verified_credential_and_the_declared_client(self):
        ctx = CallContext(
            "2026-07-28",
            True,
            {},
            "Claude Code",
            Principal(7, "https://claude.ai/cimd"),
        )
        self.assertEqual(ctx.via(), "https://claude.ai/cimd (Claude Code)")
        self.assertEqual(
            CallContext("2026-07-28", True, {}, "", Principal(7, "API key ci")).via(),
            "API key ci",
        )

    def test_a_reply_defaults_to_200_without_headers(self):
        self.assertEqual(
            (Reply().status, Reply().body, Reply().headers), (200, None, {})
        )
