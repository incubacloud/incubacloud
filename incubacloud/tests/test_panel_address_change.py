"""The panel's outgoing address changed: which hosts may shut it out.

Odoo.sh tells a production database that its address changed with a
``GET`` to ``/_odoo.sh/ip-change?old=…&new=…``. A host hardened with a
list of addresses admits the panel only by the address its hardening
came from, so the panel raises one alert naming the hosts whose list
does not admit the new address, and changes nothing on any of them.
The list judged is the one the host confirmed: an edit made by hand
since then has not reached the host's firewall.
"""
import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from odoo import fields
from odoo.http import Request
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase

from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport

# Public addresses: one that is not (a private or documentation range)
# counts as unknown, as Odoo.sh's production leaves with a public one.
OLD = "45.10.20.7"
NEW = "45.10.20.99"
NEW_NETWORK = "45.10.20.0/24"
LATER = "81.40.12.150"
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
        self.ready = host("pa-ready", "192.0.2.64", allowed=f"{OLD}, {NEW_NETWORK}")
        # Never hardened: no firewall of the panel's.
        self.raw = host("pa-raw", "192.0.2.65", allowed=OLD, hardened=False)
        # A catch-all in the list.
        self.anyone = host("pa-anyone", "192.0.2.66", allowed="0.0.0.0/0")
        # Archived: out of the fleet.
        self.gone = host("pa-gone", "192.0.2.67", allowed=OLD, active=False)

    def _alert(self):
        return self.Alert.search([("code", "=", CODE), ("state", "=", "active")])

    def _kept(self):
        return json.loads(self.ICP.get_param(PARAM) or "null")


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
        self.assertIn("RB-41", alert.message)
        self.assertIn("Odoo.sh", alert.message)
        self.assertEqual(
            sorted(alert.payload["host_ids"]), sorted((self.listed | self.other).ids),
        )
        self.assertEqual(alert.payload["old"], OLD)
        self.assertEqual(alert.payload["new"], NEW)
        self.assertFalse(self._kept()["settled"])

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
        self.assertIn("Odoo.sh", alert.message)

    def test_an_address_that_did_not_change_says_nothing(self):
        self.assertFalse(self.Host._panel_address_changed(OLD, OLD))
        self.assertFalse(self._alert())
        self.assertFalse(self.ICP.get_param(PARAM))

    def test_an_old_address_that_is_none_is_refused(self):
        for old in (None, "", "not-an-address", "fe80::1%eth0"):
            with self.assertRaises(ValueError):
                self.Host._panel_address_changed(old, NEW)
        self.assertFalse(self._alert())

    def test_a_new_address_that_is_not_a_public_one_counts_as_unknown(self):
        for new in ("<script>", "10.0.0.5", "127.0.0.1", "198.51.100.99"):
            with self.subTest(new=new):
                self.Alert.resolve_alert(CODE)
                self.ICP.set_param(PARAM, False)
                self.assertTrue(self.Host._panel_address_changed(OLD, new))
                alert = self._alert()
                self.assertNotIn(new, alert.message)
                self.assertIn("pa-ready", alert.message)
                self.assertFalse(alert.payload["new"])
                self.assertFalse(self._kept()["new"])

    def test_with_no_host_at_risk_nothing_is_raised_and_the_change_is_settled(self):
        (self.listed | self.other).write({"hardened": False})
        self.assertTrue(self.Host._panel_address_changed(OLD, NEW))
        self.assertFalse(self._alert())
        self.assertEqual(self._kept()["new"], NEW)
        self.assertTrue(self._kept()["settled"])

    def test_the_parameter_is_written_once_per_notice(self):
        ICP = type(self.ICP)
        with patch.object(
            ICP, "set_param", autospec=True, side_effect=ICP.set_param,
        ) as set_param:
            self.Host._panel_address_changed(OLD, NEW)
        self.assertEqual(
            [c.args[1] for c in set_param.call_args_list], [PARAM],
        )
        (self.listed | self.other).write({"active": False})
        with patch.object(
            ICP, "set_param", autospec=True, side_effect=ICP.set_param,
        ) as set_param:
            # Within pa-ready's list: no host is left at risk.
            self.Host._panel_address_changed(NEW, "45.10.20.150")
        self.assertEqual(
            [c.args[1] for c in set_param.call_args_list], [PARAM],
        )
        self.assertTrue(self._kept()["settled"])

    def test_the_same_notice_again_is_ignored(self):
        self.Host._panel_address_changed(OLD, NEW)
        first = self._alert()
        kept = self.ICP.get_param(PARAM)
        self.assertFalse(self.Host._panel_address_changed(OLD, NEW))
        self.assertEqual(self._alert(), first)
        self.assertEqual(self.ICP.get_param(PARAM), kept)

    def test_the_same_notice_after_the_alert_closed_does_not_open_it_again(self):
        self.Host._panel_address_changed(OLD, NEW)
        (self.listed | self.other).write({"active": False})
        self.assertFalse(self._alert())
        self.assertTrue(self._kept()["settled"])
        (self.listed | self.other).write({"active": True})
        self.assertFalse(self.Host._panel_address_changed(OLD, NEW))
        self.assertFalse(self._alert())
        self.assertTrue(self._kept()["settled"])

    def test_a_host_hardened_again_after_the_change_leaves_the_alert(self):
        self.Host._panel_address_changed(OLD, NEW)
        at = fields.Datetime.to_datetime(self._kept()["at"])
        (self.listed | self.other).write({"hardened_at": at + timedelta(minutes=1)})
        self.assertFalse(self._alert())
        self.assertTrue(self._kept()["settled"])

    def test_a_host_no_longer_hardened_leaves_the_alert(self):
        self.Host._panel_address_changed(OLD, NEW)
        (self.listed | self.other).write({"hardened": False})
        self.assertFalse(self._alert())

    def test_an_archived_host_leaves_the_alert(self):
        self.Host._panel_address_changed(OLD, NEW)
        (self.listed | self.other).write({"active": False})
        self.assertFalse(self._alert())

    def test_an_archived_host_is_not_named_whatever_the_context(self):
        self.Host.with_context(active_test=False)._panel_address_changed(OLD, NEW)
        alert = self._alert()
        self.assertIn("pa-listed", alert.message)
        self.assertNotIn("pa-gone", alert.message)

    def test_a_deleted_host_leaves_the_alert(self):
        self.Host._panel_address_changed(OLD, NEW)
        self.listed.unlink()
        self.assertNotIn("pa-listed", self._alert().message)
        self.other.unlink()
        self.assertFalse(self._alert())
        self.assertTrue(self._kept()["settled"])

    def test_an_address_of_the_panel_a_layer_knows_admits_it(self):
        with patch.object(
            type(self.Host), "_hardening_panel_ip", autospec=True,
            return_value=NEW_NETWORK,
        ):
            self.Host._panel_address_changed(OLD, NEW)
        self.assertFalse(self._alert())

    def test_a_second_change_raises_the_alert_again(self):
        self.Host._panel_address_changed(OLD, NEW)
        first = self._alert()
        self.Host._panel_address_changed(NEW, LATER)
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertNotEqual(alert, first)
        self.assertEqual(first.state, "dismissed")
        self.assertIn(LATER, alert.message)
        self.assertIn("pa-ready", alert.message)


