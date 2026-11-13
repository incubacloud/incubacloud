"""Git providers other than GitHub (GitLab), and their credentials.

A provider is one GitLab server: gitlab.com or a self-hosted one, maybe
behind a private CA. Its credentials are tokens scoped to a namespace;
the one a repository uses is the credential of the longest namespace
that covers its path and has the scopes the operation needs, never a
second one "just in case" after a refusal.

Where the panel may connect is one decision, :meth:`_network_policy`:
``'any'`` here, in the core, where a self-hosted panel reaches any
GitLab its owner adds, private or public; ``'public'`` where a
module that extends the panel overrides it, so that its GitLab must have only
public addresses, pinned at the moment they are checked.
"""
import hashlib
import logging
import re
from datetime import timedelta

from cryptography import x509

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..gitlab.client import GitLabClient, GitLabError
from ..net.git_reader import GitReader, GitReaderError
from ..net.outbound import OutboundError, _resolve_public
from ._git_urls import (
    base_authority,
    canonical_base_url,
    namespace_covers,
    normalize_namespace,
)
from .encrypted_char import EncryptedChar

_logger = logging.getLogger(__name__)

#: A CA bundle holds a few dozen certificates at most.
_MAX_CA_BUNDLE_BYTES = 64 * 1024

#: The PEM blocks a CA bundle may hold: certificates and nothing else,
#: so a private key pasted by mistake is refused rather than stored.
_PEM_BLOCK_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----")

#: Days before a token's expiry that the daily check warns (a
#: working week of margin).
EXPIRY_WARNING_DAYS = 14

#: Which scopes each need is met by. ``git_read`` is met by any token,
#: a deploy token included; the API is never reached with a deploy
#: token, which GitLab answers 401.
_NEED_SCOPES = {
    "git_read": None,
    "api_read": frozenset({"read_api", "api"}),
    "api_write": frozenset({"api"}),
}

_USERNAME_RE = re.compile(r"[A-Za-z0-9_.+-]{1,255}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]{8,255}")


def ca_bundle_fingerprints(pem):
    """Return the SHA-256 fingerprint of each certificate in *pem*.

    :param str pem: a CA bundle, already validated
    :return: ``AA:BB:…`` strings, in the bundle's order
    :rtype: list
    """
    certs = x509.load_pem_x509_certificates(pem.encode())
    fingerprints = []
    for cert in certs:
        digest = hashlib.sha256(
            cert.public_bytes(encoding=_der_encoding()),
        ).hexdigest().upper()
        fingerprints.append(":".join(
            digest[i:i + 2] for i in range(0, len(digest), 2)
        ))
    return fingerprints


def _der_encoding():
    """Return the DER encoding enum of ``cryptography``.

    :rtype: cryptography.hazmat.primitives.serialization.Encoding
    """
    from cryptography.hazmat.primitives.serialization import Encoding
    return Encoding.DER


def validate_ca_bundle(pem):
    """Check *pem* is a bundle of certificates with at least one CA.

    :param str pem: the bundle as pasted
    :raises ValidationError: when it is too large, holds anything that
        is not a certificate, does not parse, or has no CA certificate
    """
    if len(pem.encode()) > _MAX_CA_BUNDLE_BYTES:
        raise ValidationError(_("The CA bundle is larger than 64 KB."))
    kinds = set(_PEM_BLOCK_RE.findall(pem))
    if kinds != {"CERTIFICATE"}:
        raise ValidationError(_(
            "The CA bundle must contain PEM certificates only."
        ))
    try:
        certs = x509.load_pem_x509_certificates(pem.encode())
    except ValueError as exc:
        raise ValidationError(_("The CA bundle is not valid PEM.")) from exc
    for cert in certs:
        try:
            constraints = cert.extensions.get_extension_for_class(
                x509.BasicConstraints,
            ).value
        except x509.ExtensionNotFound:
            continue
        if constraints.ca:
            return
    raise ValidationError(_(
        "The CA bundle has no CA certificate (BasicConstraints CA=TRUE)."
    ))


