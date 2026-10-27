"""Secrets never go in or out: names from the registry, values from the database."""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.encrypted_char import EncryptedChar
from odoo.addons.incubacloud_mcp.protocol.redact import MASK, drop_secret_keys

HOST_PASSWORD = "host-pass-1234567"


@tagged("post_install", "-at_install")
class TestSecrets(TransactionCase):
    """Which names and values are secrets, and arguments that carry one.

    ``post_install``: it creates a host, whose table other modules may
    extend with required columns.
    """

    def setUp(self):
        """A host with a password, stored encrypted."""
        super().setUp()
        self.Secrets = self.env["incubacloud.mcp.secrets"]
        self.host = self.env["cloud.host"].create(
            {
                "name": "secret-host",
                "ip_address": "10.0.0.82",
                "user": "ubuntu",
                "wildcard_domain": "secret.example.com",
                "login_type": "password",
                "password": HOST_PASSWORD,
            }
        )

    def test_every_encrypted_field_is_a_secret_name(self):
        """The encrypted fields of every model are secret names; others are not."""
        names = self.Secrets._field_names()
        for name in (
            "password",
            "postgres_password",
            "s3_secret_access_key",
            "secret_payload",
            "cloud_telegram_bot_token",
            "auth_token",
        ):
            self.assertIn(name, names)
        self.assertNotIn("name", names)

    def test_a_new_encrypted_field_is_covered_without_touching_the_mcp(self):
        """An encrypted field added tomorrow is a secret name the same day."""
        Host = type(self.env["cloud.host"])
        # Odoo 19 keeps a model's fields in a read-only mapping: the whole
        # attribute is swapped, not one entry.
        grown = Host._fields | {"ic_test_secret": EncryptedChar()}
        with patch.object(Host, "_fields", grown):
            names = self.Secrets._field_names()
        self.assertIn("ic_test_secret", names)
        self.assertEqual(
            drop_secret_keys({"ic_test_secret": "x", "id": 1}, names), {"id": 1}
        )

    def test_stored_values_are_known_short_and_unreadable_ones_are_not(self):
        """Stored secrets are known and masked; short or unreadable ones are not."""
        self.env.cr.execute(
            "UPDATE cloud_host SET traefik_panel_password = 'enc:not-a-token' "
            "WHERE id = %s",
            (self.host.id,),
        )
        values = self.Secrets._known_values()
        self.assertIn(HOST_PASSWORD, values)
        self.assertFalse([value for value in values if len(value) < 8])
        self.assertEqual(
            self.Secrets._scrubber().text(f"ssh with {HOST_PASSWORD}"),
            f"ssh with {MASK}",
        )

    def test_an_argument_named_like_a_secret_is_refused_at_any_depth(self):
        """A key named like an encrypted field is refused, however deep."""
        with self.assertRaisesRegex(UserError, "Secrets are changed in the panel"):
            self.Secrets._check_arguments({"vals": {"db": {"postgres_password": "x"}}})
        self.Secrets._check_arguments({"vals": {"name": "x"}})
