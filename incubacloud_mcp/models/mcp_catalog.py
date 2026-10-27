"""The registry: every tool, and what becomes of every route of the panel.

Each module adds its own: this one core's, the PITR bridge PITR's, the
tenant panel its routes. ``_route_policies`` maps each rule of the routing
map (its string, as ``ir.http.routing_map()`` holds it) to
``("tool", <name>)`` or ``("excluded", <reason>)``; the inventory test
fails on a ``/cloud/*`` route nobody classified.
"""

from odoo import api, models

_EXCLUDED_FOR_THE_BROWSER = "A page or a file for a browser; {tool} replaces it."
_SPA = "The panel's single-page application."
_UPLOAD = "Uploads of gigabytes belong in the panel."
_TERMINAL = "Interactive terminal (a PTY); run_command replaces it."
_HOST_TERMINAL = "Interactive host console; run_host_command replaces it."
_PREFERENCES = "The user's own notification preferences, not the operation."
_FORMS = "Serves the panel's own forms only."
_GITHUB_APP = "Installs the GitHub App in a browser."
_PUBLIC = "Public endpoint, not an action of the user."

#: Core routes no tool calls, each with why.
CORE_EXCLUDED = {
    "/cloud": _SPA,
    "/cloud/<path:subpath>": _SPA,
    "/cloud/get_secret": "Secrets never leave through an agent (decision 6).",
    "/cloud/save_github_pat": "Credentials are only written in the panel (decision 6).",
    "/cloud/instance/<int:instance_id>/restore": _UPLOAD,
    "/cloud/instance/<int:instance_id>/restore/begin": _UPLOAD,
    "/cloud/instance/<int:instance_id>/restore/part": _UPLOAD,
    "/cloud/instance/<int:instance_id>/restore/finish": _UPLOAD,
    "/cloud/terminal/open": _TERMINAL,
    "/cloud/terminal/<string:session_id>": _TERMINAL,
    "/cloud/terminal/<string:session_id>/output": _TERMINAL,
    "/cloud/terminal/<string:session_id>/input": _TERMINAL,
    "/cloud/terminal/<string:session_id>/resize": _TERMINAL,
    "/cloud/terminal/<string:session_id>/close": _TERMINAL,
    "/cloud/host_terminal/open": _HOST_TERMINAL,
    "/cloud/host_terminal/<string:session_id>": _HOST_TERMINAL,
    "/cloud/host_terminal/<string:session_id>/output": _HOST_TERMINAL,
    "/cloud/host_terminal/<string:session_id>/input": _HOST_TERMINAL,
    "/cloud/host_terminal/<string:session_id>/resize": _HOST_TERMINAL,
    "/cloud/host_terminal/<string:session_id>/close": _HOST_TERMINAL,
    "/cloud/log/<int:job_id>": _EXCLUDED_FOR_THE_BROWSER.format(tool="get_job"),
    "/cloud/log/<int:job_id>/download": _EXCLUDED_FOR_THE_BROWSER.format(
        tool="get_job"
    ),
    "/cloud/instance/<int:instance_id>/logs": _EXCLUDED_FOR_THE_BROWSER.format(
        tool="read_logs"
    ),
    "/cloud/instance/<int:instance_id>/log_archive/<string:name>": (
        _EXCLUDED_FOR_THE_BROWSER.format(tool="read_logs")
    ),
    "/cloud/get_user_preferences": _PREFERENCES,
    "/cloud/save_user_preferences": _PREFERENCES,
    "/cloud/telegram_detect_chat_id": _PREFERENCES,
    "/cloud/telegram_send_test": _PREFERENCES,
    "/cloud/ping": _FORMS,
    "/cloud/get_langs": _FORMS,
    "/cloud/get_users": _FORMS,
    "/cloud/host_defaults": _FORMS,
    "/cloud/fetch_odoojs_submodules": _FORMS,
    "/cloud/github/setup": _GITHUB_APP,
    "/cloud/github/callback": _GITHUB_APP,
    "/cloud/github/webhook": _PUBLIC,
    "/cloud/health": _PUBLIC,
    "/cloud/mcp/approve/<string:token>": (
        "A page for a person: where agents' actions are approved."
    ),
    "/cloud/mcp/download/<string:token>": "The link download_backup hands out.",
    "/cloud/mcp/config": "What the Connect an agent page shows.",
}


def tool_policies(mapping):
    """Return ``{route: ("tool", name)}`` for a ``{route: name}`` mapping."""
    return {route: ("tool", name) for route, name in mapping.items()}


class McpCatalog(models.AbstractModel):
    """The tools agents may call, and the routes no tool calls."""

    _name = "incubacloud.mcp.catalog"
    _description = "Tools agents may call, and the routes they may not"

    @api.model
    def _tool_specs(self):
        """Return every :class:`ToolSpec` of the panel (modules extend it)."""
        return []

    @api.model
    def _route_policies(self):
        """Return ``{route rule: ("tool", name) | ("excluded", reason)}``."""
        return {route: ("excluded", reason) for route, reason in CORE_EXCLUDED.items()}

    @api.model
    def _spec(self, name):
        """Return the spec of tool *name*, or None."""
        return next((spec for spec in self._tool_specs() if spec.name == name), None)
