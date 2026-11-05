"""Every /cloud route and every call of the screens has a policy; no read
tool leaks a secret.

A route added to core, to PITR or to a module built on the MCP (the
tenant panel's) without a policy fails here — in saas's CI too. So does a
call the panel's screens make that no route policy covers: an ``orm``
call to a model's method, or a job type they queue, read from the files
of the panel's bundle (``js_calls``).
"""

import json
from pathlib import Path
from unittest.mock import patch

from werkzeug.exceptions import HTTPException

from odoo import Command
from odoo.tests import tagged
from odoo.tests.common import BaseCase, HttpCase, TransactionCase

from . import js_calls
from .common import PG_PASSWORD, McpHttpMixin, McpUsersMixin, PanelFixtureMixin

#: The bundle of the panel's single-page application.
PANEL_BUNDLE = "incubacloud.assets_prod"
#: The rule that serves the application itself: a path that falls on it is
#: no route of the panel.
SPA_RULE = "/cloud/<path:subpath>"

#: Read tools that reach GitHub, or a model's method, rather than a route of
#: the panel; what they answer is masked like any answer (test_tools_code.py
#: checks it for GitHub, test_tools_jobs.py reads the jobs' history).
NO_ROUTE_READS = frozenset({"repo_read", "list_jobs"})


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


def watched_modules(env):
    """Return core, PITR, the MCP and every installed module built on it,
    directly or through another one (the bridge's, the tenant's).

    :rtype: set[str]
    """
    dependents = (
        env["ir.module.module"]
        .sudo()
        .search([("name", "=", "incubacloud_mcp")])
        .downstream_dependencies()
        .filtered(lambda module: module.state == "installed")
    )
    return {
        "incubacloud",
        "incubacloud_pitr",
        "incubacloud_mcp",
        *dependents.mapped("name"),
    }


@tagged("post_install", "-at_install")
class TestRouteInventory(TransactionCase):
    """The registry classifies every route of the panel.

    After every module is installed: while this one installs, the modules
    on top of it — the PITR bridge, the tenant panel — have neither their
    routes nor their policies loaded yet.
    """

    def _watched(self):
        """The modules whose routes are checked (:func:`watched_modules`)."""
        return watched_modules(self.env)

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
class TestToolRequirements(TransactionCase):
    """A tool that needs something first says what, in its listing."""

    #: Tools that act on a record in a given state, or after another tool.
    STATEFUL = (
        "deploy_instance",
        "rebuild_instance",
        "create_instance",
        "clone_to_staging",
        "create_backup",
        "restore_backup",
        "refresh_staging",
        "move_instance",
        "rollback_move",
        "run_command",
        "run_instance_action",
        "export_instance",
        "run_host_action",
        "retry_job",
        "download_backup",
        "restore_from_upload",
    )

    def test_stateful_tools_say_what_they_require(self):
        """Each says it after its description, beside its fields' types."""
        catalog = self.env["incubacloud.mcp.catalog"]
        for name in self.STATEFUL:
            with self.subTest(tool=name):
                listed = catalog._spec(name).listed()["description"]
                self.assertIn("Requires: ", listed)
        listed = catalog._spec("deploy_instance").listed()["description"]
        self.assertIn("a production instance needs a backup destination", listed)
        listed = catalog._spec("create_instance").listed()["description"]
        self.assertIn("vals takes these fields", listed)


