"""How a tool presents itself in tools/list, and who a call came from."""

import dataclasses

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.protocol.context import CallContext, Principal, Reply
from odoo.addons.incubacloud_mcp.protocol.tool_spec import (
    LIMIT,
    OFFSET,
    ToolSpec,
    boolean,
    integer,
    obj,
    record_id,
    string,
)


class TestToolSpec(BaseCase):
    """How a tool presents itself in tools/list."""

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
        """The listing names the tool, its minimum role and its schema."""
        listed = self._spec().listed()
        self.assertEqual(listed["name"], "delete_instance")
        self.assertIn("Minimum role: consultant.", listed["description"])
        self.assertEqual(listed["inputSchema"]["required"], ["instance_id"])
        self.assertFalse(listed["inputSchema"]["additionalProperties"])

    def test_a_destructive_tool_says_so_and_that_it_asks(self):
        """A destructive tool says so, that it asks, and is hinted so."""
        listed = self._spec(destructive=True, confirm="always").listed()
        self.assertIn("Destructive.", listed["description"])
        self.assertIn("asks the user to approve it", listed["description"])
        self.assertEqual(
            listed["annotations"],
            {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
        )

    def test_a_read_tool_is_hinted_read_only_and_untrusted_output_is_announced(self):
        """A read tool is hinted read-only; untrusted output is announced."""
        listed = self._spec(read_only=True, untrusted=True).listed()
        self.assertTrue(listed["annotations"]["readOnlyHint"])
        self.assertFalse(listed["annotations"]["destructiveHint"])
        self.assertIn("untrusted data", listed["description"])

    def test_the_stakeholder_role_is_named_as_the_panel_names_it(self):
        """group_cloud_user is called stakeholder, as in the panel."""
        self.assertIn(
            "Minimum role: stakeholder.",
            self._spec(min_group="group_cloud_user").listed()["description"],
        )

    def test_schema_helpers(self):
        """The schema helpers build what they say."""
        self.assertEqual(
            string("x", max_length=3),
            {"type": "string", "description": "x", "maxLength": 3},
        )
        self.assertEqual(boolean("y"), {"type": "boolean", "description": "y"})
        self.assertNotIn("required", obj({}))

    def test_a_record_id_fits_a_postgres_integer(self):
        """A record id is capped at PostgreSQL's integer."""
        self.assertEqual(record_id("r")["maximum"], 2**31 - 1)

    def test_a_listing_is_a_copy_of_the_schema(self):
        """Changing a listing changes neither the spec nor OFFSET and LIMIT."""
        spec = self._spec(input_schema=obj({"offset": OFFSET, "limit": LIMIT}))
        listed = spec.listed()
        listed["inputSchema"]["properties"]["offset"]["minimum"] = 5
        listed["inputSchema"]["properties"]["limit"]["maximum"] = 5
        self.assertEqual((OFFSET["minimum"], LIMIT["maximum"]), (0, 1000))
        self.assertEqual(spec.input_schema["properties"]["offset"]["minimum"], 0)

    def test_a_destructive_tool_cannot_skip_the_approval(self):
        """A destructive tool that would run without approval is not declared."""
        with self.assertRaises(ValueError):
            self._spec(destructive=True)
        with self.assertRaises(ValueError):
            self._spec(confirm="sometimes")

    def test_a_spec_is_immutable(self):
        """A spec cannot be changed once declared."""
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self._spec().name = "x"


class TestCallContext(BaseCase):
    """Who a call came from, as the audit records it."""

    def test_the_audit_names_the_verified_credential_and_the_declared_client(self):
        """The verified credential first, then the client's own name."""
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

    def test_a_declared_name_cannot_close_the_brackets(self):
        """A client cannot pass itself off as another credential by its name."""
        from odoo.addons.incubacloud_mcp.models.mcp_server import _client_name

        name = _client_name({"name": "Claude) (API key admin"})
        self.assertNotIn(")", name)
        self.assertNotIn("(", name)
        self.assertEqual(name, "Claude API key admin")

    def test_a_reply_defaults_to_200_without_headers(self):
        """A reply defaults to 200, no body, no headers."""
        self.assertEqual(
            (Reply().status, Reply().body, Reply().headers), (200, None, {})
        )