class CloudGitProvider(models.Model):
    _name = "cloud.git.provider"
    _description = "Git provider"
    _order = "name, id"

    name = fields.Char(required=True)
    kind = fields.Selection(
        [("gitlab", "GitLab")], required=True, default="gitlab",
    )
    base_url = fields.Char(
        string="Base URL", required=True,
        help="https://gitlab.com, or https://git.example.com[:port][/prefix]",
    )
    authority = fields.Char(
        compute="_compute_authority", store=True, index=True,
        help="host[:port] of the base URL: one provider per server.",
    )
    api_url = fields.Char(string="API URL", compute="_compute_api_url")
    ca_bundle = fields.Text(
        string="CA bundle",
        help="PEM certificates of a private CA the server's certificate "
        "chains to. Public: it is not a secret.",
    )
    ca_fingerprints = fields.Text(
        string="CA fingerprints", compute="_compute_ca_fingerprints",
    )
    credential_ids = fields.One2many(
        "cloud.git.credential", "provider_id", string="Credentials",
    )
    active = fields.Boolean(default=True)
    last_check_at = fields.Datetime(readonly=True)
    last_check_git = fields.Char(readonly=True)
    last_check_api = fields.Char(readonly=True)

    _sql_constraints = [
        (
            "authority_unique",
            "UNIQUE(authority)",
            "There is already a Git provider for this server.",
        ),
    ]

    @api.depends("base_url")
    def _compute_authority(self):
        """Compute ``host[:port]`` of the base URL, or False when it is invalid."""
        for rec in self:
            try:
                rec.authority = base_authority(canonical_base_url(rec.base_url))
            except ValueError:
                rec.authority = False

    @api.depends("base_url")
    def _compute_api_url(self):
        """Compute the REST API's root: the base URL and ``/api/v4``."""
        for rec in self:
            rec.api_url = f"{rec.base_url}/api/v4" if rec.base_url else False

    @api.depends("ca_bundle")
    def _compute_ca_fingerprints(self):
        """Compute the SHA-256 fingerprint of each CA certificate, one per line.

        False without a bundle, or with one that does not parse.
        """
        for rec in self:
            try:
                rec.ca_fingerprints = (
                    "\n".join(ca_bundle_fingerprints(rec.ca_bundle))
                    if rec.ca_bundle else False
                )
            except ValueError:
                rec.ca_fingerprints = False

    # ── Network policy ─────────────────────────────────────────────────

    @api.model
    def _network_policy(self):
        """Return where this panel may connect to a Git provider.

        ``'any'`` in the core: a self-hosted panel reaches any GitLab its
        manager adds, private addresses included (8-nov-2026). A layer may answer ``'public'``.

        :rtype: str
        """
        return "any"

    def _host_port(self):
        """Return the ``(host, port)`` of the provider's base URL.

        :rtype: tuple
        """
        self.ensure_one()
        host, sep, port = self.authority.rpartition(":")
        if sep and port.isdigit():
            return host, int(port)
        return self.authority, 443

    def _resolve_pin(self):
        """Return the address every connection to this provider dials, or None.

        With the ``'public'`` policy the name is resolved and each of its
        addresses must be public (``net.outbound._resolve_public``); the
        one returned is pinned in the panel, on the host and in the
        build, so a later DNS change cannot point it inward. With
        ``'any'`` nothing is pinned.

        :rtype: str or None
        :raises UserError: when the policy is ``'public'`` and the name
            does not resolve to public addresses only
        """
        self.ensure_one()
        if self._network_policy() != "public":
            return None
        host, port = self._host_port()
        try:
            return _resolve_public(host, port, version=4)
        except OutboundError as exc:
            raise UserError(_(
                "%(host)s must resolve to public addresses only: this panel "
                "connects only to Git servers on a public address.",
                host=host,
            )) from exc

    # ── Constraints and writes ─────────────────────────────────────────

    @api.model
    def _normalize_vals(self, vals):
        """Return *vals* with the base URL and CA bundle normalized.

        :param dict vals: values for ``create`` or ``write``
        :rtype: dict
        :raises ValidationError: on a base URL or bundle that is refused
        """
        vals = dict(vals)
        if vals.get("base_url"):
            try:
                vals["base_url"] = canonical_base_url(vals["base_url"])
            except ValueError as exc:
                raise ValidationError(_(
                    "Invalid base URL: %s", str(exc),
                )) from exc
        if vals.get("ca_bundle"):
            vals["ca_bundle"] = vals["ca_bundle"].strip() + "\n"
            validate_ca_bundle(vals["ca_bundle"])
        return vals

    def _check_public_address(self):
        """Refuse a provider the network policy does not let the panel reach."""
        for rec in self:
            if rec._network_policy() == "public":
                rec._resolve_pin()

    @api.model_create_multi
    def create(self, vals_list):
        """Create providers with their base URL and CA bundle normalized.

        Each is checked against the network policy and audited.

        :param list vals_list: the values of each provider
        :rtype: cloud.git.provider
        :raises ValidationError: on a base URL or bundle that is refused
        :raises UserError: when the ``public`` policy refuses the server
        """
        records = super().create([self._normalize_vals(v) for v in vals_list])
        records._check_public_address()
        for rec in records:
            rec._audit("Git provider added")
        return records

    def write(self, vals):
        """Write *vals*, keeping the tokens with the server they were given for.

        A new base URL empties the token, scopes and user of each
        credential of a provider it moves, so none is sent to the new
        server before it is entered again; it is refused while
        repository lines use the provider, and so is archiving it. A new
        base URL is checked against the network policy. Each provider
        is audited with the fields written.

        :param dict vals: the values to write
        :raises ValidationError: on a base URL or bundle that is refused
        :raises UserError: when lines use a provider whose base URL
            changes or that is archived, or when the ``public`` policy
            refuses the new server
        """
        vals = self._normalize_vals(vals)
        if "base_url" in vals:
            for rec in self:
                if rec.base_url != vals["base_url"] and rec._lines_in_use():
                    raise UserError(_(
                        "The base URL of %s cannot change while repository "
                        "lines use it.", rec.name,
                    ))
        if vals.get("active") is False and any(r._lines_in_use() for r in self):
            raise UserError(_(
                "A Git provider cannot be archived while repository lines use it."
            ))
        moved = self.filtered(
            lambda r: "base_url" in vals and r.base_url != vals["base_url"]
        )
        result = super().write(vals)
        if "base_url" in vals:
            self._check_public_address()
        # A token is for the server it was given for: with a new server or
        # prefix, none is sent there before someone enters it again
        # («Test connection» would have).
        moved.sudo().credential_ids.write(
            {"token": False, "scopes": False, "token_user": False},
        )
        for rec in self:
            rec._audit("Git provider changed", sorted(vals))
        return result

    def unlink(self):
        """Delete the providers, audited; their credentials go with them.

        A provider that repository lines use is refused by the database
        (``ondelete='restrict'``); the delete route counts them first to
        say how many.
        """
        for rec in self:
            rec._audit("Git provider removed")
        return super().unlink()

    def _lines_in_use(self):
        """Return whether a repository line of a project or instance uses it.

        :rtype: bool
        """
        self.ensure_one()
        domain = [("provider_id", "=", self.id)]
        for model in ("cloud.project.repo", "cloud.instance.repo"):
            if "provider_id" in self.env[model]._fields and self.env[
                model
            ].sudo().with_context(active_test=False).search_count(domain, limit=1):
                return True
        return False

    def _audit(self, action, field_names=()):
        """Write an audit row naming the provider and the fields, never values.

        :param str action: the audit action
        :param field_names: the fields written, if any
        """
        self.ensure_one()
        details = f"{self.name} ({self.authority})"
        if field_names:
            details += ": " + ", ".join(field_names)
        self.env["cloud.audit.log"].sudo().create({
            "action": action, "details": details[:255],
        })

    # ── Credentials ────────────────────────────────────────────────────

    def _credential_for(self, path, need):
        """Return the credential for the repository *path* and *need*.

        The credential of the longest namespace covering *path* by
        segments, among those meeting *need*: ``git_read`` (any token),
        ``api_read`` (``read_api`` or ``api``), ``api_write`` (``api``).
        A deploy token never meets an API need.

        :param str path: a project path, lower case
        :param str need: ``git_read``, ``api_read`` or ``api_write``
        :rtype: cloud.git.credential (sudo, one record)
        :raises UserError: naming what is missing when none qualifies
        """
        self.ensure_one()
        wanted = _NEED_SCOPES[need]
        covering = self.sudo().credential_ids.filtered(
            lambda c: c.token and namespace_covers(c.namespace or "", path),
        )
        fitting = covering.filtered(lambda c: c._meets(wanted))
        if not fitting:
            if not covering:
                raise UserError(_(
                    "No credential of %(provider)s covers %(path)s: add one "
                    "under Settings → Git providers.",
                    provider=self.name, path=path,
                ))
            raise UserError(_(
                "No credential of %(provider)s for %(path)s has the scope "
                "%(scope)s.",
                provider=self.name, path=path,
                scope=" or ".join(sorted(wanted)),
            ))
        return max(
            fitting,
            key=lambda c: len(c.namespace.split("/")) if c.namespace else 0,
        )

    def _git_read_credential(self, path):
        """Return the credential git reads *path* with, or an empty recordset.

        Of the credentials with a token that cover the path, one that
        cannot write first (a deploy token, or a token without ``api`` nor
        ``write_repository``): the build of 11.0 to 19.0 carries it inside
        the image; then the longest
        namespace. Empty when none covers the path: the repository is
        read as a public one. «Test connection» tests this same
        credential on both channels.

        :param str path: a project path, lower case
        :rtype: cloud.git.credential (sudo, one record or empty)
        """
        self.ensure_one()
        covering = self.sudo().credential_ids.filtered(
            lambda c: c.token and namespace_covers(c.namespace or "", path),
        )
        if not covering:
            return covering
        return max(covering, key=lambda c: (
            not c._writes(),
            len(c.namespace.split("/")) if c.namespace else 0,
        ))

    def _git_credential(self, path):
        """Return ``(username, token)`` to read *path* with git, or None.

        The pair of :meth:`_git_read_credential`; None when no credential
        covers the path.

        :param str path: a project path, lower case
        :rtype: tuple or None
        """
        cred = self._git_read_credential(path)
        return cred._git_pair() if cred else None

    def _git_reader(self):
        """Return the panel's git reader for this provider.

        With its CA, and with the ``public`` policy the address checked
        now, pinned for every read of this reader.

        :rtype: net.git_reader.GitReader
        :raises GitReaderError: ``private_address`` when the policy is
            ``public`` and the name does not resolve to public addresses
        """
        self.ensure_one()
        try:
            pin = self._resolve_pin()
        except UserError as exc:
            raise GitReaderError("private_address") from exc
        return GitReader(ca_pem=self.sudo().ca_bundle or None, pin_ip=pin)

    # ── Expiry check (daily cron) ──────────────────────────────────────

    @api.model
    def _cron_check_git_providers(self):
        """Warn about tokens about to expire, and check the panel's webhooks.

        One alert per credential whose ``expires_on`` is within
        :data:`EXPIRY_WARNING_DAYS` days (or past); a credential that
        is no longer due has its alert resolved. Each provider in its
        own savepoint, so one that fails does not stop the others.
        """
        Alert = self.env["cloud.alert"].sudo()
        horizon = fields.Date.context_today(self) + timedelta(
            days=EXPIRY_WARNING_DAYS,
        )
        for provider in self.sudo().search([]):
            try:
                with self.env.cr.savepoint():
                    for cred in provider.credential_ids:
                        key = f"git_credential:{cred.id}"
                        if cred.expires_on and cred.expires_on <= horizon:
                            Alert.raise_alert(
                                "git_token_expiring",
                                _(
                                    "The token of %(provider)s for %(ns)s "
                                    "expires on %(date)s.",
                                    provider=provider.name,
                                    ns=cred.namespace or provider.authority,
                                    date=cred.expires_on,
                                ),
                                key=key,
                            )
                        else:
                            Alert.resolve_alert("git_token_expiring", key=key)
            except Exception:
                _logger.exception(
                    "Git provider %s: the expiry check failed", provider.id,
                )
            provider._check_hooks()

    def _check_hooks(self):
        """Read the state of the panel's webhooks in GitLab and sweep the orphans.

        Each webhook in its own savepoint, so one GitLab that fails does
        not stop the others. Never sends a test nor re-enables a webhook.
        """
        self.ensure_one()
        for hook in self.env["cloud.gitlab.hook"].sudo().search(
            [("provider_id", "=", self.id)],
        ):
            try:
                with self.env.cr.savepoint():
                    if hook.remote_hook_id and hook.state != "orphan":
                        hook._check_remote_state()
                    hook._sweep_orphan()
            except Exception:
                _logger.exception("GitLab webhook %s: the check failed", hook.id)


