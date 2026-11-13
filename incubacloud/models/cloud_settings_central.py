"""The central's single path: one name, the platform port, an authority of its own.

Behind ``metrics_central_single_path``. With it on and a public name set,
the central is published under that one name, outside every CDN, on the
platform port of its host (the ``direct`` entrypoint, ``direct_port``),
with a certificate this panel signs: ``https://<name>:<port>``. The
panel's URLs move to it only once the name answers from the central's
host (``_central_name_ready``); until then they stay as they were and
``metrics_central_dns_pending`` says which record to create.

Off, every line of the path before it runs as it did: a database that
already had a central when this code arrived keeps it off
(``_auto_init``), and a new one starts with it on.

The authority (ECDSA P-256, ten years, no intermediate) signs one leaf
per deployment of the central, for one year. The agents trust it through
a file of their own, never through the system's store, and the panel
through a bundle of the usual roots plus it (``_central_tls_verify``), so
a URL still on a public certificate keeps validating.
"""
import hashlib
import ipaddress
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from odoo import _, api, fields, models
from odoo.tools.sql import column_exists

from . import _direct_probe
from .encrypted_char import EncryptedChar

_logger = logging.getLogger(__name__)

#: The prefix the single name's probe state is kept under, as the direct
#: names' (``_direct_probe.SUFFIXES``).
NAME_PREFIX = _direct_probe.CENTRAL_NAME_PREFIX

#: The authority's life. Rotating it re-applies every agent and every
#: panel it is sent to, so it is long; the leaf below is what renews.
CA_LIFETIME = timedelta(days=3650)

#: A leaf's life, and how long before its end a deployment renews it and
#: an alert says it is close (21 days, as the direct certificates').
LEAF_LIFETIME = timedelta(days=365)
LEAF_RENEW_BEFORE = timedelta(days=60)
LEAF_WARN_BEFORE = timedelta(days=21)

#: Where the URLs in force before the switch to the single name are kept,
#: for the way back (switch off). The retirement of the old paths removes
#: it.
LEGACY_URLS_PARAM = "incubacloud.metrics_central_legacy_urls"

#: A pending record turns critical after this long when a central was
#: already serving (a name that answered before and moved away).
DNS_PENDING_CRITICAL = timedelta(hours=12)

#: Who may write to the central, by the address the request comes from.
WRITE_ACCESS = [
    ("servers", "Only this panel's servers, by their address"),
    ("anyone", "Any address, with each account's credential"),
]


def _now_utc():
    """Return the current time, aware and in UTC.

    :rtype: datetime
    """
    return datetime.now(timezone.utc)


def _naive_utc(moment):
    """Return *moment* as the naive UTC datetime Odoo stores.

    :param datetime moment: an aware or naive UTC datetime.
    :rtype: datetime
    """
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment.replace(microsecond=0)


