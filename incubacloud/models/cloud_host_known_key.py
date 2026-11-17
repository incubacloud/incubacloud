"""A server's known SSH keys: their sign-ins are recorded, without an alert.

The security events' cron (``cloud_security_events.py``) raises
``security_ssh_login_foreign`` for every SSH sign-in with a key that is
not the panel's. An administrator who signs in to their own servers with
their own key was paged on every sign-in. Each server keeps a list of the
keys its administrators know: a sign-in with one of them is shown in
Metrics → Security as the known key's, and raises nothing (the design
note of 17-nov-2026, reviewed by Fable).

**Only the fingerprint.** What an administrator pastes (the line of a
``.pub`` file, the line ``ssh-keygen -lf`` prints, or a ``SHA256:``
fingerprint) is read into the fingerprint sshd logs, before anything is
written; a private key is refused without being written or logged, and
its text is not repeated in the error.

**Written by two methods of the server alone.** Nobody may create or
write a row over the ORM, Administrators included: ``cloud.host``'s
``_security_known_key_add`` and ``_security_known_key_remove`` check the
role and the write access to the server, and write as the system. A key
is never edited: its fingerprint, its addresses, its server and its dates
stay as they were added; only its name may be written. Removing a key
archives it, and an archived key never comes back: reactivating it would
open its window again over the sign-ins between.

**Each key counts in its window**, from ``added_at`` to ``removed_at``,
compared with the instant of the sign-in's line, which is the server's
own clock. A key added now covers no earlier sign-in, also one the cron
reads late (a silent server's, up to 24 hours back), and a key removed
covers none after.

**Whoever adds a key is seen.** The addition writes an audit row, raises
the critical alert ``security_known_key_added`` (one per key, so each one
notifies, and left open when the key is removed) and emails every
Administrator and the account's holder, in the same transaction as the
row. The removal and a new name write an audit row.

A known key covers the sign-in alone: the ``sudo`` commands and the
changes to the watched files of its session alert as before (the design
note, section 3).
"""
import base64
import binascii
import hashlib
import ipaddress
import logging
import re
import unicodedata
from typing import NamedTuple

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ._concurrency import is_concurrency_error

_logger = logging.getLogger(__name__)

#: The alert an addition raises: critical, one per key, never among the
#: events' alerts (``cloud_security_events.EVENT_CODES``), whose hourly cap
#: would hold it back.
CODE_KNOWN_ADDED = "security_known_key_added"
#: The fingerprint as sshd logs it and ``ssh-keygen -lf`` prints it.
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
#: A fingerprint alone: with or without its prefix, with or without the
#: padding base64 would add.
_FINGERPRINT_ALONE = re.compile(r"^(?:SHA256:)?([A-Za-z0-9+/]{43})=?$")
#: A line of ``ssh-keygen -lf``: ``256 SHA256:… comment (ED25519)``.
_KEYGEN_LINE = re.compile(
    r"^\d{1,5} SHA256:([A-Za-z0-9+/]{43})=?(?: .*)? \(([A-Z0-9-]{1,32})\)$",
    re.DOTALL,
)
#: An MD5 fingerprint, prefixed or as sixteen pairs of hex digits.
_MD5 = re.compile(r"(?:^|\s)(?:MD5:)?(?:[0-9a-f]{2}:){15}[0-9a-f]{2}(?:\s|$)", re.IGNORECASE)
#: What only a private key holds.
_PRIVATE_MARKS = ("PRIVATE KEY", "PUTTY-USER-KEY-FILE")
#: The key types a public key line may name, and how sshd writes them.
KEY_TYPES = {
    "ssh-ed25519": "ED25519",
    "ssh-rsa": "RSA",
    "ssh-dss": "DSA",
    "ecdsa-sha2-nistp256": "ECDSA",
    "ecdsa-sha2-nistp384": "ECDSA",
    "ecdsa-sha2-nistp521": "ECDSA",
    "sk-ssh-ed25519@openssh.com": "ED25519-SK",
    "sk-ecdsa-sha2-nistp256@openssh.com": "ECDSA-SK",
}
#: How long a key's name may be, how many addresses it may name, and how
#: many active keys a server keeps; how many removed keys the card lists.
NAME_MAX = 64
NETWORKS_MAX = 20
ACTIVE_MAX = 20
REMOVED_SHOWN = 20
#: The widest network Only from takes, by IP version: two halves of the
#: whole space would be any address again, as a /0 is (Fable's review of
#: e5acd3f4).
NETWORK_MIN_PREFIX = {4: 8, 6: 32}
#: What a name may not hold besides control characters: the quotes the
#: alerts put around it and the angle brackets of markup.
_NAME_DROPPED = frozenset("«»‹›<>")
#: The fields only the panel writes, and what a write may hold.
_WRITABLE = frozenset({"name", "last_used_at"})
_ARCHIVING = frozenset({"active", "removed_at", "removed_by"})


