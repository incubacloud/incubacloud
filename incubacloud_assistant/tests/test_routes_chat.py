"""The chat's routes: whose they are, who may use them, and what they answer."""

import json
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.queue_job.tests.common import trap_jobs

from .common import KEY, AssistantMixin


@tagged("post_install", "-at_install")
class TestChatRoutes(AssistantMixin, HttpCase):
    """Through HTTP, as the panel's side panel calls them."""

    def setUp(self):
        """Two developers allowed, one not, and a manager allowed."""
        super().setUp()
        self._panel_fixture()
        self.dev = self._cloud_user("asis-rc-dev")
        self.other = self._cloud_user("asis-rc-other")
        self.outsider = self._cloud_user("asis-rc-out")
        self.manager = self._cloud_user("asis-rc-mgr", group="group_cloud_manager")
        self._configure(users=[self.dev, self.other, self.manager])

    def _rpc(self, login, path, **params):
        """Call *path* as *login*; return ``(result, error, raw text)``."""
        self.authenticate(login, login)
        response = self.url_open(
            f"/cloud/assistant/{path}",
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": params}),
            headers={"Content-Type": "application/json"},
        )
        body = response.json()
        return body.get("result"), body.get("error"), response.text

    def _accepted(self, user):
        """*user* read the first-use notice."""
        self._rpc(user.login, "accept_notice")

    def test_status_shows_only_to_whom_the_assistant_is_on(self):
        """Not allowed: not available. Allowed: models, notice, usage; never the key."""
        result, _error, _text = self._rpc(self.outsider.login, "status")
        self.assertEqual(result, {"available": False, "conversations": 0})
        result, _error, text = self._rpc(self.dev.login, "status")
        self.assertTrue(result["available"])
        self.assertTrue(result["notice_needed"])
        self.assertEqual(
            [(m["id"], m["recommended"]) for m in result["models"]],
            [("claude-opus-5-5", True), ("claude-sonnet-5-5", True)],
        )
        self.assertNotIn(KEY, text)
        self.assertIn("docs.incubacloud.io", result["link_hosts"])
        # A server's wildcard is shared: only each instance's own name links.
        self.assertNotIn(self.host.wildcard_domain, result["link_hosts"])
        self._accepted(self.dev)
        self.assertFalse(self._rpc(self.dev.login, "status")[0]["notice_needed"])

    def test_nothing_is_sent_before_the_notice(self):
        """The person reads what goes to the provider before the first message."""
        result, _error, _text = self._rpc(self.dev.login, "send", text="Hello")
        self.assertFalse(result["ok"])
        self.assertFalse(
            self.env["incubacloud.assistant.conversation"].sudo().search([])
        )

    def test_an_empty_message_opens_no_conversation(self):
        """Refused before a conversation exists: none is left empty."""
        self._accepted(self.dev)
        for text in ("", "   \n", None):
            with self.subTest(text=text):
                result, _error, _text = self._rpc(self.dev.login, "send", text=text)
                self.assertFalse(result["ok"])
        self.assertFalse(
            self.env["incubacloud.assistant.conversation"].sudo().search([])
        )

    def test_a_message_opens_a_conversation_and_starts_its_task(self):
        """A conversation of the person's, one step queued; a second message waits."""
        self._accepted(self.dev)
        with trap_jobs() as trap:
            result, _error, _text = self._rpc(
                self.dev.login,
                "send",
                text="What is a staging?",
                context={"route": "projects"},
            )
            conversation_id = result["conversation"]["id"]
            second, _error, _text = self._rpc(
                self.dev.login,
                "send",
                text="And a backup?",
                conversation_id=conversation_id,
            )
        trap.assert_jobs_count(1)
        self.assertTrue(second["message"]["pending"])
        listed, _error, _text = self._rpc(self.dev.login, "conversations")
        self.assertEqual([c["id"] for c in listed["conversations"]], [conversation_id])

    def test_nobody_else_reads_or_touches_it_a_manager_neither(self):
        """Another user, or the manager, gets an error from every route."""
        self._accepted(self.dev)
        with trap_jobs():
            result, _error, _text = self._rpc(self.dev.login, "send", text="Mine")
        conversation_id = result["conversation"]["id"]
        for login in (self.other.login, self.manager.login):
            for path in ("conversation", "stop", "retry", "delete"):
                with self.subTest(login=login, path=path):
                    result, error, _text = self._rpc(
                        login, path, conversation_id=conversation_id
                    )
                    self.assertIsNone(result)
                    self.assertTrue(error)
        self.assertTrue(
            self.env["incubacloud.assistant.conversation"]
            .sudo()
            .browse(conversation_id)
            .exists()
        )

    def test_reading_marks_it_read_and_lists_its_messages(self):
        """Its messages in order; unread no more."""
        self._accepted(self.dev)
        with trap_jobs():
            result, _error, _text = self._rpc(self.dev.login, "send", text="Hello")
        conversation = (
            self.env["incubacloud.assistant.conversation"]
            .sudo()
            .browse(result["conversation"]["id"])
        )
        # The step is trapped, so no queue job exists: reading would heal the
        # conversation as lost. Here nothing runs.
        conversation.write({"state": "idle", "unread": True})
        read, _error, _text = self._rpc(
            self.dev.login, "conversation", conversation_id=conversation.id
        )
        self.assertEqual([m["role"] for m in read["messages"]], ["user"])
        self.assertFalse(conversation.unread)

    def test_the_card_is_answered_here_only_by_its_owner(self):
        """Confirm approves and goes on; another user cannot; cancel withdraws."""
        conversation = (
            self.env["incubacloud.assistant.conversation"].with_user(self.dev)._start()
        )
        approvals, cards = [], []
        for call_id in ("c1", "c2"):
            approval, _token = (
                self.env["incubacloud.mcp.approval"]
                .with_user(self.dev)
                ._open(
                    "delete_project",
                    call_id,
                    "Delete.",
                    "assistant",
                    f"assistant:{conversation.id}",
                )
            )
            approvals.append(approval)
            cards.append(
                conversation._add_message(
                    {
                        "role": "card",
                        "approval_id": approval.id,
                        "card_state": "pending",
                        "tool_call_id": call_id,
                        "tool_name": "delete_project",
                    }
                )
            )
        conversation.sudo().state = "waiting_confirmation"
        _result, error, _text = self._rpc(
            self.other.login, "confirm", message_id=cards[0].id
        )
        self.assertTrue(error)
        with trap_jobs() as trap:
            result, _error, _text = self._rpc(
                self.dev.login, "confirm", message_id=cards[0].id
            )
            self.assertTrue(result["ok"])
            # The other card still waits: no step yet.
            trap.assert_jobs_count(0)
            result, _error, _text = self._rpc(
                self.dev.login, "cancel", message_id=cards[1].id
            )
        self.assertTrue(result["ok"])
        trap.assert_jobs_count(1)
        self.assertEqual(approvals[0].sudo().state, "approved")
        self.assertEqual(approvals[1].sudo().state, "cancelled")

    def test_the_model_is_one_of_the_allowed(self):
        """An allowed model is kept; any other, refused."""
        conversation = (
            self.env["incubacloud.assistant.conversation"].with_user(self.dev)._start()
        )
        ok, _error, _text = self._rpc(
            self.dev.login,
            "set_model",
            conversation_id=conversation.id,
            model_id="claude-sonnet-5-5",
        )
        self.assertTrue(ok["ok"])
        refused, _error, _text = self._rpc(
            self.dev.login,
            "set_model",
            conversation_id=conversation.id,
            model_id="claude-haiku-5-5",
        )
        self.assertFalse(refused["ok"])
        self.assertEqual(conversation.sudo().model_id, "claude-sonnet-5-5")

    def test_switched_off_the_person_reads_and_deletes_but_sends_nothing(self):
        """Off: listed, read, deleted; nothing sent, nothing confirmed."""
        self._accepted(self.dev)
        with trap_jobs():
            result, _error, _text = self._rpc(self.dev.login, "send", text="Hello")
        conversation_id = result["conversation"]["id"]
        self._configure(users=[self.dev], enabled=False)
        self.assertEqual(
            self._rpc(self.dev.login, "status")[0],
            {"available": False, "conversations": 1},
        )
        for path, params in (
            ("send", {"text": "Again", "conversation_id": conversation_id}),
            ("confirm", {"message_id": 0}),
            ("retry", {"conversation_id": conversation_id}),
        ):
            with self.subTest(path):
                result, error, _text = self._rpc(self.dev.login, path, **params)
                self.assertIsNone(result)
                self.assertTrue(error)
        listed, _error, _text = self._rpc(self.dev.login, "conversations")
        self.assertEqual([c["id"] for c in listed["conversations"]], [conversation_id])
        read, _error, _text = self._rpc(
            self.dev.login, "conversation", conversation_id=conversation_id
        )
        self.assertEqual(read["messages"][0]["text"], "Hello")
        self.assertTrue(
            self._rpc(self.dev.login, "delete", conversation_id=conversation_id)[0][
                "ok"
            ]
        )

    def test_writing_too_fast_is_refused(self):
        """Past the messages of a minute: refused, said; the bucket is the person's.

        Under ``test_enable`` the counter lets everything through
        (``cloud.rate.limit.hit``): its answer is given here.
        """
        self._accepted(self.dev)
        allowed = iter([True, True, False])
        buckets = []

        def hit(_rate_limit, bucket, **_limits):
            """Count the send bucket: two allowed, then over; any other passes."""
            if not bucket.startswith("assistant_send_user:"):
                return True
            buckets.append(bucket)
            return next(allowed)

        RateLimit = type(self.env["cloud.rate.limit"])
        with (
            patch.object(RateLimit, "hit", autospec=True, side_effect=hit),
            trap_jobs(),
        ):
            answers = [
                self._rpc(self.dev.login, "send", text=f"Hello {n}")[0]
                for n in range(3)
            ]
        self.assertEqual([a["ok"] for a in answers], [True, True, False])
        self.assertIn("too fast", answers[2]["error"])
        self.assertEqual(set(buckets), {f"assistant_send_user:{self.dev.id}"})

    def test_under_a_monthly_limit_an_unpriced_model_is_shown_disabled_and_refused(
        self,
    ):
        """``usable`` false in the status; picking it, refused with why."""
        self._configure(
            users=[self.dev],
            models=("claude-opus-5-5", "claude-new-x"),
            allowed=("claude-opus-5-5", "claude-new-x"),
            limit=10.0,
        )
        status, _error, _text = self._rpc(self.dev.login, "status")
        self.assertEqual(
            {m["id"]: m["usable"] for m in status["models"]},
            {"claude-opus-5-5": True, "claude-new-x": False},
        )
        conversation = (
            self.env["incubacloud.assistant.conversation"].with_user(self.dev)._start()
        )
        refused, _error, _text = self._rpc(
            self.dev.login,
            "set_model",
            conversation_id=conversation.id,
            model_id="claude-new-x",
        )
        self.assertFalse(refused["ok"])
        self.assertIn("monthly limit", refused["error"])
        self._accepted(self.dev)
        sent, _error, _text = self._rpc(
            self.dev.login, "send", text="Hi", model_id="claude-new-x"
        )
        self.assertFalse(sent["ok"])

    def test_the_mcp_never_calls_the_chat_s_routes(self):
        """Every route of the chat is excluded from the MCP's tools, with its reason."""
        policies = self.env["incubacloud.mcp.catalog"]._route_policies()
        for path in (
            "status",
            "send",
            "confirm",
            "cancel",
            "stop",
            "retry",
            "delete",
            "conversation",
            "conversations",
            "set_model",
            "accept_notice",
        ):
            with self.subTest(path):
                self.assertEqual(policies[f"/cloud/assistant/{path}"][0], "excluded")
