"""Where the MCP answers: ``/cloud/mcp`` always; ``/mcp``, its document and
the root one while no other installed module declares them."""

import json
from contextlib import contextmanager
from unittest.mock import patch

from werkzeug.exceptions import MethodNotAllowed

from odoo import http
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase

from odoo.addons.incubacloud_mcp.models.cloud_settings import RESOURCE_PARAM
from odoo.addons.incubacloud_mcp.models.ir_http import MODULE, route_module

from .common import JwtMixin, McpHttpMixin, McpUsersMixin

#: The made-up addon of another MCP server, and what its routes answer.
OTHER = "mcp_probe_other"
OTHER_ANSWER = "answered by the other module"
ROUTING_LOGGER = "odoo.addons.incubacloud_mcp.models.ir_http"
METADATA = "/.well-known/oauth-protected-resource"
#: Every URL of the MCP and of its documents.
OUR_URLS = ("/cloud/mcp", "/mcp", METADATA + "/cloud/mcp", METADATA + "/mcp", METADATA)


def _declare_other_module():
    """Declare the controller of another MCP server addon, as its own
    module would: ``POST /mcp`` and the root RFC 9728 document.

    :return: the controller class, now in ``Controller.children_classes``
    """

    class OtherMcpServer(http.Controller):
        """Another MCP server: an addon of its own, by its module path."""

        __module__ = f"odoo.addons.{OTHER}.controllers.main"

        @http.route(
            "/mcp",
            type="http",
            auth="none",
            methods=["POST"],
            csrf=False,
            save_session=False,
        )
        def other_mcp(self, **_kwargs):
            """Answer as the other server."""
            return OTHER_ANSWER

        @http.route(
            METADATA,
            type="http",
            auth="none",
            methods=["GET"],
            csrf=False,
            save_session=False,
        )
        def other_metadata(self, **_kwargs):
            """Answer as the other server's document."""
            return OTHER_ANSWER

    return OtherMcpServer


class OtherModuleMixin:
    """Install another MCP server module beside this one, for a while."""

    @contextmanager
    def _other_module(self):
        """Have :data:`OTHER` installed: its controller declared, its name
        among the registry's modules and the routing map built again with it.

        The map is built here, inside a check of the warning it logs.
        """
        controller = _declare_other_module()
        installed = self.registry._init_modules | {OTHER}
        try:
            with patch.object(self.registry, "_init_modules", installed):
                self.registry.clear_cache("routing")
                with self.assertLogs(ROUTING_LOGGER, "WARNING") as logs:
                    self.env["ir.http"].routing_map()
                self.routing_warnings = logs.output
                yield
        finally:
            classes = http.Controller.children_classes[OTHER]
            classes.remove(controller)
            if not classes:
                del http.Controller.children_classes[OTHER]
            self.registry.clear_cache("routing")

    def _owners(self):
        """Return ``{URL: {addons whose rule declares it}}`` for the MCP's URLs."""
        owners = {url: set() for url in OUR_URLS}
        for rule in self.env["ir.http"].routing_map().iter_rules():
            if rule.rule in owners:
                owners[rule.rule].add(route_module(rule.endpoint))
        return owners


