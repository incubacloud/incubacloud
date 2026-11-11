"""Settings → Assistant in a browser: it saves its edits, never shows the key."""

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

_LIMIT = "Monthly limit per user"
_KEY_FIELD = "Provider key"
_ON = "Assistant on"
#: The box and the Terms of a panel whose layer has Terms of its own.
_TERMS = "I accept the AI section of the Terms of Service"
_TERMS_URL = "https://www.example.com/terms"
#: The options of a selector of the tab, once it is opened: the menu of the
#: field whose search box has the id given.
_MENU = """
const openMenu = async (id) => {
    const input = await until(`the selector ${id}`, () => document.getElementById(id));
    input.closest(".ic-multi-select__field").click();
    return until(`the menu of ${id}`, () => document.getElementById(`${id}-menu`));
};
const option = (id, label) => [
    ...(document.getElementById(`${id}-menu`)?.querySelectorAll(".ic-multi-select__option") || []),
].find((el) => el.querySelector(".ic-multi-select__label").textContent.trim() === label);
const pick = async (id, label) => {
    await openMenu(id);
    const found = await until(`the option ${label}`, () => option(id, label));
    found.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
    await until(`${label} chosen`, () => [
        ...document.getElementById(id).closest(".ic-multi-select")
            .querySelectorAll(".ic-multi-select__chip-label"),
    ].some((chip) => chip.textContent.trim() === label));
};
"""


