"""What a neutralized copy of the panel keeps of the MCP's settings.

The module keeps its HMAC key and the canonical URL of ``/mcp`` as system
parameters, and the agents' issuer on the settings. A restored copy of
production must not sign with production's key, nor present itself as
production's ``/mcp``, nor trust production's issuer: neutralizing a
database clears the three, and the key is created again on first use. Nor
may an agent with a credential of production act on the copy without
asking: its live auto-approval rules are revoked.
"""

from odoo.modules.neutralize import neutralize_database
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.incubacloud_mcp.models.cloud_settings import (
    RESOURCE_PARAM,
    SECRET_PARAM,
)
from odoo.addons.incubacloud_mcp.models.mcp_auto_approval import _INTERNAL


@tagged("post_install", "-at_install")
class TestNeutralizeMcp(TransactionCase):
    """Neutralizing the database clears the MCP's key, URL and issuer."""

    def setUp(self):
        """Give the copy production's key, ``/mcp`` URL and issuer."""
        super().setUp()
        self.ICP = self.env["ir.config_parameter"].sudo()
        self.Settings = self.env["cloud.settings"].sudo()
        self.key = self.Settings._mcp_secret()
        self.ICP.set_param(RESOURCE_PARAM, "https://panel.example.test/mcp")
        self.Settings._get_system().mcp_oauth_issuer = "https://login.example.test"
        self.env.flush_all()

    def _neutralize(self):
        """Neutralize the database as ``odoo neutralize`` does, then forget caches."""
        neutralize_database(self.env.cr)
        self.env.registry.clear_cache("stable")
        self.env.invalidate_all()

    def _params(self):
        """Return the MCP's system parameters left in the database.

        :rtype: list[str]
        """
        self.env.cr.execute(
            "SELECT key FROM ir_config_parameter WHERE key IN %s ORDER BY key",
            ((SECRET_PARAM, RESOURCE_PARAM),),
        )
        return [key for (key,) in self.env.cr.fetchall()]

    def test_the_key_and_the_url_are_gone(self):
        """Neither the HMAC key nor production's ``/mcp`` URL stays."""
        self._neutralize()
        self.assertEqual(self._params(), [])

    def test_the_issuer_is_cleared(self):
        """Agents of the copy connect with API keys only."""
        self._neutralize()
        self.env.cr.execute("SELECT mcp_oauth_issuer FROM cloud_settings")
        self.assertEqual(set(self.env.cr.fetchall()), {(None,)})
        self.assertEqual(self.Settings._mcp_issuer(), "")

    def test_a_new_key_is_created_on_first_use(self):
        """The copy signs with a key of its own, never production's."""
        self._neutralize()
        key = self.Settings._mcp_secret()
        self.assertEqual(len(key), 32)
        self.assertNotEqual(key, self.key)
        self.assertEqual(self._params(), [SECRET_PARAM])

    def test_live_auto_approval_rules_are_revoked(self):
        """A live rule ends, by nobody; one already ended keeps how it ended."""
        Rule = self.env["incubacloud.mcp.auto.approval"].sudo().with_context(
            **{_INTERNAL: True}
        )
        values = {
            "user_id": self.env.ref("base.user_admin").id,
            "kind": "api_key",
            "role_at_grant": "manager",
        }
        live = Rule.create(values | {"credential": "API key #9001"})
        ended = Rule.create(
            values
            | {
                "credential": "API key #9002",
                "revoked_at": "2026-11-01 10:00:00",
                "revoked_reason": "superseded",
            }
        )
        self.env.flush_all()
        self._neutralize()
        self.assertTrue(live.revoked_at)
        self.assertEqual(live.revoked_reason, "manual")
        self.assertFalse(live.revoked_by_id)
        self.assertEqual(live._state(), "revoked")
        self.assertEqual(ended.revoked_reason, "superseded")
        self.assertEqual(str(ended.revoked_at), "2026-11-01 10:00:00")
