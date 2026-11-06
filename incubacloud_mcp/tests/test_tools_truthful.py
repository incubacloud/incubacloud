"""Every tool answers what it did, or says why it did nothing (6-nov-2026).

``update_instance`` with ``{"state": "draft"}`` answered ``{"ok": true}``
and changed nothing (issue #2). The owner's rule since: in every case the
MCP answers what it actually did, and a message that says so when it cannot
find or cannot do what it was asked.
"""

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.incubacloud_mcp.protocol.tool_spec import MAX_RECORD_ID

from .common import McpHttpMixin, McpUsersMixin, PanelFixtureMixin

#: The arguments that name a panel record, in every tool that has them.
RECORD_ARGS = frozenset({
    "instance_id", "source_instance_id", "project_id", "host_id",
    "target_host_id", "job_id", "alert_id", "backend_id", "grant_id",
})


def _types(schema):
    """Return the JSON types *schema* takes, as a list."""
    kind = schema.get("type")
    return list(kind) if isinstance(kind, list) else [kind] if kind else []


def _filler(schema):
    """Return a value *schema* accepts, the first of its choices if it has any."""
    if "enum" in schema:
        return schema["enum"][0]
    kinds = [kind for kind in _types(schema) if kind != "null"] or ["string"]
    return {
        "integer": 1,
        "number": 1,
        "string": "x",
        "boolean": False,
        "array": [],
        "object": {},
    }[kinds[0]]


