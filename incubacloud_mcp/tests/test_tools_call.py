"""tools/list and tools/call: role, arguments, transaction, audit, output."""

import json
from unittest.mock import patch

import psycopg2.errors
from psycopg2.errorcodes import SERIALIZATION_FAILURE

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase

from odoo.addons.incubacloud_mcp.protocol.context import CallContext, Principal
from odoo.addons.incubacloud_mcp.protocol.limits import MAX_BYTES
from odoo.addons.incubacloud_mcp.protocol.redact import MASK
from odoo.addons.incubacloud_mcp.protocol.tool_spec import (
    ToolSpec,
    obj,
    record_id,
    string,
)
from odoo.addons.incubacloud_mcp.protocol.versions import MODERN

from .common import McpHttpMixin, McpUsersMixin

HOST_PASSWORD = "probe-host-pass-123"
RESET_LINK = "https://acme.example/web/reset_password?token=abc123"
SAFE_ERROR_LOGGER = "odoo.addons.incubacloud.controllers._safe_error"


class _CallProbes(http.Controller):
    """Routes with known behaviour (test only)."""

    @http.route("/cloud/mcp_test/echo", type="jsonrpc", auth="user")
    def mcp_test_echo(self, text=""):
        """Say who ran it."""
        return {
            "ok": True,
            "text": text,
            "uid": request.env.uid,
            "su": request.env.su,
            "lang": request.env.lang,
        }

    @http.route("/cloud/mcp_test/refuse", type="jsonrpc", auth="user")
    def mcp_test_refuse(self, name="", vals=None):
        """Write, then answer the panel's refusal shape."""
        request.env["cloud.tag"].create({"name": name})
        return {"ok": False, "error": "Refused on purpose"}

    @http.route("/cloud/mcp_test/write_then_deny", type="jsonrpc", auth="user")
    def mcp_test_write_then_deny(self, name=""):
        """Write, then raise."""
        request.env["cloud.tag"].create({"name": name})
        raise AccessError("Denied on purpose")

    @http.route("/cloud/mcp_test/crash", type="jsonrpc", auth="user")
    def mcp_test_crash(self):
        """Fail with internal detail."""
        raise RuntimeError("internal detail /srv/very/private/path")

    @http.route("/cloud/mcp_test/leak", type="jsonrpc", auth="user")
    def mcp_test_leak(self, host_id):
        """Answer a secret, by key and inside text."""
        host = request.env["cloud.host"].sudo().browse(host_id)
        return {
            "ok": True,
            "password": host.password,
            "has_password": True,
            "log": f"connecting with {host.password}",
        }

    @http.route("/cloud/mcp_test/rotate", type="jsonrpc", auth="user")
    def mcp_test_rotate(self, host_id, new_value=""):
        """Change a host's password; answer the old and the new one."""
        host = request.env["cloud.host"].sudo().browse(host_id)
        old = host.password
        host.password = new_value
        return {"ok": True, "log": f"rotated {old} to {new_value}"}

    @http.route("/cloud/mcp_test/rotate_then_fail", type="jsonrpc", auth="user")
    def mcp_test_rotate_then_fail(self, host_id, new_value=""):
        """Change a host's password, then fail quoting the new one."""
        host = request.env["cloud.host"].sudo().browse(host_id)
        host.password = new_value
        raise UserError(f"could not finish rotating to {new_value}")

    @http.route("/cloud/mcp_test/huge_list", type="jsonrpc", auth="user")
    def mcp_test_huge_list(self):
        """Answer a list far over 64 KB."""
        return [{"n": index, "text": "y" * 1000} for index in range(500)]

    @http.route("/cloud/mcp_test/nothing", type="jsonrpc", auth="user")
    def mcp_test_nothing(self):
        """Do its job and answer nothing, as some panel routes do."""
        request.env["cloud.tag"].create({"name": "mcp-did-it"})

    @http.route("/cloud/mcp_test/queued", type="jsonrpc", auth="user")
    def mcp_test_queued(self, job_id):
        """Answer a job id alone, as most routes that enqueue do."""
        return {"ok": True, "job_id": job_id}

    @http.route("/cloud/mcp_test/link", type="jsonrpc", auth="user")
    def mcp_test_link(self):
        """Answer a link that signs someone in."""
        return {"ok": True, "body": f"Reset: {RESET_LINK}"}

    @http.route("/cloud/mcp_test/deny_quoting", type="jsonrpc", auth="user")
    def mcp_test_deny_quoting(self, host_id):
        """Refuse, quoting the host's password."""
        host = request.env["cloud.host"].sudo().browse(host_id)
        raise UserError(f"denied for {host.password}")

    @http.route("/cloud/mcp_test/huge", type="jsonrpc", auth="user")
    def mcp_test_huge(self):
        """Answer far more than 64 KB, in a list and in one string."""
        return {
            "ok": True,
            "rows": [{"n": index, "text": "y" * 1000} for index in range(500)],
            "note": "z" * 200_000,
        }


