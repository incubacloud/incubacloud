"""The fleet's Security block in Monitoring (``/cloud/fleet/resource_series``).

Plan #84, cut C7, decision D4: figures only, read with this panel's
account and never with the operator's credential (17-nov-2026); the
servers named are this panel's own, never another account's, whatever
its id; the switch and the hardening are counted from the panel's
database, not from the central; no address, user or alert text in the
answer, for any role; nothing asked where this panel's servers may not
send their security logs. Driven over real HTTP, with the central
replaced at ``requests.get``.
"""
import json
from contextlib import nullcontext
from unittest.mock import patch

from odoo.tests.common import BaseCase, HttpCase, tagged

from ..models.metrics_charts import PANELS, shape_hosts
from .test_host_fleet_series import _ROLES, _matrix, _refusal, _vector

_MODULE = "odoo.addons.incubacloud.models.cloud_metric_rule"

#: The roles of the Security panels' queries, which tell them apart.
_SECURITY_ROLES = {
    q["role"] for p in PANELS["fleet"] if p["needs"] == "fleet_security"
    for q in p["queries"]
}

#: The address an alert's text carries: it may not reach the answer.
_ALERT_ADDRESS = "203.0.113.97"


class _Central:
    """A central with servers of this panel's account and of another.

    The other account's server has the same id as one of this panel's,
    as ids repeat across panels; it is under brute force and silent.
    A central filters the account's route by account; *unfiltered*
    stands for a gateway that does not, and answers the other account's
    rows too. Keeps the queries it was sent.
    """

    def __init__(self, mine, off, refuse_security=False, unfiltered=False):
        """Answer about *mine* (under SSH brute force) and *off* (switch off).

        :param bool refuse_security: answer the Security panels 403.
        :param bool unfiltered: answer the other account's rows as well.
        """
        self.queries = []
        self.mine, self.off = str(mine.id), str(off.id)
        self.refuse_security = refuse_security
        self.unfiltered = unfiltered

    def __call__(self, url, params, **kw):
        """Answer one ``requests.get`` of the board."""
        roles = _ROLES.findall(params["query"])
        self.queries.append((url, params["query"]))
        security = set(roles) & _SECURITY_ROLES
        if security and self.refuse_security:
            return _refusal(403)
        if "step" in params:
            return _matrix(params, [({"ic_role": role}, 1) for role in roles])
        if "ssh_failed_1h" in roles:
            return _vector([
                ({"ic_role": "ssh_failed_1h"}, 340),
                ({"ic_role": "ssh_bruteforce"}, 1),
                ({"ic_role": "bans_1h"}, 5),
                ({"ic_role": "weblogin_bruteforce"}, 3),
                ({"ic_role": "silent"}, 1),
                # Not a role of the central's: the panel counts it.
                ({"ic_role": "off"}, 99),
            ])
        if security:
            rows = [
                ({"ic_account": "acct_test", "host_id": self.mine,
                  "ic_role": "ssh_bruteforce"}, 340),
                ({"ic_account": "acct_test", "host_id": self.mine,
                  "ic_role": "weblogin_bruteforce"}, 2),
                # Its switch is off on the panel: not a silence.
                ({"ic_account": "acct_test", "host_id": self.off,
                  "ic_role": "silent"}, 1),
            ]
            if self.unfiltered:
                rows += [
                    ({"ic_account": "acct_other", "host_id": self.off,
                      "ic_role": "ssh_bruteforce"}, 999),
                    ({"ic_account": "acct_other", "host_id": "987654",
                      "ic_role": "silent"}, 1),
                ]
            return _vector(rows)
        return _vector([({"ic_role": role}, 2) for role in roles])

    def security_queries(self):
        """Return the URLs of the Security panels' queries."""
        return [
            url for url, query in self.queries
            if set(_ROLES.findall(query)) & _SECURITY_ROLES
        ]


