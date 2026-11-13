"""The panel's outgoing address changed: which hosts may shut it out.

Odoo.sh tells a production database that its address changed with a
``GET`` to ``/_odoo.sh/ip-change?old=…&new=…``. A host hardened with a
list of addresses admits the panel only by the address its hardening
came from, so the panel raises one alert naming the hosts whose list
does not admit the new address, and changes nothing on any of them.
"""
import json
import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.http import Request
from odoo.tests.common import TransactionCase

OLD = "198.51.100.7"
NEW = "198.51.100.99"
CODE = "panel_address_changed"
PARAM = "incubacloud.panel_address_change"


class PanelAddressCase(TransactionCase):
    """Hosts in each state the judgement tells apart."""

    def setUp(self):
        super().setUp()
        self.Host = self.env["cloud.host"]
        self.Alert = self.env["cloud.alert"].sudo()
        self.ICP = self.env["ir.config_parameter"].sudo()
        before = fields.Datetime.now() - timedelta(days=3)

        def host(name, ip, allowed=False, hardened=True, active=True):
            record = self.Host.create({
                "name": name, "ip_address": ip, "user": "ubuntu",
                "wildcard_domain": f"{name}.example.com",
                "allowed_ssh_ips": allowed,
            })
            record.write({
                "hardened": hardened,
                "hardened_at": before if hardened else False,
                "active": active,
            })
            return record

        # Lists the old address by name: the user restricted SSH to it.
        self.listed = host("pa-listed", "192.0.2.61", allowed=OLD)
        # A list without the panel: the run that hardened it added the
        # address it came from, the old one, to the host's firewall.
        self.other = host("pa-other", "192.0.2.62", allowed="203.0.113.7")
        # No list: SSH open to any address, key only.
        self.open = host("pa-open", "192.0.2.63")
        # Already lets the new address in.
        self.ready = host("pa-ready", "192.0.2.64", allowed=f"{OLD}, 198.51.100.0/24")
        # Never hardened: no firewall of the panel's.
        self.raw = host("pa-raw", "192.0.2.65", allowed=OLD, hardened=False)
        # A catch-all in the list.
        self.anyone = host("pa-anyone", "192.0.2.66", allowed="0.0.0.0/0")
        # Archived: out of the fleet.
        self.gone = host("pa-gone", "192.0.2.67", allowed=OLD, active=False)

    def _alert(self):
        return self.Alert.search([("code", "=", CODE), ("state", "=", "active")])


