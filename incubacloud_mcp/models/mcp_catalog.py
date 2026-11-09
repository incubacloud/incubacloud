"""The registry: every tool, and what becomes of every action of the panel.

Each module adds its own: this one core's, the PITR bridge PITR's, the
tenant panel its routes. ``_route_policies`` maps each rule of the routing
map (its string, as ``ir.http.routing_map()`` holds it) to
``("tool", <name>)`` or ``("excluded", <reason>)``; the inventory test
fails on a ``/cloud/*`` route nobody classified.

``_call_policies`` does the same for what the panel's screens ask of the
models directly: ``orm:<model>.<method>`` for an ``orm`` call, and
``job:<code>`` for a job they queue with ``cloud.job.enqueue``. The
inventory test reads the panel's bundle for those calls, and fails on one
nobody classified. ``_dynamic_calls`` and ``_call_wrappers`` declare the
calls whose route is not a literal the test can read.
"""

from odoo import api, models

_EXCLUDED_FOR_THE_BROWSER = "A page or a file for a browser; {tool} replaces it."
_SPA = "The panel's single-page application."
_UPLOAD = (
    "The browser's upload belongs in the panel; restore_from_link, "
    "restore_from_host_file and restore_from_upload replace it."
)
_TERMINAL = "Interactive terminal (a PTY); run_command replaces it."
_HOST_TERMINAL = "Interactive host console; run_host_command replaces it."
_FORMS = "Serves the panel's own forms only."
_GITHUB_APP = "Installs the GitHub App in a browser."
_PUBLIC = "Public endpoint, not an action of the user."
_GIT_PROVIDERS = (
    "Git providers and their tokens are managed in the panel (decision 6); a "
    "GitLab repository is read through its lines (repo_read, github_repo_info)."
)
_GIT_HOOKS = (
    "GitLab webhooks and their secrets are managed in the panel (decision 6), "
    "as their Git providers are."
)

#: Core routes no tool calls, each with why.
CORE_EXCLUDED = {
    "/cloud": _SPA,
    "/cloud/<path:subpath>": _SPA,
    "/cloud/get_secret": "Secrets never leave through an agent (decision 6).",
    "/cloud/save_github_pat": "Credentials are only written in the panel (decision 6).",
    "/cloud/get_git_providers": _GIT_PROVIDERS,
    "/cloud/save_git_provider": _GIT_PROVIDERS,
    "/cloud/delete_git_provider": _GIT_PROVIDERS,
    "/cloud/test_git_provider": _GIT_PROVIDERS,
    "/cloud/get_git_hooks": _GIT_HOOKS,
    "/cloud/register_git_hook": _GIT_HOOKS,
    "/cloud/verify_git_hook": _GIT_HOOKS,
    "/cloud/reveal_git_hook_secret": _GIT_HOOKS,
    "/cloud/delete_git_hook": _GIT_HOOKS,
    "/cloud/reprocess_git_event": _GIT_HOOKS,
    "/cloud/generate_host_key": (
        "The new server form's SSH key; credentials are only written in the "
        "panel (decision 6)."
    ),
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
    "/cloud/ping": _FORMS,
    "/cloud/get_nav_counts": (
        "The sidebar's count badges; the list tools count the same records."
    ),
    "/cloud/host_defaults": (
        "The defaults of the new server form; manage_host applies the same ones."
    ),
    "/cloud/fetch_odoojs_submodules": (
        "No screen calls it; import_project reads the submodules itself."
    ),
    "/cloud/github/setup": _GITHUB_APP,
    "/cloud/github/callback": _GITHUB_APP,
    "/cloud/github/webhook": _PUBLIC,
    "/cloud/gitlab/webhook/<string:hook_key>": _PUBLIC,
    "/cloud/health": _PUBLIC,
    "/cloud/mcp/approve/<string:token>": (
        "A page for a person: where agents' actions are approved."
    ),
    "/cloud/mcp/download/<string:token>": "The link download_backup hands out.",
    "/cloud/mcp/config": "What the Connect an agent page shows.",
}


#: What core's screens queue or call on the models, and the tool that does it.
CORE_CALLS = {
    "orm:cloud.job.load_jobs": "get_job",
    "orm:cloud.job.get_instance_jobs": "list_jobs",
    "orm:cloud.job.get_host_jobs": "list_jobs",
    "orm:cloud.job.load_history": "list_jobs",
    "orm:cloud.job.cancel_job": "cancel_job",
    "orm:cloud.job.retry_job": "retry_job",
    "orm:cloud.instance.grant_restore_upload": "grant_restore_upload",
    "orm:cloud.instance.verify_restore_upload": "check_restore_upload",
    "orm:cloud.restore.upload.grant.read": "check_restore_upload",
    "orm:cloud.instance.restore_db": "restore_from_upload",
    "orm:cloud.instance.restore_from_url": "restore_from_link",
    "job:start_instance": "run_instance_action",
    "job:stop_instance": "run_instance_action",
    "job:restart_instance": "run_instance_action",
    "job:export_instance": "export_instance",
    "job:delete_instance": "delete_instance",
    "job:host_probe": "run_host_action",
    "job:full_setup": "run_host_action",
    "job:push_trusted_proxies": "run_host_action",
    "job:sync_direct_path": "run_host_action",
}
#: Core's calls whose route is a variable: per file of the bundle, the
#: expression the call passes and the paths it reaches.
CORE_DYNAMIC_CALLS = {
    # The actions the pages offer from data (``show_as_action``): every job
    # type offered so has a policy of its own.
    "/incubacloud/static/src/components/host_detail/host_detail.js": {
        "action.code": frozenset(),
    },
    "/incubacloud/static/src/components/instance_detail/instance_detail.js": {
        "action.code": frozenset(),
    },
    "/incubacloud/static/src/components/instance_detail/instance_detail_restore.js": {
        "`/cloud/instance/${this.props.instance_id}/restore/${path}`": frozenset(
            {
                "/cloud/instance/1/restore/begin",
                "/cloud/instance/1/restore/part",
                "/cloud/instance/1/restore/finish",
            }
        ),
    },
}


def tool_policies(mapping):
    """Return ``{key: ("tool", name)}`` for a ``{route or call key: name}`` mapping."""
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
    def _call_policies(self):
        """Return ``{orm:<model>.<method> | job:<code>: ("tool", name) |
        ("excluded", reason)}``: what the screens ask of the models."""
        return tool_policies(CORE_CALLS)

    @api.model
    def _dynamic_calls(self):
        """Return ``{bundle file: {expression: routes}}``: calls of a variable route.

        The inventory test refuses a call whose route it cannot read
        unless its file declares it here, checks that each route declared
        is still written in that file and that each has a policy.
        """
        return CORE_DYNAMIC_CALLS.copy()

    @api.model
    def _call_wrappers(self):
        """Return ``{function: route prefix}``: helpers that call ``rpc`` for a
        route named by their first argument (a module's, such as the
        tenant's ``_bff``)."""
        return {}

    @api.model
    def _spec(self, name):
        """Return the spec of tool *name*, or None."""
        return next((spec for spec in self._tool_specs() if spec.name == name), None)
