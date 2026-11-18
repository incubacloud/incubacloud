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
        """Return the canonical URL of this panel's MCP: the agents' audience.

        The configured one (:meth:`_mcp_stored_resource`), except where it
        is ``…/mcp`` and another installed module answers ``/mcp``
        (``ir.http._mcp_alias_owners``): then the same origin with
        ``/cloud/mcp``, which is still this panel's. Nothing is written:
        once that module is gone, the configured URL is back.
        """
        resource = self._mcp_stored_resource()
        if not resource:
            return ""
        parts = urlsplit(resource)
        if parts.path != ALIAS_PATH:
            return resource
        owner = dict(self.env["ir.http"]._mcp_alias_owners()).get(ALIAS_PATH)
        if not owner:
            return resource
        moved = f"{parts.scheme}://{parts.netloc}{MCP_PATH}"
        _logger.debug(
            "[mcp] %s answers %s: this panel's MCP is %s instead of %s",
            owner,
            ALIAS_PATH,
            moved,
            resource,
        )
        return moved

    @api.model
    def _mcp_conflicts(self):
        """Return the aliases another installed module answers, and which.

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
        and so does the ``…/mcp`` of a panel installed before ``/cloud/mcp``.
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
        and Settings shows them read-only.
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
