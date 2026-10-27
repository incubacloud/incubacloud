"""Every /cloud route has a policy, and no read tool leaks a secret.

A route added to core, to PITR or to a module built on the MCP (the
tenant panel's) without a policy fails here — in saas's CI too.
"""

import json
from unittest.mock import patch

from odoo import Command
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase

from .common import PG_PASSWORD, McpHttpMixin, McpUsersMixin, PanelFixtureMixin


def declaring_addons(endpoint):
    """Return the addons whose classes define the method behind *endpoint*.

    The routing map holds the final (most derived) endpoint; a route
    declared by core and overridden by saas is core's too. Classes of test
    modules do not count: their routes (``/cloud/mcp_test/*``) exist only
    while tests run.

    :rtype: set[str]
    """
    func = getattr(endpoint, "func", endpoint)
    name = getattr(func, "__name__", "")
    owner = getattr(func, "__self__", None)
    addons = set()
    for cls in type(owner).__mro__ if owner is not None else ():
        module = cls.__module__
        if (
            name in vars(cls)
            and module.startswith("odoo.addons.")
            and ".tests." not in module
        ):
            addons.add(module.split(".")[2])
    return addons


@tagged("post_install", "-at_install")
class TestRouteInventory(TransactionCase):
    """The registry classifies every route of the panel.

    After every module is installed: while this one installs, the modules
    on top of it — the PITR bridge, the tenant panel — have neither their
    routes nor their policies loaded yet.
    """

    def _watched(self):
        """Core, PITR, the MCP and every installed module built on it,
        directly or through another one (the bridge's, the tenant's)."""
        mcp = (
            self.env["ir.module.module"]
            .sudo()
            .search([("name", "=", "incubacloud_mcp")])
        )
        dependents = mcp.downstream_dependencies().filtered(
            lambda module: module.state == "installed"
        )
        return {
            "incubacloud",
            "incubacloud_pitr",
            "incubacloud_mcp",
            *dependents.mapped("name"),
        }

    def test_a_module_on_top_of_a_module_on_top_is_watched(self):
        """A module that reaches the MCP through another one — on top of the
        PITR bridge, or of the tenant panel — is watched too."""
        Module = self.env["ir.module.module"].sudo()
        Module.create(
            {
                "name": "mcp_probe_direct",
                "state": "installed",
                "dependencies_id": [Command.create({"name": "incubacloud_mcp"})],
            }
        )
        Module.create(
            {
                "name": "mcp_probe_transitive",
                "state": "installed",
                "dependencies_id": [Command.create({"name": "mcp_probe_direct"})],
            }
        )
        Module.create(
            {
                "name": "mcp_probe_uninstalled",
                "state": "uninstalled",
                "dependencies_id": [Command.create({"name": "mcp_probe_direct"})],
            }
        )
        self.env.flush_all()
        watched = self._watched()
        self.assertIn("mcp_probe_direct", watched)
        self.assertIn("mcp_probe_transitive", watched)
        self.assertNotIn("mcp_probe_uninstalled", watched)

    def test_every_cloud_route_is_classified(self):
        """A /cloud route of a watched module is a tool's or excluded."""
        policies = self.env["incubacloud.mcp.catalog"]._route_policies()
        watched = self._watched()
        checked = {
            rule.rule
            for rule in self.env["ir.http"].routing_map().iter_rules()
            if rule.rule.startswith("/cloud")
            and declaring_addons(rule.endpoint) & watched
        }
        # Core's routes and the MCP's own are found: the check is not empty;
        # and so are those of a module on top, loaded after this one.
        self.assertIn("/cloud/get_secret", checked)
        self.assertIn("/cloud/mcp/config", checked)
        if "incubacloud_mcp_pitr" in watched:
            self.assertIn("/cloud/pitr/restore", checked)
        missing = sorted(checked - set(policies))
        self.assertEqual(
            missing, [], "Classify these routes in a module's _route_policies"
        )

    def test_every_policy_names_a_tool_or_gives_a_reason(self):
        """A policy names a tool that exists, or says why no tool calls it."""
        catalog = self.env["incubacloud.mcp.catalog"]
        names = [spec.name for spec in catalog._tool_specs()]
        self.assertEqual(len(names), len(set(names)), "tool names must be unique")
        for route, (kind, value) in catalog._route_policies().items():
            with self.subTest(route=route):
                self.assertIn(kind, ("tool", "excluded"))
                if kind == "tool":
                    self.assertIn(value, names)
                else:
                    self.assertTrue(value)


@tagged("post_install", "-at_install")
class TestNoReadToolLeaks(PanelFixtureMixin, McpUsersMixin, McpHttpMixin, HttpCase):
    """What a read tool's routes answer reaches the agent without its secrets."""

    def test_every_read_tool_masks_secrets_in_what_its_routes_answer(self):
        """Every route answers with a secret; no read tool lets it through.

        Each tool answers without an error, having called a route: one that
        refused the generic arguments would pass without showing anything.
        """
        self._resource_here()
        self._panel_fixture()
        key = self._api_key(
            self._cloud_user("mcp-inv-mgr", group="group_cloud_manager")
        )
        ids = {
            "instance_id": self.production.id,
            "project_id": self.project.id,
            "host_id": self.host.id,
        }

        def canned(_self, path, params):
            """Every route answers with a secret, by key and inside text,
            in its lists' rows too."""
            row = {
                "id": 1,
                "name": "row",
                "postgres_password": PG_PASSWORD,
                "note": f"secret {PG_PASSWORD}",
            }
            return {
                "ok": True,
                "lines": [f"secret {PG_PASSWORD}"],
                "postgres_password": PG_PASSWORD,
                "items": [dict(row)],
                "instances": [dict(row)],
                "count": 1,
            }

        specs = [
            spec
            for spec in self.env["incubacloud.mcp.catalog"]._tool_specs()
            if spec.read_only
        ]
        self.assertTrue(specs)
        Dispatch = type(self.env["incubacloud.mcp.dispatch"])
        with patch.object(Dispatch, "_call", autospec=True, side_effect=canned) as call:
            for spec in specs:
                properties = spec.input_schema.get("properties", {})
                required = spec.input_schema.get("required", [])
                args = {name: ids[name] for name in properties if name in ids}
                if (
                    "project_id" in args
                    and "host_id" in args
                    and spec.name == "list_instances"
                ):
                    args.pop("host_id")
                for name in required:
                    args.setdefault(
                        name, "x" if properties[name].get("type") == "string" else 1
                    )
                if "what" in properties:
                    args["what"] = "branches"
                with self.subTest(tool=spec.name):
                    calls = call.call_count
                    result = self._call_tool(key, spec.name, args)
                    self.assertFalse(result["isError"], result)
                    self.assertGreater(call.call_count, calls)
                    text = json.dumps(result)
                    self._assert_no_secret(text)
                    self.assertNotIn('"postgres_password"', text)