@tagged("post_install", "-at_install")
class TestToolsSayWhatTheyDid(PanelFixtureMixin, McpUsersMixin, McpHttpMixin, HttpCase):
    """What an agent asks is done and said, or refused with the reason."""

    def setUp(self):
        """A manager, who is offered every tool."""
        super().setUp()
        self._resource_here()
        self._panel_fixture()
        self.manager = self._cloud_user("mcp-truth-manager", group="group_cloud_manager")
        self.key = self._api_key(self.manager)

    def _error(self, name, args):
        """Call *name*, check it failed, and return its message."""
        result = self._call_tool(self.key, name, args)
        self.assertTrue(result.get("isError"), result)
        return result["content"][0]["text"]

    def _ok(self, name, args):
        """Call *name*, check it went through, and return its answer."""
        result = self._call_tool(self.key, name, args)
        self.assertFalse(result.get("isError"), result)
        return self._payload(result)

    def test_a_record_that_does_not_exist_is_said_to_be_missing(self):
        """Every tool that names a record refuses one that does not exist,
        before anything runs and before anyone is asked to approve it: a
        filter, a setting or a form for the approval answered as if it had
        found it."""
        tools = 0
        for spec in self.env["incubacloud.mcp.catalog"]._tool_specs():
            properties = spec.input_schema.get("properties", {})
            named = set(properties) & set(RECORD_ARGS)
            if not named:
                continue
            args = {
                key: _filler(properties[key])
                for key in spec.input_schema.get("required", ())
            }
            args |= dict.fromkeys(named, MAX_RECORD_ID)
            with self.subTest(tool=spec.name):
                text = self._error(spec.name, args)
                self.assertRegex(
                    text,
                    rf"arguments\.\w+_id: no [a-z ]+ you can reach has the id "
                    rf"{MAX_RECORD_ID}\.",
                )
            tools += 1
        self.assertGreater(tools, 40)

    def test_a_record_out_of_reach_is_refused_as_one_that_does_not_exist(self):
        """A consultant outside the project asks for one of its instances:
        the answer is the one an id that does not exist gets, so it does
        not tell the instance is there (as ``connect.py`` answers)."""
        key = self._api_key(
            self._cloud_user("mcp-truth-outsider", group="group_cloud_consultant"),
        )
        texts = []
        for value in (self.staging.id, MAX_RECORD_ID):
            result = self._call_tool(key, "get_instance", {"instance_id": value})
            self.assertTrue(result.get("isError"), result)
            texts.append(result["content"][0]["text"].replace(str(value), "N"))
        self.assertEqual(texts[0], texts[1])
        self.assertIn("no instance you can reach has the id N", texts[0])

    def test_a_field_an_update_does_not_take_is_refused_by_name(self):
        """The issue's call: refused naming state, and nothing changes."""
        text = self._error(
            "update_instance",
            {"instance_id": self.staging.id, "vals": {"state": "draft"}},
        )
        self.assertIn("unknown argument(s): state", text)
        self.assertIn("Accepted:", text)
        self.assertEqual(self.staging.state, "draft")

    def test_an_update_says_which_fields_changed(self):
        """A field whose value is now another is named; an update whose
        every value was already there says so; one given none is refused."""
        for name, args in (
            ("update_instance", {"instance_id": self.staging.id}),
            ("update_project", {"project_id": self.project.id}),
            ("manage_host", {"host_id": self.host.id}),
        ):
            with self.subTest(tool=name):
                vals = {"description": "kept"} if name == "manage_host" else {
                    "apt_dependencies": "curl",
                }
                answer = self._ok(name, {**args, "vals": vals})
                self.assertEqual(answer["changed"], sorted(vals))
                self.assertNotIn("message", answer)
                answer = self._ok(name, {**args, "vals": vals})
                self.assertEqual(answer["changed"], [])
                self.assertIn("Nothing changed", answer["message"])
                self.assertIn("Nothing to change", self._error(name, {**args, "vals": {}}))

    def test_a_server_field_the_route_drops_is_refused(self):
        """The routes keep the fields they know: a misspelt one answered ok."""
        text = self._error(
            "manage_host", {"host_id": self.host.id, "vals": {"nmae": "renamed"}},
        )
        self.assertIn("nmae", text)
        self.assertIn("Accepted:", text)
        text = self._error(
            "manage_host",
            {"vals": {"name": "mcp-new-host", "whitelist": ["pypi.org"]}},
        )
        self.assertIn("whitelist", text)
        self.assertEqual(self.host.name, "mcp-host")
        self.assertFalse(
            self.env["cloud.host"].search([("name", "=", "mcp-new-host")])
        )

    def test_a_settings_save_says_what_changed_and_refuses_what_it_ignores(self):
        """Nothing to save, a destination another section ignores, a field
        a destination does not have: refused; a save says what changed."""
        self.assertIn(
            "Nothing to change",
            self._error("update_settings", {"section": "general"}),
        )
        self.assertIn(
            "backend_id is for the backup_backend section",
            self._error(
                "update_settings",
                {
                    "section": "general",
                    "backend_id": self.backend.id,
                    "values": {"autoassign_enabled": True},
                },
            ),
        )
        self.assertIn(
            "s3_bukcet",
            self._error(
                "update_settings",
                {
                    "section": "backup_backend",
                    "backend_id": self.backend.id,
                    "values": {"s3_bukcet": "other"},
                },
            ),
        )
        self.assertIn(
            "without values",
            self._error(
                "update_settings",
                {
                    "section": "backup_backend",
                    "backend_id": self.backend.id,
                    "action": "test",
                    "values": {"name": "x"},
                },
            ),
        )
        current = self._ok("get_settings", {})["general"]["autoassign_enabled"]
        values = {"autoassign_enabled": not current}
        answer = self._ok("update_settings", {"section": "general", "values": values})
        self.assertEqual(answer["changed"], ["autoassign_enabled"])
        answer = self._ok("update_settings", {"section": "general", "values": values})
        self.assertEqual(answer["changed"], [])
        self.assertIn("Nothing changed", answer["message"])
        answer = self._ok(
            "update_settings",
            {
                "section": "backup_backend",
                "backend_id": self.backend.id,
                "values": {"s3_path": "elsewhere"},
            },
        )
        self.assertEqual(answer["changed"], ["s3_path"])

    def test_a_preferences_save_says_what_changed(self):
        """None given is refused; the same value again changed nothing."""
        self.assertIn(
            "Nothing to change",
            self._error("update_notification_preferences", {}),
        )
        mode = self._ok("get_notification_preferences", {})["cloud_notification_mode"]
        other = "daily_digest" if mode == "immediate" else "immediate"
        answer = self._ok(
            "update_notification_preferences", {"notification_mode": other}
        )
        self.assertEqual(answer["changed"], ["notification_mode"])
        answer = self._ok(
            "update_notification_preferences", {"notification_mode": other}
        )
        self.assertEqual(answer["changed"], [])
        self.assertIn("Nothing changed", answer["message"])