class KnownKey(NamedTuple):
    """One known key of a server, as the cron and the view compare it.

    :ivar id: the ``cloud.host.known.key`` id.
    :ivar name: what the administrator called it.
    :ivar fingerprint: ``SHA256:…``.
    :ivar networks: the networks it may sign in from; empty for any.
    :ivar added_at: naive UTC.
    :ivar removed_at: naive UTC, or None while it is known.
    """

    id: int
    name: str
    fingerprint: str
    networks: tuple
    added_at: object
    removed_at: object

    def covers(self, at):
        """Return whether this key was known at *at* (naive UTC).

        :rtype: bool
        """
        return self.added_at <= at and (self.removed_at is None or at < self.removed_at)

    def allows(self, address):
        """Return whether a sign-in with this key may come from *address*.

        Any address when it names none.

        :param str address: the sign-in's address.
        :rtype: bool
        """
        if not self.networks:
            return True
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip.version == net.version and ip in net for net in self.networks)


def match_known_key(keys, fingerprint, at):
    """Return the key of *keys* with *fingerprint* known at *at*, or None.

    :param keys: :class:`KnownKey` tuples of one server.
    :param str fingerprint: the sign-in's, or None.
    :param datetime at: the sign-in's instant, naive UTC.
    :rtype: KnownKey | None
    """
    if not fingerprint:
        return None
    for key in keys:
        if key.fingerprint == fingerprint and key.covers(at):
            return key
    return None


def _fingerprint_of_blob(blob):
    """Return ``SHA256:…`` of a public key's blob, as ssh-keygen computes it.

    :param bytes blob: the decoded base64 of a public key line.
    :rtype: str
    """
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _blob_type(blob):
    """Return the key type a public key's blob starts with, or None.

    :param bytes blob: the decoded base64 of a public key line.
    :rtype: str | None
    """
    if len(blob) < 4:
        return None
    size = int.from_bytes(blob[:4], "big")
    if size <= 0 or size > 64 or len(blob) < 4 + size:
        return None
    try:
        return blob[4:4 + size].decode("ascii")
    except UnicodeDecodeError:
        return None