@tagged("post_install", "-at_install")
class TestTheAliasesInTheMap(OtherModuleMixin, TransactionCase):
    """The routing map, with and without another module on ``/mcp``."""

    def setUp(self):
        """Keep the settings and the parameters at hand; a map without leftovers."""
        super().setUp()
        self.Settings = self.env["cloud.settings"]
        self.ICP = self.env["ir.config_parameter"].sudo()
        self.registry.clear_cache("routing")

    def test_alone_every_url_is_ours(self):
        """Without another module, both paths and the three documents are ours."""
        self.assertEqual(self._owners(), {url: {MODULE} for url in OUR_URLS})
        self.assertEqual(self.Settings._mcp_conflicts(), {})

    def test_another_module_takes_the_urls_it_declares_and_it_is_logged(self):
        """Its ``/mcp`` and root document stay, ours leave; the warning names it."""
        with self._other_module():
            owners = self._owners()
            adapter = self.env["ir.http"].routing_map().bind("localhost")
            endpoint, _args = adapter.match("/mcp", method="POST")
            self.assertEqual(route_module(endpoint), OTHER)
            # Our GET and DELETE went with our rule: no stray handler is left.
            with self.assertRaises(MethodNotAllowed):
                adapter.match("/mcp", method="GET")
            conflicts = self.Settings._mcp_conflicts()
        self.assertEqual(owners["/mcp"], {OTHER})
        self.assertEqual(owners[METADATA], {OTHER})
        for url in ("/cloud/mcp", METADATA + "/cloud/mcp", METADATA + "/mcp"):
            self.assertEqual(owners[url], {MODULE}, url)
        self.assertEqual(conflicts, {"/mcp": OTHER, METADATA: OTHER})
        self.assertEqual(len(self.routing_warnings), 2)
        for alias in ("/mcp", METADATA):
            line = next(
                line
                for line in self.routing_warnings
                if f"[mcp] {alias} is also declared by {OTHER}" in line
            )
            self.assertIn("/cloud/mcp", line)

    def test_the_aliases_come_back_once_it_is_gone(self):
        """Removed, the other module leaves the URLs to this one again."""
        with self._other_module():
            pass
        self.assertEqual(self._owners(), {url: {MODULE} for url in OUR_URLS})
        self.assertEqual(self.Settings._mcp_conflicts(), {})

    def test_a_new_panel_names_cloud_mcp(self):
        """Without a stored URL, web.base.url + ``/cloud/mcp``, also stored."""
        self.ICP.set_param(RESOURCE_PARAM, False)
        self.ICP.set_param("web.base.url", "HTTPS://Panel.Example.TEST:443/")
        self.assertEqual(
            self.Settings._mcp_resource(), "https://panel.example.test/cloud/mcp"
        )
        self.Settings._mcp_install_defaults()
        self.assertEqual(
            self.ICP.get_param(RESOURCE_PARAM), "https://panel.example.test/cloud/mcp"
        )

    def test_a_panel_installed_before_keeps_mcp(self):
        """A stored ``…/mcp`` stays the resource while ``/mcp`` is ours."""
        self.ICP.set_param(RESOURCE_PARAM, "https://acme.example.test/mcp")
        self.Settings._mcp_install_defaults()
        self.assertEqual(self.Settings._mcp_resource(), "https://acme.example.test/mcp")

    def test_with_another_module_on_mcp_the_resource_is_cloud_mcp_unwritten(self):
        """A stored ``…/mcp`` gives ``…/cloud/mcp`` while another module
        answers ``/mcp``, and the parameter is left as it was; any other
        stored URL is kept as it is."""
        self.ICP.set_param(RESOURCE_PARAM, "https://acme.example.test/mcp")
        with self._other_module():
            self.assertEqual(
                self.Settings._mcp_resource(), "https://acme.example.test/cloud/mcp"
            )
            self.Settings._mcp_install_defaults()
            for stored in (
                "https://acme.example.test/cloud/mcp",
                "https://acme.example.test/panel/mcp",
            ):
                self.ICP.set_param(RESOURCE_PARAM, stored)
                self.assertEqual(self.Settings._mcp_resource(), stored)
            self.ICP.set_param(RESOURCE_PARAM, "https://acme.example.test/mcp")
        self.assertEqual(
            self.ICP.get_param(RESOURCE_PARAM), "https://acme.example.test/mcp"
        )
        self.assertEqual(self.Settings._mcp_resource(), "https://acme.example.test/mcp")


