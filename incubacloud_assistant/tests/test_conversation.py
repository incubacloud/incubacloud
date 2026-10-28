"""Conversations: whose they are, one task at a time, cards, stop, retention."""

import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.queue_job.tests.common import trap_jobs

from ..connectors.base import Usage
from .common import AssistantMixin


@tagged("post_install", "-at_install")
class TestConversation(AssistantMixin, TransactionCase):
    """The data model and what the person can do to a conversation."""

    def setUp(self):
        """Two developers and a manager, all allowed to use the assistant."""
        super().setUp()
        self._panel_fixture()
        self.dev = self._cloud_user("asis-conv-dev")
        self.other = self._cloud_user("asis-conv-other")
        self.manager = self._cloud_user("asis-conv-mgr", group="group_cloud_manager")
        # A developer is a project manager too, who reads every project: the
        # one who must not read the project is a consultant who is no member.
        self.stranger = self._cloud_user(
            "asis-conv-stranger", group="group_cloud_consultant"
        )
        self._configure(users=[self.dev, self.other, self.manager])
        self.Conversation = self.env["incubacloud.assistant.conversation"]

    def _conversation(self, user=None):
        """Open a conversation as *user* (the developer by default)."""
        return self.Conversation.with_user(user or self.dev)._start()

    def _card(self, conversation, tool="delete_project", calls=("c1",)):
        """Pending confirmation cards of *conversation*, one per call, with approvals.

        :return: the first card, or all of them when *calls* has several
        """
        user = conversation.user_id
        conversation.sudo().write({"state": "waiting_confirmation"})
        conversation._add_message(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": json.dumps(
                    [{"id": call, "name": tool, "arguments": {}} for call in calls]
                ),
            }
        )
        cards = self.env["incubacloud.assistant.message"]
        for call in calls:
            approval, _token = (
                self.env["incubacloud.mcp.approval"]
                .with_user(user)
                ._open(
                    tool,
                    f"digest-{call}",
                    "Delete the project.",
                    "assistant",
                    f"assistant:{conversation.id}",
                )
            )
            cards |= conversation._add_message(
                {
                    "role": "card",
                    "content": approval.message,
                    "approval_id": approval.id,
                    "card_state": "pending",
                    "tool_call_id": call,
                    "tool_name": tool,
                }
            )
        cards = cards.with_user(user)
        return cards if len(calls) > 1 else cards[0]

    def test_each_person_reads_only_their_own_a_manager_too(self):
        """The record rule has no group: a manager does not read another's."""
        mine = self._conversation()
        for user in (self.other, self.manager):
            with self.subTest(user.login):
                Conversation = self.Conversation.with_user(user)
                self.assertFalse(Conversation.search([("id", "=", mine.id)]))
                with self.assertRaises(AccessError):
                    Conversation.browse(mine.id).read(["name"])

    def test_nobody_writes_a_conversation_through_the_orm(self):
        """Its cost and state are the server's to write."""
        mine = self._conversation()
        with self.assertRaises(AccessError):
            mine.write({"cost": 0.0})

    def test_a_message_starts_a_task_as_its_owner(self):
        """One step queued on the assistant's channel, run as the owner."""
        mine = self._conversation()
        with trap_jobs() as trap:
            mine._post("What is a staging?", {"route": "projects"})
        trap.assert_jobs_count(1)
        job = trap.enqueued_jobs[0]
        self.assertEqual(job.channel, "root.assistant")
        self.assertEqual(job.recordset.env.uid, self.dev.id)
        self.assertFalse(job.recordset.env.su)
        self.assertEqual(mine.state, "working")
        self.assertEqual(mine.name, "What is a staging?")

    def test_one_task_at_a_time(self):
        """While a step runs, what the person writes waits; no second step."""
        mine = self._conversation()
        with trap_jobs() as trap:
            mine._post("First", {})
            message = mine._post("And another thing", {})
        trap.assert_jobs_count(1)
        self.assertTrue(message.pending)
        self.assertEqual(message.sequence, 0)

    def test_the_screen_note_names_what_the_person_sees(self):
        """Instance and project, by name and id; nothing the person cannot read."""
        self.project.member_ids = [(4, self.dev.id)]
        note = self._conversation()._screen_note(
            {
                "route": "instance_detail",
                "instance_id": self.production.id,
                "project_id": self.project.id,
            }
        )
        self.assertIn(f"id {self.production.id}, production", note)
        self.assertIn(self.project.name, note)
        stranger = self.Conversation.with_user(self.stranger)._screen_note(
            {"instance_id": self.production.id}
        )
        self.assertNotIn(self.production.name, stranger)

    def test_confirming_a_card_approves_once_and_goes_on(self):
        """Approved for ten minutes from now; the next step is queued; once only."""
        mine = self._conversation()
        card = self._card(mine)
        card.approval_id.sudo().expires_at = fields.Datetime.now() + timedelta(
            minutes=1
        )
        with trap_jobs() as trap:
            self.assertTrue(card._confirm_card()["ok"])
            self.assertFalse(card._confirm_card()["ok"])
        trap.assert_jobs_count(1)
        approval = card.approval_id.sudo()
        self.assertEqual(approval.state, "approved")
        self.assertGreater(
            approval.expires_at, fields.Datetime.now() + timedelta(minutes=9)
        )
        self.assertEqual(card.card_state, "confirmed")

    def test_with_two_cards_it_goes_on_once_both_are_answered(self):
        """One step, after the last card: never two steps of one task."""
        mine = self._conversation()
        first, second = self._card(mine, calls=("c1", "c2"))
        with trap_jobs() as trap:
            first._confirm_card()
            trap.assert_jobs_count(0)
            second._cancel_card()
        trap.assert_jobs_count(1)

    def test_an_expired_card_tells_the_model(self):
        """Past ten minutes: expired, and the call answered so the model knows."""
        mine = self._conversation()
        card = self._card(mine)
        card.approval_id.sudo().expires_at = fields.Datetime.now() - timedelta(
            minutes=1
        )
        with trap_jobs():
            self.assertFalse(card._confirm_card()["ok"])
        self.assertEqual(card.card_state, "expired")
        answer = mine.message_ids.filtered(lambda m: m.role == "tool")
        self.assertIn("expired", answer.content)

    def test_cancelling_a_card_withdraws_the_approval_and_answers_the_call(self):
        """Cancelled: the approval cannot be used, the model is told."""
        mine = self._conversation()
        card = self._card(mine)
        with trap_jobs() as trap:
            card._cancel_card()
        trap.assert_jobs_count(1)
        self.assertEqual(card.approval_id.sudo().state, "cancelled")
        answer = mine.message_ids.filtered(lambda m: m.role == "tool")
        self.assertEqual((answer.tool_call_id, answer.is_error), ("c1", True))

    def test_writing_instead_of_confirming_cancels_the_card(self):
        """A new message while a card waits: the card is cancelled, the task goes on."""
        mine = self._conversation()
        card = self._card(mine)
        with trap_jobs() as trap:
            message = mine._post("No, do something else", {})
        trap.assert_jobs_count(1)
        self.assertEqual(card.card_state, "cancelled")
        self.assertFalse(message.pending)

    def test_stop_waits_for_the_step_or_stops_at_once(self):
        """Working: flagged for the step's next check. Waiting: stopped now."""
        mine = self._conversation()
        mine.sudo().state = "working"
        mine._stop()
        self.assertTrue(mine.stop_requested)
        self.assertEqual(mine.state, "working")
        waiting = self._conversation()
        waiting.sudo().state = "waiting_job"
        waiting._stop()
        self.assertEqual(waiting.state, "stopped")
        self.assertIn("Stopped", waiting.message_ids[-1].content)

    def test_a_lost_step_is_healed_into_an_error_with_retry(self):
        """Working for more than an hour: error, and Retry queues a step."""
        mine = self._conversation()
        mine.sudo().write(
            {
                "state": "working",
                "step_started_at": fields.Datetime.now() - timedelta(minutes=61),
            }
        )
        mine._heal()
        self.assertEqual(mine.state, "error")
        with trap_jobs() as trap:
            self.assertTrue(mine._retry())
        trap.assert_jobs_count(1)

    def test_thirty_days_without_activity_and_it_is_gone(self):
        """The cron deletes the old, a stuck one too, and keeps the recent."""
        old, recent, stuck = (self._conversation() for _i in range(3))
        long_ago = fields.Datetime.now() - timedelta(days=31)
        old.sudo().last_activity = long_ago
        # A step touches last_activity: working for a month, it is lost.
        stuck.sudo().write({"last_activity": long_ago, "state": "working"})
        self.Conversation._cron_purge()
        self.assertFalse(old.exists() or stuck.exists())
        self.assertTrue(recent.exists())

    def test_a_conversation_that_cannot_be_healed_keeps_none_from_the_purge(self):
        """Healing fails for one: logged, and the old ones still go."""
        old, recent, stuck = (self._conversation() for _i in range(3))
        long_ago = fields.Datetime.now() - timedelta(days=31)
        old.sudo().last_activity = long_ago
        stuck.sudo().write({"last_activity": long_ago, "state": "working"})
        recent.sudo().state = "waiting_job"
        with (
            patch.object(
                type(self.Conversation),
                "_heal",
                autospec=True,
                side_effect=AccessError("The owner lost their role."),
            ) as heal,
            self.assertLogs(
                "odoo.addons.incubacloud_assistant.models.assistant_conversation",
                "ERROR",
            ),
        ):
            self.Conversation._cron_purge()
        self.assertEqual(heal.call_count, 2)
        self.assertFalse(old.exists() or stuck.exists())
        self.assertTrue(recent.exists())

    def test_a_deleted_user_takes_their_conversations(self):
        """The user goes, their conversations and usage with them."""
        user = self._cloud_user("asis-conv-gone")
        self._configure(users=[user])
        mine = self._conversation(user)
        user.sudo().unlink()
        self.assertFalse(mine.exists())

    def test_usage_adds_up_per_user_month_and_model(self):
        """Two calls of one model: one row with their sum; the month's cost."""
        Usage_ = self.env["incubacloud.assistant.usage"]
        for _i in range(2):
            Usage_._add(
                self.dev.id, "anthropic", "claude-opus-5-5", Usage(100, 10), 0.5
            )
        Usage_._add(self.dev.id, "anthropic", "mystery-model", Usage(7, 3), None)
        cost, unpriced = Usage_._month_cost(self.dev.id)
        self.assertAlmostEqual(cost, 1.0)
        self.assertTrue(unpriced)
        rows = Usage_.sudo().search([("user_id", "=", self.dev.id)])
        self.assertEqual(sorted(rows.mapped("input_tokens")), [7, 200])
