"""Continuous recovery through an agent: its state, its switch, its restore."""

from odoo import api, fields, models
from odoo.exceptions import UserError

from odoo.addons.incubacloud_mcp.models.mcp_catalog import tool_policies
from odoo.addons.incubacloud_mcp.models.mcp_tools import quoted
from odoo.addons.incubacloud_mcp.protocol.tool_spec import (
    ToolSpec,
    obj,
    record_id,
    string,
)
from odoo.addons.incubacloud_pitr.models._constants import OBJECTIVES

_MANAGER = "group_cloud_manager"

TOOLS = (
    ToolSpec(
        "get_pitr",
        "Continuous recovery of a production instance: its state, its objective "
        "and the window it can be restored within (from and to, UTC).",
        "group_cloud_user",
        obj({"instance_id": record_id("The instance.")}, required=("instance_id",)),
        route="/cloud/pitr/status",
        read_only=True,
    ),
    ToolSpec(
        "configure_pitr",
        "Switch continuous recovery on or off for a production instance, or "
        "change its objective (the most it may lose, in minutes). Switching "
        "rebuilds the instance.",
        _MANAGER,
        obj(
            {
                "instance_id": record_id("The instance."),
                "action": string(
                    "What to do.", enum=("enable", "disable", "set_objective")
                ),
                "objective": string(
                    "Minutes; with enable it is optional.",
                    enum=[key for key, _label in OBJECTIVES],
                ),
            },
            required=("instance_id", "action"),
        ),
        handler="_tool_configure_pitr",
    ),
    ToolSpec(
        "pitr_restore",
        "Restore a production instance to a moment of its window (get_pitr "
        "gives it), in UTC. What happened after that moment is undone.",
        _MANAGER,
        obj(
            {
                "instance_id": record_id("The instance."),
                "target": string(
                    "The moment, YYYY-MM-DD HH:MM[:SS] in UTC, within get_pitr's "
                    "window.",
                    max_length=64,
                ),
            },
            required=("instance_id", "target"),
        ),
        handler="_tool_pitr_restore",
        destructive=True,
        confirm="always",
    ),
)

ROUTES = {
    "/cloud/pitr/status": "get_pitr",
    "/cloud/pitr/enable": "configure_pitr",
    "/cloud/pitr/disable": "configure_pitr",
    "/cloud/pitr/set_objective": "configure_pitr",
    "/cloud/pitr/restore": "pitr_restore",
}


class McpCatalogPitr(models.AbstractModel):
    """The continuous recovery tools, and the routes they call."""

    _inherit = "incubacloud.mcp.catalog"

    @api.model
    def _tool_specs(self):
        """Add the continuous recovery tools."""
        return super()._tool_specs() + list(TOOLS)

    @api.model
    def _route_policies(self):
        """Classify the continuous recovery routes."""
        return {**super()._route_policies(), **tool_policies(ROUTES)}


class McpToolsPitr(models.AbstractModel):
    """Switching continuous recovery, and what a restore asks the user."""

    _inherit = "incubacloud.mcp.tools"

    @api.model
    def _tool_configure_pitr(self, args):
        """Switch it on or off, or change the objective; say how the job stands.

        :raises UserError: for ``set_objective`` without an objective
        """
        action, instance_id = args["action"], args["instance_id"]
        if action == "disable":
            result = self._route("/cloud/pitr/disable", instance_id=instance_id)
        elif action == "enable":
            result = self._route(
                "/cloud/pitr/enable",
                instance_id=instance_id,
                objective=args.get("objective"),
            )
        else:
            if not args.get("objective"):
                raise UserError(self.env._("Give the objective."))
            result = self._route(
                "/cloud/pitr/set_objective",
                instance_id=instance_id,
                objective=args["objective"],
            )
        return self._with_job_state(result)

    @api.model
    def _confirm_pitr_restore(self, args):
        """Name the instance and the moment, as the panel reads it.

        The restore's own checks run first — a manager, a stack the panel
        runs, a moment of the instance's window, no continuous recovery job
        queued or running — so a restore that would be refused is refused
        before anyone is asked.

        :raises AccessError: below the manager role, or for an instance
            the user may not read
        :raises UserError: for a missing instance, a stack configured
            outside the panel, a moment that is not one or is outside the
            window, or a continuous recovery job already queued or running
        """
        self.env["cloud.security.mixin"]._check_cloud_group(_MANAGER)
        instance = self._instance(args["instance_id"])
        if instance.pitr_stack_external:
            raise UserError(
                self.env._(
                    "This instance's stack is configured outside the panel: "
                    "restore it with its runbook."
                )
            )
        when = instance._pitr_parse_target(args["target"])
        window = instance._pitr_window()
        if not window or not window[0] <= when <= window[1]:
            raise UserError(
                self.env._(
                    "%(when)s is outside the window this instance can be restored to.",
                    when=fields.Datetime.to_string(when),
                )
            )
        instance._pitr_refuse_busy()
        return self.env._(
            "Restore the production instance %(name)s to %(target)s (UTC). "
            "Whatever happened after that moment is undone.",
            name=quoted(instance.name),
            target=quoted(fields.Datetime.to_string(when)),
        )

    @api.model
    def _tool_pitr_restore(self, args):
        """Restore; the approval stands for typing the instance's name."""
        instance = self._instance(args["instance_id"])
        return self._with_job_state(
            self._route(
                "/cloud/pitr/restore",
                instance_id=instance.id,
                target=args["target"],
                confirm_name=instance.name,
            )
        )