class TestJsCalls(BaseCase):
    """The reader of the screens finds the calls, and nothing in a comment."""

    def test_comments_and_strings(self):
        """A call in a comment or a JSDoc example is not one; a ``//`` in a
        string or a regular expression starts no comment."""
        text = (
            "// rpc('/cloud/in_a_comment')\n"
            "/* orm.call('cloud.job', 'in_a_block', []) */\n"
            "const url = 'http://x//y'; rpc('/cloud/real', {});\n"
            "const re = /\\/\\//g; orm.call('cloud.job', 'cancel_job', [1]);\n"
        )
        calls = js_calls.script_calls(text)
        self.assertEqual(
            [(call.kind, call.value) for call in calls],
            [("rpc", "/cloud/real"), ("orm", "cloud.job.cancel_job")],
        )

    def test_enqueue_names_the_job_type(self):
        """``cloud.job.enqueue`` counts by the job type it queues, written
        on several lines; one from data says so."""
        text = (
            "await this.orm.call(\n  'cloud.job',\n  'enqueue',\n"
            "  [host, inst, 'export_instance', {a: [1, 2]}],\n);\n"
            "this.orm.call('cloud.job', 'enqueue', [h, false, action.code]);\n"
        )
        self.assertEqual(
            [(call.kind, call.value) for call in js_calls.script_calls(text)],
            [("job", "export_instance"), ("data_job", "action.code")],
        )

    def test_templates_wrappers_and_variables(self):
        """A template's value counts as any value, a wrapper's literal as its
        route, and a variable route as dynamic."""
        text = (
            "rpc(`/cloud/instance/${this.props.id}/x`);\n"
            "this._bff('plan_list', {});\n"
            "rpc(route, {});\n"
            "window.open(`/cloud/log/${job.id}`, '_blank');\n"
        )
        calls = js_calls.script_calls(text, {"_bff": "/cloud/billing/"})
        self.assertEqual(
            sorted((call.kind, call.value) for call in calls),
            [
                ("dynamic", "route"),
                ("page", "/cloud/log/1"),
                ("rpc", "/cloud/billing/plan_list"),
                ("rpc", "/cloud/instance/1/x"),
            ],
        )

    def test_template_actions(self):
        """An OWL template's ``instanceAction({code})`` names its job type;
        one in an XML comment does not."""
        text = (
            "<!-- instanceAction({ code: 'old' }) -->\n"
            "<button t-on-click=\"() => this.instanceAction({ code: 'stop_instance' })\"/>"
        )
        self.assertEqual(
            [call.value for call in js_calls.template_calls(text)], ["stop_instance"]
        )


