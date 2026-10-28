"""Switching the assistant on needs section 12A of the Terms accepted (28-oct).

The manager ticks «I accept section 12A (AI features) of the Terms» with the
switch; without it the assistant stays off. The acceptance (date, version,
address, user) is kept on the settings and shown in the tab; on a tenant
panel IncubaCloud records it first (incubacloud_tenant, task 24).
"""

import json
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase

from ..models.cloud_settings import BLOCKED_PARAM
from .common import AssistantMixin

#: The version the stand-in for IncubaCloud answers.
VERSION = "2026-11-01"


def _recorded(_settings, acceptance):
    """Stand in for the hook as IncubaCloud answers: its version, the rest kept."""
    return {**acceptance, "v": VERSION}


@tagged("post_install", "-at_install")
class TestTermsAcceptance(AssistantMixin, HttpCase):
    """The box gates switching on; what is kept and what is shown."""

    def setUp(self):
        """Two managers, a developer, the assistant set up but off, nothing accepted."""
        super().setUp()
        self.manager = self._cloud_user("asis-terms-mgr", group="group_cloud_manager")
        self.other = self._cloud_user("asis-terms-mgr2", group="group_cloud_manager")
        self.dev = self._cloud_user("asis-terms-dev")
        self._configure(users=[self.dev], enabled=False, terms=False)
        self.Settings = type(self.env["cloud.settings"])
        patcher = patch.object(
            self.Settings,
            "_assistant_record_terms_acceptance",
            autospec=True,
            side_effect=_recorded,
        )
        self.hook = patcher.start()
        self.addCleanup(patcher.stop)

    def _rpc(self, path, login=None, **params):
        """Call *path* as a manager; return its result."""
        login = login or self.manager.login
        self.authenticate(login, login)
        return (
            self.url_open(
                f"/cloud/assistant/{path}",
                data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": params}),
                headers={"Content-Type": "application/json"},
            )
            .json()
            .get("result")
        )

    def _settings(self):
        """The settings as the database has them now."""
        self.env.invalidate_all()
        return self.env["cloud.settings"]._get_system()

    def test_without_the_box_it_stays_off(self):
        """Missing, false or anything but true: refused, nothing recorded."""
        for vals in (
            {"enabled": True},
            {"enabled": True, "accept_ai_terms": False},
            {"enabled": True, "accept_ai_terms": "yes"},
        ):
            with self.subTest(vals):
                result = self._rpc("save_settings", vals=vals)
                self.assertFalse(result["ok"])
                self.assertIn("12A", result["error"])
        self.hook.assert_not_called()
        settings = self._settings()
        self.assertFalse(settings.assistant_enabled)
        self.assertFalse(settings.assistant_terms_accepted_on)

    def test_with_the_box_it_is_on_and_the_acceptance_is_kept(self):
        """Date, the version answered, who and from where; shown in the tab."""
        result = self._rpc(
            "save_settings", vals={"enabled": True, "accept_ai_terms": True}
        )
        self.assertTrue(result["ok"], result)
        settings = self._settings()
        self.assertTrue(settings.assistant_enabled)
        self.assertTrue(settings.assistant_terms_accepted_on)
        self.assertEqual(settings.assistant_terms_version, VERSION)
        self.assertIn(self.manager.login, settings.assistant_terms_accepted_by)
        self.assertTrue(settings.assistant_terms_accepted_ip)
        shown = self._rpc("get_settings")["terms_acceptance"]
        self.assertEqual(
            shown,
            {
                "accepted_on": fields.Datetime.to_string(
                    settings.assistant_terms_accepted_on
                ),
                "version": VERSION,
                "user": settings.assistant_terms_accepted_by,
                "ip": settings.assistant_terms_accepted_ip,
            },
        )
        self.assertTrue(self.env["cloud.settings"]._assistant_available_for(self.dev))

    def test_a_save_that_keeps_it_on_records_nothing(self):
        """On already: another change, or the switch sent again, asks for nothing."""
        self._rpc("save_settings", vals={"enabled": True, "accept_ai_terms": True})
        self.hook.reset_mock()
        result = self._rpc("save_settings", vals={"enabled": True, "monthly_limit": 5})
        self.assertTrue(result["ok"], result)
        self.hook.assert_not_called()

    def test_off_keeps_it_and_on_again_is_a_new_acceptance(self):
        """Off: the record stays. On again needs the box, and the latest is kept."""
        self._rpc("save_settings", vals={"enabled": True, "accept_ai_terms": True})
        self._rpc("save_settings", vals={"enabled": False})
        self.assertIn(self.manager.login, self._settings().assistant_terms_accepted_by)
        result = self._rpc(
            "save_settings", login=self.other.login, vals={"enabled": True}
        )
        self.assertFalse(result["ok"])
        result = self._rpc(
            "save_settings",
            login=self.other.login,
            vals={"enabled": True, "accept_ai_terms": True},
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.hook.call_count, 2)
        self.assertIn(self.other.login, self._settings().assistant_terms_accepted_by)

    def test_an_acceptance_not_recorded_leaves_it_off(self):
        """The hook refuses (IncubaCloud unreachable): its text, and still off."""
        self.hook.side_effect = UserError("IncubaCloud did not answer.")
        result = self._rpc(
            "save_settings", vals={"enabled": True, "accept_ai_terms": True}
        )
        self.assertEqual(result, {"ok": False, "error": "IncubaCloud did not answer."})
        settings = self._settings()
        self.assertFalse(settings.assistant_enabled)
        self.assertFalse(settings.assistant_terms_accepted_on)

    def test_a_database_that_blocks_it_never_asks_for_the_acceptance(self):
        """IncubaCloud's own panel: the block answers first; nothing is recorded."""
        self.env["ir.config_parameter"].sudo().set_param(BLOCKED_PARAM, "Not here.")
        result = self._rpc(
            "save_settings", vals={"enabled": True, "accept_ai_terms": True}
        )
        self.assertEqual(result, {"ok": False, "error": "Not here."})
        self.hook.assert_not_called()

    def test_switched_on_through_the_orm_nobody_has_it(self):
        """Without an acceptance the switch alone gives the assistant to nobody."""
        self._configure(users=[self.dev], terms=False)
        self.assertFalse(self.env["cloud.settings"]._assistant_available_for(self.dev))

    def test_a_manager_cannot_write_the_acceptance_through_the_orm(self):
        """The four fields are the system's: a manager's write is refused."""
        settings = self._settings().with_user(self.manager)
        with self.assertRaises(AccessError):
            settings.write({"assistant_terms_accepted_on": fields.Datetime.now()})


@tagged("post_install", "-at_install")
class TestCoreKeepsItOnThePanel(AssistantMixin, TransactionCase):
    """Core alone: the hook keeps the acceptance here, with no version."""

    def test_the_panel_keeps_it_without_a_version(self):
        """Nothing outside the panel knows the Terms here."""
        tenant = self.env["ir.module.module"].search(
            [("name", "=", "incubacloud_tenant"), ("state", "=", "installed")]
        )
        if tenant:
            self.skipTest(
                "a tenant panel has IncubaCloud record it (incubacloud_tenant)"
            )
        acceptance = {
            "at": fields.Datetime.now(),
            "ip": "198.51.100.7",
            "user": "Ana (ana@example.test)",
        }
        kept = self.env["cloud.settings"]._assistant_record_terms_acceptance(acceptance)
        self.assertEqual(kept, {**acceptance, "v": ""})
