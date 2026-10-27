"""Secrets never go in or out: names from the registry, values from the database."""

import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.encrypted_char import EncryptedChar
from odoo.addons.incubacloud.models.password_utils import decrypt_value
from odoo.addons.incubacloud_mcp.models import mcp_secrets
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

    def test_the_names_are_read_once_per_registry(self):
        """The names are computed once, not on every call."""
        self.assertIs(self.Secrets._field_names(), self.Secrets._field_names())

    def test_a_new_encrypted_field_is_covered_without_touching_the_mcp(self):
        """An encrypted field added tomorrow is a secret name the same day.

        A module that adds one loads a new registry, whose cache is empty:
        here the cache is cleared instead.
        """
        Host = type(self.env["cloud.host"])
        # Odoo 19 keeps a model's fields in a read-only mapping: the whole
        # attribute is swapped, not one entry.
        grown = Host._fields | {"ic_test_secret": EncryptedChar()}
        self.addCleanup(self.env.registry.clear_cache)
        with patch.object(Host, "_fields", grown):
            self.env.registry.clear_cache()
            names = self.Secrets._field_names()
        self.assertIn("ic_test_secret", names)
        self.assertEqual(
            drop_secret_keys({"ic_test_secret": "x", "id": 1}, names), {"id": 1}
        )

    def test_stored_values_are_known_short_and_unreadable_ones_are_not(self):
        """Stored secrets are known and masked; short or unreadable ones are not."""
        self.host.key_file = "sh0rt6"
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_host SET traefik_panel_password = 'enc:not-a-token' "
            "WHERE id = %s",
            (self.host.id,),
        )
        values = self.Secrets._known_values()
        self.assertIn(HOST_PASSWORD, values)
        self.assertNotIn("sh0rt6", values)
        self.assertFalse([value for value in values if len(value) < 8])
        self.assertEqual(
            self.Secrets._scrubber().text(f"ssh with {HOST_PASSWORD}"),
            f"ssh with {MASK}",
        )

    def test_a_secret_stored_as_json_is_masked_by_its_leaves(self):
        """A job's netrc credential is masked where a log shows its password."""
        job_type = self.env["cloud.job.type"].create(
            {"name": "Secret job", "code": "mcp_secret_job", "apply_to": "host"}
        )
        self.env["cloud.job"].sudo().create(
            {
                "name": "restore",
                "host_id": self.host.id,
                "job_type_id": job_type.id,
                "secret_payload": json.dumps(
                    {
                        "machine": "backup.example.test",
                        "login": "backup",
                        "password": "bk-pass-5531",
                    }
                ),
            }
        )
        self.assertEqual(
            self.Secrets._scrubber().text(
                "curl -u backup:bk-pass-5531 https://backup.example.test/x"
            ),
            f"curl -u backup:{MASK} https://backup.example.test/x",
        )

    def test_a_secret_is_decrypted_once_and_never_outlives_its_record(self):
        """The second read decrypts nothing; a deleted secret is no longer known."""
        host = self.env["cloud.host"].create(
            {
                "name": "cached-host",
                "ip_address": "10.0.0.84",
                "user": "ubuntu",
                "wildcard_domain": "cached.example.com",
                "login_type": "password",
                "password": "cached-pass-24680",
            }
        )
        self.env.flush_all()
        self.assertIn("cached-pass-24680", self.Secrets._known_values())
        with patch.object(
            mcp_secrets, "decrypt_value", autospec=True, side_effect=decrypt_value
        ) as decrypt:
            self.assertIn("cached-pass-24680", self.Secrets._known_values())
        decrypt.assert_not_called()
        host.unlink()
        self.env.flush_all()
        self.assertNotIn("cached-pass-24680", self.Secrets._known_values())

    def test_the_jobs_that_hold_a_secret_are_indexed(self):
        """A partial index finds the few jobs with a secret among all the jobs."""
        self.env.cr.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = %s",
            ("cloud_job_secret_payload_set_index",),
        )
        (definition,) = self.env.cr.fetchone() or ("",)
        self.assertIn("secret_payload IS NOT NULL", definition)

    def test_secrets_kept_in_system_parameters_are_known(self):
        """The database secret and the module's key are masked too."""
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("database.secret", "db-secret-1357924680")
        self.env["cloud.settings"]._mcp_secret()
        values = self.Secrets._known_values()
        self.assertIn("db-secret-1357924680", values)
        self.assertIn(ICP.get_param("incubacloud.mcp.secret"), values)
        self.assertEqual(
            self.Secrets._scrubber().text("key db-secret-1357924680"), f"key {MASK}"
        )

    def test_an_argument_named_like_a_secret_is_refused_at_any_depth(self):
        """A key named like an encrypted field is refused, however deep."""
        with self.assertRaisesRegex(UserError, "Secrets are changed in the panel"):
            self.Secrets._check_arguments({"vals": {"db": {"postgres_password": "x"}}})
        self.Secrets._check_arguments({"vals": {"name": "x"}})
