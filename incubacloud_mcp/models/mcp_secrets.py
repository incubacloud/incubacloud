"""Which values are secrets, read where they live.

Names come from the registry — every field built on
``EncryptedFieldMixin``, of every model — so a new encrypted field is
covered the day it is added. Values are read with SQL and decrypted here,
not through the ORM: reading an unreadable value through the field raises
an operator alert, and a scan must not.

Every call reads the ciphertexts again — cheap: the one big table, the
jobs, has an index of the few rows that hold one — and decrypts only
those this process has not seen. Nothing expires because nothing can go
stale: a secret changed or deleted is a ciphertext no longer read.
"""

import threading

from psycopg2 import sql

from odoo import api, models
from odoo.exceptions import UserError

from odoo.addons.incubacloud.models.encrypted_char import EncryptedFieldMixin
from odoo.addons.incubacloud.models.password_utils import decrypt_value

from ..protocol.redact import MIN_SECRET_LEN, Scrubber, find_secret_key, secret_parts
from .cloud_settings import SECRET_PARAM

#: System parameters that hold a secret outside any encrypted field.
SECRET_PARAMS = (SECRET_PARAM, "database.secret")

#: ``{(database, ciphertext): what it hides}`` in this process.
_PARTS_BY_CIPHER = {}
#: Entries kept before the map starts over.
_MAX_CACHED = 20_000
_CACHE_LOCK = threading.Lock()


def _parts_of(env, cipher):
    """Return what *cipher* hides, 8 characters or more, decrypting it once.

    An unreadable value hides nothing, and is not retried in this process.

    :rtype: frozenset[str]
    """
    key = (env.cr.dbname, cipher)
    parts = _PARTS_BY_CIPHER.get(key)
    if parts is not None:
        return parts
    try:
        plain = decrypt_value(cipher, env)
    except ValueError:
        plain = ""
    parts = frozenset(
        part for part in secret_parts(plain or "") if len(part) >= MIN_SECRET_LEN
    )
    with _CACHE_LOCK:
        if len(_PARTS_BY_CIPHER) >= _MAX_CACHED:
            _PARTS_BY_CIPHER.clear()
        _PARTS_BY_CIPHER[key] = parts
    return parts


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
        pieces (:func:`secret_parts`); so do those of
        :meth:`_extra_known_values`.

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
                    values |= _parts_of(self.env, cipher)
        values |= {
            value
            for value in self._extra_known_values()
            if value and len(value) >= MIN_SECRET_LEN
        }
        return values

    @api.model
    def _extra_known_values(self):
        """Return the secrets kept outside encrypted fields.

        Here the system parameters of :data:`SECRET_PARAMS`; a module on
        top adds its own (the tenant, its OIDC client secret).

        :rtype: set[str]
        """
        ICP = self.env["ir.config_parameter"].sudo()
        return {ICP.get_param(key) or "" for key in SECRET_PARAMS}

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
