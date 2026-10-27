"""Security controls meant for a person go through a person (D2)."""

from datetime import timedelta
from unittest.mock import patch

import asyncssh

from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from .common import (
    ACCEPT,
    FORM_CAPABILITIES,
    ConfirmMixin,
    McpHttpMixin,
    McpUsersMixin,
    PanelFixtureMixin,
)


def _entry(ip):
    """A known_hosts line for a freshly generated key at *ip*."""
    key = asyncssh.generate_private_key("ssh-ed25519")
    return f"{ip} {key.export_public_key('openssh').decode().strip()}"


@tagged("post_install", "-at_install")
class TestGuardedTools(
    ConfirmMixin, PanelFixtureMixin, McpUsersMixin, McpHttpMixin, HttpCase
):
    """Host keys, login links, audit purges and security settings ask first,
    saying exactly what changes."""

    def setUp(self):
        """A manager with an API key, and the panel's fixture."""
        super().setUp()
        self._resource_here()
        self._panel_fixture()
        self.manager = self._cloud_user("mcp-guard-mgr", group="group_cloud_manager")
        self.key = self._api_key(self.manager)
        self.Host = type(self.env["cloud.host"])

    def _server(self, presented, logged_in=None):
        """Stub the server: the key it presents, and the one the login meets.

        :param presented: what the key exchange alone returns, a
            known_hosts line, or a list of them for successive calls
        :param logged_in: what the login returns; *presented* by default
        """
        side = presented if isinstance(presented, list) else None
        peek = patch.object(
            self.Host,
            "_presented_host_key_entry",
            autospec=True,
            side_effect=side,
            return_value=None if side else presented,
        )
        login = patch.object(
            self.Host,
            "_scan_host_key_entry",
            autospec=True,
            return_value=logged_in or presented,
        )
        return peek, login

    def test_trusting_a_key_shows_both_fingerprints_and_stores_the_approved_one(self):
        """The approval shows the key trusted until now and the one presented
        now; once approved, the presented key is the trusted one."""
        self.host.known_hosts_key = _entry("10.0.0.90")
        old = self.host.known_hosts_fingerprint
        new_entry = _entry("10.0.0.90")
        new = self.env["cloud.host"]._known_hosts_fingerprint(new_entry)
        peek, login = self._server(new_entry)
        with peek, login:
            first = self._call_tool(
                self.key,
                "trust_host_key",
                {"host_id": self.host.id},
                FORM_CAPABILITIES,
            )
            message = first["inputRequests"]["confirm"]["params"]["message"]
            self.assertIn(old, message)
            self.assertIn(new, message)
            final = self._confirmed_call(
                self.key, "trust_host_key", {"host_id": self.host.id}
            )
        self.assertFalse(final["isError"], final)
        self.assertEqual(self.host.known_hosts_fingerprint, new)

    def test_a_key_that_changes_after_the_approval_is_not_trusted(self):
        """A server that presents another key at the retry is not trusted,
        and no login is attempted."""
        trusted = self.host.known_hosts_key = _entry("10.0.0.90")
        shown, swapped = _entry("10.0.0.90"), _entry("10.0.0.90")
        peek, login = self._server([shown, swapped])
        with peek, login as logged_in:
            first = self._call_tool(
                self.key,
                "trust_host_key",
                {"host_id": self.host.id},
                FORM_CAPABILITIES,
            )
            final = self._call_tool(
                self.key,
                "trust_host_key",
                {"host_id": self.host.id},
                FORM_CAPABILITIES,
                requestState=first["requestState"],
                inputResponses=ACCEPT,
            )
        self.assertTrue(final["isError"])
        logged_in.assert_not_called()
        self.assertEqual(self.host.known_hosts_key, trusted)

    def test_a_server_that_presents_no_key_is_refused_before_asking(self):
        """No key presented, nothing to approve: refused, nobody asked."""
        peek, login = self._server("")
        with peek, login:
            result = self._call_tool(
                self.key,
                "trust_host_key",
                {"host_id": self.host.id},
                FORM_CAPABILITIES,
            )
        self.assertNotEqual(result.get("resultType"), "input_required")
        self.assertTrue(result["isError"])

    def test_a_security_setting_says_from_what_to_what_and_changes_once_approved(
        self,
    ):
        """Shortening the audit retention says from what to what, and
        changes once approved."""
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("incubacloud.audit_log_retention_days", "90")
        args = {"section": "general", "values": {"audit_log_retention_days": 1}}
        first = self._call_tool(self.key, "update_settings", args, FORM_CAPABILITIES)
        self.assertIn(
            "audit_log_retention_days: 90 → 1",
            first["inputRequests"]["confirm"]["params"]["message"],
        )
        self.assertFalse(
            self._confirmed_call(self.key, "update_settings", args)["isError"]
        )
        self.assertEqual(ICP.get_param("incubacloud.audit_log_retention_days"), "1")

    def test_an_ordinary_setting_asks_nothing(self):
        """A setting that is not a security one changes without asking."""
        result = self._call_tool(
            self.key,
            "update_settings",
            {"section": "general", "values": {"job_log_retention_days": 31}},
            FORM_CAPABILITIES,
        )
        self.assertNotEqual(result.get("resultType"), "input_required")
        self.assertFalse(result["isError"], result)

    def test_a_login_link_names_the_user_and_production(self):
        """The approval of a login link names the user, by name and id, and
        says the instance is production; once approved, the link comes."""

        def canned(_self, path, params):
            """The instance answers with a one-time link."""
            return {"ok": True, "url": "https://acme.example/ic/login?t=one-time"}

        args = {
            "instance_id": self.production.id,
            "user_id": 2,
            "user_name": "Mitchell Admin",
        }
        first = self._call_tool(
            self.key, "instance_login_link", args, FORM_CAPABILITIES
        )
        message = first["inputRequests"]["confirm"]["params"]["message"]
        self.assertIn("production", message)
        self.assertIn("Mitchell Admin (id 2)", message)
        with patch.object(
            type(self.env["incubacloud.mcp.dispatch"]),
            "_call",
            autospec=True,
            side_effect=canned,
        ):
            final = self._confirmed_call(self.key, "instance_login_link", args)
        self.assertEqual(
            self._payload(final)["url"], "https://acme.example/ic/login?t=one-time"
        )

    def test_listing_an_instance_s_users_asks_nothing(self):
        """Without a user, the tool lists the instance's users, unasked."""
        Dispatch = type(self.env["incubacloud.mcp.dispatch"])
        with patch.object(
            Dispatch, "_call", autospec=True, return_value={"ok": True, "users": []}
        ) as call:
            result = self._call_tool(
                self.key,
                "instance_login_link",
                {"instance_id": self.production.id},
                FORM_CAPABILITIES,
            )
        self.assertNotEqual(result.get("resultType"), "input_required")
        self.assertFalse(result["isError"], result)
        self.assertEqual(call.call_args.args[1], "/cloud/get_instance_users")

    def test_purging_the_audit_asks_and_purges(self):
        """Purging the audit says how much goes, and purges once approved."""
        old = self.env["cloud.audit.log"].sudo().create({"action": "Very old"})
        self.env.cr.execute(
            "UPDATE cloud_audit_log SET create_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=400), old.id),
        )
        first = self._call_tool(
            self.key, "purge_audit_logs", {"days": 365}, FORM_CAPABILITIES
        )
        self.assertIn("365", first["inputRequests"]["confirm"]["params"]["message"])
        self.assertFalse(
            self._confirmed_call(self.key, "purge_audit_logs", {"days": 365})["isError"]
        )
        self.assertFalse(old.exists())
