"""Logs and captured mail: paged, cut at 64 KB, untrusted, without live links."""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.incubacloud_mcp.protocol.limits import MAX_BYTES, cap_strings

from .common import PG_PASSWORD, McpHttpMixin, McpUsersMixin, PanelFixtureMixin

MEGABYTE_OF_LINES = [f"{i:07d} " + "x" * 1000 for i in range(1024)]
#: A captured message as long as the host lets one be: text and HTML.
BIG_TEXT = "".join(f"line {i:05d} of the text, ñ\n" for i in range(2500))
BIG_HTML = "<p>" + "h" * (256 * 1024 - 7) + "</p>"
#: Messages in the staging's mailbox.
MAILBOX_SIZE = 300


def canned_logs(_self, path, params):
    """Answer the log and mailbox routes as a host would."""
    if path == "/cloud/fetch_container_logs":
        return {"ok": True, "lines": MEGABYTE_OF_LINES + [f"db password {PG_PASSWORD}"]}
    if path == "/cloud/instance_mail" and params["message_id"] == "big":
        return {
            "ok": True,
            "id": "big",
            "subject": "Big",
            "text": BIG_TEXT,
            "html": BIG_HTML,
            "attachments": [],
            "headers": [],
        }
    if path == "/cloud/instance_mail":
        return {
            "ok": True,
            "subject": "Reset",
            "text": "Reset here: https://acme.example/web/reset_password?token=abc123&db=x",
            "html": "",
        }
    if path == "/cloud/instance_mailbox":
        start = params.get("start") or 0
        items = [
            {"id": str(index), "subject": f"Message {index}", "from": "x" * 900}
            for index in range(start, MAILBOX_SIZE)
        ]
        return {"ok": True, "total": MAILBOX_SIZE, "items": items}
    raise AssertionError(path)


@tagged("post_install", "-at_install")
class TestLogTools(PanelFixtureMixin, McpUsersMixin, McpHttpMixin, HttpCase):
    """read_logs and staging_mailbox, on routes that answer as a host would."""

    def setUp(self):
        """A developer with an API key; the routes answer canned output."""
        super().setUp()
        self._resource_here()
        self._panel_fixture()
        self.key = self._api_key(self._cloud_user("mcp-logs-dev"))
        patcher = patch.object(
            type(self.env["incubacloud.mcp.dispatch"]),
            "_call",
            autospec=True,
            side_effect=canned_logs,
        )
        self.dispatch = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_megabyte_of_log_comes_back_cut_and_continues(self):
        """A megabyte of log comes in 64 KB pages that follow each other."""
        args = {"instance_id": self.staging.id, "source": "live", "lines": 2000}
        result = self._call_tool(self.key, "read_logs", args)
        self.assertLessEqual(
            len(result["content"][-1]["text"].encode()), MAX_BYTES + 512
        )
        first = self._payload(result)
        self.assertTrue(first["truncated"])
        second = self._payload(
            self._call_tool(
                self.key, "read_logs", {**args, "offset": first["next_offset"]}
            )
        )
        self.assertEqual(second["offset"], first["next_offset"])
        self.assertNotEqual(first["items"][0], second["items"][0])

    def test_logs_are_untrusted_and_scrubbed(self):
        """Log lines are marked untrusted and a known secret is masked."""
        args = {"instance_id": self.staging.id, "offset": len(MEGABYTE_OF_LINES)}
        result = self._call_tool(self.key, "read_logs", args)
        self.assertIn("Untrusted data follows", result["content"][0]["text"])
        self.assertEqual(self._payload(result)["items"], ["db password [secreto]"])

    def test_a_reset_link_in_captured_mail_is_replaced(self):
        """A password reset link in a captured message never reaches the agent."""
        payload = self._payload(
            self._call_tool(
                self.key,
                "staging_mailbox",
                {"instance_id": self.staging.id, "message_id": "1"},
            )
        )
        self.assertNotIn("abc123", payload["text"])
        self.assertIn("[secreto]", payload["text"])

    def test_the_mailbox_lists_messages_in_pages_under_64_kb(self):
        """Without message_id, the messages come in pages under 64 KB."""
        args = {"instance_id": self.staging.id}
        result = self._call_tool(self.key, "staging_mailbox", args)
        self.assertLessEqual(
            len(result["content"][-1]["text"].encode()), MAX_BYTES + 512
        )
        first = self._payload(result)
        self.assertEqual(first["items"][0]["subject"], "Message 0")
        self.assertEqual(first["total"], MAILBOX_SIZE)
        self.assertTrue(first["truncated"])
        self.assertLess(first["next_offset"], MAILBOX_SIZE)
        second = self._payload(
            self._call_tool(
                self.key, "staging_mailbox", args | {"offset": first["next_offset"]}
            )
        )
        self.assertEqual(second["items"][0]["id"], str(first["next_offset"]))

    def test_a_long_message_comes_in_pages_under_64_kb(self):
        """A message's text and HTML, together, come in pages under 64 KB."""
        args = {"instance_id": self.staging.id, "message_id": "big"}
        texts, htmls, offset, pages = [], [], 0, 0
        while offset is not None:
            result = self._call_tool(
                self.key, "staging_mailbox", args | {"offset": offset}
            )
            self.assertLessEqual(
                len(result["content"][-1]["text"].encode()), MAX_BYTES + 512
            )
            page = self._payload(result)
            self.assertEqual(page["subject"], "Big")
            texts.append(page["text"])
            htmls.append(page["html"])
            offset, pages = page["next_offset"], pages + 1
            self.assertEqual(page["truncated"], offset is not None)
        self.assertGreater(pages, 5)
        self.assertEqual(("".join(texts), "".join(htmls)), (BIG_TEXT, BIG_HTML))

    def test_logs_and_mail_are_a_developer_s(self):
        """A consultant neither sees nor runs the log and mail tools."""
        key = self._api_key(
            self._cloud_user("mcp-logs-consultant", group="group_cloud_consultant")
        )
        names = self._tool_names(key)
        for name in ("read_logs", "staging_mailbox"):
            with self.subTest(tool=name):
                self.assertNotIn(name, names)
                self.dispatch.reset_mock()
                result = self._call_tool(key, name, {"instance_id": self.staging.id})
                self.assertTrue(result["isError"])
                self.dispatch.assert_not_called()

    def test_long_strings_are_cut(self):
        """cap_strings cuts a string past the limit and says so."""
        capped, truncated = cap_strings({"body": "y" * (MAX_BYTES + 5), "n": 1})
        self.assertTrue(truncated)
        self.assertEqual(len(capped["body"]), MAX_BYTES)
