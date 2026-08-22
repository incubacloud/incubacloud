"""The GitHub App private key must not be readable from the database.

It was a plain ``fields.Text`` next to an already-encrypted
``webhook_secret``, so a dump, a filesystem backup or read access to
the table handed over a key that signs App JWTs.

Two of these tests exist for a trap rather than for the feature. A PEM
is multi-line, so it needs a ``text`` column, which meant a second
field shape — and ``_rotate_all_secrets`` finds what to rotate with an
``isinstance`` check. A shape that encrypts but is not recognised there
produces ciphertext key rotation silently skips, which is worse than
plain text because nothing shows it.
"""
from odoo.tests.common import TransactionCase

_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "ZmFrZWtleWZha2VrZXlmYWtla2V5ZmFrZWtleQ==\n"
    "bGluZXR3b2xpbmV0d29saW5ldHdvbGluZXR3bw==\n"
    "-----END RSA PRIVATE KEY-----"
)


class TestPrivateKeyAtRest(TransactionCase):

    def setUp(self):
        super().setUp()
        self.App = self.env['cloud.github.app']
        self.App.search([]).unlink()

    def _raw(self, app):
        """Return the private_key column straight from Postgres."""
        app.flush_recordset(['private_key'])
        self.env.cr.execute(
            "SELECT private_key FROM cloud_github_app WHERE id = %s",
            (app.id,),
        )
        return self.env.cr.fetchone()[0]

    def test_key_is_ciphertext_on_disk(self):
        app = self.App.create({'app_id': '1', 'private_key': _PEM})
        raw = self._raw(app)
        self.assertTrue(
            raw.startswith('enc:'),
            "expected enc:<token> in the column, got %r" % raw[:40],
        )
        self.assertNotIn('BEGIN RSA PRIVATE KEY', raw)

    def test_orm_still_returns_the_pem(self):
        app = self.App.create({'app_id': '1', 'private_key': _PEM})
        app.invalidate_recordset(['private_key'])
        # Newlines intact: a varchar column would have been the wrong
        # shape for a PEM even if it survived the round trip.
        self.assertEqual(app.private_key, _PEM)

    def test_column_stays_text(self):
        # Guards the shape itself. Char would truncate nothing today,
        # but it would also swap the form widget for a single-line
        # input, which is not a place to paste a private key.
        self.env.cr.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'cloud_github_app' "
            "AND column_name = 'private_key'"
        )
        self.assertEqual(self.env.cr.fetchone()[0], 'text')

    def test_credentials_helper_gets_plaintext(self):
        # The JWT signer parses this PEM. If decryption were skipped
        # anywhere on that path, GitHub authentication would break.
        app = self.App.create({'app_id': '77', 'private_key': _PEM})
        app.invalidate_recordset(['private_key'])
        self.assertEqual(app._get_credentials().private_key_pem, _PEM)

    def test_legacy_plaintext_row_still_reads(self):
        # Rows written before the migration have no ``enc:`` prefix.
        # ``decrypt_value`` passes those through, which is what makes
        # the migration safe to run late rather than at flag-day.
        app = self.App.create({'app_id': '2', 'private_key': _PEM})
        self.env.cr.execute(
            "UPDATE cloud_github_app SET private_key = %s WHERE id = %s",
            (_PEM, app.id),
        )
        app.invalidate_recordset(['private_key'])
        self.assertEqual(app.private_key, _PEM)


class TestPrivateKeyIsRotatable(TransactionCase):
    """The trap: encrypted but invisible to key rotation."""

    def setUp(self):
        super().setUp()
        self.App = self.env['cloud.github.app']
        self.App.search([]).unlink()

    def _raw(self, app):
        """Return the private_key column straight from Postgres."""
        app.flush_recordset(['private_key'])
        self.env.cr.execute(
            "SELECT private_key FROM cloud_github_app WHERE id = %s",
            (app.id,),
        )
        return self.env.cr.fetchone()[0]

    def test_rotation_sweep_picks_up_the_private_key(self):
        app = self.App.create({'app_id': '3', 'private_key': _PEM})
        before = self._raw(app)

        stats = self.env['cloud.settings'].sudo()._rotate_all_secrets()

        key = 'cloud_github_app.private_key'
        self.assertIn(
            key, stats,
            "EncryptedText escaped the rotation sweep: the key would be "
            "encrypted with a retired Fernet key forever, and nothing "
            "would report it. Found: %s" % sorted(stats),
        )
        self.assertEqual(stats[key]['rotated'], 1)
        self.assertFalse(stats[key]['failed'])
        # Re-encrypted, and still the same key underneath.
        self.assertNotEqual(self._raw(app), before)
        app.invalidate_recordset(['private_key'])
        self.assertEqual(app.private_key, _PEM)

    def test_sweep_still_picks_up_char_fields(self):
        # The refactor moved the behaviour to a mixin; this pins that
        # the original shape did not fall out of the sweep with it.
        app = self.App.create({
            'app_id': '4',
            'private_key': _PEM,
            'webhook_secret': 'a-webhook-secret',
        })
        app.flush_recordset(['webhook_secret'])

        stats = self.env['cloud.settings'].sudo()._rotate_all_secrets()

        self.assertIn('cloud_github_app.webhook_secret', stats)


class TestSettingsPanelSurvivesABrokenKey(TransactionCase):
    """An unreadable secret must not take the settings screen down."""

    def setUp(self):
        super().setUp()
        self.App = self.env['cloud.github.app']
        self.App.search([]).unlink()

    def test_has_private_key_reports_stored_when_undecryptable(self):
        from odoo.addons.incubacloud.controllers._data_load._helpers import (
            _has_encrypted,
        )
        app = self.App.create({'app_id': '5', 'private_key': _PEM})
        # Ciphertext this Fernet chain cannot open — what a restore
        # from another environment leaves behind.
        self.env.cr.execute(
            "UPDATE cloud_github_app SET private_key = %s WHERE id = %s",
            ('enc:not-a-real-fernet-token', app.id),
        )
        app.invalidate_recordset(['private_key'])
        # True, not False: there IS a key stored, it just cannot be
        # read. Reporting False would invite the operator to think the
        # field is empty.
        self.assertTrue(_has_encrypted(app, 'private_key'))