PROBES = [
    ToolSpec(
        "probe_echo",
        "Echo.",
        "group_cloud_user",
        obj({"text": string("Text.")}),
        route="/cloud/mcp_test/echo",
        read_only=True,
    ),
    ToolSpec(
        "probe_refuse",
        "Refuse.",
        "group_cloud_user",
        obj(
            {
                "name": string("Tag."),
                "vals": {"type": "object", "additionalProperties": True},
            },
            required=("name",),
        ),
        route="/cloud/mcp_test/refuse",
    ),
    ToolSpec(
        "probe_deny",
        "Deny.",
        "group_cloud_user",
        obj({"name": string("Tag.")}, required=("name",)),
        route="/cloud/mcp_test/write_then_deny",
    ),
    ToolSpec(
        "probe_crash",
        "Crash.",
        "group_cloud_user",
        obj(),
        route="/cloud/mcp_test/crash",
    ),
    ToolSpec(
        "probe_leak",
        "Leak.",
        "group_cloud_user",
        obj({"host_id": record_id("Host.")}),
        route="/cloud/mcp_test/leak",
        read_only=True,
    ),
    ToolSpec(
        "probe_rotate",
        "Rotate.",
        "group_cloud_user",
        obj({"host_id": record_id("Host."), "new_value": string("New.")}),
        route="/cloud/mcp_test/rotate",
    ),
    ToolSpec(
        "probe_rotate_fail",
        "Rotate, then fail.",
        "group_cloud_user",
        obj({"host_id": record_id("Host."), "new_value": string("New.")}),
        route="/cloud/mcp_test/rotate_then_fail",
    ),
    ToolSpec(
        "probe_always",
        "Always asks, with no text of its own.",
        "group_cloud_user",
        obj({"name": string("Tag.")}, required=("name",)),
        route="/cloud/mcp_test/refuse",
        confirm="always",
    ),
    ToolSpec(
        "probe_conditional",
        "Asks when its own method says so, and has none.",
        "group_cloud_user",
        obj({"name": string("Tag.")}, required=("name",)),
        route="/cloud/mcp_test/refuse",
        confirm="conditional",
    ),
    ToolSpec(
        "probe_nothing",
        "Answers nothing.",
        "group_cloud_user",
        obj(),
        route="/cloud/mcp_test/nothing",
    ),
    ToolSpec(
        "probe_developer",
        "Developer only.",
        "group_cloud_developer",
        obj(),
        route="/cloud/mcp_test/echo",
        read_only=True,
    ),
    ToolSpec(
        "probe_queued",
        "Enqueue.",
        "group_cloud_user",
        obj({"job_id": record_id("Job.")}),
        route="/cloud/mcp_test/queued",
        enqueues=True,
    ),
    ToolSpec(
        "probe_link_untrusted",
        "Untrusted link.",
        "group_cloud_user",
        obj(),
        route="/cloud/mcp_test/link",
        read_only=True,
        untrusted=True,
    ),
    ToolSpec(
        "probe_link",
        "Link.",
        "group_cloud_user",
        obj(),
        route="/cloud/mcp_test/link",
        read_only=True,
    ),
    ToolSpec(
        "probe_unaudited",
        "Writes its own audit.",
        "group_cloud_user",
        obj({"name": string("Tag.")}, required=("name",)),
        route="/cloud/mcp_test/refuse",
        audit=False,
    ),
    ToolSpec(
        "probe_deny_quoting",
        "Deny quoting a secret.",
        "group_cloud_user",
        obj({"host_id": record_id("Host.")}),
        route="/cloud/mcp_test/deny_quoting",
    ),
    ToolSpec(
        "probe_huge_list",
        "Huge list.",
        "group_cloud_user",
        obj(),
        route="/cloud/mcp_test/huge_list",
        read_only=True,
    ),
    ToolSpec(
        "probe_huge",
        "Huge.",
        "group_cloud_user",
        obj(),
        route="/cloud/mcp_test/huge",
        read_only=True,
    ),
    ToolSpec(
        "probe_manager",
        "Manager only.",
        "group_cloud_manager",
        obj(),
        route="/cloud/mcp_test/echo",
        read_only=True,
    ),
]


