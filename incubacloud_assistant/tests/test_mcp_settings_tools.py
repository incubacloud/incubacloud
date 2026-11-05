"""Settings → Assistant through agents' tools: a manager's, each change asked."""

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.incubacloud_mcp.tests.common import ConfirmMixin, McpHttpMixin

from .common import KEY, MODELS, AssistantMixin


@tagged("post_install", "-at_install")
class TestAssistantSettingsTools(AssistantMixin, McpHttpMixin, ConfirmMixin, HttpCase):
    """What an agent reads and changes of the assistant, and who may."""

    def setUp(self):
        """The assistant on; a manager's agent and a developer's."""
        super().setUp()
        self._resource_here()
        self.manager = self._cloud_user("asis-mcp-mgr", group="group_cloud_manager")
        self.dev = self._cloud_user("asis-mcp-dev")
        self._configure(users=[self.dev])
        self.key = self._api_key(self.manager)

    def test_the_settings_are_read_without_the_key(self):
        """has_api_key says it is set; the key itself never shows."""
        result = self._call_tool(self.key, "get_assistant_settings")
        self.assertFalse(result["isError"], result)
        payload = self._payload(result)
        self.assertTrue(payload["has_api_key"])
        self.assertIn("usage", payload)
        self.assertNotIn(KEY, str(result))

    def test_a_change_is_asked_from_what_to_what_and_saved(self):
        """The monthly limit changes once the manager approves it."""
        first = self._call_tool(
            self.key,
            "update_assistant_settings",
            {"values": {"monthly_limit": 25}},
            {"elicitation": {"form": {}}},
        )
        self.assertEqual(first.get("resultType"), "input_required", first)
        self.assertIn("monthly_limit", str(first))
        result = self._confirmed_call(
            self.key, "update_assistant_settings", {"values": {"monthly_limit": 25}}
        )
        self.assertFalse(result["isError"], result)
        self.assertEqual(
            self.env["cloud.settings"]._assistant_config().monthly_limit, 25
        )

    def test_switching_it_on_and_its_key_stay_in_the_panel(self):
        """enabled=true is refused; api_key and provider are no values."""
        self.env["cloud.settings"]._assistant_settings().assistant_enabled = False
        result = self._call_tool(
            self.key, "update_assistant_settings", {"values": {"enabled": True}}
        )
        self.assertTrue(result["isError"])
        self.assertIn("in the panel", result["content"][0]["text"])
        for values in ({"api_key": "sk-new"}, {"provider": "openai"}):
            result = self._call_tool(
                self.key, "update_assistant_settings", {"values": values}
            )
            self.assertTrue(result["isError"], values)
        self.assertFalse(self.env["cloud.settings"]._assistant_config().enabled)

    def test_below_a_manager_the_tools_are_neither_listed_nor_run(self):
        """A developer's agent is refused both."""
        key = self._api_key(self.dev)
        self._assert_refused_by_role(key, "get_assistant_settings", {})
        self._assert_refused_by_role(
            key, "update_assistant_settings", {"values": {"allowed_models": [MODELS[0]]}}
        )
