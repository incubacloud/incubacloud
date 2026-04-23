"""Verify that a row with un-rotatable ciphertext does NOT block the
rest of the _rotate_all_secrets batch.

Stranded rows happen when a value was encrypted with a key that is no
longer present in the MultiFernet chain (operator rotated the env var
without running the cron while the old key was still there, or the DB
was restored from a different environment). Previously a single
stranded row raised `ValueError` and aborted the entire pass; the fix
logs + optionally raises a `cloud.alert` and keeps going.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestRotationTolerance(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Settings = self.env['cloud.settings']
        self.Host = self.env['cloud.host']
        self.Alert = self.env['cloud.alert']

    def _make_host(self, name, traefik_pwd):
        """Create a host and force-write a raw ciphertext into the
        ``traefik_panel_password`` column, bypassing the ORM so we can
        smuggle in a value that will fail to rotate."""
        host = self.Host.create({
            'name': name,
            'ip_address': '127.0.0.1',
            'port': 22,
            'user': 'root',
            'wildcard_domain': 'example.com',
            'traefik_panel_password': 'ok-value',
        })
        self.env.cr.execute(
            "UPDATE cloud_host SET traefik_panel_password=%s WHERE id=%s",
            (traefik_pwd, host.id),
        )
        return host

    def test_stranded_row_does_not_block_others(self):
        """Row A has rotatable ciphertext, row B has un-rotatable.
        After the pass: A was rotated (or at least not crashed on),
        B is flagged, the method returns without raising."""
        host_a = self._make_host('rotate-a', 'enc:valid-looking-token-aaa')
        host_b = self._make_host('rotate-b', 'enc:valid-looking-token-bbb')

        def _fake_rotate(value, env=None):
            # Succeed for host_a's value, fail for host_b's.
            if 'aaa' in value:
                return value  # no-op rotation, still counts as OK
            raise ValueError(
                "Failed to decrypt stored secret; the encryption key "
                "may have been rotated or the ciphertext corrupted."
            )

        with patch(
            'odoo.addons.incubacloud.models.cloud_settings.rotate_value',
            side_effect=_fake_rotate,
        ):
            stats = self.Settings.sudo()._rotate_all_secrets()

        key = 'cloud_host.traefik_panel_password'
        self.assertIn(key, stats)
        self.assertIn(host_b.id, stats[key]['failed'])
        self.assertNotIn(host_a.id, stats[key]['failed'])

    def test_stranded_row_opens_alert_on_host(self):
        host = self._make_host('rotate-c', 'enc:valid-looking-token-ccc')

        with patch(
            'odoo.addons.incubacloud.models.cloud_settings.rotate_value',
            side_effect=ValueError("broken"),
        ):
            self.Settings.sudo()._rotate_all_secrets()

        alerts = self.Alert.search([
            ('host_id', '=', host.id),
            ('code', '=',
             'secret_rotation_stranded:cloud.host.traefik_panel_password'),
            ('state', '=', 'active'),
        ])
        self.assertEqual(len(alerts), 1)
        self.assertIn(
            'traefik_panel_password', alerts.message,
        )

    def test_alert_is_deduped_on_second_pass(self):
        host = self._make_host('rotate-d', 'enc:valid-looking-token-ddd')

        with patch(
            'odoo.addons.incubacloud.models.cloud_settings.rotate_value',
            side_effect=ValueError("broken"),
        ):
            self.Settings.sudo()._rotate_all_secrets()
            self.Settings.sudo()._rotate_all_secrets()

        alerts = self.Alert.search([
            ('host_id', '=', host.id),
            ('code', '=',
             'secret_rotation_stranded:cloud.host.traefik_panel_password'),
            ('state', '=', 'active'),
        ])
        self.assertEqual(len(alerts), 1)