def read_key(env, text):
    """Return ``(fingerprint, key type)`` of what an administrator pasted.

    The line of a ``.pub`` file (options of ``authorized_keys`` before it
    are skipped), a line of ``ssh-keygen -lf``, or a SHA256 fingerprint
    with or without its prefix and padding. Case is kept: a fingerprint
    is base64. The key type is empty for a fingerprint alone.

    :param env: the environment whose language the errors are in.
    :param str text: what was pasted.
    :raise ValidationError: for a private key (whose text is never
        repeated), a certificate, an MD5 fingerprint, or anything else.
    :rtype: tuple[str, str]
    """
    _ = env._
    text = (text or "").strip()
    if not text:
        raise ValidationError(_("Paste the public key or its SHA256 fingerprint."))
    upper = text.upper()
    if any(mark in upper for mark in _PRIVATE_MARKS):
        raise ValidationError(_(
            "This is a private key. Paste the public key (the .pub file) or "
            "its fingerprint; the private key was not saved."
        ))
    if "-cert-v01@openssh.com" in text:
        raise ValidationError(_(
            "This is a certificate. Paste the public key it certifies (the "
            ".pub file) or that key's fingerprint."
        ))
    if "\n" in text:
        raise ValidationError(_("Paste a single line: one public key or one fingerprint."))
    match = _KEYGEN_LINE.match(text)
    if match:
        return "SHA256:" + match[1], match[2]
    match = _FINGERPRINT_ALONE.match(text)
    if match:
        return "SHA256:" + match[1], ""
    if _MD5.search(text):
        raise ValidationError(_(
            "This is an MD5 fingerprint. The servers log SHA256 ones: paste "
            "the public key, or what ssh-keygen -lf prints for it."
        ))
    words = text.split()
    for index, word in enumerate(words[:-1]):
        if word not in KEY_TYPES:
            continue
        try:
            blob = base64.b64decode(words[index + 1], validate=True)
        except (binascii.Error, ValueError):
            break
        if _blob_type(blob) != word:
            break
        return _fingerprint_of_blob(blob), KEY_TYPES[word]
    raise ValidationError(_("That is not a public key or a SHA256 fingerprint."))


def read_networks(env, text):
    """Return the networks a key may sign in from, normalized, and as networks.

    Addresses or networks separated by commas, spaces or lines. An IPv6
    address or a network narrower than a /64 is widened to its /64, as
    the panel's own addresses are compared: a host with a /64 may leave
    by any address of it. A network wider than
    :data:`NETWORK_MIN_PREFIX` is refused (a /0 means any address, which an
    empty field already says, and two /1 say the same), and so is an IPv4
    address written as IPv6 (``::ffff:…``), which a sign-in never comes
    from. At most :data:`NETWORKS_MAX`: the reading stops at the first
    one past it.

    :param env: the environment whose language the errors are in.
    :param str text: what was typed; empty for any address.
    :raise ValidationError: for something that is not an address or a
        network, a /0, or too many.
    :returns: ``(text kept, tuple of networks)``.
    :rtype: tuple
    """
    _ = env._
    found = []
    words = 0
    for word in re.split(r"[\s,;]+", (text or "").strip()):
        if not word:
            continue
        words += 1
        if words > NETWORKS_MAX:
            raise ValidationError(_(
                "Only from may name %(count)s addresses or networks at most.",
                count=NETWORKS_MAX,
            ))
        try:
            network = ipaddress.ip_network(word, strict=False)
        except ValueError:
            raise ValidationError(_(
                "%(value)s is not an address or a network.", value=word[:64],
            )) from None
        if network.prefixlen == 0:
            raise ValidationError(_(
                "%(value)s means any address: leave Only from empty instead.",
                value=word[:64],
            ))
        if network.version == 6 and network.network_address.ipv4_mapped:
            raise ValidationError(_(
                "%(value)s is an IPv4 address written as IPv6: write it as IPv4.",
                value=word[:64],
            ))
        if network.prefixlen < NETWORK_MIN_PREFIX[network.version]:
            raise ValidationError(_(
                "%(value)s is too wide: a network of Only from is a /%(prefix)s "
                "or narrower.",
                value=word[:64], prefix=NETWORK_MIN_PREFIX[network.version],
            ))
        if network.version == 6 and network.prefixlen > 64:
            network = network.supernet(new_prefix=64)
        if network not in found:
            found.append(network)
    return ", ".join(str(network) for network in found), tuple(found)


