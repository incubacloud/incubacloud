"""The agents' Settings keep what is typed in them until it is saved.

The Agents (MCP) section, and the agents' limit among the Rate limits, are
blocks with their own forms and save handlers, drawn into core's Settings
(see core's ``test_settings_tabs.py``). Settings sends either only when it
changed, counting the change as unsaved until then, and leaving the page
with it asks first. What is saved is what was sent: an edit made while the
request is out is sent next time. A block that failed to load shows the
error and Retry and is never sent, and one that answers after the page
moved on never stands in for the one on screen.
"""

from odoo.tests import HttpCase, tagged

from odoo.addons.incubacloud.tests.test_settings_tabs import SettingsInBrowser

from .common import StandalonePanelMixin

#: The issuer typed in the Agents section.
ISSUER = "https://login.example.test"


@tagged("post_install", "-at_install")
class TestMcpSettingsTabsKeepTheirEdits(
    StandalonePanelMixin, SettingsInBrowser, HttpCase
):
    """The Agents (MCP) section and the agents' limit, edited and saved."""

    _ISSUER = "Agents' OAuth issuer"
    _LIMIT = "Agent calls (per user, all their agents)"

    def test_an_edit_on_agents_is_unsaved_until_saved(self):
        """Leaving with the issuer typed asks; staying, Save stores it."""
        settings = self.env["cloud.settings"]._get_system()
        self.assertNotEqual(settings.mcp_oauth_issuer, ISSUER)
        self._in_settings(
            "agents",
            f"""
            await type("{self._ISSUER}", "{ISSUER}");
            await expectUnsaved();
            (await menuItem("General")).click();
            await answerLeave(false);
            await expectValue("{self._ISSUER}", "{ISSUER}");
            await save();
            await expectAllSaved();
        """,
        )
        self.env.invalidate_all()
        self.assertEqual(settings.mcp_oauth_issuer, ISSUER)

    def test_the_agents_limit_is_sent_only_once_changed(self):
        """Untouched, Save on Rate limits leaves the limit alone; edited, it is
        unsaved until Save sends it."""
        settings = self.env["cloud.settings"]._get_system()
        self._in_settings(
            "rates",
            f"""
            await field("{self._LIMIT}");
            await type("GitHub webhook (per client IP)", "4321");
            await save();
            expectSent("/cloud/mcp/save_settings", false);
            await type("{self._LIMIT}", "77");
            await expectUnsaved();
            await save();
            await expectAllSaved();
            expectSent("/cloud/mcp/save_settings", true);
        """,
        )
        self.env.invalidate_all()
        self.assertEqual(settings.rate_limit_mcp_user_per_min, 77)

    def test_an_edit_made_while_saving_stays_unsaved(self):
        """What the Agents section saved is what it sent: an issuer typed while
        the request is out stays unsaved, and the next Save sends it."""
        settings = self.env["cloud.settings"]._get_system()
        self._in_settings(
            "agents",
            f"""
            delayRpc("/cloud/mcp/save_settings", 1500);
            await type("{self._ISSUER}", "https://a.example.test");
            await pressSave();
            await until("the agents save to go out", () => sent.some(
                (url) => url.endsWith("/cloud/mcp/save_settings")));
            await type("{self._ISSUER}", "https://b.example.test");
            await saved();
            await expectUnsaved();
            await expectValue("{self._ISSUER}", "https://b.example.test");
            await save();
            await expectAllSaved();
            const sends = sent.filter(
                (url) => url.endsWith("/cloud/mcp/save_settings")).length;
            if (sends !== 2) {{
                throw new Error(`save_settings sent ${{sends}} time(s), not 2`);
            }}
        """,
        )
        self.env.invalidate_all()
        self.assertEqual(settings.mcp_oauth_issuer, "https://b.example.test")

    def test_a_block_that_failed_to_load_shows_retry_not_its_form(self):
        """No form to edit, no bar, nothing sent; Retry loads it, and it counts."""
        settings = self.env["cloud.settings"]._get_system()
        before = settings.mcp_oauth_issuer
        self._in_settings(
            "general",
            f"""
            failRpc("/cloud/mcp/get_settings");
            await openTab("Agents (MCP)");
            const failed = await until("the load to fail", () => [
                ...document.querySelectorAll(".rl-stub[role=alert]"),
            ].find((box) => box.textContent.includes(
                "Failed to load the agents' settings")));
            if (shownRow("{self._ISSUER}")) {{
                throw new Error("a block that failed to load shows its form");
            }}
            expectNoSave();
            delete rpcStubs["/cloud/mcp/get_settings"];
            failed.querySelector("button").click();
            await type("{self._ISSUER}", "{ISSUER}");
            await expectUnsaved();
        """,
        )
        self.env.invalidate_all()
        self.assertEqual(settings.mcp_oauth_issuer, before)

    def test_read_only_agents_offer_no_save(self):
        """Where the platform sets both, the section shows them and no Save.

        A managed panel's Agents tab showed "Save changes" under two read-only
        fields (visual pass of the release candidate, 29-oct-2026).
        """
        with self._managed_panel():
            self._in_settings(
                "agents",
                f"""
                const issuer = await field("{self._ISSUER}");
                if (!issuer.readOnly) throw new Error("the issuer is editable");
                expectNoSave();
            """,
            )

    def test_a_block_answered_after_its_page_went_does_not_stand_in(self):
        """Agents left before its first load answers: the block on screen counts."""
        self._in_settings(
            "general",
            f"""
            delayRpc("/cloud/mcp/get_settings", 3000, 0);
            (await menuItem("Agents (MCP)")).click();
            await pause(200);
            await openTab("General");
            await pause(3500);
            await openTab("Agents (MCP)");
            await type("{self._ISSUER}", "{ISSUER}");
            await expectUnsaved();
            await save();
            expectSent("/cloud/mcp/save_settings", true);
        """,
        )