class TestTheListTheHostConfirmed(PanelAddressCase):
    """A host leaves the alert by what its firewall holds, not by the field."""

    def setUp(self):
        super().setUp()
        # The list is kept on a cursor of its own: test mode makes it ride
        # on this transaction, so the write is seen and rolled back.
        self.registry.enter_test_mode(self.cr)
        self.addCleanup(self.registry.leave_test_mode)

    def _confirm(self, host, op, ips, answer):
        """Change *host*'s list live, the host answering *answer*."""
        transport = MagicMock(spec=SSHTransport)
        script = CommandResult(f"[incubacloud] ok\nIC_SSH_ALLOW {answer}", 0)
        transport.run = AsyncMock(side_effect=lambda cmd, **kw: (
            script if "ssh_allowlist.sh" in cmd else CommandResult("", 0)
        ))
        transport.upload_text_files = AsyncMock()

        @asynccontextmanager
        async def _open(record):
            yield transport

        with patch.object(type(host), "get_transport", _open):
            result = host._ssh_allowlist_change(op, ips)
        # The host's answer and the judgement it led to were written in
        # another environment: read them afresh.
        self.env.invalidate_all()
        return result

    def test_a_host_whose_list_admits_the_new_address_leaves_the_alert(self):
        self.Host._panel_address_changed(OLD, NEW)
        self._confirm(self.listed, "add", [NEW], f"{OLD}, {NEW}")
        alert = self._alert()
        self.assertNotIn("pa-listed", alert.message)
        self.assertIn("pa-other", alert.message)
        self._confirm(self.other, "add", [NEW], f"203.0.113.7, {OLD}, {NEW}")
        self.assertFalse(self._alert())
        self.assertTrue(self._kept()["settled"])

    def test_a_hand_edit_of_the_list_does_not_take_a_host_out(self):
        self.Host._panel_address_changed(OLD, NEW)
        self.listed.write({"allowed_ssh_ips": f"{OLD}, {NEW}"})
        self.assertTrue(self.listed.ssh_allowlist_unconfirmed)
        self.assertIn("pa-listed", self._alert().message)
        self.listed.write({"allowed_ssh_ips": False})
        self.assertIn("pa-listed", self._alert().message)

    def test_a_list_emptied_after_the_last_hardening_is_named(self):
        self.listed.write({"allowed_ssh_ips": False})
        self.Host._panel_address_changed(OLD, NEW)
        self.assertIn("pa-listed", self._alert().message)

    def test_the_host_s_answer_confirms_a_list_edited_by_hand(self):
        self.listed.write({"allowed_ssh_ips": f"{OLD}, {NEW}"})
        # The edit by hand is a request of its own, done before.
        self.env.flush_all()
        self._confirm(self.listed, "add", [NEW], f"{OLD}, {NEW}")
        self.assertFalse(self.listed.ssh_allowlist_unconfirmed)
        self.Host._panel_address_changed(OLD, NEW)
        self.assertNotIn("pa-listed", self._alert().message)


