"""Which values are secrets, read where they live.

Names come from the registry — every field built on
``EncryptedFieldMixin``, of every model — so a new encrypted field is
covered the day it is added. Values are read with SQL and decrypted here,
not through the ORM: reading an unreadable value through the field raises
an operator alert, and a scan must not.
"""

from psycopg2 import sql

from odoo import api, models
from odoo.exceptions import UserError

from odoo.addons.incubacloud.models.encrypted_char import EncryptedFieldMixin
from odoo.addons.incubacloud.models.password_utils import decrypt_value

from ..protocol.redact import MIN_SECRET_LEN, Scrubber, find_secret_key, secret_parts


class McpSecrets(models.AbstractModel):
    """The secrets of the database, by name and by value."""

    _name = "incubacloud.mcp.secrets"
    _description = "Secrets agents never see"

    @api.model
    def _field_names(self):
        """Return the names of every encrypted field of the registry.

        :rtype: frozenset[str]
        """
        return frozenset(
            name
            for model in self.env.registry.values()
            for name, field in model._fields.items()
            if isinstance(field, EncryptedFieldMixin)
        )

    @api.model
    def _known_values(self):
        """Return every decryptable secret of the database, 8 characters or more.

        A secret stored as a JSON document or a PEM block counts with its
        pieces (:func:`secret_parts`).

        :rtype: set[str]
        """
        values = set()
        cr = self.env.cr
        for model in self.env.registry.values():
            if model._abstract or model._transient or not model._auto:
                continue
            for name, field in model._fields.items():
                if not isinstance(field, EncryptedFieldMixin) or not field.store:
                    continue
                cr.execute(
                    sql.SQL(
                        "SELECT {f} FROM {t} WHERE {f} IS NOT NULL AND {f} != ''"
                    ).format(f=sql.Identifier(name), t=sql.Identifier(model._table))
                )
                for (cipher,) in cr.fetchall():
                    try:
                        plain = decrypt_value(cipher, self.env)
                    except ValueError:
                        continue
                    if plain:
                        values |= {
                            part
                            for part in secret_parts(plain)
                            if len(part) >= MIN_SECRET_LEN
                        }
        return values

    @api.model
    def _scrubber(self, extra=()):
        """Return a :class:`Scrubber` of this database's secrets.

        :param extra: more values to mask, such as the secrets known
            before a call that may have changed them
        """
        return Scrubber(self._known_values() | set(extra))

    @api.model
    def _check_arguments(self, args):
        """Refuse arguments that carry a secret, at any depth.

        :raises UserError: when a key is the name of an encrypted field
        """
        if find_secret_key(args, self._field_names()):
            raise UserError(self.env._("Secrets are changed in the panel."))
