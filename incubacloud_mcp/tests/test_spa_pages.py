"""The SPA pieces open in a real browser, and their anchors still exist."""

import pathlib
import re

from lxml import etree

from odoo.modules.module import get_module_path
from odoo.tests import tagged
from odoo.tests.common import BaseCase, HttpCase

from .common import McpUsersMixin

_CORE_SRC = pathlib.Path(get_module_path("incubacloud")) / "static" / "src"
_OUR_SRC = pathlib.Path(get_module_path("incubacloud_mcp")) / "static" / "src"

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


@tagged("post_install", "-at_install")
class TestTheSpaPages(McpUsersMixin, HttpCase):
    """The page and the Settings tab, opened by a user of each role."""

    def test_connect_an_agent_opens_from_its_route(self):
        """A stakeholder opens Connect an agent and reads the panel's MCP URL."""
        self._cloud_user("mcp-spa-user", group="group_cloud_user")
        self.browser_js(
            "/cloud/connect-agent",
            "console.log('test successful')",
            _READY.format(selector=".ic-mcp-connect .ic-mcp-url"),
            login="mcp-spa-user",
            timeout=180,
        )

    def test_the_agents_settings_tab_opens(self):
        """A manager opens Settings on the Agents tab and finds its fields."""
        self._cloud_user("mcp-spa-mgr", group="group_cloud_manager")
        self.browser_js(
            "/cloud/settings?tab=agents",
            "console.log('test successful')",
            _READY.format(selector=".ic-mcp-settings input"),
            login="mcp-spa-mgr",
            timeout=180,
        )
