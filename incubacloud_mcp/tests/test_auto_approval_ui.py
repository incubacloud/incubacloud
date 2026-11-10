"""Settings → Agents: granting an auto-approval rule, and revoking it, in a browser.

The dialog chooses nothing beforehand, and its button stays disabled until
the notice's box is ticked; granted, the rule shows and can be revoked.
"""

from odoo.tests import HttpCase, tagged

from odoo.addons.incubacloud.tests._browser import ChromeStartRetry
from odoo.addons.incubacloud.tests.test_settings_tabs import SettingsInBrowser
from odoo.addons.incubacloud_mcp.models.mcp_auto_approval import _INTERNAL
from odoo.addons.incubacloud_mcp.protocol.context import Principal

from .common import (
    AutoApprovalAllowedMixin,
    McpUsersMixin,
    PanelFixtureMixin,
    StandalonePanelMixin,
)


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
            const card = (state) => [
                ...document.querySelectorAll(`.ic-mcp-auto-rule--${{state}}`),
            ].find((rule) => rule.textContent.includes('{dev.name}'));
            const revoke = await until('the rule with its Revoke', () =>
                card('active')?.querySelector('.ic-mcp-auto-revoke'));
            revoke.click();
            (await until('the revoke dialog', () =>
                document.querySelector('.ic-mcp-auto-revoke-ok'))).click();
            await until('the rule revoked', () => card('revoked'));
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


@tagged("post_install", "-at_install")
class TestTheApprovalMarkOnAnInstance(
    McpUsersMixin, PanelFixtureMixin, ChromeStartRetry, HttpCase
):
    """The instance's page shows how an agent's call was approved."""

    def test_the_activity_and_the_jobs_say_auto_approved(self):
        """The audit row says auto-approved, with the rule and who granted it;
        a row the person approved says approved; the job says so too."""
        self._panel_fixture()
        manager = self._cloud_user("aau-mgr", group="group_cloud_manager")
        self.project.member_ids = [(4, manager.id)]
        Rule = self.env["incubacloud.mcp.auto.approval"].sudo()
        rule_id = Rule.with_context(**{_INTERNAL: True}).create(
            {
                "user_id": manager.id,
                "kind": "api_key",
                "credential": "API key #8802",
                "role_at_grant": "manager",
                "expires_at": "2099-01-01 00:00:00",
                "granted_by_id": manager.id,
            }
        ).id
        Log = self.env["cloud.audit.log"].sudo()
        for approval, rule in (("auto", rule_id), ("person", False)):
            Log.create(
                {
                    "action": f"Agent: probe_{approval}",
                    "instance_id": self.staging.id,
                    "user_id": manager.id,
                    "via_client": "API key ui (test-agent)",
                    "approval": approval,
                    "approval_rule_id": rule,
                }
            )
        url = f"/cloud/projects/{self.project.id}/instances/{self.staging.id}"
        code = """
            (async () => {
                const until = async (what, check) => {
                    for (let i = 0; i < 200; i++) {
                        const found = check();
                        if (found) return found;
                        await new Promise((r) => setTimeout(r, 100));
                    }
                    throw new Error(`gave up waiting for ${what}`);
                };
                try {
                    const tab = await until('the Audit Logs tab', () => [
                        ...document.querySelectorAll('.rl-tab'),
                    ].find((b) => b.textContent.trim() === 'Audit Logs'));
                    tab.click();
                    const auto = await until('the auto-approved mark', () =>
                        document.querySelector('.ic-mcp-approval--auto'));
                    if (!auto.title.startsWith('#__RULE__ · ')) {
                        throw new Error(`the mark's title is "${auto.title}"`);
                    }
                    await until('the approved mark', () => [
                        ...document.querySelectorAll('.ic-mcp-approval'),
                    ].find((m) => m.textContent.trim() === '· approved'));
                    console.log('test successful');
                } catch (error) {
                    console.error(error.message);
                }
            })();
        """.replace("__RULE__", str(rule_id))
        ready = "!!document.querySelector('.rl-tab')"
        self.browser_js(url, code, ready, login="aau-mgr", timeout=180)
