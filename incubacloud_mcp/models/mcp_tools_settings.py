"""Settings tools: read them all; change one section, keeping the rest."""

from odoo import api, models
from odoo.exceptions import UserError

from odoo.addons.incubacloud.net.trusted_proxies import invalid_ranges, parse_ranges

from ..protocol.canonical import canonical_resource
from ..protocol.tool_spec import ToolSpec, obj, record_id, string
from .mcp_catalog import tool_policies

_MANAGER = "group_cloud_manager"

#: What ``save_general_settings`` accepts, the secret token excepted.
SAVE_GENERAL_PARAMS = (
    "autoassign_enabled",
    "default_backup_backend_id",
    "audit_log_retention_days",
    "job_log_retention_days",
    "job_retention_days",
    "staging_autopurge_days",
    "default_backup_alert_threshold_pct",
    "github_event_retention_days",
    "github_event_truncate_days",
    "metrics_enabled",
    "metrics_central_url",
    "metrics_remote_write_url",
    "metrics_retention_days",
    "grafana_base_url",
    "container_log_max_size",
    "container_log_max_file",
    "odoo_log_archive_days",
    "log_download_max_mb",
    "log_search_max_files",
    "log_search_timeout_s",
    "trusted_proxy_ranges",
    "metrics_central_public_host",
    "metrics_central_operator_sources",
    "metrics_ship_security_logs",
)
#: What ``save_core_rate_limits`` accepts.
CORE_RATE_KEYS = (
    "rate_limit_webhook_per_min",
    "rate_limit_terminal_per_min",
    "rate_limit_terminal_user_per_min",
    "rate_limit_host_console_per_min",
    "rate_limit_connect_per_min",
    "rate_limit_connect_user_per_min",
    "rate_limit_logs_per_min",
    "rate_limit_mail_reads_per_min",
    "rate_limit_log_search_per_min",
    "rate_limit_github_previews_per_hour",
    "rate_limit_github_imports_per_hour",
)
#: What ``save_github_app`` takes besides its secrets, which agents never send.
GITHUB_APP_KEYS = ("app_id", "installation_id")
MCP_RATE_KEY = "rate_limit_mcp_user_per_min"
MCP_KEYS = ("mcp_oauth_issuer", "mcp_resource")
#: General settings that always ask for approval (D2).
SENSITIVE_GENERAL = ("audit_log_retention_days", "trusted_proxy_ranges")
#: General settings that ask for approval where the panel hides them.
OBSERVABILITY_FIELDS = (
    "metrics_enabled",
    "metrics_central_url",
    "metrics_remote_write_url",
    "metrics_retention_days",
    "metrics_ship_security_logs",
    "grafana_base_url",
    "metrics_central_public_host",
    "metrics_central_operator_sources",
)


def normalized_setting(key, value, current=None):
    """Return *value* as the panel would hold it, to compare with *current*.

    Numbers count as numbers and booleans as booleans (by the type of the
    value held), text without its surrounding spaces, the issuer without
    its final slash, the MCP URL in canonical form and trusted proxy
    ranges as the networks they name. A value that cannot be read so is
    returned as it is: it counts as a change.

    :param str key: the setting's name
    :param value: a value given, or the one held
    :param current: the value held, whose type decides
    """
    if key == "trusted_proxy_ranges":
        text = "" if value in (None, False) else str(value)
        return tuple(parse_ranges(text)), tuple(invalid_ranges(text))
    if key == "mcp_oauth_issuer":
        return str(value or "").strip().rstrip("/")
    if key == "mcp_resource":
        try:
            return canonical_resource(value)
        except (AttributeError, ValueError):
            return value
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if isinstance(current, str) and isinstance(value, str):
        return value.strip()
    return value


_ACTIONS = {
    "general": ("save",),
    "rate_limits": ("save",),
    "mcp": ("save",),
    "backup_backend": ("save", "test", "measure"),
    "github_app": ("save", "detect_installation", "test_connection"),
}

