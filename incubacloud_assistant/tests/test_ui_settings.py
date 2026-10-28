"""Settings → Assistant in a browser: it keeps its edits, saves, never shows the key."""

import pathlib
from unittest.mock import MagicMock, patch

from odoo.modules.module import get_module_path
from odoo.tests import HttpCase, tagged
from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.tests.test_settings_tabs import SettingsInBrowser

from ..connectors.anthropic_connector import AnthropicConnector
from ..connectors.base import ModelInfo
from ..controllers import assistant_settings
from .common import KEY, MODELS, AssistantMixin

_LIMIT = "Monthly limit per user (EUR)"
_KEY_FIELD = "Provider key"
_ON = "Assistant on"
#: The box and the Terms of a tenant panel, as incubacloud_tenant answers them.
_TERMS = "I accept section 12A (AI features) of the Terms of Service"
_TERMS_URL = "https://www.incubacloud.io/terms"


@tagged("post_install", "-at_install")
class TestAssistantSettingsTab(AssistantMixin, SettingsInBrowser, HttpCase):
    """The tab, edited and saved like core's own tabs."""

    def setUp(self):
        """The assistant configured; the provider answering a listing."""
        super().setUp()
        self._configure()
        connector = MagicMock(spec=AnthropicConnector)
        connector.list_models.return_value = [
            ModelInfo("claude-opus-5-5", "Claude Opus 5.5", True)
        ]
        patcher = patch.object(
            assistant_settings, "connector_for", autospec=True, return_value=connector
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_limit_is_saved_from_another_tab(self):
        """Typed on Assistant, saved from General."""
        self._in_settings(
            "assistant",
            f"""
            await type("{_LIMIT}", "30");
            await expectUnsaved();
            await openTab("General");
            await save();
            await expectAllSaved();
            expectSent("/cloud/assistant/save_settings", true);
        """,
        )
        self.env.invalidate_all()
        self.assertEqual(
            self.env["cloud.settings"]._assistant_config().monthly_limit, 30.0
        )

    def test_the_key_is_never_on_the_page_and_a_new_one_is_saved(self):
        """The stored key is nowhere in the page; a typed one is sent and emptied."""
        self._in_settings(
            "assistant",
            f"""
            if (document.body.innerHTML.includes("{KEY}")) {{
                throw new Error("the stored key is on the page");
            }}
            await type("{_KEY_FIELD}", "sk-ant-typed-0005");
            await save();
            await expectAllSaved();
            await expectValue("{_KEY_FIELD}", "");
        """,
        )
        self.assertEqual(
            self.env["cloud.settings"]._assistant_key(), "sk-ant-typed-0005"
        )

    def test_under_a_limit_an_unpriced_model_cannot_be_allowed(self):
        """Its box is disabled while the form has a limit, and free again at 0."""
        self._configure(models=MODELS + ("claude-new-x",), limit=10.0)
        self._in_settings(
            "assistant",
            f"""
            const box = () => document.querySelector(
                '.ic-asst-models input[aria-label="claude-new-x"]');
            await until("the unpriced model's box", box);
            if (!box().disabled) {{
                throw new Error("an unpriced model can be allowed under a limit");
            }}
            await type("{_LIMIT}", "0");
            await until("the box to be free without a limit", () => !box().disabled);
        """,
        )

    def _tenant_terms(self):
        """Answer the box and the Terms a tenant panel has (incubacloud_tenant)."""
        Settings = type(self.env["cloud.settings"])
        for name, value in (
            ("_assistant_terms_label", _TERMS),
            ("_assistant_terms_url", _TERMS_URL),
        ):
            patcher = patch.object(Settings, name, autospec=True, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_switching_on_needs_section_12a(self):
        """A tenant panel: the switch waits for the box; the Terms are a link.

        Saved, the tab says who accepted, when, and the version.
        """
        self._configure(enabled=False, terms=False)
        self._tenant_terms()
        Settings = type(self.env["cloud.settings"])
        with patch.object(
            Settings,
            "_assistant_record_terms_acceptance",
            autospec=True,
            side_effect=lambda _settings, acceptance: {**acceptance, "v": "2026-11-01"},
        ):
            self._in_settings(
                "assistant",
                f"""
                const on = await field("{_ON}");
                if (!on.disabled) {{
                    throw new Error("the switch goes on without the box");
                }}
                const link = await until("the link to the Terms", () =>
                    document.querySelector('.ic-asst-settings a[href="{_TERMS_URL}"]'));
                if (!getComputedStyle(link).textDecorationLine.includes("underline")) {{
                    throw new Error("the link to the Terms does not look like one");
                }}
                (await field("{_TERMS}")).click();
                await until("the switch to be free", () => !on.disabled);
                on.click();
                await save();
                await expectAllSaved();
                await until("the acceptance shown", () => document.body.textContent
                    .includes("Section 12A of the Terms accepted on"));
                if (!document.body.textContent.includes("(terms of 2026-11-01)")) {{
                    throw new Error("the line does not name the version");
                }}
                expectHidden("{_TERMS}");
            """,
            )
        self.env.invalidate_all()
        settings = self.env["cloud.settings"]._get_system()
        self.assertTrue(settings.assistant_enabled)
        self.assertIn("settings-tabs-admin", settings.assistant_terms_accepted_by)
        self.assertEqual(settings.assistant_terms_version, "2026-11-01")

    def test_a_self_hosted_panel_asks_an_acknowledgement_and_links_nowhere(self):
        """No Terms of IncubaCloud's on core alone: another box, and no link."""
        if self.env["ir.module.module"].search_count(
            [("name", "=", "incubacloud_tenant"), ("state", "=", "installed")]
        ):
            self.skipTest("a tenant panel has IncubaCloud's Terms (incubacloud_tenant)")
        self._configure(enabled=False, terms=False)
        label = self.env["cloud.settings"]._assistant_terms_label()
        self._in_settings(
            "assistant",
            f"""
            const box = await field("{label}");
            const tab = document.querySelector(".ic-asst-settings");
            if (tab.querySelector("a")) {{
                throw new Error("a self-hosted panel links somewhere: "
                    + tab.querySelector("a").href);
            }}
            if (tab.textContent.includes("12A") || tab.textContent.includes("Terms")) {{
                throw new Error("a self-hosted panel names IncubaCloud's Terms");
            }}
            const on = await field("{_ON}");
            box.click();
            await until("the switch to be free", () => !on.disabled);
            on.click();
            await save();
            await expectAllSaved();
            await until("the acknowledgement shown", () => tab.textContent
                .includes("Last switched on, with the box ticked, on"));
        """,
        )
        self.env.invalidate_all()
        settings = self.env["cloud.settings"]._get_system()
        self.assertTrue(settings.assistant_enabled)
        self.assertFalse(settings.assistant_terms_version)

    def test_the_acceptance_line_needs_no_version(self):
        """A panel that records none (self-hosted) shows who and when, no version."""
        self._in_settings(
            "assistant",
            """
            const said = "Last switched on, with the box ticked, on";
            const line = await until("the acceptance line", () => [
                ...document.querySelectorAll(".ic-asst-settings p"),
            ].find((p) => p.textContent.includes(said)));
            const text = line.textContent;
            if (!text.includes("Test (asis-test)") || !text.includes("127.0.0.1")) {
                throw new Error("the line misses who or from where: " + text);
            }
            if (text.includes("terms of") || text.includes("12A")) {
                throw new Error("the line names Terms there are none of: " + text);
            }
        """,
        )

    def test_another_provider_empties_the_models_and_needs_its_key(self):
        """Its models are asked for again, and saved with the key typed with it.

        Meanwhile the switch is off and cannot go on: saved, switching on
        asks for the box again.
        """
        label = self.env["cloud.settings"]._assistant_terms_label()
        self._in_settings(
            "assistant",
            f"""
            const select = await until("the provider", () => document.querySelector(
                "#ic_asst_provider"));
            select.value = "openai";
            select.dispatchEvent(new Event("change", {{ bubbles: true }}));
            await until("the models to go", () => document.body.textContent.includes(
                "Save the provider's key to read its models."));
            const on = await field("{_ON}");
            if (on.checked || !on.disabled) {{
                throw new Error("the switch stays on, or can be, with no models");
            }}
            await type("{_KEY_FIELD}", "sk-openai-typed-0007");
            await save();
            await expectAllSaved();
            await until("the box asked for again", () => shownRow("{label}"));
        """,
        )
        self.env.invalidate_all()
        config = self.env["cloud.settings"]._assistant_config()
        self.assertEqual(config.provider, "openai")
        self.assertEqual(list(config.allowed_models), [])
        self.assertFalse(config.default_model)
        self.assertFalse(config.enabled)


class TestTheTemplatesWeExtendKeepTheirAnchors(BaseCase):
    """Each xpath of ``assistant_templates.xml`` targets something core still has."""

    def test_the_header_and_the_settings(self):
        """The user menu's wrapper once; the Settings tabs and the GitHub tab."""
        src = (
            pathlib.Path(get_module_path("incubacloud"))
            / "static"
            / "src"
            / "components"
        )
        header = (src / "app_header" / "app_header.xml").read_text()
        self.assertEqual(header.count('<div class="ic-header-user-wrap">'), 1)
        settings = (src / "settings" / "settings.xml").read_text()
        self.assertIn('<div class="rl-tabs">', settings)
        self.assertIn("t-if=\"state.tab === 'github'\"", settings)