@tagged("post_install", "-at_install")
class TestAssistantSettingsTab(AssistantMixin, SettingsInBrowser, HttpCase):
    """The section, edited and saved like core's own sections."""

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

    def test_the_limit_is_saved_with_the_save_bar(self):
        """Typed on Assistant, it is unsaved until the bar saves it."""
        self._in_settings(
            "assistant",
            f"""
            await type("{_LIMIT}", "30");
            await expectUnsaved();
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
        """Its option is disabled while the form has a limit, and free again at 0."""
        self._configure(models=MODELS + ("claude-new-x",), limit=10.0)
        self._in_settings(
            "assistant",
            _MENU
            + f"""
            await openMenu("ic_asst_allowed");
            const unpriced = () => option("ic_asst_allowed", "claude-new-x");
            await until("the unpriced model's option", unpriced);
            if (unpriced().getAttribute("aria-disabled") !== "true"
                    || !unpriced().textContent.includes("Not usable with a monthly limit")) {{
                throw new Error("an unpriced model can be allowed under a limit");
            }}
            await type("{_LIMIT}", "0");
            await until("the option to be free without a limit",
                () => unpriced().getAttribute("aria-disabled") === "false");
        """,
        )

    def _layer_terms(self):
        """Answer the box and the Terms a layer with Terms of its own has."""
        Settings = type(self.env["cloud.settings"])
        for name, value in (
            ("_assistant_terms_label", _TERMS),
            ("_assistant_terms_url", _TERMS_URL),
        ):
            patcher = patch.object(Settings, name, autospec=True, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_switching_on_needs_the_layers_terms(self):
        """A layer with Terms: the switch waits for the box; the Terms are a link.

        Saved, the tab says who accepted, when, and the version.
        """
        self._configure(enabled=False, terms=False)
        self._layer_terms()
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
                    .includes("Terms accepted on"));
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
        """No Terms on core alone: another box, and no link."""
        if self.env["cloud.settings"]._assistant_terms_url():
            self.skipTest("a layer installed here has Terms of its own")
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
            if (tab.textContent.includes("Terms")) {{
                throw new Error("a self-hosted panel names Terms");
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
            if (text.includes("terms of") || text.includes("Terms")) {
                throw new Error("the line names Terms there are none of: " + text);
            }
        """,
        )

    def test_another_provider_is_read_chosen_and_switched_on_in_one_save(self):
        """Its key typed, its models read before saving, chosen, and on, in one save.

        Switching provider empties the models and the switch cannot go on;
        the switch says what it waits for at each step. The models are read
        with the key typed, and nothing is saved until Save, which keeps the
        key, the models chosen and the switch together.
        """
        # No acceptance on record yet: the line the save brings is this one's.
        self._configure(terms=False)
        label = self.env["cloud.settings"]._assistant_terms_label()
        Settings = type(self.env["cloud.settings"])
        with patch.object(
            Settings,
            "_assistant_record_terms_acceptance",
            autospec=True,
            side_effect=lambda _settings, acceptance: {**acceptance, "v": ""},
        ):
            self._in_settings(
                "assistant",
                _MENU
                + f"""
                const hint = () => document.getElementById("ic_asst_switch_hint")?.textContent || "";
                const select = await until("the provider", () => document.querySelector(
                    "#ic_asst_provider"));
                select.value = "openai";
                select.dispatchEvent(new Event("change", {{ bubbles: true }}));
                await until("the models to go", () => document.body.textContent.includes(
                    "Type the provider's key, then read its models"));
                const on = await field("{_ON}");
                if (on.checked || !on.disabled || !hint().includes("Type the provider's key")) {{
                    throw new Error("the switch stays on, or can be, with no key: " + hint());
                }}
                await type("{_KEY_FIELD}", "sk-openai-typed-0007");
                await until("the switch to ask for the models",
                    () => hint().includes("Read the provider's models"));
                document.getElementById("ic_asst_read_models").click();
                // The provider names its models: the selector shows their names.
                await pick("ic_asst_allowed", "Claude Opus 5.5");
                expectSent("/cloud/assistant/read_models", true);
                expectSent("/cloud/assistant/save_settings", false);
                const chosen = document.querySelector(".ic-asst-default .rl-sel__value");
                if (chosen.textContent.trim() !== "Claude Opus 5.5") {{
                    throw new Error("the default is not the model allowed: " + chosen.textContent);
                }}
                await until("the switch to ask for the box",
                    () => hint().includes("acceptance box"));
                (await field("{label}")).click();
                await until("the switch to be free", () => !on.disabled);
                on.click();
                await save();
                await expectAllSaved();
                await until("the acknowledgement shown", () => document.body.textContent
                    .includes("Last switched on, with the box ticked, on"));
                await expectValue("{_KEY_FIELD}", "");
            """,
            )
        self.env.invalidate_all()
        config = self.env["cloud.settings"]._assistant_config()
        self.assertEqual(config.provider, "openai")
        self.assertEqual(self.env["cloud.settings"]._assistant_key(), "sk-openai-typed-0007")
        self.assertEqual(list(config.allowed_models), ["claude-opus-5-5"])
        self.assertEqual(config.default_model, "claude-opus-5-5")
        self.assertTrue(config.enabled)

    def test_the_users_are_chosen_in_a_selector(self):
        """A user picked from the selector is saved; the platform's are not offered."""
        self._configure()
        user = self._cloud_user("asis-ui-dev")
        platform = self._cloud_user("__asis-ui-platform__", group="group_cloud_manager")
        self._in_settings(
            "assistant",
            _MENU
            + f"""
            await openMenu("ic_asst_users");
            if (option("ic_asst_users", "{platform.name}")) {{
                throw new Error("a user of the platform is offered");
            }}
            await pick("ic_asst_users", "{user.name}");
            await save();
            await expectAllSaved();
        """,
        )
        self.env.invalidate_all()
        self.assertIn(user.id, self.env["cloud.settings"]._assistant_config().user_ids)


class TestTheTemplatesWeExtendKeepTheirAnchors(BaseCase):
    """Each xpath of ``assistant_templates.xml`` targets something core still has."""

    def test_the_header(self):
        """The user menu's wrapper once; Settings takes the section by registry."""
        src = (
            pathlib.Path(get_module_path("incubacloud"))
            / "static"
            / "src"
            / "components"
        )
        header = (src / "app_header" / "app_header.xml").read_text()
        self.assertEqual(header.count('<div class="ic-header-user-wrap">'), 1)
        registry = (src / "settings" / "settings_registry.js").read_text()
        self.assertIn('registry.category("incubacloud.settings_sections")', registry)
        self.assertIn('registry.category("incubacloud.settings_blocks")', registry)