@tagged("post_install", "-at_install")
class TestFleetSecurity(HttpCase):

    def setUp(self):
        super().setUp()
        self.available = self.env["cloud.host"]._security_logs_available()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.write({
            "metrics_enabled": True,
            "metrics_central_url": "https://central.test/r",
            "metrics_account": "acct_test",
            "metrics_remote_write_token": "s3cr3t",
            "metrics_operator_token": "op3r",
        })
        Host = self.env["cloud.host"].sudo()
        self.mine = Host.create({
            "name": "FSecMine", "ip_address": "203.0.113.91", "user": "root",
            "wildcard_domain": "fsecmine.example.com",
        })
        self.off = Host.create({
            "name": "FSecOff", "ip_address": "203.0.113.92", "user": "root",
            "wildcard_domain": "fsecoff.example.com",
        })
        # A layer that withholds security logs keeps the switch off.
        if self.available:
            self.mine.security_logs_enabled = True
        self.off.security_logs_enabled = False
        (self.mine | self.off).write({"hardened": True})
        for login, group in (
            ("fsec-dev", "incubacloud.group_cloud_developer"),
            ("fsec-mgr", "incubacloud.group_cloud_manager"),
        ):
            self.env["res.users"].create({
                "name": login, "login": login, "password": login,
                "group_ids": [(6, 0, [
                    self.env.ref("base.group_user").id, self.env.ref(group).id,
                ])],
            })

    def _board(self, login, central):
        """Ask the fleet's board as *login* against *central*."""
        self.authenticate(login, login)
        payload = {"jsonrpc": "2.0", "method": "call", "id": 1, "params": {}}
        with patch(f"{_MODULE}.requests.get", side_effect=central):
            res = self.url_open(
                "/cloud/fleet/resource_series", data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            ).json()["result"]
        self.assertTrue(res["ok"], res)
        return res

    def _require_available(self):
        """Skip where this panel's servers may not send security logs."""
        if not self.available:
            self.skipTest("security logs withheld on this panel: see "
                          "test_withheld_security_logs_ask_nothing")

    @staticmethod
    def _rows(res):
        """Return the Security block's rows by server id."""
        return {row["host_id"]: row for row in res["security"]["rows"]}

    def test_the_central_s_owner_names_only_this_panel_s_servers(self):
        """Read with the account; another account's server, same id or not, never named."""
        self._require_available()
        central = _Central(self.mine, self.off, unfiltered=True)
        res = self._board("fsec-mgr", central)
        self.assertNotIn("scope", res)
        self.assertTrue(central.security_queries())
        self.assertTrue(all("/r/api/v1/" in url for url, _query in central.queries))
        self.assertEqual(res["panels"]["security"]["value"]["ssh_bruteforce"], 1)
        self.assertEqual(res["panels"]["security"]["value"]["weblogin_bruteforce"], 3)
        self.assertNotIn("security_hosts", res["panels"])
        rows = self._rows(res)
        self.assertNotIn(987654, rows)
        self.assertEqual(rows[self.mine.id]["ssh_bruteforce"], 340)
        self.assertEqual(rows[self.mine.id]["weblogin_bruteforce"], 2)
        self.assertEqual(rows[self.mine.id]["name"], "FSecMine")
        # The other account's 999 on the same id is not this server's.
        self.assertNotIn("ssh_bruteforce", rows[self.off.id])
        self.assertTrue(all(row["own"] for row in rows.values()))
        self.assertNotIn("acct_", json.dumps(res))

    def test_without_the_operator_credential_the_answer_is_the_same(self):
        self._require_available()
        self.settings.metrics_operator_token = False
        central = _Central(self.mine, self.off)
        res = self._board("fsec-mgr", central)
        self.assertTrue(central.security_queries())
        self.assertTrue(all("/r/api/v1/" in url for url in central.security_queries()))
        self.assertEqual(res["panels"]["security"]["value"]["ssh_bruteforce"], 1)
        self.assertEqual(self._rows(res)[self.mine.id]["ssh_bruteforce"], 340)

    def test_the_switch_is_counted_from_the_panel_not_the_central(self):
        """Off and unhardened from the database, also with the central refusing."""
        self._require_available()
        expected_off = self.env["cloud.host"].search_count([("security_logs_enabled", "=", False)])
        for refuse in (False, True):
            res = self._board("fsec-mgr", _Central(self.mine, self.off, refuse_security=refuse))
            self.assertEqual(res["security"]["off"], expected_off)
            self.assertNotEqual(res["security"]["off"], 99)
            rows = self._rows(res)
            self.assertTrue(rows[self.off.id]["off"])
            # A server whose switch is off is not silent.
            self.assertNotIn("silent", rows[self.off.id])
        # Refused, the central's figures say so and name no server.
        self.assertIn("error", res["panels"]["security"])
        self.assertNotIn(self.mine.id, rows)

    def test_a_server_without_hardening_is_listed(self):
        self._require_available()
        self.mine.hardened = False
        res = self._board("fsec-dev", _Central(self.mine, self.off))
        self.assertTrue(self._rows(res)[self.mine.id]["unhardened"])
        self.assertGreaterEqual(res["security"]["unhardened"], 1)

    def test_the_alerts_list_their_server_never_their_text(self):
        """A count and a level: the text may carry an address (D4)."""
        self._require_available()
        self.env["cloud.alert"].sudo().raise_alert(
            "security_test_fleet_c7",
            f"FSecMine was signed in to from {_ALERT_ADDRESS} as invitado.",
            level="critical", host=self.mine,
        )
        res = self._board("fsec-mgr", _Central(self.mine, self.off))
        row = self._rows(res)[self.mine.id]
        self.assertEqual(row["alerts"], 1)
        self.assertEqual(row["level"], "critical")
        self.assertGreaterEqual(res["security"]["alerts_hosts"], 1)
        # The worst first.
        self.assertEqual(res["security"]["rows"][0]["host_id"], self.mine.id)
        for login in ("fsec-mgr", "fsec-dev"):
            body = json.dumps(self._board(login, _Central(self.mine, self.off)))
            self.assertNotIn(_ALERT_ADDRESS, body, login)
            self.assertNotIn("invitado", body, login)

    def test_the_figures_count_with_the_rules_thresholds(self):
        self._require_available()
        Rule = self.env["cloud.metric.rule"].sudo()
        Rule.search([("code", "=", "security_ssh_bruteforce")]).threshold = 7
        Rule.search([("code", "=", "security_weblogin_bruteforce")]).threshold = 9
        central = _Central(self.mine, self.off)
        self._board("fsec-dev", central)
        sent = " ".join(q for _url, q in central.queries)
        self.assertIn("increase(ic_ssh_auth_failed_total[10m]))) > 7.0", sent)
        self.assertIn('instance_id!="0"}[10m]))) > 9.0', sent)

    def test_withheld_security_logs_ask_nothing(self):
        """A managed panel without its manager's permission: the state alone.

        Where a layer withholds security logs its own answer is used;
        elsewhere the withholding is patched in.
        """
        with nullcontext() if not self.available else patch.object(
            type(self.env["cloud.host"]), "_security_logs_available",
            autospec=True, return_value=False,
        ):
            central = _Central(self.mine, self.off)
            res = self._board("fsec-mgr", central)
        self.assertEqual(res["security"], {"state": "unavailable"})
        self.assertEqual(central.security_queries(), [])
        self.assertNotIn("security_hosts", res["panels"])
        self.assertEqual(res["panels"]["summary"]["value"]["hosts"], 2)


class TestFleetSecurityPanels(BaseCase):
    """How the Security panels' rows are shaped."""

    def test_a_server_of_each_account_is_a_row_of_its_own(self):
        panel = next(p for p in PANELS["fleet"] if p["key"] == "security_hosts")
        rows = shape_hosts(panel, [
            ({"ic_account": "a", "host_id": "3", "ic_role": "ssh_bruteforce"}, 120.4),
            ({"ic_account": "a", "host_id": "3", "ic_role": "silent"}, 1),
            ({"ic_account": "b", "host_id": "3", "ic_role": "silent"}, 1),
            ({"ic_account": "a", "ic_role": "silent"}, 1),
        ])
        self.assertEqual(rows, [
            {"host_id": "3", "account": "a", "ssh_bruteforce": 120, "silent": 1},
            {"host_id": "3", "account": "b", "silent": 1},
        ])