class CloudGitCredential(models.Model):
    _name = "cloud.git.credential"
    _description = "Git provider credential"
    _order = "provider_id, namespace, id"

    provider_id = fields.Many2one(
        "cloud.git.provider", required=True, ondelete="cascade", index=True,
    )
    namespace = fields.Char(
        default="",
        help="Empty for the whole server; group, group/subgroup or a "
        "project path.",
    )
    kind = fields.Selection(
        [
            ("personal", "Personal access token"),
            ("group", "Group access token"),
            ("project", "Project access token"),
            ("deploy", "Deploy token"),
        ],
        required=True, default="project",
    )
    username = fields.Char(
        help="oauth2 for personal, group and project tokens; the deploy "
        "token's own user name (gitlab+deploy-token-N).",
    )
    # Never copied: a token is entered for its credential alone.
    token = EncryptedChar(groups="incubacloud.group_cloud_manager", copy=False)
    scopes = fields.Char(
        readonly=True,
        help="Read from GitLab when the token is tested or saved, and "
        "emptied when the token changes. Empty: a deploy token, or one "
        "whose scopes were not read, taken as one that can write.",
    )
    token_user = fields.Char(readonly=True)
    expires_on = fields.Date()

    def init(self):
        """Create the unique index on (provider_id, namespace).

        Odoo 19 declares it with ``models.UniqueIndex``; Odoo 18 has no
        such declaration, so the index is created here, under the name
        Odoo 19 gives it.
        """
        super().init()
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "cloud_git_credential_provider_namespace_unique "
            "ON cloud_git_credential (provider_id, COALESCE(namespace, ''))"
        )

    def _meets(self, wanted):
        """Return whether this credential meets the scopes *wanted*.

        :param wanted: a value of ``_NEED_SCOPES`` (None = any token)
        :rtype: bool
        """
        self.ensure_one()
        if wanted is None:
            return True
        if self.kind == "deploy":
            return False
        return bool(wanted & set((self.scopes or "").replace(",", " ").split()))

    def _scopes_unread(self):
        """Return whether nothing is known of this token's scopes.

        A token that is not a deploy token and has no scopes: they were
        not read (a save past the hourly cap, a new token not tested
        yet), or GitLab did not answer about it.

        :rtype: bool
        """
        self.ensure_one()
        return self.kind != "deploy" and not (self.scopes or "").strip()

    def _writes(self):
        """Return whether this credential's token can write to repositories.

        ``api`` or ``write_repository`` among its scopes, or scopes not
        read (:meth:`_scopes_unread`), which could be any; a deploy token never, as the panel keeps none
        that writes.

        :rtype: bool
        """
        self.ensure_one()
        if self.kind == "deploy":
            return False
        if self._scopes_unread():
            return True
        return bool(
            {"api", "write_repository"}
            & set((self.scopes or "").replace(",", " ").split())
        )

    def _git_pair(self):
        """Return ``(username, token)`` for git over https.

        :rtype: tuple
        """
        self.ensure_one()
        cred = self.sudo()
        return cred.username or "oauth2", cred.token

    def _introspect(self):
        """Read from GitLab what the token is: scopes, expiry and user.

        ``GET /personal_access_tokens/self`` serves personal, group and
        project tokens of any scope; ``current_user`` is asked only with
        ``api`` or ``read_api``. A 401 (a deploy token) leaves the
        credential without API (``scopes=''``): not an error, it only
        limits what the panel does with it.

        :return: the scopes read, empty for a token without API
        :rtype: str
        :raises UserError: when GitLab cannot be asked, without the token
        """
        self.ensure_one()
        cred = self.sudo()
        provider = cred.provider_id
        client = GitLabClient(
            provider.api_url, cred.token,
            ca_pem=provider.ca_bundle or None, pin_ip=provider._resolve_pin(),
        )
        try:
            info = client.token_self()
            scopes = sorted(info.get("scopes") or ())
            user = False
            if {"api", "read_api"} & set(scopes):
                user = client.current_user().get("username") or False
        except GitLabError as exc:
            if exc.status == 401:
                cred.write({"scopes": "", "token_user": False})
                return ""
            raise UserError(_(
                "%(provider)s did not answer about the token (%(code)s).",
                provider=provider.name, code=exc.code,
            )) from exc
        vals = {"scopes": " ".join(scopes), "token_user": user}
        if info.get("expires_at"):
            vals["expires_on"] = fields.Date.to_date(info["expires_at"])
        cred.write(vals)
        return vals["scopes"]

    @api.model
    def _normalize_vals(self, vals):
        """Return *vals* with the namespace, user and token checked.

        :param dict vals: values for ``create`` or ``write``
        :rtype: dict
        :raises ValidationError: on a namespace, user name or token that
            is refused
        """
        vals = dict(vals)
        if "namespace" in vals:
            try:
                vals["namespace"] = normalize_namespace(vals["namespace"])
            except ValueError as exc:
                raise ValidationError(_("Invalid namespace.")) from exc
        if vals.get("username") and not _USERNAME_RE.fullmatch(vals["username"]):
            raise ValidationError(_("Invalid user name."))
        if vals.get("token") and not _TOKEN_RE.fullmatch(vals["token"]):
            raise ValidationError(_(
                "The token may hold only letters, digits, '_', '.' and '-' "
                "(8 to 255)."
            ))
        return vals

    @api.constrains("kind", "username")
    def _check_username(self):
        """Require a deploy token's own user name (``gitlab+deploy-token-N``).

        :raises ValidationError: for a deploy token without one
        """
        for rec in self:
            if rec.kind == "deploy" and not rec.username:
                raise ValidationError(_(
                    "A deploy token needs its own user name "
                    "(gitlab+deploy-token-N)."
                ))

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [self._normalize_vals(v) for v in vals_list]
        for vals in vals_list:
            vals.setdefault("namespace", "")
            if vals.get("kind", "project") != "deploy" and not vals.get("username"):
                vals["username"] = "oauth2"
        records = super().create(vals_list)
        for rec in records:
            rec._audit("Git credential added")
        return records

    def write(self, vals):
        """Write *vals*, keeping each token with its server and what was read of it.

        A credential never moves to another provider: its token was given
        for that server, and a move would send it elsewhere without the
        base URL changing. A new
        token empties the scopes and user read for the old one, unless
        the same write sets them.

        :param dict vals: the values to write
        :raises UserError: when *vals* moves a credential to another provider
        """
        vals = self._normalize_vals(vals)
        if "provider_id" in vals and any(
            rec.provider_id.id != vals["provider_id"] for rec in self
        ):
            raise UserError(_(
                "A Git credential cannot move to another Git provider: add "
                "it there with its own token."
            ))
        if "token" in vals:
            vals.setdefault("scopes", False)
            vals.setdefault("token_user", False)
        result = super().write(vals)
        for rec in self:
            rec._audit("Git credential changed", sorted(vals))
        return result

    def unlink(self):
        for rec in self:
            rec._audit("Git credential removed")
        return super().unlink()

    def _audit(self, action, field_names=()):
        """Write an audit row naming the credential and fields, never values.

        :param str action: the audit action
        :param field_names: the fields written, if any
        """
        self.ensure_one()
        details = (
            f"{self.provider_id.name} ({self.provider_id.authority}), "
            f"namespace '{self.namespace or ''}'"
        )
        if field_names:
            details += ": " + ", ".join(field_names)
        self.env["cloud.audit.log"].sudo().create({
            "action": action, "details": details[:255],
        })
