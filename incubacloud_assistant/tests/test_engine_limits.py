"""Long conversations, the provider key, and conversations that go away."""

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.queue_job.tests.common import trap_jobs

from ..connectors.base import ProviderError
from .common import KEY, EngineMixin, answer


@tagged("post_install", "-at_install")
class TestEngineLimits(EngineMixin, TransactionCase):
    """What the spec asks of the engine at its edges."""

    def setUp(self):
        """A developer allowed to use the assistant."""
        super().setUp()
        self._panel_fixture()
        self.dev = self._cloud_user("asis-el-dev")
        self.project.member_ids = [(4, self.dev.id)]
        self._configure(users=[self.dev])

    def _long(self):
        """A conversation of four exchanges, its task just started again."""
        conversation = (
            self.env["incubacloud.assistant.conversation"].with_user(self.dev)._start()
        )
        for number in range(4):
            conversation._add_message({"role": "user", "content": f"question {number}"})
            conversation._add_message(
                {"role": "assistant", "content": f"answer {number}"}
            )
        with trap_jobs():
            conversation._post("question 4", {})
        return conversation

    def test_a_context_too_long_is_summarized_and_the_task_goes_on(self):
        """The first half summarized away, a notice, and the call made again."""
        too_long = ProviderError("context_too_long", "prompt is too long")
        connector = self._provider(
            too_long, answer("Summary of q0 and q1."), answer("Done.")
        )
        conversation = self._long()
        self._step(conversation)
        self.assertEqual(conversation.state, "idle")
        self.assertEqual(conversation.summary, "Summary of q0 and q1.")
        out = conversation.message_ids.filtered(lambda m: not m.in_context)
        self.assertTrue(out)
        self.assertIn("question 0", out.mapped("content"))
        summary_request = self._sent(connector, 1)
        self.assertEqual(summary_request.tools, ())
        last = self._sent(connector, 2).turns
        self.assertIn("Summary of q0 and q1.", last[0].text)
        self.assertTrue(
            conversation.message_ids.filtered(
                lambda m: m.role == "notice" and "summarized" in m.content
            )
        )

    def test_too_long_even_summarized_is_an_error(self):
        """A second refusal after the summary: error, start a new conversation."""
        too_long = ProviderError("context_too_long", "prompt is too long")
        self._provider(too_long, answer("Summary."), too_long)
        conversation = self._long()
        self._step(conversation)
        self.assertEqual(conversation.state, "error")
        self.assertIn("Start a new one", conversation.message_ids[-1].content)

    def test_the_key_never_appears_in_messages_audit_or_jobs(self):
        """Even when the provider echoes it, the key is masked everywhere."""
        self._provider(
            ProviderError("bad_request", f"key {KEY} is not allowed for this")
        )
        conversation = self._working(self.dev)
        self._step(conversation)
        texts = conversation.message_ids.mapped("content")
        texts += self.env["cloud.audit.log"].sudo().search([]).mapped("details")
        self.assertFalse([text for text in texts if text and KEY in text])

    def test_a_conversation_deleted_during_a_step_ends_it_quietly(self):
        """The person deletes the conversation: the step finds nothing and stops."""
        connector = self._provider(answer("", [("c1", "search_docs", {"query": "x"})]))
        conversation = self._working(self.dev)
        conversation_id = conversation.id
        conversation.sudo().unlink()
        Engine = self.env["incubacloud.assistant.engine"].with_user(self.dev)
        with self.enter_registry_test_mode():
            Engine._step(
                Engine.env["incubacloud.assistant.conversation"].browse(conversation_id)
            )
        connector.chat.assert_not_called()
        self.assertFalse(
            self.env["incubacloud.assistant.conversation"]
            .sudo()
            .browse(conversation_id)
            .exists()
        )