@tagged("post_install", "-at_install")
class TestToolsCall(McpUsersMixin, McpHttpMixin, HttpCase):
    """The two tool methods over HTTP, with probe tools of known behaviour.

    ``post_install``: its users need a partner, and a module installed
    later may add a column to it.
    """

    def setUp(self):
        """Serve the probes as the catalog; a developer with an API key."""
        super().setUp()
        self.env.registry.clear_cache("routing")
        patcher = patch.object(
            type(self.env["incubacloud.mcp.catalog"]),
            "_tool_specs",
            autospec=True,
            return_value=PROBES,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self._resource_here()
        self.user = self._cloud_user("mcp-call-dev")
        self.key = self._api_key(self.user, name="call-key")

    def _tag(self, name):
        """The tag *name*, if it exists."""
        return self.env["cloud.tag"].search([("name", "=", name)])

    def test_the_list_follows_the_role_in_a_stable_order(self):
        """The list holds the role's tools, by name, the same every time."""
        first = self._modern("tools/list", credential=self.key).json()["result"]
        second = self._modern("tools/list", credential=self.key).json()["result"]
        names = [tool["name"] for tool in first["tools"]]
        self.assertEqual(names, sorted(names))
        self.assertIn("probe_echo", names)
        self.assertNotIn("probe_manager", names)
        self.assertEqual(first["tools"], second["tools"])
        self.assertEqual((first["ttlMs"], first["cacheScope"]), (300000, "private"))
        self.assertEqual(first["resultType"], "complete")

    def test_each_role_lists_its_tools_and_those_below(self):
        """A developer sees a developer's tool, a manager a manager's, and a
        stakeholder neither."""
        cases = (
            (self.key, {"probe_developer"}, {"probe_manager"}),
            (
                self._api_key(self._cloud_user("mcp-call-mgr", "group_cloud_manager")),
                {"probe_developer", "probe_manager"},
                set(),
            ),
            (
                self._api_key(self._cloud_user("mcp-call-user", "group_cloud_user")),
                {"probe_echo"},
                {"probe_developer", "probe_manager"},
            ),
        )
        for key, seen, unseen in cases:
            with self.subTest(seen=seen):
                listing = self._modern("tools/list", credential=key).json()["result"]
                names = {tool["name"] for tool in listing["tools"]}
                self.assertLessEqual(seen, names)
                self.assertFalse(unseen & names)

    def test_a_tool_runs_as_the_user_never_as_superuser(self):
        """The route runs as the credential's user, in the user's language."""
        payload = self._payload(self._call_tool(self.key, "probe_echo", {"text": "hi"}))
        self.assertEqual((payload["uid"], payload["su"]), (self.user.id, False))
        self.assertEqual(payload["lang"], self.user.lang)

    def test_an_unknown_argument_or_tool_is_refused(self):
        """An unknown argument is a tool error; an unknown tool, -32602."""
        result = self._call_tool(self.key, "probe_echo", {"txt": "hi"})
        self.assertTrue(result["isError"])
        self.assertIn("txt", result["content"][0]["text"])
        response = self._modern(
            "tools/call", {"name": "no_such_tool", "arguments": {}}, self.key
        )
        self.assertEqual(
            (response.status_code, response.json()["error"]["code"]), (400, -32602)
        )

    def test_arguments_that_are_not_an_object_are_refused(self):
        """``arguments`` that is not an object is -32602."""
        response = self._modern(
            "tools/call", {"name": "probe_echo", "arguments": ["hi"]}, self.key
        )
        self.assertEqual(
            (response.status_code, response.json()["error"]["code"]), (400, -32602)
        )

    def test_a_tool_above_the_role_answers_the_role_check(self):
        """A tool above the user's role answers what the role check says."""
        result = self._call_tool(self.key, "probe_manager")
        self.assertTrue(result["isError"])
        self.assertIn("group_cloud_manager", result["content"][0]["text"])

    def test_a_refusal_keeps_what_the_route_wrote_like_the_panel(self):
        """An ``{'ok': False}`` answer keeps what the route wrote, as in the panel."""
        result = self._call_tool(self.key, "probe_refuse", {"name": "mcp-kept"})
        self.assertTrue(result["isError"])
        self.assertIn("Refused on purpose", result["content"][0]["text"])
        self.assertTrue(self._tag("mcp-kept"))

    def test_an_exception_rolls_back_what_the_route_wrote(self):
        """An exception rolls back what the route wrote before it."""
        result = self._call_tool(self.key, "probe_deny", {"name": "mcp-rolled-back"})
        self.assertTrue(result["isError"])
        self.assertIn("Denied on purpose", result["content"][0]["text"])
        self.assertFalse(self._tag("mcp-rolled-back"))

    def test_a_route_that_answers_nothing_went_through(self):
        """A route that returns None did its job: no error, no refusal audited."""
        result = self._call_tool(self.key, "probe_nothing")
        self.assertFalse(result["isError"], result)
        self.assertIsNone(self._payload(result))
        self.assertTrue(self._tag("mcp-did-it"))
        row = self.env["cloud.audit.log"].search(
            [("action", "=", "Agent: probe_nothing")], limit=1
        )
        self.assertNotIn("refused", row.details)

    def test_an_exception_is_audited_as_a_refusal(self):
        """A call that raised leaves its audit row, saying why."""
        self._call_tool(self.key, "probe_deny", {"name": "mcp-denied-audit"})
        row = self.env["cloud.audit.log"].search(
            [("action", "=", "Agent: probe_deny")], limit=1
        )
        self.assertIn("refused: Denied on purpose", row.details)

    def test_a_secret_in_an_exception_is_masked(self):
        """The text of an exception is masked like an answer."""
        host = self._probe_host()
        result = self._call_tool(self.key, "probe_deny_quoting", {"host_id": host.id})
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], f"denied for {MASK}")

    def test_a_job_id_alone_comes_back_with_its_state(self):
        """A route that answers only the job id gets the job's state added."""
        host = self._probe_host()
        job_type = self.env["cloud.job.type"].create(
            {"name": "Probe job", "code": "mcp_probe_job", "apply_to": "host"}
        )
        job = (
            self.env["cloud.job"]
            .sudo()
            .create({"name": "probe", "host_id": host.id, "job_type_id": job_type.id})
        )
        payload = self._payload(
            self._call_tool(self.key, "probe_queued", {"job_id": job.id})
        )
        self.assertEqual(payload["job_id"], job.id)
        self.assertIn("state", payload)
        self.assertIn("queued_behind", payload)

    def test_links_that_sign_in_are_masked_only_in_untrusted_output(self):
        """A reset link is masked in an untrusted tool's answer, and kept in a
        trusted one's."""
        untrusted = self._payload(self._call_tool(self.key, "probe_link_untrusted"))
        self.assertEqual(untrusted["body"], f"Reset: {MASK}")
        trusted = self._payload(self._call_tool(self.key, "probe_link"))
        self.assertEqual(trusted["body"], f"Reset: {RESET_LINK}")

    def test_a_tool_that_audits_itself_leaves_no_second_row(self):
        """A spec with ``audit=False`` is not audited by the server."""
        self._call_tool(self.key, "probe_unaudited", {"name": "mcp-unaudited"})
        self.assertFalse(
            self.env["cloud.audit.log"].search(
                [("action", "=", "Agent: probe_unaudited")]
            )
        )

    def test_the_audit_row_points_at_the_record_named_in_the_arguments(self):
        """``host_id`` in the arguments sets the row's host, if it exists."""
        host = self._probe_host()
        self._call_tool(
            self.key,
            "probe_rotate",
            {"host_id": host.id, "new_value": "probe-rotated-pass-1111"},
        )
        row = self.env["cloud.audit.log"].search(
            [("action", "=", "Agent: probe_rotate")], limit=1
        )
        self.assertEqual(row.host_id, host)
        self._call_tool(self.key, "probe_deny_quoting", {"host_id": 2**31 - 1})
        row = self.env["cloud.audit.log"].search(
            [("action", "=", "Agent: probe_deny_quoting")], limit=1
        )
        self.assertTrue(row)
        self.assertFalse(row.host_id)

    def test_no_answer_passes_64_kb(self):
        """An answer far over 64 KB that no tool pages is cut, and says so."""
        result = self._call_tool(self.key, "probe_huge")
        self.assertLessEqual(len(result["content"][-1]["text"].encode()), MAX_BYTES)
        payload = self._payload(result)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["rows"][0]["n"], 0)

    def test_a_list_answer_that_is_cut_says_so(self):
        """An answer that is a list, far over 64 KB, is cut and says so."""
        result = self._call_tool(self.key, "probe_huge_list")
        self.assertLessEqual(len(result["content"][-1]["text"].encode()), MAX_BYTES)
        payload = self._payload(result)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["items"][0]["n"], 0)

    def test_an_unexpected_error_hides_its_detail(self):
        """An unexpected error says a reference, not its internal detail."""
        with self.assertLogs(SAFE_ERROR_LOGGER, level="ERROR"):
            text = self._call_tool(self.key, "probe_crash")["content"][0]["text"]
        self.assertIn("(ref: ", text)
        self.assertNotIn("/srv/very/private/path", text)

    def _probe_host(self):
        """A host whose password is :data:`HOST_PASSWORD`."""
        return self.env["cloud.host"].create(
            {
                "name": "probe-host",
                "ip_address": "10.0.0.83",
                "user": "ubuntu",
                "wildcard_domain": "probe.example.com",
                "login_type": "password",
                "password": HOST_PASSWORD,
            }
        )

    def test_secrets_leave_neither_by_key_nor_inside_text(self):
        """A secret is dropped by its key and masked inside any text."""
        host = self._probe_host()
        payload = self._payload(
            self._call_tool(self.key, "probe_leak", {"host_id": host.id})
        )
        self.assertNotIn("password", payload)
        self.assertTrue(payload["has_password"])
        self.assertEqual(payload["log"], f"connecting with {MASK}")

    def test_a_secret_the_call_changes_is_masked_old_and_new(self):
        """A secret the call writes, and the one it replaces, are both masked."""
        host = self._probe_host()
        payload = self._payload(
            self._call_tool(
                self.key,
                "probe_rotate",
                {"host_id": host.id, "new_value": "probe-rotated-pass-9876"},
            )
        )
        self.assertEqual(payload["log"], f"rotated {MASK} to {MASK}")

    def test_a_secret_the_call_wrote_before_failing_is_masked_in_the_error(self):
        """A secret the call wrote, then quoted in its failure, is masked even
        though the failure undid the write."""
        host = self._probe_host()
        result = self._call_tool(
            self.key,
            "probe_rotate_fail",
            {"host_id": host.id, "new_value": "probe-failed-pass-5150"},
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["content"][0]["text"], f"could not finish rotating to {MASK}"
        )
        self.assertEqual(host.password, HOST_PASSWORD)

    def test_a_tool_that_asks_without_a_text_of_its_own_does_not_run(self):
        """``always``, or ``conditional`` without its method, fails closed."""
        for name in ("probe_always", "probe_conditional"):
            with self.subTest(tool=name):
                tag = f"mcp-unapproved-{name}"
                result = self._call_tool(self.key, name, {"name": tag})
                self.assertTrue(result["isError"])
                self.assertIn("approval", result["content"][0]["text"])
                self.assertFalse(self._tag(tag))

    def test_a_secret_in_an_error_before_the_call_is_masked(self):
        """What stops a call before it runs is masked like its answer."""
        self._probe_host()

        def prepare(_tools, args):
            """Refuse, quoting a secret of the database."""
            raise UserError(f"cannot prepare with {HOST_PASSWORD}")

        Tools = type(self.env["incubacloud.mcp.tools"])
        with patch.object(Tools, "_prepare_probe_echo", prepare, create=True):
            result = self._call_tool(self.key, "probe_echo", {"text": "x"})
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], f"cannot prepare with {MASK}")

    def test_a_secret_argument_is_refused_before_anything_runs(self):
        """An argument named like a secret stops the call before the route."""
        result = self._call_tool(
            self.key,
            "probe_refuse",
            {"name": "mcp-never", "vals": {"db": {"postgres_password": "x"}}},
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["content"][0]["text"],
            "Secrets are changed in the panel (arguments.vals.db.postgres_password).",
        )
        self.assertFalse(self._tag("mcp-never"))

    def test_a_call_that_may_change_something_is_audited_with_the_agent(self):
        """A call that may write is audited with the user and the agent; a
        read-only one is not."""
        self._call_tool(self.key, "probe_refuse", {"name": "mcp-audited"})
        row = self.env["cloud.audit.log"].search(
            [("action", "=", "Agent: probe_refuse")], limit=1
        )
        self.assertEqual(row.user_id, self.user)
        self.assertEqual(row.via_client, "API key call-key (test-agent)")
        self.assertIn("refused: Refused on purpose", row.details)
        self._call_tool(self.key, "probe_echo", {"text": "x"})
        self.assertFalse(
            self.env["cloud.audit.log"].search([("action", "=", "Agent: probe_echo")])
        )

    def test_a_secret_in_the_arguments_is_masked_in_the_audit_however_spelt(self):
        """A secret with a quote and an accent is masked in the audit details,
        not left there escaped by JSON."""
        secret = 'mcp-s\u00e9cret"0042'
        host = self._probe_host()
        host.password = secret
        self._call_tool(self.key, "probe_refuse", {"name": f"tag {secret}"})
        row = self.env["cloud.audit.log"].search(
            [("action", "=", "Agent: probe_refuse")], limit=1
        )
        self.assertIn(MASK, row.details)
        for form in (secret, json.dumps(secret)[1:-1]):
            self.assertNotIn(form, row.details)

    def test_an_earlier_client_initializes_and_calls_a_tool(self):
        """A 2025-06-18 client lists and calls tools in its session."""
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "older-agent", "version": "1"},
                },
            },
            self.key,
        )
        headers = {
            "Mcp-Session-Id": response.headers["Mcp-Session-Id"],
            "MCP-Protocol-Version": "2025-06-18",
        }
        listing = self._post(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            self.key,
            headers,
        ).json()["result"]
        self.assertNotIn("ttlMs", listing)
        self.assertIn("probe_echo", [tool["name"] for tool in listing["tools"]])
        call = self._post(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "probe_echo", "arguments": {"text": "hi"}},
            },
            self.key,
            headers,
        )
        result = call.json()["result"]
        self.assertNotIn("resultType", result)
        self.assertEqual(self._payload(result)["text"], "hi")

    def test_over_the_limit_the_call_is_refused(self):
        """Over the per-user limit, the call is refused with a tool error."""
        with patch.object(
            type(self.env["incubacloud.mcp.server"]),
            "_over_user_limit",
            autospec=True,
            return_value=True,
        ):
            result = self._call_tool(self.key, "probe_echo", {"text": "x"})
            refused = self._call_tool(self.key, "probe_refuse", {"name": "mcp-over"})
        self.assertTrue(result["isError"])
        self.assertIn("too many calls", result["content"][0]["text"])
        self.assertTrue(refused["isError"])
        self.assertFalse(self._tag("mcp-over"))
        self.assertFalse(
            self.env["cloud.audit.log"].search([("action", "=", "Agent: probe_refuse")])
        )


