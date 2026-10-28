"""A tool the assistant runs from a queue job runs as the user, never as root."""

from werkzeug.test import EnvironBuilder

from odoo import http
from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from ..request_scope import ASSISTANT_VERSION, call_context, tool_request
from .common import AssistantMixin

VIA = "Assistant (conversation 7)"


@tagged("post_install", "-at_install")
class TestToolRequest(AssistantMixin, TransactionCase):
    """The request bound while a tool runs is the user's own."""

    def setUp(self):
        """A consultant member of the project and one who is not."""
        super().setUp()
        self._panel_fixture()
        self.member = self._cloud_user(
            "asis-req-member", group="group_cloud_consultant"
        )
        self.outsider = self._cloud_user("asis-req-out", group="group_cloud_consultant")
        self.project.member_ids = [(4, self.member.id)]

    def _runner_request(self):
        """The job runner's request: ``/queue_job/runjob``, the superuser's env."""
        environ = EnvironBuilder(path="/queue_job/runjob").get_environ()
        runner = http.Request(http.HTTPRequest(environ))
        runner.env = self.env(su=True)
        http._request_stack.push(runner)
        self.addCleanup(http._request_stack.pop)
        return runner

    def test_a_route_runs_as_the_user_not_as_the_job_runner(self):
        """Inside, the panel's route reads the user's env; after, the runner's again."""
        runner = self._runner_request()
        env = self.env(user=self.member)
        with tool_request(env, VIA) as bound:
            self.assertIs(http.request._get_current_object(), bound)
            self.assertEqual(http.request.env.uid, self.member.id)
            self.assertFalse(http.request.env.su)
            answer = env["incubacloud.mcp.dispatch"]._call(
                "/cloud/get_project", {"project_id": self.project.id}
            )
            self.assertEqual(answer["id"], self.project.id)
        self.assertIs(http.request._get_current_object(), runner)

    def test_what_the_user_cannot_see_stays_unseen(self):
        """A project the user is not a member of: the route refuses it."""
        self._runner_request()
        env = self.env(user=self.outsider)
        with tool_request(env, VIA), self.assertRaises(AccessError):
            env["incubacloud.mcp.dispatch"]._call(
                "/cloud/get_project", {"project_id": self.project.id}
            )

    def test_the_website_routing_key_is_bound(self):
        """With the website module, the routing map finds the request's website."""
        if "website" not in self.env.registry:
            self.skipTest("The website module is not installed.")
        with tool_request(self.env(user=self.member), VIA) as bound:
            current = self.env["website"].sudo().get_current_website()
            self.assertEqual(bound.website_routing, current.id)

    def test_never_as_the_superuser(self):
        """A superuser environment is refused before anything is bound."""
        with self.assertRaises(ValueError), tool_request(self.env(su=True), VIA):
            pass

    def test_what_a_tool_writes_is_marked_as_the_assistant_s(self):
        """An audit row written inside carries the conversation's mark."""
        env = self.env(user=self.member)
        with tool_request(env, VIA):
            row = (
                env["cloud.audit.log"].sudo().create({"action": "probe", "details": ""})
            )
        self.assertEqual(row.via_client, VIA)

    def test_a_card_is_never_confirmed_from_inside_a_tool(self):
        """The mark of the request refuses the confirmation; the approval waits."""
        self._configure(users=[self.member])
        conversation = (
            self.env["incubacloud.assistant.conversation"]
            .with_user(self.member)
            ._start()
        )
        approval, _token = (
            self.env["incubacloud.mcp.approval"]
            .with_user(self.member)
            ._open(
                "delete_project",
                "h",
                "Delete.",
                "assistant",
                f"assistant:{conversation.id}",
            )
        )
        card = conversation._add_message(
            {
                "role": "card",
                "approval_id": approval.id,
                "card_state": "pending",
                "tool_call_id": "c1",
                "tool_name": "delete_project",
            }
        )
        with tool_request(self.env(user=self.member), VIA):
            self.assertFalse(card._confirm_card()["ok"])
        self.assertEqual(approval.state, "pending")

    def test_the_call_context_binds_approvals_to_the_conversation(self):
        """The conversation is the credential the approvals are bound to."""
        ctx = call_context(5, 42)
        self.assertEqual(ctx.version, ASSISTANT_VERSION)
        self.assertFalse(ctx.modern)
        self.assertEqual(ctx.principal.client, "assistant:42")
        self.assertEqual(ctx.via(), "Assistant (conversation 42)")
