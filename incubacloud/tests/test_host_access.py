"""A server's Access tab (``/cloud/host/access``).

What the panel signs in with, the last sign-ins of the security logs, and
who may read them: whoever manages servers. The security board is
patched: its own tests read the central.
"""
import json
from unittest.mock import patch

from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.incubacloud.models import cloud_host_access as access_mod


def _row(n, attribution="panel"):
    """A sign-in as ``_security_accepted`` gives it."""
    return {
        "at": f"2026-11-17T10:{n:02d}:00+00:00", "user": "incubacloud",
        "address": "203.0.113.5", "country": "DE", "method": "publickey",
        "key": "panel", "attribution": attribution,
    }


class TestAccessSummary(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Host = type(self.env["cloud.host"])
        self.host = self.env["cloud.host"].create({
            "name": "HAccess", "ip_address": "203.0.113.90", "port": 2222,
            "user": "incubacloud", "wildcard_domain": "haccess.example.com",
        })

    def _summary(self, board, fingerprints=("SHA256:abc",)):
        """The summary with the board and the key's fingerprint patched."""
        with patch.object(
            self.Host, "_security_board", autospec=True, return_value=board,
        ) as read, patch.object(
            self.Host, "_panel_key_fingerprints", autospec=True,
            return_value=list(fingerprints),
        ):
            summary = self.host._access_summary()
        return summary, read

    def test_it_says_how_the_panel_signs_in(self):
        summary, read = self._summary({"ok": True, "state": "off"})
        self.assertEqual(summary["user"], "incubacloud")
        self.assertEqual(summary["address"], "203.0.113.90")
        self.assertEqual(summary["port"], 2222)
        self.assertEqual(summary["panel_key_fingerprint"], "SHA256:abc")
        self.assertEqual(summary["sign_ins"], {"state": "off"})
        read.assert_called_once_with(self.host, range_key="7d", detail=True)

    def test_without_a_key_it_reads_empty(self):
        summary, _read = self._summary({"ok": True, "state": "off"}, fingerprints=())
        self.assertEqual(summary["panel_key_fingerprint"], "")

    def test_it_keeps_the_newest_sign_ins(self):
        rows = [_row(n) for n in range(access_mod.ACCESS_SIGN_INS + 2)]
        summary, _read = self._summary({
            "ok": True, "state": "ready", "tables": {"accepted": {"rows": rows}},
        })
        sign_ins = summary["sign_ins"]
        self.assertEqual(sign_ins["state"], "ready")
        self.assertEqual(sign_ins["rows"], rows[:access_mod.ACCESS_SIGN_INS])
        self.assertTrue(sign_ins["more"])

    def test_a_failed_read_leaves_the_rest(self):
        with self.assertLogs(access_mod.__name__, "WARNING") as logged:
            summary, _read = self._summary({"ok": False, "error": "central down"})
        self.assertEqual(summary["sign_ins"], {"state": "error"})
        self.assertEqual(summary["user"], "incubacloud")
        self.assertIn("central down", logged.output[0])

    def test_a_raised_read_reaches_the_browser_without_its_detail(self):
        """The exception's text may name the central and its token: logged."""
        with patch.object(
            self.Host, "_security_board", autospec=True,
            side_effect=RuntimeError("https://central.example/?token=s3cret"),
        ), patch.object(
            self.Host, "_panel_key_fingerprints", autospec=True, return_value=[],
        ), self.assertLogs(access_mod.__name__, "WARNING") as logged:
            summary = self.host._access_summary()
        self.assertEqual(summary["sign_ins"], {"state": "error"})
        self.assertNotIn("s3cret", json.dumps(summary))
        self.assertIn("s3cret", "\n".join(logged.output))

    def test_a_failed_table_reaches_the_browser_without_its_detail(self):
        with self.assertLogs(access_mod.__name__, "WARNING") as logged:
            summary, _read = self._summary({
                "ok": True, "state": "ready",
                "tables": {"accepted": {"error": "query 10.0.0.5 timed out"}},
            })
        self.assertEqual(summary["sign_ins"], {"state": "error"})
        self.assertIn("10.0.0.5", logged.output[0])


@tagged("post_install", "-at_install")
class TestAccessRoute(HttpCase):
    """The route, by role; after every module, as its users are created
    through the ORM (``account``'s NOT NULL ``autopost_bills`` on
    ``res_partner`` has no default while core loads alone)."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "HAccessRoute", "ip_address": "203.0.113.91",
            "user": "root", "wildcard_domain": "haccessroute.example.com",
        })
        for login, group in (
            ("acc-mgr", "incubacloud.group_cloud_manager"),
            ("acc-dev", "incubacloud.group_cloud_developer"),
        ):
            self.env["res.users"].create({
                "name": login, "login": login, "password": login,
                "group_ids": [(6, 0, [
                    self.env.ref("base.group_user").id, self.env.ref(group).id,
                ])],
            })
        board = patch.object(
            type(self.env["cloud.host"]), "_security_board", autospec=True,
            return_value={"ok": True, "state": "off"},
        )
        board.start()
        self.addCleanup(board.stop)

    def _access(self, login, host_id):
        """``/cloud/host/access`` as *login*."""
        self.authenticate(login, login)
        return self.url_open(
            "/cloud/host/access",
            data=json.dumps({
                "jsonrpc": "2.0", "method": "call", "id": 1,
                "params": {"host_id": host_id},
            }),
            headers={"Content-Type": "application/json"},
        ).json()

    def test_a_manager_reads_it(self):
        answer = self._access("acc-mgr", self.host.id)["result"]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["user"], "root")
        self.assertEqual(answer["sign_ins"], {"state": "off"})

    def test_a_developer_does_not(self):
        answer = self._access("acc-dev", self.host.id)
        self.assertIn("error", answer)
        self.assertNotIn("result", answer)

    def test_an_unknown_server_is_not_found(self):
        answer = self._access("acc-mgr", 999999)["result"]
        self.assertEqual(answer, {"ok": False, "error": "Host not found"})
