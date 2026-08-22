"""
Transparently encrypted field types: the value is encrypted before it is
written to the database and decrypted when it is read back into Python.

Two shapes, differing only in their column type:

    from .encrypted_char import EncryptedChar, EncryptedText

    class MyModel(models.Model):
        secret = EncryptedChar(string="Secret")        # varchar, one line
        pem = EncryptedText(string="Private Key")      # text, multi-line

Both share ``EncryptedFieldMixin``, which is also what
``cloud.settings._rotate_all_secrets`` tests against to decide what to
rotate. Encrypting through anything else would produce ciphertext that
key rotation silently skips.

The raw database column always stores the ``enc:<token>`` form.
Python code (executors, controllers) always sees plain text.

Unreadable ciphertext is reported as it is read: a value the current
``INCUBACLOUD_SECRET_KEY`` chain cannot open raises (as it always did)
*and* opens a ``cloud.alert``. Before that, the only symptom was an
operation dying halfway with a generic error and no trace of which
secret was at fault — see ``_alert_unreadable`` for why the alert is
written on a cursor of its own.
"""
import logging

from odoo import SUPERUSER_ID, api, fields
from odoo.modules.registry import Registry

from .password_utils import decrypt_value, encrypt_value

_logger = logging.getLogger(__name__)

# Models that ARE the alert target — an alert about a host's password
# points at the host itself.
_SELF_ALERT_LINK = {
    'cloud.host': 'host_id',
    'cloud.instance': 'instance_id',
    'cloud.project': 'project_id',
}

ALERT_CODE_UNREADABLE = 'encrypted_value_unreadable'


def _alert_link(record):
    """Return ``(link_field, link_id)`` for *record*'s cloud.alert.

    Mirrors ``cloud.settings._rotation_alert_link``: an alert with no
    host/instance/project is invisible to the member-scoped record rule
    and impossible to trace back, so those stay log-only.
    """
    self_target = _SELF_ALERT_LINK.get(record._name)
    if self_target:
        return self_target, record.id
    for candidate in ('host_id', 'instance_id', 'project_id'):
        if candidate in record._fields and record[candidate]:
            return candidate, record[candidate].id
    return None, None


def unreadable_alert_vals(record, field_name):
    """Return the ``cloud.alert`` values for an unreadable secret.

    Pure: no database access, no side effect. ``link_field`` is None
    when the record has nothing to hang an alert on, in which case the
    caller logs instead of creating a targetless (and therefore
    invisible) alert.
    """
    link_field, link_id = _alert_link(record)
    vals = {
        'code': f"{ALERT_CODE_UNREADABLE}:{record._name}.{field_name}",
        'level': 'critical',
        'message': (
            f"Cannot decrypt {record._name}.{field_name} (id={record.id}). "
            f"The ciphertext was written with a key that is no longer in "
            f"INCUBACLOUD_SECRET_KEY, or it is corrupted. Any operation "
            f"reading this secret will keep failing until the value is "
            f"set again."
        ),
    }
    if link_field:
        vals[link_field] = link_id
    return link_field, vals


def create_unreadable_alert(env, record, field_name):
    """Create the alert for *record.field_name* in *env*, deduplicated.

    Split from ``_alert_unreadable`` so the payload and the dedup rule
    are exercisable against a normal test environment: the production
    path hands in an environment built on a private cursor, which a
    test transaction cannot observe.

    Returns the alert (empty recordset when deduplicated or targetless).
    """
    link_field, vals = unreadable_alert_vals(record, field_name)
    if not link_field:
        _logger.critical('[encrypted] %s', vals['message'])
        return env['cloud.alert'].browse()
    Alert = env['cloud.alert']
    # Dedup per (code, target): a secret read in a loop must not open
    # one alert per read.
    if Alert.search_count([
        ('code', '=', vals['code']),
        ('state', '=', 'active'),
        (link_field, '=', vals[link_field]),
    ], limit=1):
        return Alert.browse()
    return Alert.create(vals)


def _alert_unreadable(record, field_name):
    """Open a ``cloud.alert`` for a secret that will not decrypt.

    Written on a **separate cursor** on purpose. The caller is about to
    raise, and that exception normally aborts the whole transaction —
    an alert written on the current cursor would be rolled back with it
    and the operator would never see it. A private cursor commits on its
    own and survives.

    Best-effort by design: this runs inside a field read on an already
    failing path, so it must never be the thing that raises. Anything
    going wrong here is logged and swallowed, and the original
    decryption error still propagates to the caller.
    """
    try:
        with Registry(record.env.cr.dbname).cursor() as cr:
            create_unreadable_alert(
                api.Environment(cr, SUPERUSER_ID, {}), record, field_name,
            )
    except Exception:  # noqa: BLE001 — never mask the decryption error
        _logger.exception(
            '[encrypted] could not raise the unreadable-secret alert '
            'for %s.%s (id=%s)', record._name, field_name, record.id,
        )


class EncryptedFieldMixin:
    """Fernet encryption for a stored textual field.

    Mixed into a concrete ``fields`` class instead of being one,
    because the encrypted shapes differ only in their column type.
    Keeping the behaviour in a single place means a new shape cannot
    drift from the others, and gives rotation one type to test for.
    """

    def _decrypt_or_alert(self, value, record):
        """Decrypt *value*, flagging the record loudly if it cannot be."""
        try:
            return decrypt_value(value, record.env)
        except ValueError:
            # ``isinstance(id, int)`` filters out NewId: an in-memory
            # record has no row to point an alert at, and onchange
            # machinery must not spawn operator alerts.
            if isinstance(record.id, int):
                _alert_unreadable(record, self.name)
            raise

    # ── Write path ──────────────────────────────────────────────────────────

    def convert_to_write(self, value, record):
        value = super().convert_to_write(value, record)
        if not value:
            return value
        return encrypt_value(value, record.env)

    def convert_to_column(self, value, record, values=None, validate=True):
        value = super().convert_to_column(value, record, values=values, validate=validate)
        if not value:
            return value
        return encrypt_value(value, record.env)

    # ── Read path ───────────────────────────────────────────────────────────

    def convert_to_cache(self, value, record, validate=True):
        value = super().convert_to_cache(value, record, validate=validate)
        if not value:
            return value
        return self._decrypt_or_alert(value, record)

    def convert_to_record(self, value, record):
        value = super().convert_to_record(value, record)
        if not value:
            return value
        return self._decrypt_or_alert(value, record)


class EncryptedChar(EncryptedFieldMixin, fields.Char):
    """fields.Char that stores its value encrypted with Fernet."""


class EncryptedText(EncryptedFieldMixin, fields.Text):
    """fields.Text that stores its value encrypted with Fernet.

    Same behaviour as ``EncryptedChar`` on a ``text`` column. Use it
    for secrets that are not one-liners — a PEM key in a ``varchar``
    would also lose its multi-line widget in the form view.
    """
