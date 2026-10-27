"""The MCP's own panel routes: the Connect an agent page and its settings."""

from odoo import _, http
from odoo.http import request

from ..models.cloud_settings import RESOURCE_PARAM
from ..protocol.canonical import canonical_resource

#: The user documentation of agents.
DOCS_URL = "https://docs.incubacloud.io/agents/"
#: Where an internal user of a self-hosted panel creates an API key.
PREFERENCES_URL = "/odoo/action-base.action_res_users_my"
#: What a panel that is set from above refuses to save.
_MANAGED = {"mcp_oauth_issuer", "mcp_resource"}
_SAVABLE = _MANAGED | {"rate_limit_mcp_user_per_min"}


class McpSettingsController(http.Controller):
    """What the Connect an agent page and the Agents tab of Settings read and save."""

    @http.route("/cloud/mcp/config", type="jsonrpc", auth="user")
    def mcp_config(self):
        """What the Connect an agent page shows. Every cloud role."""
        request.env["cloud.security.mixin"]._check_cloud_group("group_cloud_user")
        Settings = request.env["cloud.settings"].sudo()
        api_keys = Settings._mcp_api_keys_allowed()
        return {
            "resource": Settings._mcp_resource(),
            "issuer": Settings._mcp_issuer(),
            "api_keys": api_keys,
            "managed": Settings._mcp_settings_managed(),
            "agents_url": Settings._mcp_agents_url(),
            "docs_url": DOCS_URL,
            "preferences_url": PREFERENCES_URL if api_keys else "",
        }

    @http.route("/cloud/mcp/get_settings", type="jsonrpc", auth="user")
    def mcp_get_settings(self):
        """The MCP's settings, for Settings. Managers."""
        request.env["cloud.security.mixin"]._check_can_manage_settings()
        Settings = request.env["cloud.settings"].sudo()
        settings = Settings._get_system()
        return {
            "mcp_oauth_issuer": settings.mcp_oauth_issuer or "",
            "mcp_resource": Settings._mcp_resource(),
            "rate_limit_mcp_user_per_min": settings.rate_limit_mcp_user_per_min or 0,
            "managed": Settings._mcp_settings_managed(),
        }

    @http.route("/cloud/mcp/save_settings", type="jsonrpc", auth="user")
    def mcp_save_settings(self, vals):
        """Save what *vals* holds of the MCP's settings. Managers.

        On a panel whose issuer and URL are set from above, both are
        refused; the limit is still the manager's.
        """
        request.env["cloud.security.mixin"]._check_can_manage_settings()
        vals = dict(vals or {})
        unknown = sorted(set(vals) - _SAVABLE)
        if unknown:
            return {
                "ok": False,
                "error": _("Unknown setting(s): %s", ", ".join(unknown)),
            }
        Settings = request.env["cloud.settings"].sudo()
        if _MANAGED & set(vals) and Settings._mcp_settings_managed():
            return {
                "ok": False,
                "error": _(
                    "On this panel the agents' issuer and the MCP URL are set by "
                    "the platform."
                ),
            }
        write = {}
        if "mcp_oauth_issuer" in vals:
            write["mcp_oauth_issuer"] = (
                (vals["mcp_oauth_issuer"] or "").strip().rstrip("/")
            )
        if "rate_limit_mcp_user_per_min" in vals:
            try:
                limit = int(vals["rate_limit_mcp_user_per_min"] or 0)
            except (TypeError, ValueError):
                return {"ok": False, "error": _("The agents' limit must be a number.")}
            write["rate_limit_mcp_user_per_min"] = max(0, limit)
        if "mcp_resource" in vals:
            try:
                resource = canonical_resource(vals["mcp_resource"])
            except ValueError:
                return {
                    "ok": False,
                    "error": _("The MCP URL must be an absolute https URL."),
                }
            request.env["ir.config_parameter"].sudo().set_param(
                RESOURCE_PARAM, resource
            )
        if write:
            Settings._get_system().write(write)
        return {"ok": True}