@tagged("post_install", "-at_install")
class TestCallInventory(TransactionCase):
    """Every call of the panel's screens has a tool or a reason.

    The screens are the files of the panel's bundle from the watched
    modules (``watched_modules``): not their tests, the website's bundle,
    nor the master's, which no MCP serves.
    """

    def setUp(self):
        """The registry, the routing map and the screens' files."""
        super().setUp()
        catalog = self.env["incubacloud.mcp.catalog"]
        self.routes = catalog._route_policies()
        self.calls = catalog._call_policies()
        self.dynamic = catalog._dynamic_calls()
        self.wrappers = catalog._call_wrappers()
        self.adapter = self.env["ir.http"].routing_map().bind("localhost")

    def _files(self):
        """Return ``{bundle path: text}`` of the screens' scripts and templates."""
        watched = watched_modules(self.env)
        files = {}
        for path, full_path, *_rest in self.env["ir.asset"]._get_asset_paths(
            PANEL_BUNDLE, {}
        ):
            if path.split("/")[1] in watched and path.endswith((".js", ".xml")):
                files[path] = Path(full_path).read_text(encoding="utf-8")
        return files

    def _found(self, files):
        """Return ``[(bundle path, Call)]`` of every call in *files*."""
        found = []
        for path, text in files.items():
            reader = (
                js_calls.template_calls(text)
                if path.endswith(".xml")
                else js_calls.script_calls(text, self.wrappers)
            )
            found.extend((path, call) for call in reader)
        return found

    def _rule(self, path, json_only=False):
        """Return the rule of the routing map *path* falls on, or None.

        The application's own rule is none of the panel's routes; with
        *json_only*, nor is a rule that does not answer JSON-RPC.
        """
        for method in ("POST", "GET"):
            try:
                rule, _args = self.adapter.match(path, method=method, return_rule=True)
            except HTTPException:
                continue
            if rule.rule == SPA_RULE:
                return None
            if json_only and rule.endpoint.routing.get("type") != "jsonrpc":
                return None
            return rule
        return None

    def _unclassified(self, files):
        """Return what the screens in *files* call without a policy, as text.

        :rtype: list[str]
        """
        missing = []
        for path, call in self._found(files):
            where = f"{path}:{call.line}"
            if call.kind in ("rpc", "page"):
                rule = self._rule(call.value, json_only=call.kind == "rpc")
                if rule is not None:
                    if rule.rule not in self.routes:
                        missing.append(f"{where} route {rule.rule}")
                elif call.source not in self.dynamic.get(path, {}):
                    missing.append(f"{where} no route of the panel: {call.value}")
            elif call.kind in ("orm", "job"):
                if f"{call.kind}:{call.value}" not in self.calls:
                    missing.append(f"{where} {call.kind}:{call.value}")
            elif call.kind in (
                "dynamic",
                "data_job",
            ) and call.source not in self.dynamic.get(path, {}):
                missing.append(f"{where} a variable call: {call.source}")
        return missing

    def test_every_call_of_the_screens_is_classified(self):
        """A route, a model's method or a job type the screens call has a
        tool or a reason (``_route_policies``, ``_call_policies``), and a
        variable route is declared (``_dynamic_calls``)."""
        files = self._files()
        found = self._found(files)
        # The reader finds what it must: the check is not empty.
        kinds = {call.kind for _path, call in found}
        self.assertTrue({"rpc", "orm", "job", "page"} <= kinds, kinds)
        self.assertEqual(
            self._unclassified(files),
            [],
            "Give these calls a tool or a reason in a module's _call_policies "
            "(or _route_policies), or declare them in _dynamic_calls",
        )

    def test_a_new_call_without_a_policy_fails(self):
        """A screen that calls a method, queues a job type or reaches a route
        no policy covers is reported, each once."""
        path = "/incubacloud/static/src/components/probe/probe.js"
        text = (
            "this.orm.call('cloud.job', 'purge_everything', [1]);\n"
            "this.orm.call('cloud.job', 'enqueue', [h, i, 'wipe_disk']);\n"
            "rpc('/cloud/no_such_route', {});\n"
            "rpc(someRoute, {});\n"
            "this.orm.call('cloud.job', 'cancel_job', [1]);\n"
            "this.orm.call('cloud.job', 'enqueue', [h, i, someCode]);\n"
            "this.orm.search('cloud.instance', []);\n"
            "this.orm.nameSearch('cloud.host', 'x');\n"
        )
        missing = self._unclassified({path: text})
        self.assertEqual(len(missing), 7, missing)
        self.assertIn(f"{path}:1 orm:cloud.job.purge_everything", missing)
        self.assertIn(f"{path}:2 job:wipe_disk", missing)
        self.assertIn(f"{path}:6 a variable call: someCode", missing)
        self.assertIn(f"{path}:7 orm:cloud.instance.search", missing)
        self.assertIn(f"{path}:8 orm:cloud.host.nameSearch", missing)

    def test_every_job_type_offered_as_an_action_is_classified(self):
        """The job types the pages offer from data (``show_as_action``),
        which the reader cannot see, are classified too."""
        offered = self.env["cloud.job.type"].search([("show_as_action", "=", True)])
        self.assertTrue(offered)
        missing = sorted(
            f"job:{code}"
            for code in offered.mapped("code")
            if f"job:{code}" not in self.calls
        )
        self.assertEqual(missing, [])

    def test_every_call_policy_is_used_and_names_a_tool_or_a_reason(self):
        """A policy names a tool that exists or says why; and something still
        makes the call it classifies, so the list does not rot."""
        names = {spec.name for spec in self.env["incubacloud.mcp.catalog"]._tool_specs()}
        used = {
            f"{call.kind}:{call.value}"
            for _path, call in self._found(self._files())
            if call.kind in ("orm", "job")
        } | {
            f"job:{code}"
            for code in self.env["cloud.job.type"]
            .search([("show_as_action", "=", True)])
            .mapped("code")
        }
        for key, (kind, value) in self.calls.items():
            with self.subTest(key=key):
                self.assertIn(kind, ("tool", "excluded"))
                self.assertTrue(value)
                if kind == "tool":
                    self.assertIn(value, names)
                self.assertIn(key, used, "no screen makes this call any more")

    def test_declared_variable_calls_still_hold(self):
        """Each variable call declared is still in its file, and each path it
        reaches is still written there and has a route policy."""
        files = self._files()
        sources = {
            (path, call.source) for path, call in self._found(files) if call.source
        }
        for path, expressions in self.dynamic.items():
            with self.subTest(path=path):
                self.assertIn(path, files, "declared for a file the bundle lacks")
                for expression, reached in expressions.items():
                    self.assertIn((path, expression), sources)
                    for target in reached:
                        name = target.rstrip("/").rsplit("/", 1)[-1]
                        self.assertTrue(
                            any(
                                f"{quote}{text}{quote}" in files[path]
                                for quote in "\"'"
                                for text in (target, name)
                            ),
                            f"{target} is no longer written in {path}",
                        )
                        rule = self._rule(target)
                        self.assertIsNotNone(rule, target)
                        self.assertIn(rule.rule, self.routes)


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
            if spec.read_only and spec.name not in NO_ROUTE_READS
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
