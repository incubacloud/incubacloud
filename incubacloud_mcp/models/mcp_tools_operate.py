"""Tools that operate the panel: create, change, deploy, back up, resolve."""

from odoo import api, models
from odoo.exceptions import UserError

from ..protocol.tool_spec import ToolSpec, boolean, integer, obj, record_id, string
from .mcp_catalog import tool_policies

_CONSULTANT = "group_cloud_consultant"
_PROJECT_MANAGER = "group_cloud_project_manager"
_DEVELOPER = "group_cloud_developer"

_INSTANCE = record_id("The instance.")
_VALS = {
    "type": "object",
    "additionalProperties": True,
    "description": "Fields to set, as the panel's form sends them. Secrets are "
    "refused: the user sets them in the panel.",
}
_REPO_MODELS = ("cloud.project.repo", "cloud.instance.repo")

TOOLS = (
    ToolSpec(
        "create_project",
        "Create a project.",
        _PROJECT_MANAGER,
        obj({"vals": _VALS}, required=("vals",)),
        route="/cloud/create_project",
    ),
    ToolSpec(
        "update_project",
        "Change a project's settings.",
        _CONSULTANT,
        obj(
            {"project_id": record_id("The project."), "vals": _VALS},
            required=("project_id", "vals"),
        ),
        route="/cloud/save_project",
    ),
    ToolSpec(
        "import_project",
        "Create a project and its production instance from a GitHub repository.",
        _CONSULTANT,
        obj(
            {
                "url": string("The repository's https URL.", max_length=500),
                "branch": string("The branch.", max_length=200),
            },
            required=("url",),
        ),
        route="/cloud/import_project",
    ),
    ToolSpec(
        "create_instance",
        "Create an instance in a project.",
        _CONSULTANT,
        obj({"vals": _VALS}, required=("vals",)),
        route="/cloud/create_instance",
    ),
    ToolSpec(
        "update_instance",
        "Change an instance's settings.",
        _CONSULTANT,
        obj(
            {"instance_id": _INSTANCE, "vals": _VALS}, required=("instance_id", "vals")
        ),
        route="/cloud/save_instance",
    ),
    ToolSpec(
        "keep_staging",
        "Keep a staging from its automatic purge.",
        _CONSULTANT,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        route="/cloud/keep_instance",
    ),
    ToolSpec(
        "revive_instance",
        "Bring an archived instance back, on its server or another.",
        _DEVELOPER,
        obj(
            {"instance_id": _INSTANCE, "host_id": record_id("Another server.")},
            required=("instance_id",),
        ),
        route="/cloud/revive_instance",
        enqueues=True,
    ),
    ToolSpec(
        "deploy_instance",
        "Deploy an instance. Answers the job_id; follow it with get_job.",
        _CONSULTANT,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        route="/cloud/deploy_instance",
        enqueues=True,
    ),
    ToolSpec(
        "rebuild_instance",
        "Rebuild an instance's image and restart it with a boot test.",
        _CONSULTANT,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        route="/cloud/rebuild_instance",
        enqueues=True,
    ),
    ToolSpec(
        "clone_to_staging",
        "Clone an instance into a new staging.",
        _DEVELOPER,
        obj(
            {
                "instance_id": _INSTANCE,
                "staging_name": string("The new staging's name.", max_length=63),
            },
            required=("instance_id", "staging_name"),
        ),
        route="/cloud/clone_to_staging",
        enqueues=True,
    ),
    ToolSpec(
        "create_backup",
        "Back up an instance now.",
        _DEVELOPER,
        obj(
            {
                "instance_id": _INSTANCE,
                "with_filestore": boolean("Include the filestore."),
            },
            required=("instance_id",),
        ),
        route="/cloud/create_backup",
        enqueues=True,
    ),
    ToolSpec(
        "resolve_conflict",
        "Resolve a dependency conflict alert: kind pip or addon, with the choices "
        "the alert's conflicts list.",
        _CONSULTANT,
        obj(
            {
                "alert_id": record_id("The conflict alert."),
                "kind": string("Which conflict.", enum=("pip", "addon")),
                "resolutions": {"description": "The choice for each conflict."},
            },
            required=("alert_id", "kind", "resolutions"),
        ),
        handler="_tool_resolve_conflict",
    ),
    ToolSpec(
        "apply_config",
        "Apply the project's configuration to an instance (see compare_config).",
        _CONSULTANT,
        obj(
            {
                "instance_id": _INSTANCE,
                "actions": {"description": "The actions compare_config proposed."},
            },
            required=("instance_id", "actions"),
        ),
        route="/cloud/apply_sync",
    ),
    ToolSpec(
        "dismiss_alerts",
        "Dismiss one alert (alert_id) or every alert the user sees (all=true).",
        _CONSULTANT,
        obj(
            {
                "alert_id": record_id("One alert."),
                "all": boolean("Every alert."),
                "level_filter": string(
                    "With all: only this level.", enum=("warning", "critical")
                ),
            }
        ),
        handler="_tool_dismiss_alerts",
    ),
    ToolSpec(
        "freeze_repos",
        "Freeze repositories at their branch's current commit, or unfreeze them.",
        _CONSULTANT,
        obj(
            {
                "model": string("Whose repositories.", enum=_REPO_MODELS),
                "repo_ids": {
                    "type": "array",
                    "description": "The repositories.",
                    "items": integer("A repository.", minimum=1),
                },
                "freeze": boolean("False unfreezes."),
            },
            required=("model", "repo_ids"),
        ),
        handler="_tool_freeze_repos",
    ),
    ToolSpec(
        "manage_tags",
        "List the tags of a scope, or create one.",
        _CONSULTANT,
        obj(
            {
                "action": string("What to do.", enum=("list", "create")),
                "scope": string("Whose tags.", enum=("project", "host", "instance")),
                "name": string("The new tag's name.", max_length=64),
            },
            required=("action",),
        ),
        handler="_tool_manage_tags",
    ),
    ToolSpec(
        "clear_staging_mailbox",
        "Empty the mail a staging caught.",
        _DEVELOPER,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        route="/cloud/instance_mailbox_clear",
    ),
)