@tagged("post_install", "-at_install")
class TestTheTwoPaths(
    OtherModuleMixin, JwtMixin, McpUsersMixin, McpHttpMixin, HttpCase
):
    """What an agent meets at ``/cloud/mcp`` and ``/mcp``, over HTTP."""

    def setUp(self):
        """The test issuer; a developer with a key, linked to the issuer."""
        super().setUp()
        self.registry.clear_cache("routing")
        self.addCleanup(self.registry.clear_cache, "routing")
        self.origin = self.base_url()
        resource = self._use(self.origin + "/mcp")
        self._setup_issuer(resource)
        self.provider = self._configure_issuer(resource)
        self.user = self._cloud_user("mcp-route-dev")
        self.user.write({"oauth_provider_id": self.provider.id, "oauth_uid": "4242"})
        self.key = self._api_key(self.user)
        self.database = {"X-Odoo-Database": self.env.cr.dbname}

    def _use(self, resource):
        """Store *resource* as the panel's and as the audience of new tokens."""
        self.env["ir.config_parameter"].sudo().set_param(RESOURCE_PARAM, resource)
        self._resource = resource
        return resource

    def _discover(self, path, credential=None):
        """``server/discover`` at *path* with *credential*."""
        return self._modern("server/discover", credential=credential, path=path)

    def _document(self, path):
        """GET the RFC 9728 document at *path*."""
        return self.url_open(path, headers=self.database)

    def _challenge_points_to(self, path, document):
        """Check that a call at *path* without credential is a 401 whose
        challenge names *document*."""
        response = self._discover(path)
        self.assertEqual(response.status_code, 401, path)
        self.assertEqual(
            response.headers["WWW-Authenticate"],
            f'Bearer resource_metadata="{self.origin}{document}"',
            path,
        )

    def _session_rpc(self, route):
        """Call a JSON-RPC route of the panel with the logged-in session."""
        body = {"jsonrpc": "2.0", "method": "call", "params": {}}
        return self.url_open(
            route, data=json.dumps(body), headers={"Content-Type": "application/json"}
        ).json()["result"]

    def test_a_panel_installed_before_answers_on_both_paths(self):
        """Resource ``…/mcp``: both paths open with a key and a token, and the
        challenge and the documents name ``…/mcp``."""
        for path in ("/mcp", "/cloud/mcp"):
            with self.subTest(path=path):
                self.assertEqual(self._discover(path, self.key).status_code, 200)
                self.assertEqual(self._discover(path, self._token()).status_code, 200)
                self._challenge_points_to(path, METADATA + "/mcp")
        for path in (METADATA + "/mcp", METADATA):
            response = self._document(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.json()["resource"], self.origin + "/mcp")
        self.assertEqual(self._document(METADATA + "/cloud/mcp").status_code, 404)

    def test_a_new_panel_names_cloud_mcp(self):
        """Resource ``…/cloud/mcp``: its token opens both paths, the challenge
        names its document, and the document of ``/mcp`` is 404."""
        resource = self._use(self.origin + "/cloud/mcp")
        for path in ("/mcp", "/cloud/mcp"):
            with self.subTest(path=path):
                self.assertEqual(self._discover(path, self._token()).status_code, 200)
                self._challenge_points_to(path, METADATA + "/cloud/mcp")
        for path in (METADATA + "/cloud/mcp", METADATA):
            response = self._document(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.json()["resource"], resource)
        self.assertEqual(self._document(METADATA + "/mcp").status_code, 404)

    def test_with_another_module_on_mcp_agents_go_to_cloud_mcp(self):
        """The other module answers ``/mcp`` and the root document; this
        panel answers at ``/cloud/mcp``, names it and says why."""
        old_token = self._token()
        with self._other_module():
            response = self._post({}, path="/mcp", headers=self.database)
            self.assertEqual(response.text, OTHER_ANSWER)
            self.assertEqual(self._document(METADATA).text, OTHER_ANSWER)
            self._challenge_points_to("/cloud/mcp", METADATA + "/cloud/mcp")
            document = self._document(METADATA + "/cloud/mcp")
            self.assertEqual(document.status_code, 200)
            self.assertEqual(document.json()["resource"], self.origin + "/cloud/mcp")
            self.assertEqual(self._document(METADATA + "/mcp").status_code, 404)
            # A token for the moved audience opens it; one for ``…/mcp`` no
            # longer does, an API key still does.
            self._resource = self.origin + "/cloud/mcp"
            self.assertEqual(
                self._discover("/cloud/mcp", self._token()).status_code, 200
            )
            self.assertEqual(self._discover("/cloud/mcp", old_token).status_code, 401)
            self.assertEqual(self._discover("/cloud/mcp", self.key).status_code, 200)

            self.authenticate("mcp-route-dev", "mcp-route-dev")
            config = self._session_rpc("/cloud/mcp/config")
            self.assertEqual(config["resource"], self.origin + "/cloud/mcp")
            self.assertEqual(config["conflict"], {"/mcp": OTHER, METADATA: OTHER})
            self._cloud_user("mcp-route-mgr", group="group_cloud_manager")
            self.authenticate("mcp-route-mgr", "mcp-route-mgr")
            settings = self._session_rpc("/cloud/mcp/get_settings")
            self.assertEqual(settings["mcp_resource"], self.origin + "/cloud/mcp")
            self.assertEqual(settings["conflict"], {"/mcp": OTHER, METADATA: OTHER})
        self.assertEqual(
            self.env["ir.config_parameter"].sudo().get_param(RESOURCE_PARAM),
            self.origin + "/mcp",
        )
        self.assertEqual(self._discover("/mcp", old_token).status_code, 200)
