"""Commands without a terminal: one command, its output, a minute at most.

``run_command`` runs in the instance's ``odoo`` service with the controls
of ``terminal_open``: developer, deployed and running, the service among
``_shell_services()``, the same rate buckets. ``run_host_command`` runs on
the server as its SSH user, the user the host console logs in as, with
the console's controls; it always asks. A production instance asks too,
for each command, bound to its exact text.

The minute is kept where the command runs: ``timeout`` inside the
container (a ``docker exec`` whose client is killed leaves its process
running), and around the client and the SSH session as a backstop.
Before the SSH, everything the command writes is committed in
transactions of their own: the buckets, the approval it uses, its audit
row (the whole command, secrets masked) and the staging's clock. A
worker that dies while it runs leaves it audited and its approval spent,
and the request holds no lock on the instance while it runs.
"""

import asyncio
import shlex

import asyncssh

from odoo import api, models
from odoo.exceptions import UserError

from odoo.addons.incubacloud.controllers._safe_error import safe_error_response
from odoo.addons.incubacloud.controllers.async_utils import run_async

from ..protocol.limits import MAX_BYTES, cap_text
from ..protocol.tool_spec import ToolSpec, obj, record_id, string
from .mcp_tools import quoted

#: Seconds a command may run.
COMMAND_TIMEOUT = 60
#: Seconds the processes get, after the timeout, to end before being killed.
_KILL_AFTER = 5
#: Seconds the docker client and the SSH session wait at most, past which
#: the command is given up on even if ``timeout`` did not end it.
_CLIENT_TIMEOUT = COMMAND_TIMEOUT + 15
_SSH_TIMEOUT = _CLIENT_TIMEOUT + 15
#: What the answer shows of each stream; the two fit in 64 KB.
_STDERR_BYTES = 8 * 1024
_STDOUT_BYTES = MAX_BYTES - _STDERR_BYTES - 2048
#: Characters kept of each stream while the command runs.
_KEEP_CHARS = 2 * MAX_BYTES
#: Characters read at a time from each stream.
_READ_SIZE = 65536
#: The exit statuses of ``timeout`` when it ended the command: stopped
#: (124), or killed once its grace ran out (137).
_TIMED_OUT = (124, 137)
#: The tools whose approval ends with the command, quoted, on a line of its
#: own: the approval page shows it in a block of its own.
COMMAND_TOOLS = frozenset({"run_command", "run_host_command"})

TOOLS = (
    ToolSpec(
        "run_command",
        "Run one non-interactive shell command in an instance's odoo container "
        "(60 seconds at most) and get stdout, stderr and the exit code. The "
        "minute stops the command, not what it leaves running in the "
        "background. On production the user approves each command.",
        "group_cloud_developer",
        obj(
            {
                "instance_id": record_id("The instance."),
                "command": string("The shell command.", max_length=4000),
            },
            required=("instance_id", "command"),
        ),
        handler="_tool_run_command",
        confirm="conditional",
        untrusted=True,
        audit=False,
    ),
    ToolSpec(
        "run_host_command",
        "Run one non-interactive shell command on a server, as its SSH user (60 "
        "seconds at most; what it leaves running in the background is not "
        "stopped). The user approves each command.",
        "group_cloud_manager",
        obj(
            {
                "host_id": record_id("The server."),
                "command": string("The shell command.", max_length=4000),
            },
            required=("host_id", "command"),
        ),
        handler="_tool_run_host_command",
        confirm="always",
        untrusted=True,
        audit=False,
    ),
)


def _limited(command):
    """Return *command* run by ``sh`` under the minute's ``timeout``.

    One quoted word after ``--``: the shell runs it, and reads none of it
    (a leading dash included) as its own options.
    """
    return f"timeout -k {_KILL_AFTER} {COMMAND_TIMEOUT} sh -c -- {shlex.quote(command)}"