TOOLS = (
    ToolSpec(
        "get_settings",
        "Every setting of the panel: general, rate limits, the agents' issuer and MCP "
        "URL, backup destinations (one in detail with backend_id) and the GitHub App. "
        "Secrets show only as has_<field> flags.",
        _MANAGER,
        obj({"backend_id": record_id("One backup destination in detail.")}),
        handler="_tool_get_settings",
        read_only=True,
    ),
    ToolSpec(
        "update_settings",
        "Change one section of the settings (only the values given change): general, "
        "rate_limits, mcp, backup_backend (create without backend_id; action test or "
        "measure) or github_app (action detect_installation or test_connection). "
        "Secrets are refused. Security settings ask the user to approve the change.",
        _MANAGER,
        obj(
            {
                "section": string("Which section.", enum=tuple(_ACTIONS)),
                "values": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": "The settings to change.",
                },
                "backend_id": record_id("The backup destination."),
                "action": string(
                    "What to do; save by default.",
                    enum=(
                        "save",
                        "test",
                        "measure",
                        "detect_installation",
                        "test_connection",
                    ),
                ),
            },
            required=("section",),
        ),
        handler="_tool_update_settings",
        confirm="conditional",
    ),
)

ROUTES = {
    "/cloud/get_general_settings": "get_settings",
    "/cloud/get_backup_backends": "get_settings",
    "/cloud/get_backup_backend": "get_settings",
    "/cloud/get_core_rate_limits": "get_settings",
    "/cloud/get_github_app": "get_settings",
    "/cloud/mcp/get_settings": "get_settings",
    "/cloud/save_general_settings": "update_settings",
    "/cloud/save_core_rate_limits": "update_settings",
    "/cloud/create_backup_backend": "update_settings",
    "/cloud/save_backup_backend": "update_settings",
    "/cloud/test_backup_backend": "update_settings",
    "/cloud/measure_backup_usage": "update_settings",
    "/cloud/save_github_app": "update_settings",
    "/cloud/detect_github_installation": "update_settings",
    "/cloud/test_github_connection": "update_settings",
    "/cloud/mcp/save_settings": "update_settings",
}


class McpCatalogSettings(models.AbstractModel):
    """The settings tools, and the routes they call."""

    _inherit = "incubacloud.mcp.catalog"

    @api.model
    def _tool_specs(self):
        """Add the settings tools."""
        return super()._tool_specs() + list(TOOLS)

    @api.model
    def _route_policies(self):
        """Classify the settings routes."""
        return {**super()._route_policies(), **tool_policies(ROUTES)}