@tagged("post_install", "-at_install")
class TestUserLimit(McpUsersMixin, TransactionCase):
    """The per-user limit of tool calls, all the user's agents together.

    ``post_install``: it creates a user.
    """

    def test_the_global_limit_counts_each_call(self):
        """With a cap of two, the third call in the minute is over it."""
        self.registry_enter_test_mode()
        self.env["cloud.settings"]._get_system().rate_limit_mcp_user_per_min = 2
        server = self.env["incubacloud.mcp.server"].with_context(force_rate_limit=True)
        user = self._cloud_user("mcp-limit-dev")
        self.assertFalse(server._over_user_limit(user.id))
        self.assertFalse(server._over_user_limit(user.id))
        self.assertTrue(server._over_user_limit(user.id))
        other = self._cloud_user("mcp-limit-other")
        self.assertFalse(server._over_user_limit(other.id))


class _RetryableError(psycopg2.errors.SerializationFailure):
    """A serialization failure, as PostgreSQL would raise it."""

    pgcode = SERIALIZATION_FAILURE


@tagged("post_install", "-at_install")
class TestRunConcurrency(TransactionCase):
    """A concurrency error is not a tool error: Odoo retries the request."""

    def test_a_serialization_failure_is_raised_again(self):
        """``_run`` raises a serialization failure instead of answering it."""
        Tools = type(self.env["incubacloud.mcp.tools"])
        ctx = CallContext(MODERN, True, {}, "", Principal(self.env.uid, "test"))
        with (
            patch.object(
                Tools, "_invoke", autospec=True, side_effect=_RetryableError()
            ),
            self.assertRaises(_RetryableError),
        ):
            self.env["incubacloud.mcp.server"]._run(PROBES[0], {}, ctx)