class McpCatalogCommand(models.AbstractModel):
    """The command tools."""

    _inherit = "incubacloud.mcp.catalog"

    @api.model
    def _tool_specs(self):
        """Add the command tools (they call no route, so they classify none)."""
        return super()._tool_specs() + list(TOOLS)


class McpToolsCommand(models.AbstractModel):
    """What the command tools check, ask and run."""

    _inherit = "incubacloud.mcp.tools"

    @api.model
    def _command_instance(self, instance_id):
        """Return the instance a command may run in, as terminal_open checks it.

        :raises AccessError: below developer, or an instance the user may
            not read
        :raises UserError: an instance without a host, not running, or
            without an odoo service
        """
        self.env["cloud.security.mixin"]._check_can_use_terminal()
        instance = self._instance(instance_id)
        if not instance.host_id:
            raise UserError(self.env._("Instance has no host"))
        if not instance.deployed or not instance.running:
            raise UserError(self.env._("Instance is not running"))
        if "odoo" not in instance._shell_services():
            raise UserError(self.env._("Unknown service"))
        return instance

    @api.model
    def _command_host(self, host_id):
        """Return the server a command may run on, as host_terminal_open checks it.

        :raises AccessError: below manager
        """
        self.env["cloud.security.mixin"]._check_can_manage_hosts()
        return self._record("cloud.host", host_id, self.env._("Host not found"))

    @api.model
    def _confirm_run_command(self, args):
        """Ask on production only, with the exact command, quoted, last."""
        instance = self._command_instance(args["instance_id"])
        if instance.environment != "production":
            return None
        intro = self.env._(
            "Run this command in the production instance %(name)s:",
            name=quoted(instance.name),
        )
        return f"{intro}\n{quoted(args['command'])}"

    @api.model
    def _confirm_run_host_command(self, args):
        """Always ask, with the exact command, quoted, last."""
        host = self._command_host(args["host_id"])
        intro = self.env._(
            "Run this command on the server %(name)s (%(ip)s):",
            name=quoted(host.name),
            ip=host.ip_address,
        )
        return f"{intro}\n{quoted(args['command'])}"

    @api.model
    def _before_command(self, vals, command, instance=None):
        """Write, and commit, what must outlive the command, before the SSH.

        The command's audit row (secrets masked) and, for an instance, its
        staging clock go in a transaction of their own, committed now: a
        worker that dies while the command runs leaves it audited, and the
        instance's row is not held locked for the minute it may run.

        :param dict vals: the audit row, but its details
        :param str command: the command, as the agent sent it
        :param instance: the instance it runs in, if any
        """
        details = self.env["incubacloud.mcp.secrets"]._scrubber().text(command)
        with self.env.registry.cursor() as cr:
            env = self.env(cr=cr)
            env["cloud.audit.log"].sudo().create({**vals, "details": details})
            if instance:
                instance.with_env(env)._touch_autopurge_clock()
            cr.commit()

    @api.model
    def _tool_run_command(self, args):
        """Run the command in the instance's odoo service."""
        instance = self._command_instance(args["instance_id"])
        uid = self.env.uid
        if not (
            self._consume_quota(
                f"terminal_user:{uid}", cap_key="rate_limit_terminal_user_per_min"
            )
            and self._consume_quota(
                f"terminal_instance:{instance.id}",
                cap_key="rate_limit_terminal_per_min",
            )
        ):
            return {
                "ok": False,
                "error": self.env._(
                    "Too many commands recently. Try again in a minute."
                ),
            }
        self._before_command(
            {"action": "Agent: run_command", "instance_id": instance.id},
            args["command"],
            instance,
        )
        remote_dir = instance.get_remote_dir()
        safe_dir = (
            "~/" + shlex.quote(remote_dir[2:])
            if remote_dir.startswith("~/")
            else shlex.quote(remote_dir)
        )
        compose = "prod.yaml" if instance.environment == "production" else "test.yaml"
        command = (
            f"cd {safe_dir} && timeout -k {_KILL_AFTER} {_CLIENT_TIMEOUT} "
            f"docker compose -f {compose} exec -T odoo {_limited(args['command'])}"
        )
        return self._ssh_capture(instance.host_id, command)

    @api.model
    def _tool_run_host_command(self, args):
        """Run the command on the server, as its SSH user."""
        host = self._command_host(args["host_id"])
        uid = self.env.uid
        if not (
            self._consume_quota(
                f"host_console_user:{uid}", cap_key="rate_limit_host_console_per_min"
            )
            and self._consume_quota(
                f"host_console:{host.id}", cap_key="rate_limit_host_console_per_min"
            )
        ):
            return {
                "ok": False,
                "error": self.env._(
                    "Too many commands recently. Try again in a minute."
                ),
            }
        self._before_command(
            {"action": "Agent: run_host_command", "host_id": host.id}, args["command"]
        )
        return self._ssh_capture(host, _limited(args["command"]))

    @api.model
    def _ssh_capture(self, host, command):
        """Run *command* on *host*; return exit code, stdout and stderr, cut.

        The streams are read a block at a time: what passes the part kept
        is read and dropped, so a command's output costs a bounded amount
        of memory however much it prints. A connection that fails or is
        lost answers ``{'ok': False}``, which keeps the audit row already
        written.

        :return: ``{ok, exit_code, timed_out, stdout, stdout_truncated,
            stderr, stderr_truncated}``; ``exit_code`` is None when the
            SSH session gave up on the command
        """
        connect_kw = host.sudo().ssh_connect_kwargs()
        connect_kw.update(keepalive_interval=30, keepalive_count_max=10)
        kept = {"stdout": [], "stderr": []}
        sizes = {"stdout": 0, "stderr": 0}
        dropped = set()

        async def _drain(name, stream):
            """Read *stream* to its end, keeping what fits and dropping the rest."""
            while chunk := await stream.read(_READ_SIZE):
                room = _KEEP_CHARS - sizes[name]
                if len(chunk) > room:
                    dropped.add(name)
                    chunk = chunk[: max(room, 0)]
                if chunk:
                    kept[name].append(chunk)
                    sizes[name] += len(chunk)

        started = []

        async def _run():
            """Connect, run the command, read both streams; return its exit status.

            The process is awaited until its channel closes: its output is
            read by the two drains alone.
            """
            async with asyncssh.connect(**connect_kw) as conn:
                process = await conn.create_process(
                    command, encoding="utf-8", errors="replace"
                )
                started.append(process)
                await asyncio.wait_for(
                    asyncio.gather(
                        _drain("stdout", process.stdout),
                        _drain("stderr", process.stderr),
                        process.wait_closed(),
                    ),
                    timeout=_SSH_TIMEOUT,
                )
                return process.exit_status

        # A timeout is an OSError too: it is caught first — before the
        # command started, the server could not be reached in time (the
        # connection's or the login's own timeout). Anything else answers
        # {'ok': False}: the command may have run, and its audit row,
        # already committed, says so.
        try:
            exit_code = run_async(_run())
            timed_out = exit_code in _TIMED_OUT
        except asyncio.TimeoutError:
            if not started:
                return {
                    "ok": False,
                    "error": self.env._("Could not connect to the server in time."),
                }
            exit_code, timed_out = None, True
        except Exception as exc:  # noqa: BLE001 - shown through safe_error_response
            return safe_error_response(
                exc, self.env._("The command could not be run on the server.")
            )
        out = cap_text("".join(kept["stdout"]), max_bytes=_STDOUT_BYTES)
        err = cap_text("".join(kept["stderr"]), max_bytes=_STDERR_BYTES)
        return {
            "ok": True,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "stdout": out["text"],
            "stdout_truncated": out["truncated"] or "stdout" in dropped,
            "stderr": err["text"],
            "stderr_truncated": err["truncated"] or "stderr" in dropped,
        }
