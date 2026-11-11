"""The host shell's front door: who may open one, and what it leaves behind.

The shell itself is a command-less PTY (``test_host_terminal_session``);
this covers the endpoint in front of it. It is reserved to whoever manages
hosts — a developer has the container terminal and nothing more — it
leaves an audit row and a route for every session, and it refuses a
second open session on the same host.

Also here, because the shell is what introduced it: deleting a host that
was never set up but has shell history archives it instead of failing on
the audit rows' ``ondelete='restrict'``.
"""
import os
import sys
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from werkzeug.user_agent import UserAgent
from werkzeug.wrappers import Request as WerkzeugRequest

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import Request
from odoo.tests.common import BaseCase, TransactionCase, new_test_user, tagged

from odoo.addons.incubacloud.controllers import (
    _rate_limit, host_terminal, terminal, terminal_proxy_mixin,
)
from odoo.addons.incubacloud.controllers import data_load
from odoo.addons.incubacloud.controllers._data_load import _routes_crud
from odoo.modules.module import get_module_path

if get_module_path("incubacloud") not in sys.path:
    sys.path.insert(0, get_module_path("incubacloud"))

import terminal_session_base as session_base       # noqa: E402
import terminal_subprocess_base as subprocess_base  # noqa: E402


class _HostShellCase(TransactionCase):
    """A host, a manager and a developer.

    Subclasses run post_install: creating ``res.users`` at_install trips
    over the ``res_partner.autopost_bills`` NOT NULL column (account
    loads later) — same constraint as ``test_audit_tracked_mixin``.
    """

    def setUp(self):
        """Create the host and one user per role under test."""
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "shell-host",
            "ip_address": "192.0.2.61",
            "user": "ubuntu",
            "wildcard_domain": "shell.example.com",
        })
        self.manager = new_test_user(
            self.env,
            login="host_shell_manager",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        self.developer = new_test_user(
            self.env,
            login="host_shell_developer",
            groups="base.group_user,incubacloud.group_cloud_developer",
        )


@tagged("post_install", "-at_install")
class TestHostShellOpen(_HostShellCase):
    """``/cloud/host_terminal/open`` behind the manage-hosts gate."""

    def setUp(self):
        """Stub the two things a test cannot do: SSH and spawning."""
        super().setUp()
        self.controller = host_terminal.HostTerminalController()
        Host = type(self.env["cloud.host"])
        kwargs_patch = patch.object(
            Host, "ssh_connect_kwargs", autospec=True,
            return_value={
                "host": "192.0.2.61", "port": 22, "username": "ubuntu",
                "known_hosts": object(), "password": "pw",
                "client_keys": None,
            },
        )
        kwargs_patch.start()
        self.addCleanup(kwargs_patch.stop)
        spawn_patch = patch.object(
            host_terminal, "spawn_subprocess", autospec=True,
            return_value=(45678, 4242),
        )
        self.spawn = spawn_patch.start()
        self.addCleanup(spawn_patch.stop)

    @contextmanager
    def _as(self, user):
        """Bind ``request`` for *user* in every module the route reads it."""
        fake = MagicMock(spec=Request)
        fake.env = self.env(user=user)
        fake.httprequest = MagicMock(spec=WerkzeugRequest)
        fake.httprequest.user_agent = MagicMock(spec=UserAgent)
        fake.httprequest.user_agent.string = "shell-test-agent"
        with patch.object(host_terminal, "request", fake), \
                patch.object(_rate_limit, "request", fake), \
                patch.object(
                    host_terminal, "client_ip", autospec=True,
                    return_value="203.0.113.7",
                ):
            yield

    def test_a_developer_is_refused(self):
        """Root on the machine is not part of the developer role.

        Refused by the role gate itself, not later by the audit model's
        ACL — which would also refuse a developer, but only after the
        rate-limit counters had been spent and the host read.
        """
        with self._as(self.developer), \
                self.assertRaisesRegex(AccessError, "group_cloud_manager"):
            self.controller.host_terminal_open(self.host.id)
        self.spawn.assert_not_called()
        self.assertFalse(self.env["cloud.host.session"].search(
            [("host_id", "=", self.host.id)],
        ))

    def test_a_manager_gets_an_audited_session(self):
        """One audit row, one route, a subprocess for the host shell."""
        with self._as(self.manager):
            res = self.controller.host_terminal_open(self.host.id)
        self.assertTrue(res["ok"], res)
        session = self.env["cloud.host.session"].search(
            [("session_id", "=", res["session_id"])],
        )
        self.assertEqual(session.user_id, self.manager)
        self.assertEqual(session.host_id, self.host)
        self.assertEqual(session.state, "open")
        self.assertEqual(session.client_ip, "203.0.113.7")
        self.assertEqual(session.user_agent, "shell-test-agent")
        route = self.env["cloud.host.terminal.route"].sudo().search(
            [("session_id", "=", res["session_id"])],
        )
        self.assertEqual((route.pid, route.port), (4242, 45678))
        kwargs = self.spawn.call_args.kwargs
        self.assertTrue(
            kwargs["subprocess_path"].endswith("host_terminal_subprocess.py"),
        )
        self.assertEqual(kwargs["config"]["host_label"], "shell-host")

    def test_a_second_open_session_on_the_host_is_refused(self):
        """An open row for (user, host) blocks another shell."""
        self.env["cloud.host.session"].create({
            "host_id": self.host.id,
            "session_id": "already-open",
            "user_id": self.manager.id,
        })
        with self._as(self.manager):
            res = self.controller.host_terminal_open(self.host.id)
        self.assertFalse(res["ok"])
        self.spawn.assert_not_called()


class TestInputBringsItsEcho(BaseCase):
    """The answer to what is typed carries the output that follows it.

    The page read the output every 80 ms and sent each keystroke apart;
    typing went over the per-address limit of the proxy in front of the
    panel and lost characters (11-nov-2026). Both terminals answer the
    input with the output, waited for briefly, and pass the batch's
    number so a resend reaches the shell once.
    """

    def _send(self, controller, method, answers, **kw):
        """Call *method* with ``_proxy`` answering *answers* in turn."""
        with patch.object(
            terminal_proxy_mixin.TerminalProxyMixin, "_proxy",
            autospec=True, side_effect=answers,
        ) as proxy:
            result = getattr(controller, method)("sid", "eA==", **kw)
        return result, [c.args[1:] + (c.kwargs,) for c in proxy.call_args_list]

    def _controllers(self):
        """Both terminals' input routes."""
        return (
            (host_terminal.HostTerminalController(), "host_terminal_input"),
            (terminal.TerminalController(), "terminal_input"),
        )

    def test_input_with_after_seq_reads_the_output_waiting_for_the_echo(self):
        out = {"ok": True, "chunks": [{"seq": 4, "data": "eA=="}]}
        for controller, method in self._controllers():
            with self.subTest(method=method):
                result, calls = self._send(
                    controller, method, [{"ok": True, "duplicate": False}, out],
                    seq=3, stream="page", after_seq=2,
                )
                self.assertEqual(result["output"], out)
                self.assertEqual(calls, [
                    ("sid", "POST", "/input",
                     {"body": {"data": "eA==", "seq": 3, "stream": "page"}}),
                    ("sid", "GET", "/output", {"params": {
                        "after": 2,
                        "wait": terminal_proxy_mixin.INPUT_ECHO_WAIT_MS,
                    }}),
                ])

    def test_input_alone_is_answered_as_before(self):
        for controller, method in self._controllers():
            with self.subTest(method=method):
                result, calls = self._send(controller, method, [{"ok": True}])
                self.assertEqual(result, {"ok": True})
                self.assertEqual(
                    calls, [("sid", "POST", "/input", {"body": {"data": "eA=="}})],
                )

    def test_refused_input_reads_no_output(self):
        for controller, method in self._controllers():
            with self.subTest(method=method):
                result, calls = self._send(
                    controller, method, [{"ok": False, "error": "closed"}],
                    seq=1, stream="page", after_seq=0,
                )
                self.assertNotIn("output", result)
                self.assertEqual(len(calls), 1)


class TestAnErrorOfTheSessionKeepsItOpen(TransactionCase):
    """A subprocess that answers with an error is alive.

    ``HTTPError`` is a ``URLError``: the proxy took a 500 for a dead
    subprocess, dropped the route and the page said "Session expired".
    A failed resize closed every shell it reached (11-nov-2026).
    """

    _TOKEN = "route-token"

    def setUp(self):
        """Serve the subprocess API over a session whose resize fails."""
        super().setUp()
        fake = MagicMock(spec=session_base.BaseTerminalSession)
        fake.closed = False
        fake.resize.side_effect = TypeError("A coroutine object is required")
        subprocess_base._SESSION = fake
        subprocess_base._AUTH_TOKEN = self._TOKEN
        server = ThreadingHTTPServer(("127.0.0.1", 0), subprocess_base._Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(setattr, subprocess_base, "_SESSION", None)
        self.route = self.env["cloud.host.terminal.route"].sudo().create({
            "session_id": "erroring",
            "pid": os.getpid(),
            "port": server.server_address[1],
            "auth_token": self._TOKEN,
            "user_id": self.env.uid,
        })

    def test_the_error_is_reported_and_the_route_stays(self):
        fake = MagicMock(spec=Request)
        fake.env = self.env
        controller = host_terminal.HostTerminalController()
        with patch.object(terminal_proxy_mixin, "request", fake):
            result = controller.host_terminal_resize("erroring", cols=120, rows=40)
        self.assertEqual(result, {
            "ok": False, "error": "A coroutine object is required",
        })
        self.assertTrue(self.route.exists())


class TestHostShellCap(TransactionCase):
    """The cap is a core setting on the Rates tab."""

    def setUp(self):
        """Build the Rates controller the way the other tab tests do."""
        super().setUp()
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]
        self.fake = MagicMock(spec=Request)
        self.fake.env = self.env

    def test_the_rates_tab_reads_and_saves_it(self):
        """GET serves it and SAVE persists it."""
        with patch.object(_routes_crud, "request", self.fake):
            self.controller.cloud_save_core_rate_limits({
                "rate_limit_host_console_per_min": 5,
            })
            data = self.controller.cloud_get_core_rate_limits()
        self.assertEqual(data["rate_limit_host_console_per_min"], 5)

    def test_zero_falls_back_to_the_default(self):
        """A misconfigured 0 cannot lock every manager out of the shell."""
        self.env["cloud.settings"].sudo()._get().write({
            "rate_limit_host_console_per_min": 0,
        })
        self.assertEqual(
            self.env["cloud.rate.limit"]._get_cap(
                "rate_limit_host_console_per_min",
            ),
            3,
        )


@tagged("post_install", "-at_install")
class TestDeletingAHostWithShellHistory(_HostShellCase):
    """A shell can be opened before any setup has run."""

    def _delete(self):
        """Drive ``cloud_delete_host`` as the manager."""
        fake = MagicMock(spec=http.Request)
        fake.env = self.env(user=self.manager)
        ctrl = data_load.CloudDataLoadController()
        with patch.object(http, "request", fake), \
                patch.object(data_load, "request", fake), \
                patch.object(_routes_crud, "request", fake):
            return ctrl.cloud_delete_host(self.host.id)

    def test_it_is_archived_and_keeps_its_audit_rows(self):
        """No jobs and no Traefik, but an audit row: archive, not unlink."""
        self.assertFalse(self.host.traefik_deployed)
        session = self.env["cloud.host.session"].create({
            "host_id": self.host.id,
            "session_id": "before-setup",
            "user_id": self.manager.id,
        })
        self.assertEqual(self._delete(), {"ok": True})
        self.assertTrue(self.host.exists())
        self.assertFalse(self.host.active)
        self.assertEqual(session.host_id, self.host)
