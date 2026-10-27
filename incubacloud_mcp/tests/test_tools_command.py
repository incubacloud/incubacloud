"""Commands: the terminal's controls, a minute at most, every one audited."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import asyncssh

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport
from odoo.addons.incubacloud_mcp.protocol.redact import MASK

from .common import (
    FORM_CAPABILITIES,
    PG_PASSWORD,
    ConfirmMixin,
    McpHttpMixin,
    McpUsersMixin,
    PanelFixtureMixin,
)

#: Where an unexpected failure is logged, with its reference.
SAFE_ERROR_LOGGER = "odoo.addons.incubacloud.controllers._safe_error"


@tagged("post_install", "-at_install")
class TestCommandTools(
    ConfirmMixin, PanelFixtureMixin, McpUsersMixin, McpHttpMixin, HttpCase
):
    """run_command and run_host_command, over a stubbed SSH transport."""

    def setUp(self):
        """Both instances deployed and running; a developer's agent; SSH stubbed."""
        super().setUp()
        self._resource_here()
        self._panel_fixture()
        for instance in (self.production, self.staging):
            # The lifecycle moves through its own transitions only.
            instance._transition("deploying")
            instance._transition("deployed")
            instance.sudo().write({"running": True, "compose_services": "odoo,db"})
        self.dev = self._cloud_user("mcp-cmd-dev")
        self.key = self._api_key(self.dev)
        self.transport = MagicMock(spec=SSHTransport)

        async def execute(command, on_stdout, on_stderr, timeout=None):
            """Answer like a shell that prints the database password."""
            await on_stdout(f"PGPASSWORD={PG_PASSWORD}")
            await on_stderr("warning: none")
            return CommandResult("", 0)

        self.transport.execute.side_effect = execute

        @asynccontextmanager
        async def _open(host):
            """Yield the stub transport."""
            yield self.transport

        patcher = patch.object(
            type(self.env["cloud.host"]),
            "get_transport",
            autospec=True,
            side_effect=_open,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _audit(self, action):
        """The latest audit row of *action*."""
        return self.env["cloud.audit.log"].search([("action", "=", action)], limit=1)

    def test_a_developer_runs_a_command_on_a_staging_without_asking(self):
        """A staging runs the command unasked, in its odoo container, within
        the minute, its output masked and the command audited."""
        result = self._call_tool(
            self.key,
            "run_command",
            {"instance_id": self.staging.id, "command": "env | grep PG"},
            FORM_CAPABILITIES,
        )
        payload = self._payload(result)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["stdout"], f"PGPASSWORD={MASK}")
        self.assertEqual(payload["stderr"], "warning: none")
        command = self.transport.execute.call_args.args[0]
        self.assertIn(
            "docker compose -f test.yaml exec -T odoo timeout -k 5 60 "
            "sh -c 'env | grep PG'",
            command,
        )
        self.assertTrue(command.startswith("cd ~/mcpacme/mcp-acme-staging && "))
        row = self._audit("Agent: run_command")
        self.assertEqual(
            (row.details, row.instance_id), ("env | grep PG", self.staging)
        )
        self.assertIn("(test-agent)", row.via_client)

    def test_production_asks_with_the_exact_command_each_time(self):
        """Production asks, showing the exact command, before anything runs;
        once approved it runs once, and the same command asks again."""
        args = {
            "instance_id": self.production.id,
            "command": "odoo shell -c /etc/odoo.conf",
        }
        first = self._call_tool(self.key, "run_command", args, FORM_CAPABILITIES)
        self.assertIn(
            "odoo shell -c /etc/odoo.conf",
            first["inputRequests"]["confirm"]["params"]["message"],
        )
        self.transport.execute.assert_not_called()
        self.assertFalse(self._confirmed_call(self.key, "run_command", args)["isError"])
        self.transport.execute.assert_called_once()
        self.assertIn("-f prod.yaml", self.transport.execute.call_args.args[0])
        again = self._call_tool(self.key, "run_command", args, FORM_CAPABILITIES)
        self.assertEqual(again["resultType"], "input_required")
        self.transport.execute.assert_called_once()

    def test_a_consultant_cannot(self):
        """A consultant is refused before any SSH."""
        consultant = self._cloud_user(
            "mcp-cmd-consultant", group="group_cloud_consultant"
        )
        self.project.member_ids = [(4, consultant.id)]
        result = self._call_tool(
            self._api_key(consultant),
            "run_command",
            {"instance_id": self.staging.id, "command": "id"},
        )
        self.assertTrue(result["isError"])
        self.transport.execute.assert_not_called()

    def test_a_stopped_instance_is_refused(self):
        """An instance that is not running is refused, as the terminal does."""
        self.staging.sudo().running = False
        result = self._call_tool(
            self.key, "run_command", {"instance_id": self.staging.id, "command": "id"}
        )
        self.assertTrue(result["isError"])
        self.assertIn("not running", result["content"][0]["text"])
        self.transport.execute.assert_not_called()

    def test_a_secret_in_the_command_is_masked_in_the_audit(self):
        """The audit keeps the whole command, its secrets masked."""
        self._call_tool(
            self.key,
            "run_command",
            {"instance_id": self.staging.id, "command": f"echo {PG_PASSWORD}"},
        )
        self.assertEqual(self._audit("Agent: run_command").details, f"echo {MASK}")

    def test_a_lost_connection_is_an_error_and_stays_audited(self):
        """A connection lost on the way answers an error; the audit row of
        the command stays."""
        self.transport.execute.side_effect = asyncssh.ConnectionLost("gone")
        with self.assertLogs(SAFE_ERROR_LOGGER, level="ERROR"):
            result = self._call_tool(
                self.key,
                "run_command",
                {"instance_id": self.staging.id, "command": "id"},
            )
        self.assertTrue(result["isError"])
        self.assertEqual(self._audit("Agent: run_command").details, "id")

    def test_a_command_past_its_minute_says_so(self):
        """A command the transport gives up on is reported as timed out."""
        self.transport.execute.side_effect = asyncio.TimeoutError()
        payload = self._payload(
            self._call_tool(
                self.key,
                "run_command",
                {"instance_id": self.staging.id, "command": "sleep 600"},
            )
        )
        self.assertEqual((payload["exit_code"], payload["timed_out"]), (None, True))

    def test_a_long_output_is_cut_and_says_so(self):
        """An output far over 64 KB is cut, and the answer says so."""

        async def chatty(command, on_stdout, on_stderr, timeout=None):
            """Print a megabyte."""
            for _line in range(1000):
                await on_stdout("x" * 1000)
            return CommandResult("", 0)

        self.transport.execute.side_effect = chatty
        result = self._call_tool(
            self.key, "run_command", {"instance_id": self.staging.id, "command": "yes"}
        )
        payload = self._payload(result)
        self.assertTrue(payload["stdout_truncated"])
        self.assertLessEqual(len(result["content"][-1]["text"].encode()), 64 * 1024)

    def test_a_host_command_always_asks_with_its_text(self):
        """A server command asks, with the exact command, and runs as the
        server's SSH user once approved."""
        manager_key = self._api_key(
            self._cloud_user("mcp-cmd-mgr", group="group_cloud_manager")
        )
        args = {"host_id": self.host.id, "command": "df -h"}
        first = self._call_tool(
            manager_key, "run_host_command", args, FORM_CAPABILITIES
        )
        self.assertIn("df -h", first["inputRequests"]["confirm"]["params"]["message"])
        self.transport.execute.assert_not_called()
        payload = self._payload(
            self._confirmed_call(manager_key, "run_host_command", args)
        )
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(
            self.transport.execute.call_args.args[0],
            "timeout -k 5 60 sh -c 'df -h'",
        )
        row = self._audit("Agent: run_host_command")
        self.assertEqual((row.details, row.host_id), ("df -h", self.host))

    def test_a_developer_cannot_run_a_host_command(self):
        """Server commands are a manager's: a developer is refused."""
        result = self._call_tool(
            self.key,
            "run_host_command",
            {"host_id": self.host.id, "command": "id"},
            FORM_CAPABILITIES,
        )
        self.assertTrue(result["isError"])
        self.transport.execute.assert_not_called()
