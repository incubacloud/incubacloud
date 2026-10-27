"""A destructive call runs only after its user approves it — once."""

import re
from unittest.mock import patch

import psycopg2

from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase
from odoo.tools import mute_logger

from odoo.addons.incubacloud_mcp.protocol.context import CallContext, Principal
from odoo.addons.incubacloud_mcp.protocol.signing import verify
from odoo.addons.incubacloud_mcp.protocol.versions import MODERN

from .common import (
    ACCEPT,
    FORM_CAPABILITIES,
    URL_CAPABILITIES,
    ConfirmMixin,
    McpHttpMixin,
    McpUsersMixin,
    PanelFixtureMixin,
)

_LINK_RE = re.compile(r"https?://\S+/cloud/mcp/approve/[A-Za-z0-9_-]+")


@tagged("post_install", "-at_install")
class TestConfirmation(
    ConfirmMixin, PanelFixtureMixin, McpUsersMixin, McpHttpMixin, HttpCase
):
    """Each channel asks with the exact message and runs the call once."""

    def setUp(self):
        """A manager with an API key, and the module's key to read states."""
        super().setUp()
        self._resource_here()
        self.manager = self._cloud_user("mcp-confirm-mgr", group="group_cloud_manager")
        self.key = self._api_key(self.manager)
        self.secret = self.env["cloud.settings"]._mcp_secret()

    def _project(self, name="mcp-doomed"):
        """An empty project to delete."""
        return self.env["cloud.project"].create(
            {"name": name, "remote_folder": name.replace("-", "")}
        )

    def _approval_of(self, request_state):
        """The approval a request state points at."""
        payload = verify(self.secret, "request", request_state)
        return self.env["incubacloud.mcp.approval"].browse(payload["a"])

    def test_the_form_asks_with_the_exact_message_and_accepting_runs_it(self):
        """The form says exactly what happens; accepting it runs the call."""
        project = self._project()
        args = {"project_id": project.id}
        first = self._call_tool(self.key, "delete_project", args, FORM_CAPABILITIES)
        self.assertEqual(first["resultType"], "input_required")
        request = first["inputRequests"]["confirm"]
        self.assertEqual(request["method"], "elicitation/create")
        self.assertEqual(request["params"]["mode"], "form")
        self.assertEqual(
            request["params"]["message"],
            'Delete the project "mcp-doomed". This cannot be undone.',
        )
        self.assertEqual(
            request["params"]["requestedSchema"]["properties"]["confirm"]["type"],
            "boolean",
        )
        self.assertTrue(project.exists())
        final = self._call_tool(
            self.key,
            "delete_project",
            args,
            FORM_CAPABILITIES,
            requestState=first["requestState"],
            inputResponses=ACCEPT,
        )
        self.assertFalse(final["isError"], final)
        self.assertFalse(project.exists())

    def test_an_always_tool_whose_message_comes_out_empty_still_asks(self):
        """A tool that always asks, whose own message comes out empty, asks
        with a generic one: it never runs unasked."""
        project = self._project()
        Tools = type(self.env["incubacloud.mcp.tools"])
        with patch.object(
            Tools, "_confirm_delete_project", autospec=True, return_value=""
        ):
            result = self._call_tool(
                self.key, "delete_project", {"project_id": project.id}
            )
        self.assertTrue(result["isError"])
        self.assertIn("This action needs your approval", result["content"][0]["text"])
        self.assertTrue(project.exists())

    def test_the_same_state_twice_does_not_run_twice(self):
        """A request state already used runs nothing the second time."""
        project = self._project()
        args = {"project_id": project.id}
        first = self._call_tool(self.key, "delete_project", args, FORM_CAPABILITIES)
        self._call_tool(
            self.key,
            "delete_project",
            args,
            FORM_CAPABILITIES,
            requestState=first["requestState"],
            inputResponses=ACCEPT,
        )
        again = self._call_tool(
            self.key,
            "delete_project",
            args,
            FORM_CAPABILITIES,
            requestState=first["requestState"],
            inputResponses=ACCEPT,
        )
        self.assertTrue(again["isError"])

    def test_a_state_does_not_run_anything_else(self):
        """A request state runs neither other arguments, nor for another
        user, nor when the form said no, nor once tampered with."""
        project, other = self._project(), self._project("mcp-other")
        first = self._call_tool(
            self.key, "delete_project", {"project_id": project.id}, FORM_CAPABILITIES
        )
        outsider = self._api_key(
            self._cloud_user("mcp-confirm-other", group="group_cloud_manager")
        )
        state = first["requestState"]
        body, mac = state.split(".")
        cases = {
            "other arguments": (self.key, {"project_id": other.id}, ACCEPT, state),
            "other user": (outsider, {"project_id": project.id}, ACCEPT, state),
            "tampered": (
                self.key,
                {"project_id": project.id},
                ACCEPT,
                f"{body}.{mac[::-1]}",
            ),
            "confirm false": (
                self.key,
                {"project_id": project.id},
                {"confirm": {"action": "accept", "content": {"confirm": False}}},
                state,
            ),
        }
        for label, (credential, args, responses, request_state) in cases.items():
            with self.subTest(label):
                result = self._call_tool(
                    credential,
                    "delete_project",
                    args,
                    FORM_CAPABILITIES,
                    requestState=request_state,
                    inputResponses=responses,
                )
                self.assertTrue(result["isError"])
        self.assertTrue(project.exists())
        self.assertTrue(other.exists())

    def test_an_answer_that_is_not_one_asks_again(self):
        """Input responses of the wrong shape answer nothing: the form is
        asked again and nothing runs."""
        project = self._project()
        args = {"project_id": project.id}
        first = self._call_tool(self.key, "delete_project", args, FORM_CAPABILITIES)
        for responses in (["confirm"], "confirm", {"confirm": "yes"}):
            with self.subTest(responses=responses):
                result = self._call_tool(
                    self.key,
                    "delete_project",
                    args,
                    FORM_CAPABILITIES,
                    requestState=first["requestState"],
                    inputResponses=responses,
                )
                self.assertEqual(result["resultType"], "input_required")
        self.assertEqual(self._approval_of(first["requestState"]).state, "pending")
        self.assertTrue(project.exists())

    def test_a_state_is_redeemed_only_by_the_client_it_was_given_to(self):
        """Another credential of the same user cannot redeem a request
        state, nor use an approval given on the page to another client."""
        project = self._project()
        args = {"project_id": project.id}
        second_key = self._api_key(self.manager, name="second-client")
        first = self._call_tool(self.key, "delete_project", args, FORM_CAPABILITIES)
        stolen = self._call_tool(
            second_key,
            "delete_project",
            args,
            FORM_CAPABILITIES,
            requestState=first["requestState"],
            inputResponses=ACCEPT,
        )
        self.assertTrue(stolen["isError"])
        text = self._call_tool(self.key, "delete_project", args)["content"][0]["text"]
        token = _LINK_RE.search(text).group(0).rsplit("/", 1)[1]
        self.env["incubacloud.mcp.approval"]._by_token(token).with_user(
            self.manager
        )._decide(True)
        other = self._call_tool(second_key, "delete_project", args)
        self.assertIn(
            "This action needs your approval: open", other["content"][0]["text"]
        )
        self.assertTrue(project.exists())

    def test_declining_cancels(self):
        """Declining the form cancels the approval and runs nothing."""
        project = self._project()
        args = {"project_id": project.id}
        first = self._call_tool(self.key, "delete_project", args, FORM_CAPABILITIES)
        result = self._call_tool(
            self.key,
            "delete_project",
            args,
            FORM_CAPABILITIES,
            requestState=first["requestState"],
            inputResponses={"confirm": {"action": "decline"}},
        )
        self.assertEqual(result["content"][0]["text"], "Operation cancelled.")
        self.assertEqual(self._approval_of(first["requestState"]).state, "cancelled")
        self.assertTrue(project.exists())

    def test_declining_after_the_page_approved_withdraws_it(self):
        """A client that declines after the page approved withdraws the
        approval: the same call asks again."""
        project = self._project()
        args = {"project_id": project.id}
        first = self._call_tool(self.key, "delete_project", args, URL_CAPABILITIES)
        approval = self._approval_of(first["requestState"])
        approval.with_user(self.manager)._decide(True)
        result = self._call_tool(
            self.key,
            "delete_project",
            args,
            URL_CAPABILITIES,
            requestState=first["requestState"],
            inputResponses={"confirm": {"action": "decline"}},
        )
        self.assertEqual(result["content"][0]["text"], "Operation cancelled.")
        self.assertEqual(approval.state, "cancelled")
        again = self._call_tool(self.key, "delete_project", args)
        self.assertIn(
            "This action needs your approval: open", again["content"][0]["text"]
        )
        self.assertTrue(project.exists())

    def test_after_ten_minutes_the_approval_is_gone(self):
        """An approval past its ten minutes runs nothing, even accepted."""
        project = self._project()
        args = {"project_id": project.id}
        first = self._call_tool(self.key, "delete_project", args, FORM_CAPABILITIES)
        self.env.cr.execute(
            "UPDATE incubacloud_mcp_approval SET expires_at = "
            "(now() at time zone 'UTC') - interval '1 minute' WHERE id = %s",
            (self._approval_of(first["requestState"]).id,),
        )
        result = self._call_tool(
            self.key,
            "delete_project",
            args,
            FORM_CAPABILITIES,
            requestState=first["requestState"],
            inputResponses=ACCEPT,
        )
        self.assertTrue(result["isError"])
        self.assertTrue(project.exists())

    def test_a_url_only_client_gets_the_panel_page_and_waits_for_it(self):
        """A client with links only is sent to the panel's page; its retry
        waits until the user approves there, then runs."""
        project = self._project()
        args = {"project_id": project.id}
        first = self._call_tool(self.key, "delete_project", args, URL_CAPABILITIES)
        request = first["inputRequests"]["confirm"]
        self.assertEqual(request["params"]["mode"], "url")
        self.assertRegex(request["params"]["url"], _LINK_RE)
        accepted = {"confirm": {"action": "accept"}}
        waiting = self._call_tool(
            self.key,
            "delete_project",
            args,
            URL_CAPABILITIES,
            requestState=first["requestState"],
            inputResponses=accepted,
        )
        self.assertEqual(waiting["resultType"], "input_required")
        self.assertTrue(project.exists())
        self._approval_of(first["requestState"]).with_user(self.manager)._decide(True)
        done = self._call_tool(
            self.key,
            "delete_project",
            args,
            URL_CAPABILITIES,
            requestState=first["requestState"],
            inputResponses=accepted,
        )
        self.assertFalse(done["isError"], done)
        self.assertFalse(project.exists())

    def test_a_client_without_elicitation_gets_the_link_in_the_text(self):
        """A client that cannot be asked gets the page's link in the text;
        once approved there, asking again runs it."""
        project = self._project()
        args = {"project_id": project.id}
        result = self._call_tool(self.key, "delete_project", args)
        self.assertTrue(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("This action needs your approval: open", text)
        self.assertIn('Delete the project "mcp-doomed".', text)
        self.assertTrue(project.exists())
        token = _LINK_RE.search(text).group(0).rsplit("/", 1)[1]
        self.env["incubacloud.mcp.approval"]._by_token(token).with_user(
            self.manager
        )._decide(True)
        self.assertFalse(self._call_tool(self.key, "delete_project", args)["isError"])
        self.assertFalse(project.exists())

    def test_an_earlier_client_with_url_elicitation_gets_minus_32042_then_retries(
        self,
    ):
        """A 2025-11-25 client that declared links gets -32042 with the page;
        its retry runs once approved, and uses the approval."""
        project = self._project()
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": URL_CAPABILITIES,
                    "clientInfo": {"name": "legacy-agent", "version": "1"},
                },
            },
            self.key,
        )
        session = response.headers["Mcp-Session-Id"]
        body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "delete_project",
                "arguments": {"project_id": project.id},
            },
        }
        error = self._post(body, self.key, {"Mcp-Session-Id": session}).json()["error"]
        self.assertEqual(error["code"], -32042)
        elicitation = error["data"]["elicitations"][0]
        self.assertEqual(elicitation["mode"], "url")
        self.assertRegex(elicitation["url"], _LINK_RE)
        self.assertEqual(
            elicitation["message"],
            'Delete the project "mcp-doomed". This cannot be undone.',
        )
        approval = self.env["incubacloud.mcp.approval"].browse(
            int(elicitation["elicitationId"])
        )
        approval.with_user(self.manager)._decide(True)
        result = self._post(body, self.key, {"Mcp-Session-Id": session}).json()[
            "result"
        ]
        self.assertFalse(result["isError"], result)
        self.assertFalse(project.exists())
        self.assertEqual(approval.state, "used")

    def test_an_approval_is_spent_even_when_the_panel_refuses(self):
        """An approval used on a call the panel then refuses is gone: the
        same call asks for another one."""
        self._panel_fixture()
        args = {"project_id": self.project.id}
        text = self._call_tool(self.key, "delete_project", args)["content"][0]["text"]
        token = _LINK_RE.search(text).group(0).rsplit("/", 1)[1]
        self.env["incubacloud.mcp.approval"]._by_token(token).with_user(
            self.manager
        )._decide(True)
        refused = self._call_tool(self.key, "delete_project", args)
        self.assertTrue(refused["isError"])
        self.assertIn("instance(s)", refused["content"][0]["text"])
        again = self._call_tool(self.key, "delete_project", args)
        self.assertIn(
            "This action needs your approval: open", again["content"][0]["text"]
        )
        self.assertTrue(self.project.exists())


