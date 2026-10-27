"""Server and monitoring tools: a manager's. Credentials stay in the panel."""

from odoo import api, models
from odoo.exceptions import UserError

from ..protocol.tool_spec import ToolSpec, boolean, obj, record_id, string
from .mcp_catalog import tool_policies

_MANAGER = "group_cloud_manager"

_HOST = record_id("The server.")
_HOST_VALS = {
    "type": "object",
    "additionalProperties": True,
    "description": "Server fields as the panel's form sends them. Passwords and keys "
    "are refused: the user sets the server's SSH credentials in the panel.",
}

TOOLS = (
    ToolSpec(
        "manage_host",
        "Create a server (vals), change one (host_id and vals), or set up its egress "
        "whitelist (host_id and setup_whitelist=true).",
        _MANAGER,
        obj(
            {
                "host_id": _HOST,
                "vals": _HOST_VALS,
                "setup_whitelist": boolean("Set up the server's whitelist."),
            }
        ),
        handler="_tool_manage_host",
    ),
    ToolSpec(
        "harden_host",
        "Check what hardening would change on a server, or apply it (apply=true), "
        "optionally followed by its full setup.",
        _MANAGER,
        obj(
            {
                "host_id": _HOST,
                "apply": boolean("Apply instead of checking."),
                "chain_full_setup": boolean("Run the full setup after it."),
            },
            required=("host_id",),
        ),
        handler="_tool_harden_host",
    ),
    ToolSpec(
        "browse_host_dir",
        "List a directory of a server, to find an instance to import.",
        _MANAGER,
        obj(
            {
                "host_id": _HOST,
                "path": string("The directory; ~ by default.", max_length=500),
            },
            required=("host_id",),
        ),
        route="/cloud/browse_host_dir",
        read_only=True,
        untrusted=True,
    ),
    ToolSpec(
        "import_host_instance",
        "Import an instance that already runs on a server.",
        _MANAGER,
        obj(
            {"host_id": _HOST, "path": string("Its directory.", max_length=500)},
            required=("host_id", "path"),
        ),
        route="/cloud/import_host_instance",
    ),
    ToolSpec(
        "deploy_monitoring",
        "Deploy the central monitoring stack on a server.",
        _MANAGER,
        obj({"host_id": _HOST}, required=("host_id",)),
        route="/cloud/monitoring/deploy_central",
        enqueues=True,
    ),
)

ROUTES = {
    "/cloud/create_host": "manage_host",
    "/cloud/save_host": "manage_host",
    "/cloud/setup_whitelist": "manage_host",
    "/cloud/preflight_host_hardening": "harden_host",
    "/cloud/apply_host_hardening": "harden_host",
    "/cloud/browse_host_dir": "browse_host_dir",
    "/cloud/import_host_instance": "import_host_instance",
    "/cloud/monitoring/deploy_central": "deploy_monitoring",
}


class McpCatalogManager(models.AbstractModel):
    """The server and monitoring tools, and the routes they call."""

    _inherit = "incubacloud.mcp.catalog"

    @api.model
    def _tool_specs(self):
        """Add the server and monitoring tools."""
        return super()._tool_specs() + list(TOOLS)

    @api.model
    def _route_policies(self):
        """Classify the server and monitoring routes."""
        return {**super()._route_policies(), **tool_policies(ROUTES)}


class McpToolsManager(models.AbstractModel):
    """Handlers of the server tools that choose between routes."""

    _inherit = "incubacloud.mcp.tools"

    @api.model
    def _tool_manage_host(self, args):
        """Create, change or set up the whitelist of a server."""
        host_id = args.get("host_id")
        if args.get("setup_whitelist"):
            if not host_id:
                raise UserError(self.env._("Give the server (host_id)."))
            return self._with_job_state(
                self._route("/cloud/setup_whitelist", host_id=host_id)
            )
        if not args.get("vals"):
            raise UserError(self.env._("Give the server's fields (vals)."))
        if host_id:
            return self._route("/cloud/save_host", host_id=host_id, vals=args["vals"])
        return self._route("/cloud/create_host", vals=args["vals"])

    @api.model
    def _tool_harden_host(self, args):
        """Preflight or apply the hardening of a server."""
        if not args.get("apply"):
            return self._route(
                "/cloud/preflight_host_hardening", host_id=args["host_id"]
            )
        return self._with_job_state(
            self._route(
                "/cloud/apply_host_hardening",
                host_id=args["host_id"],
                chain_full_setup=bool(args.get("chain_full_setup")),
            )
        )