class TestTheOdooShNotice(PanelAddressCase):
    """``/_odoo.sh/ip-change``: Odoo.sh's request, and only on Odoo.sh."""

    def _call(self, stage, **params):
        from odoo.addons.incubacloud.controllers import odoo_sh

        fake = MagicMock(spec=Request)
        fake.env = self.env
        environ = os.environ.copy()
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


@tagged("-at_install", "post_install")
class TestTheOdooShNoticeOverHttp(PanelAddressCase, HttpCase):
    """The request as Odoo.sh makes it: a ``GET`` with no session."""

    def _get(self, stage, query):
        environ = os.environ.copy()
        environ.pop("ODOO_STAGE", None)
        if stage:
            environ["ODOO_STAGE"] = stage
        with patch.dict(os.environ, environ, clear=True):
            return self.url_open(f"/_odoo.sh/ip-change?{query}", allow_redirects=False)

    def test_odoo_sh_s_request_raises_the_alert_as_the_public_user(self):
        response = self._get("production", f"old={OLD}&new={NEW}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")
        alert = self._alert()
        self.assertEqual(len(alert), 1)
        self.assertIn("pa-listed", alert.message)

    def test_off_odoo_sh_s_production_it_answers_404(self):
        response = self._get("staging", f"old={OLD}&new={NEW}")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self._alert())

    def test_a_request_without_an_old_address_answers_400(self):
        response = self._get("production", f"new={NEW}")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self._alert())