ROUTES = {
    "/cloud/create_project": "create_project",
    "/cloud/save_project": "update_project",
    "/cloud/import_project": "import_project",
    "/cloud/create_instance": "create_instance",
    "/cloud/save_instance": "update_instance",
    "/cloud/keep_instance": "keep_staging",
    "/cloud/revive_instance": "revive_instance",
    "/cloud/deploy_instance": "deploy_instance",
    "/cloud/rebuild_instance": "rebuild_instance",
    "/cloud/clone_to_staging": "clone_to_staging",
    "/cloud/create_backup": "create_backup",
    "/cloud/resolve_pip_conflict": "resolve_conflict",
    "/cloud/resolve_addon_conflict": "resolve_conflict",
    "/cloud/apply_sync": "apply_config",
    "/cloud/dismiss_alert": "dismiss_alerts",
    "/cloud/dismiss_all_alerts": "dismiss_alerts",
    "/cloud/freeze_repo": "freeze_repos",
    "/cloud/unfreeze_repo": "freeze_repos",
    "/cloud/freeze_all_repos": "freeze_repos",
    "/cloud/unfreeze_all_repos": "freeze_repos",
    "/cloud/create_tag": "manage_tags",
    "/cloud/get_tags": "manage_tags",
    "/cloud/instance_mailbox_clear": "clear_staging_mailbox",
}


class McpCatalogOperate(models.AbstractModel):
    """The operating tools, and the routes they call."""

    _inherit = "incubacloud.mcp.catalog"

    @api.model
    def _tool_specs(self):
        """Add the operating tools."""
        return super()._tool_specs() + list(TOOLS)

    @api.model
    def _route_policies(self):
        """Classify the routes the operating tools call."""
        return {**super()._route_policies(), **tool_policies(ROUTES)}


class McpToolsOperate(models.AbstractModel):
    """Handlers of the operating tools that choose between routes."""

    _inherit = "incubacloud.mcp.tools"

    @api.model
    def _tool_resolve_conflict(self, args):
        """Resolve a pip or addon conflict; the job it held goes back to its queue."""
        route = (
            "/cloud/resolve_pip_conflict"
            if args["kind"] == "pip"
            else "/cloud/resolve_addon_conflict"
        )
        return self._with_job_state(
            self._route(
                route, alert_id=args["alert_id"], resolutions=args["resolutions"]
            )
        )

    @api.model
    def _tool_dismiss_alerts(self, args):
        """Dismiss one alert, or all the user sees."""
        if args.get("alert_id"):
            return self._route("/cloud/dismiss_alert", alert_id=args["alert_id"])
        if args.get("all"):
            return self._route(
                "/cloud/dismiss_all_alerts", level_filter=args.get("level_filter")
            )
        raise UserError(self.env._("Give alert_id, or all=true."))

    @api.model
    def _tool_freeze_repos(self, args):
        """Freeze or unfreeze one repository or several."""
        freeze = args.get("freeze", True)
        ids = args["repo_ids"]
        if not ids:
            raise UserError(self.env._("Give at least one repository."))
        if len(ids) == 1:
            route = "/cloud/freeze_repo" if freeze else "/cloud/unfreeze_repo"
            return self._route(route, repo_id=ids[0], model=args["model"])
        route = "/cloud/freeze_all_repos" if freeze else "/cloud/unfreeze_all_repos"
        return self._route(route, repo_ids=ids, model=args["model"])

    @api.model
    def _writes_manage_tags(self, args):
        """Listing the tags is a read: only a creation is audited."""
        return args["action"] != "list"

    @api.model
    def _tool_manage_tags(self, args):
        """List a scope's tags (``{tags: [...]}``, as the panel's), or create one."""
        scope = args.get("scope") or "project"
        if args["action"] == "list":
            return self._route("/cloud/get_tags", scope=scope)
        if not args.get("name"):
            raise UserError(self.env._("Give the new tag's name."))
        return self._route("/cloud/create_tag", name=args["name"], scope=scope)
