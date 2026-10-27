"""The SPA pieces open in a real browser, and their anchors still exist."""

import pathlib
import re
from unittest.mock import patch

from lxml import etree

from odoo.modules.module import get_module_path
from odoo.tests import tagged
from odoo.tests.common import BaseCase, HttpCase

from ..models.cloud_settings import RESOURCE_PARAM
from .common import McpUsersMixin

_CORE_SRC = pathlib.Path(get_module_path("incubacloud")) / "static" / "src"
_OUR_SRC = pathlib.Path(get_module_path("incubacloud_mcp")) / "static" / "src"

#: The panel's MCP address in the browser tests.
RESOURCE = "https://panel.example.test/mcp"

#: A page is ready when *selector* is there and the panel's start-up
#: loader, still animating for a second after the panel mounts, is gone:
#: a browser closed under its frames can break the test's screencast.
_READY = (
    "!!document.querySelector('{selector}') && !document.querySelector('.cloud-loader')"
)


class TestTheTemplatesWeExtendKeepTheirAnchors(BaseCase):
    """Each xpath of ``mcp_templates.xml`` targets something core still has."""

    def _text(self, relative):
        """Return the source of the core template file *relative* to ``static/src``."""
        return (_CORE_SRC / relative).read_text()

    def test_the_audit_user_cells(self):
        """Each audit table has the one user cell the agent's mark replaces."""
        for relative in (
            "components/instance_detail/instance_detail.xml",
            "components/host_detail/host_detail.xml",
        ):
            with self.subTest(relative):
                cells = re.findall(
                    r'<td\s+t-esc="entry\.user_name"\s*/>', self._text(relative)
                )
                self.assertEqual(len(cells), 1)

    def test_the_menu_the_settings_and_the_shell(self):
        """The user menu's entry, the Settings tabs and the shell's main area."""
        self.assertIn(
            't-on-click="openNotifModal"',
            self._text("components/app_header/app_header.xml"),
        )
        settings = self._text("components/settings/settings.xml")
        self.assertIn('<div class="rl-tabs">', settings)
        self.assertIn("<CoreRatesTab/>", settings)
        self.assertIn("t-if=\"state.tab === 'github'\"", settings)
        self.assertIn("t-if=\"state.tab === 'rates'\"", settings)
        self.assertIn('<main class="ic-main" role="main">', self._text("app/app.xml"))


class TestOurTemplates(BaseCase):
    """What our own templates promise, read from their source."""

    def test_the_text_of_a_code_element_is_never_translated(self):
        """A ``<code>`` shows a command, a header or a path: its literal text
        stays out of the translations."""
        for path in sorted(_OUR_SRC.rglob("*.xml")):
            for code in etree.parse(str(path)).iter("code"):
                literal = (code.text or "").strip() or any(
                    (child.tail or "").strip() for child in code
                )
                if literal:
                    with self.subTest(path=path.name, line=code.sourceline):
                        self.assertEqual(code.get("t-translation"), "off")


#: Checks run in the page; any failure is a console error.
_CHECK = """
(async () => {{
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const fail = (what) => {{ throw new Error("{page}: " + what); }};
    {body}
    console.log("test successful");
}})().catch((error) => console.error(error.message));
"""

#: Connect an agent with API keys only: what it shows, tab by tab.
_API_KEY_PAGE = """
    const page = document.querySelector(".ic-mcp-connect");
    if (page.querySelector(".ic-mcp-url").textContent.trim() !== "{resource}") {{
        fail("the URL");
    }}
    if (!page.querySelector(".ic-mcp-notice")?.textContent.includes("API keys only")) {{
        fail("the API key notice");
    }}
    const tabs = [...page.querySelectorAll(".rl-tabs .rl-tab")];
    for (const tab of tabs) {{
        tab.click();
        await sleep(150);
        if (page.innerHTML.includes("cursor://")) {{
            fail("a one-click link that would carry an API key");
        }}
    }}
    tabs.find((tab) => tab.textContent.includes("Claude and Claude Desktop")).click();
    await sleep(150);
    for (const words of [
        "Customize", "Connectors", "Add custom connector", "Request headers",
        "Organization settings", "reachable from the internet",
    ]) {{
        if (!page.textContent.includes(words)) {{
            fail("claude.ai's steps: " + words);
        }}
    }}
"""