class TestThePanelAddressChange(PanelAddressCase):

    def test_one_critical_alert_names_the_hosts_that_may_shut_the_panel_out(self):
        self.assertTrue(self.Host._panel_address_changed(OLD, NEW))
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "critical")
        self.assertEqual(alert.audience, "fleet")
        self.assertFalse(alert.host_id)
        for name in ("pa-listed", "pa-other"):
            self.assertIn(name, alert.message)
        for name in ("pa-open", "pa-ready", "pa-raw", "pa-anyone", "pa-gone"):
            self.assertNotIn(name, alert.message)
        self.assertIn(OLD, alert.message)
        self.assertIn(NEW, alert.message)
        self.assertIn("RB-40", alert.message)
        self.assertEqual(
            sorted(alert.payload["host_ids"]), sorted((self.listed | self.other).ids),
        )
        self.assertEqual(alert.payload["old"], OLD)
        self.assertEqual(alert.payload["new"], NEW)

    def test_no_host_is_changed_and_no_job_is_started(self):
        jobs = self.env["cloud.job"].sudo().search_count([])
        lists = {h.id: h.allowed_ssh_ips for h in self.listed | self.other | self.ready}
        self.Host._panel_address_changed(OLD, NEW)
        self.assertEqual(self.env["cloud.job"].sudo().search_count([]), jobs)
        for h in self.listed | self.other | self.ready:
            self.assertEqual(h.allowed_ssh_ips, lists[h.id])

    def test_without_the_new_address_every_host_with_a_list_is_named(self):
        self.Host._panel_address_changed(OLD, None)
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        for name in ("pa-listed", "pa-other", "pa-ready"):
            self.assertIn(name, alert.message)
        for name in ("pa-open", "pa-raw", "pa-anyone", "pa-gone"):
            self.assertNotIn(name, alert.message)
        self.assertFalse(alert.payload["new"])

    def test_an_address_that_did_not_change_says_nothing(self):
        self.assertFalse(self.Host._panel_address_changed(OLD, OLD))
        self.assertFalse(self._alert())
        self.assertFalse(self.ICP.get_param(PARAM))

    def test_an_old_address_that_is_none_is_refused(self):
        for old in (None, "", "not-an-address", "fe80::1%eth0"):
            with self.assertRaises(ValueError):
                self.Host._panel_address_changed(old, NEW)
        self.assertFalse(self._alert())

    def test_a_new_address_that_is_none_counts_as_unknown(self):
        self.Host._panel_address_changed(OLD, "<script>")
        alert = self._alert()
        self.assertNotIn("<script>", alert.message)
        self.assertIn("pa-ready", alert.message)

    def test_with_no_host_at_risk_nothing_is_raised_or_kept(self):
        (self.listed | self.other).write({"hardened": False})
        self.assertTrue(self.Host._panel_address_changed(OLD, NEW))
        self.assertFalse(self._alert())
        self.assertFalse(self.ICP.get_param(PARAM))

    def test_a_host_listing_the_new_address_leaves_the_alert(self):
        self.Host._panel_address_changed(OLD, NEW)
        self.listed.write({"allowed_ssh_ips": f"{OLD}, {NEW}"})
        alert = self._alert()
        self.assertNotIn("pa-listed", alert.message)
        self.assertIn("pa-other", alert.message)
        self.other.write({"allowed_ssh_ips": f"203.0.113.7, {NEW}"})
        self.assertFalse(self._alert())
        self.assertFalse(self.ICP.get_param(PARAM))

    def test_a_host_hardened_again_after_the_change_leaves_the_alert(self):
        self.Host._panel_address_changed(OLD, NEW)
        at = fields.Datetime.to_datetime(json.loads(self.ICP.get_param(PARAM))["at"])
        (self.listed | self.other).write({"hardened_at": at + timedelta(minutes=1)})
        self.assertFalse(self._alert())

    def test_an_archived_host_leaves_the_alert(self):
        self.Host._panel_address_changed(OLD, NEW)
        (self.listed | self.other).write({"active": False})
        self.assertFalse(self._alert())

    def test_an_address_of_the_panel_a_layer_knows_admits_it(self):
        with patch.object(
            type(self.Host), "_hardening_panel_ip", return_value="198.51.100.0/24",
        ):
            self.Host._panel_address_changed(OLD, NEW)
        self.assertFalse(self._alert())

    def test_a_second_change_refreshes_the_same_alert(self):
        self.Host._panel_address_changed(OLD, NEW)
        self.Host._panel_address_changed(NEW, "198.51.100.150")
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertIn("198.51.100.150", alert.message)
        self.assertIn("pa-ready", alert.message)


class TestTheOdooShNotice(PanelAddressCase):
    """``/_odoo.sh/ip-change``: Odoo.sh's request, and only on Odoo.sh."""

    def _call(self, stage, **params):
        from odoo.addons.incubacloud.controllers import odoo_sh

        fake = MagicMock(spec=Request)
        fake.env = self.env
        environ = dict(os.environ)
        environ.pop("ODOO_STAGE", None)
        if stage:
            environ["ODOO_STAGE"] = stage
        with patch.object(odoo_sh, "request", fake), \
                patch.dict(os.environ, environ, clear=True):
            answer = odoo_sh.OdooShController.ip_change.__wrapped__
            answer(odoo_sh.OdooShController(), **params)
        return fake

    def test_the_route_is_odoo_sh_s(self):
        from odoo.addons.incubacloud.controllers.odoo_sh import OdooShController

        routing = OdooShController.ip_change.original_routing
        self.assertEqual(routing["routes"], ["/_odoo.sh/ip-change"])
        self.assertEqual(routing["auth"], "public")
        self.assertEqual(routing["methods"], ["GET"])
        self.assertFalse(routing["csrf"])

    def test_on_odoo_sh_production_it_raises_the_alert(self):
        fake = self._call("production", old=OLD, new=NEW)
        self.assertEqual(len(self._alert()), 1)
        self.assertEqual(fake.make_response.call_args.args[0], "ok")
        self.assertNotIn("status", fake.make_response.call_args.kwargs)

    def test_elsewhere_it_is_not_there(self):
        for stage in (None, "", "staging", "dev"):
            fake = self._call(stage, old=OLD, new=NEW)
            fake.not_found.assert_called_once_with()
            fake.make_response.assert_not_called()
        self.assertFalse(self._alert())
        self.assertFalse(self.ICP.get_param(PARAM))

    def test_a_notice_without_an_old_address_is_refused(self):
        fake = self._call("production", old="nonsense", new=NEW)
        self.assertEqual(fake.make_response.call_args.kwargs.get("status"), 400)
        self.assertFalse(self._alert())
