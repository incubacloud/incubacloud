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
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from werkzeug.user_agent import UserAgent
from werkzeug.wrappers import Request as WerkzeugRequest

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import Request
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.incubacloud.controllers import _rate_limit, host_terminal
from odoo.addons.incubacloud.controllers import data_load
from odoo.addons.incubacloud.controllers._data_load import _routes_crud


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