def clean_name(env, text):
    """Return a key's name on one line, without control characters.

    Every run of whitespace becomes one space; control characters, the
    angle quotes the alerts put around a name and angle brackets go.

    :param env: the environment whose language the errors are in.
    :param str text: what was typed.
    :raise ValidationError: for a name that is empty once cleaned, or
        longer than :data:`NAME_MAX`.
    :rtype: str
    """
    _ = env._
    kept = []
    for char in text or "":
        if char.isspace():
            kept.append(" ")
        elif unicodedata.category(char).startswith("C") or char in _NAME_DROPPED:
            continue
        else:
            kept.append(char)
    name = " ".join("".join(kept).split())
    if not name:
        raise ValidationError(_("Give the key a name, such as the laptop it is on."))
    if len(name) > NAME_MAX:
        raise ValidationError(_(
            "A key's name has %(count)s characters at most.", count=NAME_MAX,
        ))
    return name


class CloudHostKnownKey(models.Model):
    _name = "cloud.host.known.key"
    _description = "Known SSH key of a server"
    _order = "active desc, added_at desc, id desc"

    host_id = fields.Many2one(
        "cloud.host", string="Server", required=True, readonly=True,
        ondelete="cascade", index=True,
    )
    name = fields.Char(
        string="Name", required=True,
        help="Who or what holds the key, as the alerts and the Security view name it.",
    )
    fingerprint = fields.Char(
        string="Fingerprint", required=True, readonly=True,
        help="The key's SHA256 fingerprint, as sshd logs it.",
    )
    key_type = fields.Char(string="Type", readonly=True)
    allowed_networks = fields.Char(
        string="Only from", readonly=True,
        groups="incubacloud.group_cloud_manager",
        help="The addresses or networks a sign-in with this key may come "
             "from; from anywhere else it raises a critical alert. Empty: "
             "any address.",
    )
    added_at = fields.Datetime(
        string="Added", required=True, readonly=True,
        help="From when its sign-ins raise no alert.",
    )
    active = fields.Boolean(default=True, readonly=True)
    removed_at = fields.Datetime(string="Removed", readonly=True)
    removed_by = fields.Many2one(
        "res.users", string="Removed by", readonly=True, ondelete="set null",
    )
    last_used_at = fields.Datetime(
        string="Last used", readonly=True,
        help="The last sign-in with it the security events' check read.",
    )

    _fingerprint_active_uniq = models.UniqueIndex(
        "(host_id, fingerprint) WHERE active",
        "This key is already known on this server.",
    )
    _name_active_uniq = models.UniqueIndex(
        "(host_id, lower(name)) WHERE active",
        "Another known key of this server has that name.",
    )
    # An archived key always says since when: one without would be read
    # as covering nothing, never as covering for ever.
    _removed_dated = models.Constraint(
        "CHECK (active OR removed_at IS NOT NULL)",
        "A removed key says when it was removed.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        """Add known keys, each read, checked and told in the same transaction.

        The pasted text (``fingerprint``) is read by :func:`read_key`
        before the insert, so a private key is never written; the name
        is cleaned, the addresses normalized, ``added_at`` is now and the
        row starts active. Each key writes its audit row, raises its
        alert and emails the Administrators and the holder
        (:meth:`_known_key_tell`).

        :raise ValidationError: for what :func:`read_key`,
            :func:`read_networks` or :func:`clean_name` refuse, the
            panel's own key, or a key or name already known there.
        """
        now = fields.Datetime.now()
        prepared = []
        for vals in vals_list:
            host = self.env["cloud.host"].sudo().browse(vals.get("host_id")).exists()
            if not host:
                raise ValidationError(self.env._("A known key needs its server."))
            fingerprint, key_type = read_key(self.env, vals.get("fingerprint"))
            name = clean_name(self.env, vals.get("name"))
            networks, _networks = read_networks(self.env, vals.get("allowed_networks"))
            panel = set(host._panel_key_fingerprints())
            if host.security_previous_key_fingerprint:
                panel.add(host.security_previous_key_fingerprint)
            if fingerprint in panel:
                raise ValidationError(self.env._(
                    "That is the panel's own key for this server."
                ))
            self._known_key_check_unique(host, fingerprint, name)
            active = self.sudo().search_count([
                ("host_id", "=", host.id), ("active", "=", True),
            ])
            if active + sum(1 for row in prepared if row["host_id"] == host.id) >= ACTIVE_MAX:
                raise ValidationError(self.env._(
                    "A server has %(count)s known keys at most: remove one first.",
                    count=ACTIVE_MAX,
                ))
            prepared.append({
                "host_id": host.id,
                "name": name,
                "fingerprint": fingerprint,
                "key_type": key_type,
                "allowed_networks": networks,
                "added_at": now,
                "active": True,
            })
        keys = super().create(prepared)
        for key in keys:
            key._known_key_tell()
        return keys

    def write(self, vals):
        """Write a known key's name or last use, or archive it; nothing else.

        A new name is cleaned, checked against the server's other active
        keys and audited. Archiving takes ``active`` False on active keys
        alone, with ``removed_at`` now and ``removed_by`` the user acting,
        whatever the caller sent, and is audited. Any other field, or
        reactivating a key, is refused.

        :raise UserError: for a field a known key never changes.
        """
        keys = set(vals)
        if keys & _ARCHIVING:
            if (
                not keys <= _ARCHIVING
                or vals.get("active", False) is not False
                or not all(self.mapped("active"))
            ):
                raise UserError(self.env._(
                    "A removed key cannot be added back: add it again as a new key."
                ))
            vals = {
                "active": False,
                "removed_at": fields.Datetime.now(),
                "removed_by": self.env.uid,
            }
            result = super().write(vals)
            for key in self:
                key._known_key_audit("Known SSH key removed")
            return result
        if not keys <= _WRITABLE:
            raise UserError(self.env._(
                "A known key's fingerprint, addresses and dates never change: "
                "remove it and add it again."
            ))
        if "name" in vals:
            name = clean_name(self.env, vals["name"])
            for key in self:
                if key.name != name:
                    self._known_key_check_unique(key.host_id, None, name, exclude=key)
            renamed = {key.id: key.name for key in self if key.name != name}
            result = super().write({**vals, "name": name})
            for key in self.filtered(lambda k: k.id in renamed):
                key._known_key_audit(
                    "Known SSH key renamed", previous=renamed[key.id],
                )
            return result
        return super().write(vals)

    def unlink(self):
        """Refuse to delete a known key: removing one archives it.

        :raise UserError: always.
        """
        raise UserError(self.env._(
            "A known key is never deleted: remove it, and it stays listed as removed."
        ))

    def _known_key_check_unique(self, host, fingerprint, name, exclude=None):
        """Refuse a fingerprint or a name another active key of *host* has.

        The unique indexes say the same; this says it in words first.

        :param host: the ``cloud.host``.
        :param fingerprint: ``SHA256:…``, or None to check the name alone.
        :param str name: the cleaned name.
        :param exclude: the key being renamed, if any.
        :raise ValidationError: when one is taken.
        """
        others = self.sudo().search([
            ("host_id", "=", host.id), ("active", "=", True),
            ("id", "not in", exclude.ids if exclude else []),
        ])
        if fingerprint and fingerprint in others.mapped("fingerprint"):
            raise ValidationError(self.env._("This key is already known on this server."))
        if name.lower() in {other.name.lower() for other in others}:
            raise ValidationError(self.env._(
                "Another known key of this server has that name."
            ))

    def _known_key_audit(self, action, previous=None):
        """Write the audit row of an addition, a removal or a new name.

        :param str action: the audit row's action.
        :param str previous: the name before, for a new name.
        """
        self.ensure_one()
        details = f"{self.name} {self.fingerprint}"
        if previous is not None:
            details = f"{previous} → {details}"
        if action == "Known SSH key added" and self.allowed_networks:
            details += f" · only from {self.allowed_networks}"
        self.env["cloud.audit.log"].sudo().create({
            "action": action,
            "details": details[:255],
            "host_id": self.host_id.id,
        })

    def _known_key_tell(self):
        """Audit an addition, raise its alert and email who holds the panel.

        The alert, ``security_known_key_added``, is critical, one per key
        (``dedup_key``), so that every addition reaches the channels at
        once and none waits for a daily digest; it is not one of the
        events' alerts the hourly cap holds back, and removing the key
        leaves it open. The email goes to every active Administrator and
        to the account's holder, with no daily quota for this reason
        (``cloud.session.notice``); one that fails is logged and rolled
        back to its savepoint, the key and its alert stay.
        """
        self.ensure_one()
        self._known_key_audit("Known SSH key added")
        host = self.host_id
        actor = self.env.user
        _ = self.env._
        values = {
            "host": host.name, "user": actor.name or actor.login,
            "label": self.name, "fingerprint": self.fingerprint,
        }
        text = _(
            "%(host)s: %(user)s added the known SSH key «%(label)s» "
            "(%(fingerprint)s): its sign-ins no longer raise an alert. If you "
            "did not expect it, remove it in Metrics → Security of the server.",
            **values,
        )
        # Which addresses, never: the alert is read by roles that do not
        # see them, and goes as it is to every channel.
        limited = bool(self.allowed_networks)
        if limited:
            text += " " + _("It may sign in only from some addresses.")
        alert = self.env["cloud.alert"].sudo().raise_alert(
            CODE_KNOWN_ADDED, text, level="critical", host=host,
            key=f"known_key:{self.id}",
            payload={
                "key_id": self.id, "label": self.name,
                "fingerprint": self.fingerprint, "by": actor.id,
                "only_from": limited,
            },
            confirm_after=1, confirm_window=3600,
        )
        if not alert:
            # A cancelled job's context, or a row another transaction
            # wrote first: a key nobody was told of is not added.
            raise UserError(_("The key was not added: its alert could not be raised."))
        Notice = self.env["cloud.session.notice"].sudo()
        managers = self.env.ref("incubacloud.group_cloud_manager").sudo().all_user_ids
        recipients = (managers | Notice._session_notice_holders() | actor).filtered(
            lambda user: user.active and not user.share,
        )
        try:
            with self.env.cr.savepoint():
                Notice._session_notify(recipients, "ssh_key", {
                    "door": self.name, "server": host.name,
                    "actor": actor.name or actor.login, "by": actor.id,
                })
        except Exception as exc:  # noqa: BLE001 - the key and its alert are kept
            if is_concurrency_error(exc):
                raise
            _logger.exception(
                "[known-keys] could not tell of a new key on cloud.host(id=%s)",
                host.id,
            )

    def _known_key_row(self, detail):
        """Return the card's row of this key.

        :param bool detail: whether the caller manages servers: only then
            the addresses it may sign in from are named.
        :rtype: dict
        """
        self.ensure_one()

        def iso(moment):
            """A naive UTC moment as ISO with its zone, or None."""
            return f"{fields.Datetime.to_string(moment).replace(' ', 'T')}Z" if moment else None

        row = {
            "id": self.id,
            "name": self.name,
            "fingerprint": self.fingerprint,
            "key_type": self.key_type or "",
            "active": self.active,
            "added_at": iso(self.added_at),
            "added_by": self.create_uid.name or "",
            "removed_at": iso(self.removed_at),
            "removed_by": self.removed_by.name or "",
            "last_used_at": iso(self.last_used_at),
            "only_from": bool(self.allowed_networks),
        }
        if detail:
            row["allowed_networks"] = self.allowed_networks or ""
        return row


class CloudHost(models.Model):
    _inherit = "cloud.host"

    security_known_key_ids = fields.One2many(
        "cloud.host.known.key", "host_id", string="Known SSH keys",
        readonly=True, context={"active_test": False},
    )

    def _security_known_keys(self):
        """Return this server's known keys, removed ones included.

        :rtype: list[KnownKey]
        """
        self.ensure_one()
        rows = self.env["cloud.host.known.key"].sudo().with_context(
            active_test=False,
        ).search([("host_id", "=", self.id)])
        keys = []
        for row in rows:
            # The ORM reads an empty date as False; the window wants None.
            # A removed key without its date (the constraint forbids it)
            # covers nothing rather than for ever.
            removed = row.removed_at or (None if row.active else row.added_at)
            keys.append(KnownKey(
                row.id, row.name, row.fingerprint,
                read_networks(self.env, row.allowed_networks)[1],
                row.added_at, removed,
            ))
        return keys

    def _security_known_keys_stamp(self):
        """Return what changes whenever a key of this server is added or removed.

        Part of the Security view's cache key: a table read before a
        change is not served after it.

        :rtype: tuple
        """
        self.ensure_one()
        self.env["cloud.host.known.key"].flush_model(["host_id", "active"])
        self.env.cr.execute(
            "SELECT id, active FROM cloud_host_known_key WHERE host_id = %s ORDER BY id",
            (self.id,),
        )
        return tuple(self.env.cr.fetchall())

    def _security_known_keys_check(self):
        """Refuse a change of this server's known keys to whoever may not.

        Whoever manages servers, with write access to this one, on a
        panel whose servers may send their security logs.

        :raise AccessError: for another role or a server it may not write.
        :raise UserError: where security logs are not available.
        """
        self.ensure_one()
        self._check_can_manage_hosts()
        if not self._security_logs_available():
            raise UserError(self.env._(
                "Security logs are not available on this panel yet."
            ))
        self.check_access("write")

    def _security_known_key_add(self, key, name, networks=""):
        """Add a known key to this server, as the Security view does.

        Private: never called over RPC, by an API key or by an agent;
        the panel's route ``/cloud/host/known_key_add`` is its way in.

        :param str key: the public key line, the ``ssh-keygen -lf`` line
            or the fingerprint.
        :param str name: who or what holds it.
        :param str networks: the addresses it may sign in from; empty
            for any.
        :returns: the key added.
        :rtype: cloud.host.known.key
        """
        self._security_known_keys_check()
        # In a savepoint of its own: a key refused after its row was
        # written (its alert not raised) leaves nothing behind, also when
        # the route answers the refusal and the request commits.
        with self.env.cr.savepoint():
            return self.env["cloud.host.known.key"].sudo().create({
                "host_id": self.id,
                "fingerprint": key,
                "name": name,
                "allowed_networks": networks or "",
            })

    def _security_known_key_remove(self, key_id):
        """Remove (archive) one known key of this server.

        Private, as :meth:`_security_known_key_add`.

        :param int key_id: the ``cloud.host.known.key`` id.
        :raise UserError: when it is not an active key of this server.
        """
        self._security_known_keys_check()
        key = self.env["cloud.host.known.key"].sudo().browse(key_id).exists()
        if not key or key.host_id != self or not key.active:
            raise UserError(self.env._("That key is not a known key of this server."))
        key.write({"active": False})

    def _security_known_keys_answer(self, detail):
        """Return the Security view's card of known keys.

        :param bool detail: whether the caller manages servers: they may
            add and remove keys, and see the addresses a key may sign in
            from.
        :returns: ``{"can_edit", "rows", "removed_count"}``: every active
            key, then the last :data:`REMOVED_SHOWN` removed ones.
        :rtype: dict
        """
        self.ensure_one()
        Key = self.env["cloud.host.known.key"].sudo().with_context(active_test=False)
        removed = Key.search(
            [("host_id", "=", self.id), ("active", "=", False)],
            order="removed_at desc, id desc",
        )
        rows = Key.search([("host_id", "=", self.id), ("active", "=", True)])
        rows |= removed[:REMOVED_SHOWN]
        can_edit = bool(detail)
        if can_edit:
            try:
                self.check_access("write")
            except AccessError:
                can_edit = False
        return {
            "can_edit": can_edit,
            "rows": [row._known_key_row(bool(detail)) for row in rows],
            "removed_count": len(removed),
        }
