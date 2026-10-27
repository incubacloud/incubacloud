"""A tool reaches the route's final version, overrides included."""

from odoo import http
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud_mcp.protocol.tool_spec import ToolSpec, integer, obj


class _ProbeBase(http.Controller):
    """A route as a module declares it (test only: not loaded in production)."""

    @http.route("/cloud/mcp_test/probe", type="jsonrpc", auth="user")
    def mcp_test_probe(self, value=0):
        """The version the declaring module wrote."""
        return {"version": "base", "value": value}

    @http.route("/cloud/mcp_test/page", type="http", auth="user")
    def mcp_test_page(self):
        """A page for a browser: no tool calls it."""
        return "page"


class _ProbeOverride(_ProbeBase):
    """The same route, overridden the way saas overrides core's."""

    @http.route()
    def mcp_test_probe(self, value=0):
        """The version a dependent module puts on top: the one that must run."""
        return super().mcp_test_probe(value=value) | {"version": "final"}


@tagged("post_install", "-at_install")
class TestDispatch(TransactionCase):
    """Tools call the route the panel would serve, through the routing map.

    ``post_install``: the routing map then holds every installed module's
    overrides.
    """

    @classmethod
    def setUpClass(cls):
        """Rebuild the routing map: the probes were imported after it was built."""
        super().setUpClass()
        cls.env.registry.clear_cache("routing")

    def test_the_final_override_runs_with_the_arguments(self):
        """The most derived version of the route runs, with the arguments."""
        result = self.env["incubacloud.mcp.dispatch"]._call(
            "/cloud/mcp_test/probe", {"value": 3}
        )
        self.assertEqual(result, {"version": "final", "value": 3})

    def test_an_unknown_route_or_a_page_is_refused(self):
        """A path that is not a JSON-RPC route of the panel is refused."""
        for path in ("/cloud/no_such_route", "/cloud/mcp_test/page"):
            with self.subTest(path=path), self.assertRaises(UserError):
                self.env["incubacloud.mcp.dispatch"]._call(path, {})

    def test_a_tool_with_a_route_goes_through_the_dispatcher(self):
        """A spec with a route and no handler is served by the dispatcher."""
        spec = ToolSpec(
            "probe",
            "Probe.",
            "group_cloud_user",
            obj({"value": integer("v")}),
            route="/cloud/mcp_test/probe",
            read_only=True,
        )
        self.assertEqual(
            self.env["incubacloud.mcp.tools"]._invoke(spec, {"value": 5}),
            {"version": "final", "value": 5},
        )

    def test_core_s_exclusions_are_in_the_registry(self):
        """Core's excluded routes say why; an unknown tool has no spec."""
        Catalog = self.env["incubacloud.mcp.catalog"]
        policies = Catalog._route_policies()
        self.assertEqual(policies["/cloud/get_secret"][0], "excluded")
        self.assertTrue(policies["/cloud/get_secret"][1])
        self.assertIsNone(Catalog._spec("no_such_tool"))