#: Connect an agent with the agents' OAuth issuer.
_OAUTH_PAGE = """
    const page = document.querySelector(".ic-mcp-connect");
    if (page.querySelector(".ic-mcp-notice")) {{
        fail("an API key notice with an issuer");
    }}
    const tabs = [...page.querySelectorAll(".rl-tabs .rl-tab")];
    tabs.find((tab) => tab.textContent.includes("Cursor")).click();
    await sleep(150);
    if (!page.querySelector("a[href^='cursor://']")) {{
        fail("Cursor's one-click link");
    }}
    tabs.find((tab) => tab.textContent.includes("Claude and Claude Desktop")).click();
    await sleep(150);
    if (page.textContent.includes("Request headers")) {{
        fail("an API key header with an issuer");
    }}
"""

#: Settings → Agents: both fields, editable or not.
_SETTINGS_PAGE = """
    const inputs = [...document.querySelectorAll(".ic-mcp-settings input")];
    if (inputs.length !== 2 || inputs.some((input) => input.readOnly !== {readonly})) {{
        fail("the fields should be read-only: {readonly}");
    }}
"""


@tagged("post_install", "-at_install")
class TestTheSpaPages(McpUsersMixin, HttpCase):
    """The page and the Settings tab, opened by a user of each role."""

    def setUp(self):
        """This panel's MCP address; agents sign in with API keys only."""
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(RESOURCE_PARAM, RESOURCE)
        self.env["cloud.settings"]._get_system().mcp_oauth_issuer = ""

    def _open(self, url, selector, body, login):
        """Open *url* as *login* once *selector* is there; run the checks of *body*."""
        self.browser_js(
            url,
            _CHECK.format(page=url, body=body),
            _READY.format(selector=selector),
            login=login,
            timeout=180,
        )

    def test_connect_an_agent_opens_from_its_route(self):
        """A stakeholder opens Connect an agent: the panel's MCP URL, the API
        key notice, no one-click link that would carry the key, and
        claude.ai's steps with the key in a request header."""
        self._cloud_user("mcp-spa-user", group="group_cloud_user")
        self._open(
            "/cloud/connect-agent",
            ".ic-mcp-connect .ic-mcp-url",
            _API_KEY_PAGE.format(resource=RESOURCE),
            "mcp-spa-user",
        )

    def test_with_an_issuer_cursor_installs_in_one_click(self):
        """With the agents' issuer, no API key notice and Cursor's link."""
        self.env[
            "cloud.settings"
        ]._get_system().mcp_oauth_issuer = "https://login.example.test"
        self._cloud_user("mcp-spa-oauth", group="group_cloud_user")
        self._open(
            "/cloud/connect-agent",
            ".ic-mcp-connect .ic-mcp-url",
            _OAUTH_PAGE,
            "mcp-spa-oauth",
        )

    def test_the_agents_settings_tab_opens(self):
        """A manager opens Settings on the Agents tab and can edit its fields."""
        self._cloud_user("mcp-spa-mgr", group="group_cloud_manager")
        self._open(
            "/cloud/settings?tab=agents",
            ".ic-mcp-settings input",
            _SETTINGS_PAGE.format(readonly="false"),
            "mcp-spa-mgr",
        )

    def test_where_the_platform_sets_them_the_fields_are_read_only(self):
        """On a panel set from above (a tenant's), both fields are read-only."""
        self._cloud_user("mcp-spa-mgr2", group="group_cloud_manager")
        Settings = type(self.env["cloud.settings"])
        with patch.object(
            Settings, "_mcp_settings_managed", autospec=True, return_value=True
        ):
            self._open(
                "/cloud/settings?tab=agents",
                ".ic-mcp-settings input",
                _SETTINGS_PAGE.format(readonly="true"),
                "mcp-spa-mgr2",
            )
