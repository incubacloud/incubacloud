"""The agents' Settings keep what is typed in them until it is saved.

The Agents tab, and the agents' limit on the Rates tab, are components
with their own forms and save handlers, drawn into core's Settings. As
core's own Rates tab does (see core's ``test_settings_tabs.py``), the
Agents tab stays mounted once opened: an edit survives a visit to another
tab, and Save sends it from any tab. And Settings sends either only when
it changed, counting the change as unsaved until then. What is saved is
what was sent: an edit made while the request is out is sent next time.
A tab that failed to load shows the error and Retry and is never sent,
and one Settings cancelled while it loaded never stands in for the one
on screen.
"""

from odoo.tests import HttpCase, tagged

from odoo.addons.incubacloud.tests.test_settings_tabs import SettingsInBrowser

from .common import StandalonePanelMixin

#: The issuer typed in the Agents tab.
ISSUER = "https://login.example.test"


@tagged("post_install", "-at_install")
class TestMcpSettingsTabsKeepTheirEdits(
    StandalonePanelMixin, SettingsInBrowser, HttpCase
):
    """The Agents tab and the agents' limit, edited and saved from elsewhere."""

    _ISSUER = "Agents' OAuth issuer"
    _LIMIT = "Agent calls (per user, all their agents)"

    def test_an_edit_on_agents_survives_and_is_saved_from_another_tab(self):
        """Back on Agents, the issuer typed shows; Save on General stores it."""
        settings = self.env["cloud.settings"]._get_system()
        self.assertNotEqual(settings.mcp_oauth_issuer, ISSUER)
        self._in_settings(
            "agents",
            f"""
            await type("{self._ISSUER}", "{ISSUER}");
            await expectUnsaved();
            await openTab("General");
            expectHidden("{self._ISSUER}");
            await openTab("Agents");
            await expectValue("{self._ISSUER}", "{ISSUER}");
            await openTab("General");
            await save();
            await expectAllSaved();
        """,
        )
        self.env.invalidate_all()
        self.assertEqual(settings.mcp_oauth_issuer, ISSUER)

    def test_the_agents_limit_is_sent_only_once_changed(self):
        """Untouched, Save leaves the limit alone; edited, it is unsaved until
        Save sends it."""
        settings = self.env["cloud.settings"]._get_system()
        self._in_settings(
            "rates",
            f"""
            await field("{self._LIMIT}");
            await openTab("General");
            await save();
            expectSent("/cloud/mcp/save_settings", false);
            await openTab("Rates");
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
        """What the Agents tab saved is what it sent: an issuer typed while the
        request is out stays unsaved, and the next Save sends it."""
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

    def test_a_tab_that_failed_to_load_shows_retry_not_its_form(self):
        """No form to edit, nothing sent; Retry loads it, and it counts again."""
        settings = self.env["cloud.settings"]._get_system()
        before = settings.mcp_oauth_issuer
        self._in_settings(
            "general",
            f"""
            failRpc("/cloud/mcp/get_settings");
            await openTab("Agents");
            const failed = await until("the load to fail", () => [
                ...document.querySelectorAll(".rl-stub[role=alert]"),
            ].find((box) => box.textContent.includes(
                "Failed to load the agents' settings")));
            if (shownRow("{self._ISSUER}")) {{
                throw new Error("a tab that failed to load shows its form");
            }}
            expectNoSave();
            await openTab("General");
            await save();
            expectSent("/cloud/mcp/save_settings", false);
            await openTab("Agents");
            delete rpcStubs["/cloud/mcp/get_settings"];
            failed.querySelector("button").click();
            await type("{self._ISSUER}", "{ISSUER}");
            await expectUnsaved();
        """,
        )
        self.env.invalidate_all()
        self.assertEqual(settings.mcp_oauth_issuer, before)

    def test_read_only_agents_offer_no_save(self):
        """Where the platform sets both, the tab shows them and no Save.

        A tenant's Agents tab showed "Save changes" under two read-only
        fields (visual pass of the release candidate, 29-oct-2026).
        """
        with self._managed_panel():
            self._in_settings(
                "agents",
                f"""
                const issuer = await field("{self._ISSUER}");
                if (!issuer.readOnly) throw new Error("the issuer is editable");
                expectNoSave();
                await openTab("General");
                await until("Save on General", saveButton);
            """,
            )

    def test_a_tab_cancelled_while_loading_does_not_stand_in(self):
        """Agents left before its first load answers: the tab on screen counts.

        Leaving draws Settings again, which cancels the Agents tab still
        loading and puts another in its place; the first load answers
        last, and the cancelled tab must not be the one Settings compares
        and saves.
        """
        self._in_settings(
            "general",
            f"""
            delayRpc("/cloud/mcp/get_settings", 3000, 0);
            (await tabButton("Agents")).click();
            await pause(200);
            await openTab("General");
            await pause(3500);
            await openTab("Agents");
            await type("{self._ISSUER}", "{ISSUER}");
            await expectUnsaved();
            await save();
            expectSent("/cloud/mcp/save_settings", true);
        """,
        )
