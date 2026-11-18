"""The MCP's settings: issuer, per-user limit, key and resource URL.

The issuer and the limit are fields of the settings singleton, edited in
Settings. The HMAC key and the canonical URL of the MCP are system
parameters: the key is never shown, and on a panel whose settings another
panel pushes, the URL arrives that way, through the database's
parameters.
"""

import logging
import secrets
from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..protocol.canonical import ALIAS_PATH, MCP_PATH, canonical_resource

_logger = logging.getLogger(__name__)

SECRET_PARAM = "incubacloud.mcp.secret"
RESOURCE_PARAM = "incubacloud.mcp.resource"
#: The two paths the MCP answers at.
_PATHS = frozenset({MCP_PATH, ALIAS_PATH})
#: Calls per user and minute when the setting is empty or not positive.
MCP_USER_PER_MIN_DEFAULT = 120


class CloudSettings(models.Model):
    _inherit = "cloud.settings"

    mcp_oauth_issuer = fields.Char(
        string="Agents' OAuth issuer",
        help="Authorization server whose tokens agents may use here (its "
        "issuer URL). Empty: agents connect with API keys only.",
    )
    rate_limit_mcp_user_per_min = fields.Integer(
        string="Agent calls per user and minute",
        default=MCP_USER_PER_MIN_DEFAULT,
        help="Tool calls one user's agents may make per minute, all of them "
        "together. Zero or less falls back to the default (120).",
    )

    @api.constrains("mcp_oauth_issuer")
    def _check_mcp_oauth_issuer(self):
        """An issuer is an https URL without credentials, query or fragment."""
        for settings in self:
            issuer = (settings.mcp_oauth_issuer or "").strip()
            if not issuer:
                continue
            parts = urlsplit(issuer)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or "@" in parts.netloc
                or parts.query
                or parts.fragment
            ):
                raise ValidationError(
                    _(
                        "The agents' issuer must be an https URL, such as "
                        "https://login.example.com."
                    )
                )

    @api.model
    def _mcp_issuer(self):
        """Return the configured issuer, or ``''`` (API keys only)."""
        return (self._get_system().mcp_oauth_issuer or "").strip().rstrip("/")

    @api.model
    def _mcp_stored_resource(self):
        """Return the canonical URL of this panel's MCP, as configured.

        The stored parameter, else ``web.base.url`` + ``/cloud/mcp``; ``''``
        when neither makes an absolute URL. Panels installed before
        ``/cloud/mcp`` stored ``…/mcp`` and keep it.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        base_url = (ICP.get_param("web.base.url") or "").rstrip("/")
        raw = ICP.get_param(RESOURCE_PARAM) or f"{base_url}{MCP_PATH}"
        try:
            return canonical_resource(raw)
        except ValueError:
            return ""

    @api.model
    def _mcp_resource(self):
        """Return the canonical URL of this panel's MCP: the address it gives.

        The configured one (:meth:`_mcp_stored_resource`), except where its
        path is ``/mcp``, the address before ``/cloud/mcp``: then the same
        origin with ``/cloud/mcp``, also this panel's. That is so on every
        panel whose URL is its own to set, and on one whose URL is set from
        above (:meth:`_mcp_settings_managed`) only while another installed
        module declares an alias (``ir.http._mcp_alias_owners``): the panel
        that sets it issues the agents' tokens for the URL it pushed, so
        this one gives that URL as it is. Nothing is written: the agents
        connected to ``…/mcp`` keep their tokens (:meth:`_mcp_audiences`),
        and a pushed ``…/mcp`` is back once that module is gone.
        """
        resource = self._mcp_stored_resource()
        if not resource:
            return ""
        parts = urlsplit(resource)
        if parts.path != ALIAS_PATH:
            return resource
        if self._mcp_settings_managed():
            owners = self._mcp_conflicts()
            if not owners:
                return resource
            _logger.debug(
                "[mcp] %s declares the MCP's aliases: this panel's MCP is %s "
                "instead of %s",
                ", ".join(sorted(set(owners.values()))),
                MCP_PATH,
                resource,
            )
        return f"{parts.scheme}://{parts.netloc}{MCP_PATH}"

    @api.model
    def _mcp_resource_at(self, path):
        """Return the URL an agent that called *path* knows this MCP by.

        RFC 9728, §3.3: a client checks that the ``resource`` of the
        document it is sent to is the URL it called. On ``/mcp`` or
        ``/cloud/mcp`` of a panel whose MCP is one of them, that is the
        same origin with the path called; otherwise (no path, or a proxy
        that maps the MCP from a path of its own) the panel's
        :meth:`_mcp_resource`.

        :param str path: the path of the request, or ``''``
        :return: a canonical URL, or ``''`` without a usable one
        :rtype: str
        """
        resource = self._mcp_resource()
        if not resource:
            return ""
        parts = urlsplit(resource)
        path = (path or "").rstrip("/")
        if parts.path in _PATHS and path in _PATHS:
            return f"{parts.scheme}://{parts.netloc}{path}"
        return resource

    @api.model
    def _mcp_audiences(self):
        """Return the audiences an agent's token may carry on this panel.

        Both addresses of the MCP on the origin of :meth:`_mcp_resource`,
        ``…/cloud/mcp`` and ``…/mcp``, while it is one of them: the agents
        connected to the address before keep their tokens, and so do those
        that connected to ``/cloud/mcp`` while another module had ``/mcp``.
        A URL on a path of its own (a proxy's) is the only one. No other
        origin, ever.

        :return: canonical URLs; empty without a usable one
        :rtype: tuple
        """
        resource = self._mcp_resource()
        if not resource:
            return ()
        parts = urlsplit(resource)
        if parts.path not in _PATHS:
            return (resource,)
        origin = f"{parts.scheme}://{parts.netloc}"
        return (origin + MCP_PATH, origin + ALIAS_PATH)

    @api.model
    def _mcp_conflicts(self):
        """Return the aliases another installed module declares, and which.

        Any one of them is enough for the MCP to give up all three
        (``models/ir_http.py``).

        :return: ``{alias: module}``, as ``ir.http._mcp_alias_owners``
        :rtype: dict
        """
        return dict(self.env["ir.http"]._mcp_alias_owners())

    @api.model
    def _mcp_secret(self):
        """Return the module's 32-byte HMAC key, creating it when missing.

        :rtype: bytes
        """
        ICP = self.env["ir.config_parameter"].sudo()
        value = ICP.get_param(SECRET_PARAM)
        if not value:
            value = secrets.token_hex(32)
            ICP.set_param(SECRET_PARAM, value)
        return bytes.fromhex(value)

    @api.model
    def _mcp_user_cap(self):
        """Return the per-user cap of tool calls per minute."""
        value = self._get_system().rate_limit_mcp_user_per_min or 0
        return value if value > 0 else MCP_USER_PER_MIN_DEFAULT

    @api.model
    def _mcp_install_defaults(self):
        """Give the module its key and this panel its resource URL.

        Runs on every install and update (``data/mcp_defaults.xml``) and
        changes nothing already set: a URL pushed from another panel stays,
        and so does the ``…/mcp`` of a panel installed before ``/cloud/mcp``,
        which :meth:`_mcp_resource` reads as ``…/cloud/mcp``.
        The routing map is not read here: while a module installs, it is
        not in it yet.
        """
        self._mcp_secret()
        ICP = self.env["ir.config_parameter"].sudo()
        if not ICP.get_param(RESOURCE_PARAM):
            resource = self._mcp_stored_resource()
            if resource:
                ICP.set_param(RESOURCE_PARAM, resource)

    # ── Extension points for the layer that runs this panel ───────────

    @api.model
    def _mcp_api_keys_allowed(self):
        """Whether agents may present an API key instead of a token.

        True here: a self-hosted panel's users have passwords and keys. A
        panel whose users sign in through another panel may say False.
        """
        return True

    @api.model
    def _mcp_settings_managed(self):
        """Whether the issuer and the resource URL are set from above.

        False here; a panel whose settings another panel pushes says True
        and Settings shows them read-only. That panel's URL is then given
        as pushed, ``…/mcp`` included (:meth:`_mcp_resource`): the panel
        that pushes it issues the tokens for it, and moves the panel to
        ``…/cloud/mcp`` by pushing that.
        """
        return False

    @api.model
    def _mcp_agents_url(self):
        """Return where the user revokes connected agents, or ``''``."""
        return ""

    @api.model
    def _mcp_auto_approval_charges(self):
        """Whether agents' actions on this panel can charge an account: some
        tool of the catalog says it charges money (``ToolSpec.charges``).

        Then the grant of auto-approval offers to include charges, and its
        notice says whether it does.
        """
        return any(
            spec.may_charge
            for spec in self.env["incubacloud.mcp.catalog"]._tool_specs()
        )
