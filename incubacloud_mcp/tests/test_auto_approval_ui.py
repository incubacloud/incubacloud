"""Settings → Agents: granting an auto-approval rule, and revoking it, in a browser.

The dialog chooses nothing beforehand, and its button stays disabled until
the notice's box is ticked; granted, the rule shows and can be revoked.
"""

from odoo.tests import HttpCase, tagged

from odoo.addons.incubacloud.tests.test_settings_tabs import SettingsInBrowser
from odoo.addons.incubacloud_mcp.protocol.context import Principal

from .common import AutoApprovalAllowedMixin, McpUsersMixin, StandalonePanelMixin


@tagged("post_install", "-at_install")
class TestAutoApprovalInSettings(
    AutoApprovalAllowedMixin,
    StandalonePanelMixin,
    McpUsersMixin,
    SettingsInBrowser,
    HttpCase,
):
    """The Agents tab grants and revokes a rule."""

    def test_the_box_enables_the_grant_and_the_rule_can_be_revoked(self):
        """Disabled until everything is chosen and the box ticked; then
        granted, listed, and revoked."""
        dev = self._cloud_user("aau-dev")
        self.env["incubacloud.mcp.credential"]._seen(
            Principal(uid=dev.id, credential="API key ui", client="API key #8801"),
            "test-agent",
        )
        self._in_settings(
            "agents",
            f"""
            const grant = await until('the grant button', () =>
                document.querySelector('.ic-mcp-auto-grant'));
            grant.click();
            const confirm = await until('the dialog', () =>
                document.querySelector('.ic-mcp-auto-confirm'));
            const pick = (id, value) => {{
                const select = document.getElementById(id);
                select.value = value;
                select.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }};
            if (!confirm.disabled) throw new Error('enabled with nothing chosen');
            pick('ic_mcp_auto_user', '{dev.id}');
            await until('the credentials', () =>
                !document.getElementById('ic_mcp_auto_cred').disabled);
            pick('ic_mcp_auto_cred', 'API key #8801');
            pick('ic_mcp_auto_period', '7');
            await pause(200);
            if (!confirm.disabled) throw new Error('enabled before the box');
            const box = document.querySelector('.ic-mcp-auto-ack input');
            box.checked = true;
            box.dispatchEvent(new Event('change', {{ bubbles: true }}));
            await until('the button to enable', () => !confirm.disabled);
            confirm.click();
            const revoke = await until('the rule with its Revoke', () =>
                document.querySelector('.ic-mcp-auto-rule--active .ic-mcp-auto-revoke'));
            revoke.click();
            (await until('the revoke dialog', () =>
                document.querySelector('.ic-mcp-auto-revoke-ok'))).click();
            await until('the rule revoked', () =>
                document.querySelector('.ic-mcp-auto-rule--revoked'));
        """,
        )
        rule = self.env["incubacloud.mcp.auto.approval"].search(
            [("user_id", "=", dev.id)]
        )
        self.assertEqual(len(rule), 1)
        self.assertEqual(
            (rule.credential, rule.revoked_reason, rule.granted_by_id.login),
            ("API key #8801", "manual", "settings-tabs-admin"),
        )