class McpToolsSettings(models.AbstractModel):
    """Handlers of the settings tools, and the approval of security settings."""

    _inherit = "incubacloud.mcp.tools"

    @api.model
    def _tool_get_settings(self, args):
        """Every section, as the Settings page reads them."""
        mcp = self._route("/cloud/mcp/get_settings")
        result = {
            "general": self._route("/cloud/get_general_settings"),
            "rate_limits": {
                **self._route("/cloud/get_core_rate_limits"),
                MCP_RATE_KEY: mcp.get(MCP_RATE_KEY),
            },
            "mcp": {key: mcp.get(key) for key in (*MCP_KEYS, "managed")},
            "backup_backends": self._route("/cloud/get_backup_backends"),
            "github_app": self._route("/cloud/get_github_app"),
        }
        if args.get("backend_id"):
            result["backup_backend"] = self._route(
                "/cloud/get_backup_backend", backend_id=args["backend_id"]
            )
        return result

    @api.model
    def _only(self, section, values, allowed):
        """Refuse a key of *values* that the section does not have.

        :raises UserError: naming the unknown keys
        """
        unknown = sorted(set(values) - set(allowed))
        if unknown:
            raise UserError(
                self.env._(
                    "Unknown setting(s) for %(section)s: %(keys)s",
                    section=section,
                    keys=", ".join(unknown),
                )
            )

    @api.model
    def _tool_update_settings(self, args):
        """Change one section, keeping every value not given."""
        section, values = args["section"], args.get("values") or {}
        action = args.get("action") or "save"
        if action not in _ACTIONS[section]:
            raise UserError(
                self.env._(
                    "The section %(section)s has no action %(action)s.",
                    section=section,
                    action=action,
                )
            )
        if section == "general":
            self._only(section, values, SAVE_GENERAL_PARAMS)
            current = self._route("/cloud/get_general_settings")
            params = {
                key: current[key] for key in SAVE_GENERAL_PARAMS if key in current
            }
            params.update(values)
            return self._route("/cloud/save_general_settings", **params)
        if section == "rate_limits":
            self._only(section, values, (*CORE_RATE_KEYS, MCP_RATE_KEY))
            core = {
                key: value for key, value in values.items() if key in CORE_RATE_KEYS
            }
            result = (
                self._route("/cloud/save_core_rate_limits", vals=core)
                if core
                else {"ok": True}
            )
            if MCP_RATE_KEY in values and result.get("ok") is not False:
                result = self._route(
                    "/cloud/mcp/save_settings",
                    vals={MCP_RATE_KEY: values[MCP_RATE_KEY]},
                )
            return result
        if section == "mcp":
            self._only(section, values, MCP_KEYS)
            return self._route("/cloud/mcp/save_settings", vals=values)
        if section == "backup_backend":
            return self._update_backup_backend(args.get("backend_id"), values, action)
        if action == "detect_installation":
            return self._route("/cloud/detect_github_installation")
        if action == "test_connection":
            return self._route("/cloud/test_github_connection")
        # save_github_app writes both ids: the one not given keeps its value.
        self._only(section, values, GITHUB_APP_KEYS)
        current = self._route("/cloud/get_github_app")
        vals = {key: current.get(key) or "" for key in GITHUB_APP_KEYS}
        vals.update(values)
        return self._route("/cloud/save_github_app", vals=vals)

    @api.model
    def _update_backup_backend(self, backend_id, values, action):
        """Create, save, test or measure a backup destination."""
        if action in ("test", "measure") and not backend_id:
            raise UserError(self.env._("Give the backup destination (backend_id)."))
        if action == "test":
            return self._route("/cloud/test_backup_backend", backend_id=backend_id)
        if action == "measure":
            return self._with_job_state(
                self._route("/cloud/measure_backup_usage", backend_id=backend_id)
            )
        if backend_id:
            return self._route(
                "/cloud/save_backup_backend", backend_id=backend_id, vals=values
            )
        return self._route("/cloud/create_backup_backend", vals=values)

    @api.model
    def _sensitive_changes(self, args):
        """Return ``[(field, current, new)]`` of the security settings changed.

        D2: the audit retention, the trusted proxy ranges, every rate
        limit, the agents' issuer and MCP URL, and what ``get_config``
        hides on this panel (observability, on a tenant). A value equal
        to the one held once normalised (:func:`normalized_setting`) is
        no change.
        """
        section, values = args["section"], args.get("values") or {}
        if section == "general":
            current = self._route("/cloud/get_general_settings")
            sensitive = set(SENSITIVE_GENERAL)
            features = self._route("/cloud/get_config").get("features") or {}
            if (features.get("observability") or {}).get("configure") is False:
                sensitive |= set(OBSERVABILITY_FIELDS)
        elif section == "rate_limits":
            current = {
                **self._route("/cloud/get_core_rate_limits"),
                **self._route("/cloud/mcp/get_settings"),
            }
            sensitive = set(values)
        elif section == "mcp":
            current = self._route("/cloud/mcp/get_settings")
            sensitive = set(values)
        else:
            return []
        return [
            (key, current.get(key), values[key])
            for key in sorted(sensitive & set(values))
            if normalized_setting(key, values[key], current.get(key))
            != normalized_setting(key, current.get(key), current.get(key))
        ]

    @api.model
    def _confirm_update_settings(self, args):
        """Ask for approval when a security setting changes, saying from what to what.

        :return: the text the user approves, or None when nothing
            sensitive changes
        """
        changes = self._sensitive_changes(args)
        if not changes:
            return None
        lines = "\n".join(f"- {key}: {old!r} → {new!r}" for key, old, new in changes)
        return self.env._(
            "Change these security settings of the panel:\n%(changes)s",
            changes=lines,
        )