def mint_authority(common_name, now=None):
    """Return a new authority as ``(certificate PEM, key PEM)``.

    ECDSA P-256, ``CA:TRUE`` with ``pathlen 0`` (it signs leaves only),
    keyCertSign and cRLSign, valid for ``CA_LIFETIME``.

    :param str common_name: what the certificate calls itself.
    :param datetime now: the start of its validity (tests).
    :rtype: tuple
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = now or _now_utc()
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    public = key.public_key()
    cert_pem = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + CA_LIFETIME)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False,
            key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


def issue_leaf(ca_cert_pem, ca_key_pem, name, now=None):
    """Return a server certificate for *name*, signed by the authority.

    ECDSA P-256, *name* as its only subject alternative name, serverAuth
    only, valid for ``LEAF_LIFETIME``.

    :param str ca_cert_pem: the authority's certificate.
    :param str ca_key_pem: the authority's key.
    :param str name: the central's public name.
    :param datetime now: the start of its validity (tests).
    :return: ``(certificate PEM, key PEM, end of validity as naive UTC)``.
    :rtype: tuple
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = now or _now_utc()
    ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode())
    ca_key = serialization.load_pem_private_key(ca_key_pem.encode(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key()
    not_after = now + LEAF_LIFETIME
    cert_pem = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_cert.subject)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem, _naive_utc(not_after)


def ca_bundle_path(ca_pem, directory=None):
    """Return a file holding the usual roots and *ca_pem*, writing it once.

    ``requests`` validates against one file: the roots it ships with
    (``certifi``) plus the authority, so the same verification passes for
    the single name and for a URL still on a public certificate. Named
    by the authority's digest, so a new authority gets a new file and an
    old file is never half-replaced under a reader.

    :param str ca_pem: the authority's certificate.
    :param str directory: where to write it (tests); the system's
        temporary directory otherwise.
    :rtype: str
    """
    import certifi

    digest = hashlib.sha256(ca_pem.encode()).hexdigest()[:16]
    path = Path(directory or tempfile.gettempdir()) / f"incubacloud-central-ca-{digest}.pem"
    if path.exists():
        return str(path)
    roots = Path(certifi.where()).read_text(encoding="utf-8")
    bundle = roots.rstrip("\n") + "\n" + ca_pem.strip() + "\n"
    handle, staged = tempfile.mkstemp(dir=path.parent, prefix=".incubacloud-central-ca-")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.write(bundle)
    os.chmod(staged, 0o644)
    os.replace(staged, path)
    return str(path)


class CloudSettings(models.Model):
    _inherit = "cloud.settings"

    metrics_central_single_path = fields.Boolean(
        string="One path, without a CDN",
        default=True,
        help="The central is published under its public name only, on the "
             "platform port of its server and outside every CDN, with a "
             "certificate this panel signs. The panel and every agent move "
             "to that name once it answers. Off: the paths of before.",
    )
    metrics_central_write_access = fields.Selection(
        WRITE_ACCESS,
        string="Who can write to the central",
        default="servers",
        required=True,
        help="Only this panel's servers: the platform port admits their "
             "addresses and no other. Any address: it admits every "
             "address, and each account's credential is the only barrier; "
             "for servers behind NAT or with an address that changes.",
    )
    metrics_ca_cert = fields.Text(
        string="Central's certificate authority",
        readonly=True,
        copy=False,
        help="The public half of the authority that signs the central's "
             "certificate. The agents and the panel trust it for the "
             "central only.",
    )
    metrics_ca_key = EncryptedChar(
        string="Central's certificate authority key",
        groups="base.group_system",
        copy=False,
        help="Signs the central's certificate. Stored encrypted; never "
             "leaves the panel.",
    )
    metrics_central_cert_expires = fields.Datetime(
        string="Central's certificate expires",
        readonly=True,
        copy=False,
        help="End of the certificate the last good deployment of the "
             "central installed under its public name.",
    )
    metrics_central_name_state = fields.Selection(
        _direct_probe.STATES, string="Central's name state",
        default="unknown", readonly=True, copy=False,
    )
    metrics_central_name_failures = fields.Integer(readonly=True, copy=False)
    metrics_central_name_successes = fields.Integer(readonly=True, copy=False)
    metrics_central_name_failing_since = fields.Datetime(readonly=True, copy=False)
    metrics_central_name_dns_bad = fields.Boolean(readonly=True, copy=False)
    metrics_central_name_cert_expires = fields.Datetime(readonly=True, copy=False)
    metrics_central_name_error = fields.Char(readonly=True, copy=False)
    metrics_central_name_checked_at = fields.Datetime(
        string="Central's name last checked",
        compute="_compute_metrics_central_name_checked_at",
        help="When the name was last checked from the central's server. "
             "Kept in a system parameter, not in this row: it moves at "
             "every check.",
    )

    def _auto_init(self):
        """Keep the single path off where a central ran before it existed.

        The column is created with its default, on; a panel that already
        recorded a central keeps the paths it had until its operator
        turns the switch on (the owner's condition of 13-nov-2026: nothing
        changes for a running central on update). Only when the column is
        new, so a switch turned on later is never turned back.
        """
        new = not column_exists(self.env.cr, self._table, "metrics_central_single_path")
        result = super()._auto_init()
        if new:
            self._single_path_off_where_a_central_ran()
        return result

    def _single_path_off_where_a_central_ran(self):
        """Turn the single path off on a settings row that records a central.

        What the update does once, when the switch's column is created
        (:meth:`_auto_init`). With SQL: the registry is still being built.

        :return: how many rows it turned off.
        :rtype: int
        """
        cr = self.env.cr
        if not column_exists(cr, self._table, "metrics_central_host_id"):
            return 0
        cr.execute(
            f"UPDATE {self._table} SET metrics_central_single_path = FALSE "  # noqa: S608 — fixed name
            "WHERE metrics_central_host_id IS NOT NULL"
        )
        if cr.rowcount:
            _logger.info(
                "[metrics] a central was already recorded: its single "
                "path stays off until it is turned on",
            )
        return cr.rowcount

    def _compute_metrics_central_name_checked_at(self):
        """Show when the central's name was last checked."""
        checked = self._direct_checked_at(NAME_PREFIX)
        for settings in self:
            settings.metrics_central_name_checked_at = checked

    # ── The authority ───────────────────────────────────────────────────

    def _ensure_metrics_ca(self):
        """Return ``(certificate PEM, key PEM)`` of the authority, minting it once.

        Minted the first time a deployment needs it, on the caller's
        cursor: the central's deployment calls it from its durable one
        (``_durable_env``), like the other secrets of the central.

        :rtype: tuple
        """
        settings = self._get_system()
        if not (settings.metrics_ca_cert and settings.metrics_ca_key):
            dbname = self.env.cr.dbname or "panel"
            cert, key = mint_authority(f"IncubaCloud metrics central CA ({dbname})")
            settings.sudo().write({"metrics_ca_cert": cert, "metrics_ca_key": key})
            _logger.info("[metrics] minted the central's certificate authority")
        return settings.metrics_ca_cert, settings.metrics_ca_key

    def _issue_central_leaf(self, name):
        """Return a certificate for the central's *name*, signed now.

        :param str name: the central's public name.
        :return: ``(certificate PEM, key PEM, end as naive UTC)``.
        :rtype: tuple
        """
        cert, key = self._ensure_metrics_ca()
        return issue_leaf(cert, key, name)

    @api.model
    def _central_ca_pem(self):
        """Return the authority this panel trusts for the central, or ''.

        Core: its own. A panel whose central is another panel's answers
        with the one it is sent.

        :rtype: str
        """
        return (self._get_system().metrics_ca_cert or "").strip()

    @api.model
    def _central_agents_ca(self):
        """Return the authority the agents are installed with, or ''.

        Only once their URL is the single name: until then they write
        where they did, under a certificate the system's roots validate,
        and a file of the authority alone would refuse it.

        :rtype: str
        """
        return self._central_ca_pem() if self._central_single_in_force() else ""

    @api.model
    def _central_tls_verify(self):
        """Return what every request to the central verifies its certificate with.

        The path of a bundle of the usual roots and the authority, or
        True (the usual roots alone) while there is no authority.

        :rtype: str | bool
        """
        pem = self._central_ca_pem()
        if not pem:
            return True
        try:
            return ca_bundle_path(pem)
        except OSError as exc:
            _logger.warning("[metrics] could not write the central's CA bundle: %s", exc)
            return True

    # ── The single name ─────────────────────────────────────────────────

    @api.model
    def _central_single_path(self):
        """Whether the central is to be published under its single name.

        The switch on and a public name set. Without a name the central
        stays on its host's bridge, as before.

        :rtype: bool
        """
        settings = self._get_system()
        return bool(
            settings.metrics_central_single_path
            and (settings.metrics_central_public_host or "").strip()
        )

    @api.model
    def _central_single_base(self, host=None):
        """Return ``https://<name>:<port>`` of the single path, or ''.

        :param host: the central's host; the recorded one by default.
        :rtype: str
        """
        settings = self._get_system()
        if not self._central_single_path():
            return ""
        host = host if host is not None else settings.metrics_central_host_id
        port = host.sudo().direct_port_shipped if host else 0
        if not port:
            return ""
        name = settings.metrics_central_public_host.strip().lower()
        return f"https://{name}:{port}"

    @api.model
    def _central_single_urls(self, host=None):
        """Return the panel's read and write URLs on the single name, or {}.

        :param host: as in :meth:`_central_single_base`.
        :return: ``{"read", "write"}``.
        :rtype: dict
        """
        base = self._central_single_base(host)
        if not base:
            return {}
        return {"read": f"{base}/r", "write": f"{base}/w/api/v1/write"}

    @api.model
    def _central_single_in_force(self):
        """Whether the panel's URLs are those of the single name.

        :rtype: bool
        """
        urls = self._central_single_urls()
        settings = self._get_system()
        return bool(
            urls
            and (settings.metrics_remote_write_url or "").strip() == urls["write"]
        )

    @api.model
    def _central_write_access_fixed(self):
        """Return the write access a layer imposes, or ''.

        Core imposes none. A layer whose central several panels write to
        answers ``servers``; the settings then show it without a choice.

        :rtype: str
        """
        return ""

    @api.model
    def _central_write_access(self):
        """Return who may write to the central: ``servers`` or ``anyone``.

        :rtype: str
        """
        fixed = self._central_write_access_fixed()
        if fixed:
            return fixed
        return self._get_system().metrics_central_write_access or "servers"

    @api.model
    def _central_admits_anyone(self, host):
        """Whether the platform port of *host* admits every address.

        Only on the central's host, with the single path on and
        ``anyone`` chosen: the port then carries the central's routes,
        which ask each account for its credential. Every other host's
        port keeps the list.

        :param host: a ``cloud.host``.
        :rtype: bool
        """
        settings = self._get_system()
        return bool(
            host
            and settings.metrics_central_host_id == host
            and self._central_single_path()
            and self._central_write_access() == "anyone"
        )

    # ── Moving to the name, and back ────────────────────────────────────

    @api.model
    def _central_legacy_urls(self):
        """Return the URLs kept from before the single name, or {}.

        :return: ``{"read", "write"}``.
        :rtype: dict
        """
        raw = self.env["ir.config_parameter"].sudo().get_param(LEGACY_URLS_PARAM) or ""
        read, _sep, write = raw.partition("\n")
        if not (read or write):
            return {}
        return {"read": read.strip(), "write": write.strip()}

    @api.model
    def _central_name_ready(self):
        """Move the panel's URLs to the single name, which just answered.

        The URLs in force before are kept for the way back. Once moved,
        the reconciliation re-applies the agents with the new URL and the
        authority. Nothing moves while the switch is off.

        :return: whether the URLs moved.
        :rtype: bool
        """
        settings = self._get_system()
        urls = self._central_single_urls()
        if not urls or self._central_single_in_force():
            return False
        params = self.env["ir.config_parameter"].sudo()
        if not self._central_legacy_urls():
            params.set_param(LEGACY_URLS_PARAM, "\n".join((
                (settings.metrics_central_url or "").strip(),
                (settings.metrics_remote_write_url or "").strip(),
            )))
        settings.write({
            "metrics_central_url": urls["read"],
            "metrics_remote_write_url": urls["write"],
        })
        Alert = self.env["cloud.alert"].sudo()
        Alert.resolve_alert("metrics_central_dns_pending")
        self.env["cloud.audit.log"].sudo().create({
            "action": "Metrics central on its single name",
            "details": urls["write"],
        })
        _logger.info("[metrics] the central answers under %s: URLs moved", urls["write"])
        self.env["cloud.host"]._direct_path_changed("central's single name in force")
        return True

    @api.model
    def _central_back_to_legacy(self):
        """Put back the URLs of before the single name, when they are in force.

        The way back of the switch: what the panel and the agents used
        before, kept by :meth:`_central_name_ready`. Nothing when the
        URLs never moved.

        :return: whether the URLs moved back.
        :rtype: bool
        """
        settings = self._get_system()
        legacy = self._central_legacy_urls()
        public = (settings.metrics_central_public_host or "").strip().lower()
        write = (settings.metrics_remote_write_url or "").strip()
        on_name = bool(public) and write.startswith(f"https://{public}:")
        if not (legacy and on_name):
            return False
        settings.write({
            "metrics_central_url": legacy["read"] or False,
            "metrics_remote_write_url": legacy["write"] or False,
        })
        self.env["ir.config_parameter"].sudo().set_param(LEGACY_URLS_PARAM, False)
        Alert = self.env["cloud.alert"].sudo()
        for code in ("metrics_central_dns_pending", "metrics_central_name_down"):
            Alert.resolve_alert(code)
        self.env["cloud.audit.log"].sudo().create({
            "action": "Metrics central back on its earlier paths",
            "details": legacy["write"],
        })
        self.env["cloud.host"]._direct_path_changed("central's single path off")
        return True

    def _central_dns_record_text(self):
        """Say which DNS record the single name needs.

        :rtype: str
        """
        settings = self._get_system()
        host = settings.metrics_central_host_id.sudo()
        name = (settings.metrics_central_public_host or "").strip().lower()
        return _(
            "Create the A record of %(name)s pointing at %(address)s, with "
            "no proxy and no AAAA record.",
            name=name, address=host.ip_address or "?",
        )

    @api.model
    def _central_dns_pending(self, outcome=None):
        """Raise or refresh ``metrics_central_dns_pending``.

        With the name in use, critical from the start: every agent writes
        to it, and a record that no longer points at the central's server
        (a CDN's proxy turned back on, the record deleted) leaves the
        whole fleet writing nowhere. Not in use yet, a warning, critical
        after ``DNS_PENDING_CRITICAL`` when a central was already serving
        under other URLs (the panel and the agents are fine on those, but
        the move is stuck).

        :param dict outcome: the last check, for its layer and detail.
        """
        settings = self._get_system()
        detail = ""
        if outcome and not outcome.get("ok"):
            detail = f" ({outcome.get('layer')}: {outcome.get('detail') or ''})".rstrip()
        record = self._central_dns_record_text()
        if self._central_single_in_force():
            self.env["cloud.alert"].sudo().raise_alert(
                "metrics_central_dns_pending",
                _(
                    "The central's name no longer answers from its server%(detail)s. "
                    "%(record)s No agent can write to the central meanwhile; "
                    "they keep what they cannot send in their queues.",
                    detail=detail, record=record,
                ),
                level="critical",
            )
            return
        since = settings.metrics_central_name_failing_since
        serving = bool((settings.metrics_central_url or "").strip())
        critical = bool(
            serving and since
            and fields.Datetime.now() - since >= DNS_PENDING_CRITICAL
        )
        self.env["cloud.alert"].sudo().raise_alert(
            "metrics_central_dns_pending",
            _(
                "The central's name does not answer from its server yet%(detail)s. "
                "%(record)s The panel and the agents keep their current URLs "
                "until it does.",
                detail=detail, record=record,
            ),
            level="critical" if critical else "warning",
        )

    def _central_name_alerts(self, old_state, new_state, outcome):
        """Raise or close the alerts of the single name after a check.

        Not in force yet, or a DNS answer that is not the central's
        server: ``metrics_central_dns_pending``, naming the record. A
        success moves the URLs (:meth:`_central_name_ready`). In force:
        entering ``failing`` on any other layer raises
        ``metrics_central_name_down``, critical, with the layer; ``ok``
        closes both. There is no path to fall back to.

        A complete check that passes (the job's, which reads DNS from the
        central's server) closes ``metrics_central_dns_pending`` at once:
        the record is right. While the name is not in use it also ends
        ``failing`` and moves the URLs, so one **Check now** after the
        record is fixed is enough: the two checks and 30 minutes out of
        ``failing`` (``_direct_probe.MIN_FAILING``) protect a name in
        use from flapping, and a pending name has nobody on it yet. A
        light probe reads no DNS and never moves them.

        :param str old_state: before the check.
        :param str new_state: after it.
        :param dict outcome: as ``_direct_probe.parse_probe`` returns it.
        """
        Alert = self.env["cloud.alert"].sudo()
        if not self._central_single_path():
            return
        if outcome.get("ok") and outcome.get("source") == "job":
            Alert.resolve_alert("metrics_central_dns_pending")
            if new_state != "ok" and not self._central_single_in_force():
                self._get_system().write({
                    f"{NAME_PREFIX}_state": "ok",
                    f"{NAME_PREFIX}_failing_since": False,
                })
                new_state = "ok"
        if new_state == "ok":
            Alert.resolve_alert("metrics_central_name_down")
            Alert.resolve_alert("metrics_central_dns_pending")
            self._central_name_ready()
            return
        if not self._central_single_in_force() or outcome.get("layer") == "dns":
            # Not in use yet, or a record that does not point at the
            # central's server: after a move to another server, the record
            # is the step left (RB-36). The alert names it.
            self._central_dns_pending(outcome)
            return
        if new_state == "failing" and old_state != "failing":
            settings = self._get_system()
            Alert.raise_alert(
                "metrics_central_name_down",
                _(
                    "The central's name %(name)s does not answer (%(layer)s: "
                    "%(detail)s). The agents keep what they cannot send in "
                    "their queues meanwhile.",
                    name=settings.metrics_central_public_host,
                    layer=outcome.get("layer") or "?",
                    detail=outcome.get("detail") or "",
                ),
                level="critical",
            )

    def _direct_alerts(self, prefix, old_state, new_state, outcome):
        """Give the single name its own alerts; the direct names keep theirs.

        :param str prefix: the name's prefix.
        :param str old_state: before the check.
        :param str new_state: after it.
        :param dict outcome: as ``_direct_probe.parse_probe`` returns it.
        """
        if prefix != NAME_PREFIX:
            return super()._direct_alerts(prefix, old_state, new_state, outcome)
        self._central_name_alerts(old_state, new_state, outcome)
        self._central_cert_check()
        return None

    @api.model
    def _central_cert_check(self):
        """Raise or close ``metrics_central_cert_expiring``.

        From the certificate the last good deployment installed; the
        reconciliation renews it at ``LEAF_RENEW_BEFORE``, so this only
        speaks when renewing has been failing for over a month.
        """
        settings = self._get_system()
        expires = settings.metrics_central_cert_expires
        Alert = self.env["cloud.alert"].sudo()
        if not (expires and self._central_single_path()):
            Alert.resolve_alert("metrics_central_cert_expiring")
            return
        if expires - fields.Datetime.now() < LEAF_WARN_BEFORE:
            Alert.raise_alert(
                "metrics_central_cert_expiring",
                _(
                    "The central's certificate expires on %(date)s and could "
                    "not be renewed: deploy the central again. When it "
                    "expires, no agent can write to the central.",
                    date=fields.Date.to_string(expires.date()),
                ),
                level="critical",
            )
        else:
            Alert.resolve_alert("metrics_central_cert_expiring")

    @api.model
    def _central_cert_due(self):
        """Whether the central's certificate is to be renewed by a deployment.

        :rtype: bool
        """
        settings = self._get_system()
        if not self._central_single_path():
            return False
        expires = settings.metrics_central_cert_expires
        return not expires or expires - fields.Datetime.now() < LEAF_RENEW_BEFORE

    @api.model
    def _central_private_ranges(self):
        """Return the private ranges, as the central's operator routes admit them.

        The panel beside the central reaches its host's platform port
        through Docker's DNAT, from a container's address.

        :rtype: list
        """
        from .cloud_host_direct import DIRECT_PRIVATE_RANGES

        return [str(ipaddress.ip_network(r)) for r in DIRECT_PRIVATE_RANGES]

    @api.model
    def _forget_metrics_central(self):
        """Forget, with the central, what its single name said about it.

        The URLs of before, its certificate's end and its open alerts:
        a central deployed later starts its name over.

        :return: as the method extended.
        :rtype: str
        """
        name = super()._forget_metrics_central()
        if name:
            self.env["ir.config_parameter"].sudo().set_param(LEGACY_URLS_PARAM, False)
            self._get_system().write({"metrics_central_cert_expires": False})
            Alert = self.env["cloud.alert"].sudo()
            for code in (
                "metrics_central_dns_pending", "metrics_central_name_down",
                "metrics_central_cert_expiring",
            ):
                Alert.resolve_alert(code)
        return name

    def write(self, vals):
        """Start the single name over, or go back, when its switch or name moves.

        A new name, or the switch turned on, has proved nothing yet: its
        check starts over. The switch turned off puts back the URLs of
        before, if they had moved.
        """
        switched_off = "metrics_central_single_path" in vals and not vals[
            "metrics_central_single_path"
        ] and any(rec.metrics_central_single_path for rec in self)
        restart = (
            "metrics_central_public_host" in vals
            and any(
                (rec.metrics_central_public_host or "").strip().lower()
                != (vals["metrics_central_public_host"] or "").strip().lower()
                for rec in self
            )
        ) or (
            vals.get("metrics_central_single_path")
            and any(not rec.metrics_central_single_path for rec in self)
        )
        if switched_off:
            # Before the write: the way back reads the name the URLs use.
            self._central_back_to_legacy()
        if restart:
            vals = {**vals, **_direct_probe.reset_values(NAME_PREFIX)}
        result = super().write(vals)
        if restart:
            self._direct_note_checked(NAME_PREFIX, False)
        if switched_off or restart or "metrics_central_write_access" in vals:
            host = self._get_system().metrics_central_host_id.sudo()
            if host and host.direct_port_shipped:
                host._enqueue_direct_sync("central's single path changed")
        return result