@tagged("post_install", "-at_install")
class TestConfirmationRace(McpUsersMixin, TransactionCase):
    """The retry that loses the race for an approval is retried whole."""

    def test_a_retry_that_fails_to_serialise_is_not_answered(self):
        """A failure to serialise while the approval is used is raised, so
        Odoo retries the request, which then finds the approval used."""
        manager = self._cloud_user("mcp-race-mgr", group="group_cloud_manager")
        project = self.env["cloud.project"].create(
            {"name": "mcp-race", "remote_folder": "mcprace"}
        )
        ctx = CallContext(
            MODERN, True, {}, "test-agent", Principal(manager.id, "API key race")
        )

        def lose_the_race(confirm, *_args):
            """Fail to serialise, as the second of two racing retries does."""
            confirm.env.cr.execute(
                "DO $$ BEGIN RAISE EXCEPTION 'lost the race' "
                "USING ERRCODE = 'serialization_failure'; END $$"
            )

        Confirm = type(self.env["incubacloud.mcp.confirm"])
        server = self.env["incubacloud.mcp.server"].with_user(manager)
        params = {"name": "delete_project", "arguments": {"project_id": project.id}}
        with (
            patch.object(Confirm, "_gate", autospec=True, side_effect=lose_the_race),
            self.assertRaises(psycopg2.errors.SerializationFailure),
            mute_logger("odoo.sql_db"),
            self.env.cr.savepoint(),
        ):
            server._tools_call(params, ctx)
        self.assertTrue(project.exists())
