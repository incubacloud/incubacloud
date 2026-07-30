"""Fail-loud on unreadable secrets.

A value the current ``INCUBACLOUD_SECRET_KEY`` chain cannot open used to
surface only as a generic error thrown from wherever the secret happened
to be read — in production this showed up as ``full_setup`` dying in
Phase 1 with nothing pointing at the real cause. Reading such a value
now also opens a ``cloud.alert`` naming the exact record and field.

Coverage is split in two because the alert is written on a **private
cursor** (the caller's transaction is about to be aborted by the raise,
so an alert written on it would be rolled back with it) and a test
transaction cannot observe another cursor's work:

  * the wiring — a failed read calls the alert path, a healthy one does
    not — is asserted with a spy on the seam;
  * the payload and the dedup rule are asserted against the test
    environment through ``create_unreadable_alert``, which takes the
    environment as an argument precisely so this is possible.

What stays uncovered is the three-line cursor plumbing in between.
"""
from unittest.mock import patch

from odoo.addons.incubacloud.models import encrypted_char
from odoo.addons.incubacloud.models.encrypted_char import (
    ALERT_CODE_UNREADABLE,
    create_unreadable_alert,
    unreadable_alert_vals,
)
from odoo.tests.common import TransactionCase

# A Fernet token no key of ours can open — what a rotated-away key
# looks like from the reader's side.
_UNOPENABLE = 'enc:gAAAAABmb3Jn3WQtY2lwaGVydGV4dC10aGF0LW5vLWtleQ=='


class TestEncryptedCharUnreadableAlert(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'Unreadable Host',
            'ip_address': '10.9.9.9',
            'user': 'ubuntu',
            'wildcard_domain': 'unreadable.example.com',
            'password': 'a-real-secret',
        })
        self.code = f"{ALERT_CODE_UNREADABLE}:cloud.host.password"

    def _corrupt_password(self):
        """Replace the ciphertext with a token no key can open.

        Raw SQL on purpose: going through the ORM would re-encrypt with
        the current key, which is the opposite of the state under test.
        """
        self.env.cr.execute(
            "UPDATE cloud_host SET password = %s WHERE id = %s",
            (_UNOPENABLE, self.host.id),
        )
        self.host.invalidate_recordset(['password'])

    # ── Wiring: does a failed read reach the alert path? ─────────────

    def test_unreadable_secret_alerts_and_still_raises(self):
        """The operator gets an alert; the caller still gets the error."""
        self._corrupt_password()

        with patch.object(
            encrypted_char, '_alert_unreadable',
        ) as spy, self.assertRaises(ValueError):
            self.host.password  # noqa: B018 — reading is the trigger

        spy.assert_called_once()
        record, field_name = spy.call_args.args
        self.assertEqual(record, self.host)
        self.assertEqual(field_name, 'password')

    def test_healthy_secret_raises_no_alert(self):
        """The happy path stays silent."""
        with patch.object(encrypted_char, '_alert_unreadable') as spy:
            self.assertEqual(self.host.password, 'a-real-secret')
        spy.assert_not_called()

    # ── Payload and dedup ────────────────────────────────────────────

    def test_alert_payload_points_at_the_record_and_field(self):
        """The message must name what to fix, and target the host."""
        link_field, vals = unreadable_alert_vals(self.host, 'password')

        self.assertEqual(link_field, 'host_id')
        self.assertEqual(vals['host_id'], self.host.id)
        self.assertEqual(vals['code'], self.code)
        self.assertEqual(vals['level'], 'critical')
        self.assertIn('cloud.host.password', vals['message'])
        self.assertIn(str(self.host.id), vals['message'])

    def test_alert_is_created_once_and_deduplicated(self):
        """A secret read in a loop must not open one alert per read."""
        first = create_unreadable_alert(self.env, self.host, 'password')
        self.assertTrue(first)
        self.assertEqual(first.host_id, self.host)
        self.assertEqual(first.level, 'critical')

        again = create_unreadable_alert(self.env, self.host, 'password')
        self.assertFalse(
            again, 'a second read must reuse the open alert, not stack one',
        )
        self.assertEqual(len(self.env['cloud.alert'].search([
            ('code', '=', self.code),
            ('host_id', '=', self.host.id),
        ])), 1)

    def test_dismissed_alert_can_be_raised_again(self):
        """Once the operator dismisses it, a new failure must resurface."""
        first = create_unreadable_alert(self.env, self.host, 'password')
        first.state = 'dismissed'

        second = create_unreadable_alert(self.env, self.host, 'password')

        self.assertTrue(
            second,
            'dedup keys on ACTIVE alerts — a dismissed one must not '
            'silence the next failure',
        )

    def test_targetless_record_is_logged_not_alerted(self):
        """An alert nobody can see is worse than a log line.

        ``cloud.settings`` has no host/instance/project to hang an alert
        on, and a targetless row is hidden by the member record rule.
        """
        settings = self.env['cloud.settings'].sudo()._get()

        link_field, vals = unreadable_alert_vals(settings, 'github_pat')

        self.assertIsNone(link_field)
        self.assertNotIn('host_id', vals)
        self.assertFalse(create_unreadable_alert(
            self.env, settings, 'github_pat',
        ))
