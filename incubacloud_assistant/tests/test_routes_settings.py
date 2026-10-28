"""Settings → Assistant: managers only, the key checked and never shown back."""

import json
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from ..connectors.anthropic_connector import AnthropicConnector
from ..connectors.base import ModelInfo, ProviderError, Usage
from ..controllers import assistant_settings
from ..models.cloud_settings import BLOCKED_PARAM
from .common import KEY, MODELS, AssistantMixin

NEW_KEY = "sk-ant-new-settings-key-0009"


@tagged("post_install", "-at_install")
class TestSettingsRoutes(AssistantMixin, HttpCase):
    """What a manager reads and saves; what anybody else gets."""

    def setUp(self):
        """A manager and a developer; the key set; the provider answering a listing."""
        super().setUp()
        self.manager = self._cloud_user("asis-rs-mgr", group="group_cloud_manager")
        self.dev = self._cloud_user("asis-rs-dev")
        self.portal = self.env["res.users"].create(
            {
                "name": "Portal",
                "login": "asis-rs-portal",
                "group_ids": [(6, 0, [self.env.ref("base.group_portal").id])],
            }
        )
        self._configure(users=[self.dev])
        self.connector = MagicMock(spec=AnthropicConnector)
        self.connector.list_models.return_value = [
            ModelInfo("claude-opus-5-5", "Claude Opus 5.5", True),
            ModelInfo("claude-sonnet-5-5", "Claude Sonnet 5.5", True),
            ModelInfo("claude-new-x", "Claude New X", True),
        ]
        patcher = patch.object(
            assistant_settings,
            "connector_for",
            autospec=True,
            return_value=self.connector,
        )
        self.connector_for = patcher.start()
        self.addCleanup(patcher.stop)

    def _rpc(self, login, path, **params):
        """Call *path* as *login*; return ``(result, error, raw text)``."""
        self.authenticate(login, login)
        response = self.url_open(
            f"/cloud/assistant/{path}",
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": params}),
            headers={"Content-Type": "application/json"},
        )
        body = response.json()
        return body.get("result"), body.get("error"), response.text

    def test_managers_only(self):
        """A developer reads nothing and saves nothing."""
        for path, params in (
            ("get_settings", {}),
            ("save_settings", {"vals": {}}),
            ("refresh_models", {}),
            ("usage", {}),
        ):
            with self.subTest(path):
                result, error, _text = self._rpc(self.dev.login, path, **params)
                self.assertIsNone(result)
                self.assertTrue(error)

    def test_the_key_is_said_to_be_set_never_shown(self):
        """``has_api_key``, the listing with its tags; the key nowhere in the answer."""
        result, _error, text = self._rpc(self.manager.login, "get_settings")
        self.assertTrue(result["has_api_key"])
        self.assertNotIn(KEY, text)
        self.assertEqual(
            {m["id"]: m["recommended"] for m in result["models"]},
            {
                "claude-opus-5-5": True,
                "claude-sonnet-5-5": True,
                "claude-haiku-5-5": False,
            },
        )
        self.assertTrue(all(m["price_known"] for m in result["models"]))
        self.assertIn(self.dev.id, [c["id"] for c in result["candidates"]])
        self.assertNotIn(self.portal.id, [c["id"] for c in result["candidates"]])

    def test_a_new_key_is_tried_before_it_is_kept(self):
        """Listed with the new key, kept with the listing; a refused key is not kept."""
        result, _error, text = self._rpc(
            self.manager.login, "save_settings", vals={"api_key": NEW_KEY}
        )
        self.assertTrue(result["ok"], result)
        self.connector_for.assert_called_with("anthropic", NEW_KEY)
        self.assertEqual(self.env["cloud.settings"]._assistant_key(), NEW_KEY)
        self.assertIn(
            "claude-new-x",
            [m["id"] for m in self.env["cloud.settings"]._assistant_config().models],
        )
        self.connector.list_models.side_effect = ProviderError("invalid_key", "nope")
        result, _error, text = self._rpc(
            self.manager.login, "save_settings", vals={"api_key": "sk-ant-bad-0003"}
        )
        self.assertFalse(result["ok"])
        self.assertNotIn("sk-ant-bad-0003", text)
        self.assertEqual(self.env["cloud.settings"]._assistant_key(), NEW_KEY)

    def test_a_new_provider_needs_its_own_key(self):
        """Without one typed with it, refused: the old key never goes to it."""
        result, _error, _text = self._rpc(
            self.manager.login, "save_settings", vals={"provider": "openai"}
        )
        self.assertFalse(result["ok"])
        self.connector_for.assert_not_called()
        self.assertEqual(
            self.env["cloud.settings"]._assistant_config().provider, "anthropic"
        )
        result, _error, _text = self._rpc(
            self.manager.login,
            "save_settings",
            vals={"provider": "openai", "api_key": "sk-openai-0004"},
        )
        self.assertTrue(result["ok"], result)
        self.connector_for.assert_called_with("openai", "sk-openai-0004")
        config = self.env["cloud.settings"]._assistant_config()
        self.assertEqual((config.allowed_models, config.default_model), ((), ""))

    def test_the_key_is_not_read_through_the_orm_either(self):
        """A manager reads the settings by RPC, never the key's field."""
        self.authenticate(self.manager.login, self.manager.login)
        response = self.url_open(
            "/web/dataset/call_kw/cloud.settings/search_read",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "call",
                    "params": {
                        "model": "cloud.settings",
                        "method": "search_read",
                        "args": [[], ["assistant_api_key"]],
                        "kwargs": {},
                    },
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        self.assertIn("error", response.json())
        self.assertNotIn(KEY, response.text)

    def test_a_panel_that_cannot_reach_the_provider_is_told_on_save(self):
        """No internet towards the provider: the error says so."""
        self.connector.list_models.side_effect = ProviderError(
            "unreachable", "no route"
        )
        result, _error, _text = self._rpc(
            self.manager.login, "save_settings", vals={"api_key": NEW_KEY}
        )
        self.assertFalse(result["ok"])
        self.assertIn("reach", result["error"])

    def test_models_users_and_limits_are_checked(self):
        """Allowed among the listed, default among the allowed, users of the panel."""
        cases = (
            {"allowed_models": ["claude-unknown"]},
            {"default_model": "claude-haiku-5-5"},
            {"user_ids": [self.portal.id]},
            {"monthly_limit": -1},
            {"step_cap": 0},
        )
        for vals in cases:
            with self.subTest(vals):
                result, _error, _text = self._rpc(
                    self.manager.login, "save_settings", vals=vals
                )
                self.assertFalse(result["ok"])
        result, _error, _text = self._rpc(
            self.manager.login,
            "save_settings",
            vals={
                "allowed_models": ["claude-sonnet-5-5"],
                "default_model": "claude-sonnet-5-5",
                "user_ids": [self.dev.id, self.manager.id],
                "monthly_limit": 25.5,
                "step_cap": 40,
            },
        )
        self.assertTrue(result["ok"], result)
        config = self.env["cloud.settings"]._assistant_config()
        self.assertEqual(
            (
                config.allowed_models,
                config.default_model,
                config.monthly_limit,
                config.step_cap,
            ),
            (("claude-sonnet-5-5",), "claude-sonnet-5-5", 25.5, 40),
        )

    def test_under_a_monthly_limit_the_default_model_has_a_price(self):
        """An unpriced default and a limit are refused together; either alone, kept."""
        self._configure(
            users=[self.dev],
            models=MODELS + ("claude-new-x",),
            allowed=("claude-opus-5-5", "claude-new-x"),
        )
        result, _error, _text = self._rpc(
            self.manager.login,
            "save_settings",
            vals={"default_model": "claude-new-x", "monthly_limit": 10},
        )
        self.assertFalse(result["ok"])
        self.assertIn("monthly limit", result["error"])
        result, _error, _text = self._rpc(
            self.manager.login, "save_settings", vals={"default_model": "claude-new-x"}
        )
        self.assertTrue(result["ok"], result)
        result, _error, _text = self._rpc(
            self.manager.login, "save_settings", vals={"monthly_limit": 10}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(
            self.env["cloud.settings"]._assistant_config().monthly_limit, 0
        )

    def test_a_database_that_blocks_it_refuses_to_switch_it_on(self):
        """The reason the parameter gives is the answer."""
        self.env["ir.config_parameter"].sudo().set_param(BLOCKED_PARAM, "Not here.")
        result, _error, _text = self._rpc(
            self.manager.login, "save_settings", vals={"enabled": True}
        )
        self.assertEqual(result, {"ok": False, "error": "Not here."})

    def test_it_is_not_switched_on_without_a_key(self):
        """Removing the key switches it off; switching on without one is refused."""
        result, _error, _text = self._rpc(
            self.manager.login, "save_settings", vals={"clear_api_key": True}
        )
        self.assertTrue(result["ok"])
        self.assertFalse(self.env["cloud.settings"]._assistant_config().enabled)
        result, _error, _text = self._rpc(
            self.manager.login, "save_settings", vals={"enabled": True}
        )
        self.assertFalse(result["ok"])

    def test_usage_per_user_and_month(self):
        """The table of the month, with the user's name."""
        self.env["incubacloud.assistant.usage"]._add(
            self.dev.id, "anthropic", "claude-opus-5-5", Usage(10, 5), 0.25
        )
        result, _error, _text = self._rpc(self.manager.login, "usage")
        self.assertEqual(
            [(row["user_id"], row["cost"]) for row in result["rows"]],
            [(self.dev.id, 0.25)],
        )
