"""Destructive tools: each says exactly what it will do and waits for approval.

The confirmation method runs the route's own role check first, so a call
that would be refused is refused before anyone is asked.
"""

from odoo import api, models
from odoo.exceptions import UserError

from ..protocol.tool_spec import ToolSpec, boolean, obj, record_id, string
from .mcp_catalog import tool_policies

_CONSULTANT = "group_cloud_consultant"
_DEVELOPER = "group_cloud_developer"
_MANAGER = "group_cloud_manager"

_INSTANCE = record_id("The instance.")


def _destructive(name, description, min_group, schema, **kwargs):
    """Return the spec of a destructive tool, which always asks."""
    return ToolSpec(
        name,
        description,
        min_group,
        schema,
        destructive=True,
        confirm="always",
        **kwargs,
    )


TOOLS = (
    _destructive(
        "delete_project",
        "Delete a project that has no instances left.",
        _MANAGER,
        obj({"project_id": record_id("The project.")}, required=("project_id",)),
        route="/cloud/delete_project",
    ),
    _destructive(
        "delete_instance",
        "Delete an instance with its stack and data. A production one needs a manager.",
        _CONSULTANT,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        route="/cloud/delete_instance",
    ),
    _destructive(
        "delete_archived_instance",
        "Delete an archived instance and the backup copy it still keeps.",
        _MANAGER,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        handler="_tool_delete_archived_instance",
    ),
    _destructive(
        "delete_host",
        "Remove a server from the panel; its panel services are taken off it.",
        _MANAGER,
        obj({"host_id": record_id("The server.")}, required=("host_id",)),
        route="/cloud/delete_host",
        enqueues=True,
    ),
    _destructive(
        "delete_backup_backend",
        "Delete a backup destination nothing uses any more.",
        _MANAGER,
        obj(
            {"backend_id": record_id("The backup destination.")},
            required=("backend_id",),
        ),
        route="/cloud/delete_backup_backend",
    ),
    _destructive(
        "restore_backup",
        "Restore a production instance from one of its backups (list_backups "
        "gives the times).",
        _DEVELOPER,
        obj(
            {
                "instance_id": _INSTANCE,
                "time": string("The backup's time.", max_length=64),
            },
            required=("instance_id", "time"),
        ),
        route="/cloud/restore_backup",
        enqueues=True,
    ),
    _destructive(
        "refresh_staging",
        "Replace a staging's data with a copy of production: its last backup "
        "(source=backup, the default) or a dump taken now (live); neutralized "
        "unless neutralize=false.",
        _DEVELOPER,
        obj(
            {
                "instance_id": record_id("The staging."),
                "source_instance_id": record_id("The production instance."),
                "source": string("Where the copy comes from.", enum=("backup", "live")),
                "neutralize": boolean("Neutralize the copy (true by default)."),
            },
            required=("instance_id", "source_instance_id"),
        ),
        route="/cloud/refresh_from_production",
        enqueues=True,
    ),
    _destructive(
        "move_instance",
        "Move an instance to another server.",
        _MANAGER,
        obj(
            {"instance_id": _INSTANCE, "target_host_id": record_id("The new server.")},
            required=("instance_id", "target_host_id"),
        ),
        route="/cloud/move_instance",
        enqueues=True,
    ),
    _destructive(
        "rollback_move",
        "Undo a move that failed before its cutover.",
        _MANAGER,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        route="/cloud/rollback_move",
        enqueues=True,
    ),
    _destructive(
        "reset_github_app",
        "Delete the panel's GitHub App credentials.",
        _MANAGER,
        obj(),
        route="/cloud/reset_github_app",
    ),
)

ROUTES = {
    "/cloud/delete_project": "delete_project",
    "/cloud/delete_instance": "delete_instance",
    "/cloud/delete_archived_instance": "delete_archived_instance",
    "/cloud/delete_host": "delete_host",
    "/cloud/delete_backup_backend": "delete_backup_backend",
    "/cloud/restore_backup": "restore_backup",
    "/cloud/refresh_from_production": "refresh_staging",
    "/cloud/move_instance": "move_instance",
    "/cloud/rollback_move": "rollback_move",
    "/cloud/reset_github_app": "reset_github_app",
}


class McpCatalogDestructive(models.AbstractModel):
    """The destructive tools, and the routes they call."""

    _inherit = "incubacloud.mcp.catalog"

    @api.model
    def _tool_specs(self):
        """Add the destructive tools."""
        return super()._tool_specs() + list(TOOLS)

    @api.model
    def _route_policies(self):
        """Classify the destructive routes."""
        return {**super()._route_policies(), **tool_policies(ROUTES)}


class McpToolsDestructive(models.AbstractModel):
    """What each destructive tool asks the user, and the one with a handler."""

    _inherit = "incubacloud.mcp.tools"

    @api.model
    def _record(self, model, record_id, missing):
        """Return the record as the user may read it, archived included.

        :param str model: the model's name
        :param int record_id: the record's id
        :param str missing: what to say when it does not exist
        :raises UserError: *missing*, when it does not exist
        :raises AccessError: when the user may not read it
        """
        record = (
            self.env[model].with_context(active_test=False).browse(record_id).exists()
        )
        if not record:
            raise UserError(missing)
        record.check_access("read")
        return record

    @api.model
    def _instance(self, instance_id):
        """Return the instance *instance_id* the user may read."""
        return self._record(
            "cloud.instance", instance_id, self.env._("Instance not found")
        )

    @api.model
    def _tool_delete_archived_instance(self, args):
        """Delete an archived instance; the approval stands for typing its name."""
        instance = self._instance(args["instance_id"])
        return self._route(
            "/cloud/delete_archived_instance",
            instance_id=instance.id,
            confirm_name=instance.name,
        )

    @api.model
    def _confirm_delete_project(self, args):
        """Name the project."""
        self.env["cloud.security.mixin"]._check_can_delete_project()
        project = self._record(
            "cloud.project", args["project_id"], self.env._("Project not found")
        )
        return self.env._(
            'Delete the project "%(name)s". This cannot be undone.', name=project.name
        )

    @api.model
    def _confirm_delete_instance(self, args):
        """Name the instance and say whether it is production."""
        sec = self.env["cloud.security.mixin"]
        sec._check_cloud_group(_CONSULTANT)
        instance = self._instance(args["instance_id"])
        sec._check_can_delete_instance(instance)
        if instance.environment == "production":
            return self.env._(
                'Delete the production instance "%(name)s" (project %(project)s). '
                "This cannot be undone.",
                name=instance.name,
                project=instance.project_id.name,
            )
        return self.env._(
            'Delete the instance "%(name)s" (project %(project)s). '
            "This cannot be undone.",
            name=instance.name,
            project=instance.project_id.name,
        )

    @api.model
    def _confirm_delete_archived_instance(self, args):
        """Name the archived instance and its backup copy."""
        self.env["cloud.security.mixin"]._check_cloud_group(_MANAGER)
        instance = self._instance(args["instance_id"])
        return self.env._(
            'Delete the archived instance "%(name)s" and the backup copy it still '
            "keeps. This cannot be undone.",
            name=instance.name,
        )

    @api.model
    def _confirm_delete_host(self, args):
        """Name the server and its address."""
        self.env["cloud.security.mixin"]._check_can_manage_hosts()
        host = self._record("cloud.host", args["host_id"], self.env._("Host not found"))
        return self.env._(
            'Remove the server "%(name)s" (%(ip)s) from the panel and take the '
            "panel's services off it. This cannot be undone.",
            name=host.name,
            ip=host.ip_address,
        )

    @api.model
    def _confirm_delete_backup_backend(self, args):
        """Name the backup destination."""
        self.env["cloud.security.mixin"]._check_can_manage_settings()
        backend = self._record(
            "cloud.backup.backend", args["backend_id"], self.env._("Backend not found")
        )
        return self.env._(
            'Delete the backup destination "%(name)s". This cannot be undone.',
            name=backend.name,
        )

    @api.model
    def _confirm_restore_backup(self, args):
        """Name the instance and the backup's time.

        The panel restores production instances only: any other is refused
        here, before anyone is asked.
        """
        self.env["cloud.security.mixin"]._check_can_manage_backups()
        instance = self._instance(args["instance_id"])
        if instance.environment != "production":
            raise UserError(self.env._("Backup restore only supported for production."))
        return self.env._(
            'Restore the production instance "%(name)s" from its backup of '
            "%(time)s. What changed after it is lost.",
            name=instance.name,
            time=args["time"],
        )

    @api.model
    def _confirm_refresh_staging(self, args):
        """Name the staging, its source and how the copy is taken."""
        self.env["cloud.security.mixin"]._check_can_clone_to_staging()
        staging = self._instance(args["instance_id"])
        source = self._instance(args["source_instance_id"])
        if args.get("source") == "live":
            how = self.env._("a dump taken now")
        else:
            how = self.env._("its last backup")
        if args.get("neutralize", True):
            neutralized = self.env._("neutralized")
        else:
            neutralized = self.env._("not neutralized")
        return self.env._(
            'Replace the staging "%(staging)s" with a copy of "%(origin)s" '
            "(%(how)s, %(neutralized)s). Everything in the staging is lost.",
            staging=staging.name,
            origin=source.name,
            how=how,
            neutralized=neutralized,
        )

    @api.model
    def _confirm_move_instance(self, args):
        """Name the instance and where it goes."""
        self.env["cloud.security.mixin"]._check_can_manage_hosts()
        instance = self._instance(args["instance_id"])
        target = self._record(
            "cloud.host", args["target_host_id"], self.env._("Host not found")
        )
        return self.env._(
            'Move the instance "%(name)s" to the server "%(host)s". It is '
            "unavailable while it moves.",
            name=instance.name,
            host=target.name,
        )

    @api.model
    def _confirm_rollback_move(self, args):
        """Name the instance."""
        self.env["cloud.security.mixin"]._check_can_manage_hosts()
        instance = self._instance(args["instance_id"])
        return self.env._(
            'Undo the move of the instance "%(name)s" and bring it back to its '
            "previous server.",
            name=instance.name,
        )

    @api.model
    def _confirm_reset_github_app(self, args):
        """Say what resetting the App takes away."""
        self.env["cloud.security.mixin"]._check_can_manage_settings()
        return self.env._(
            "Delete the panel's GitHub App credentials: every repository has to "
            "be connected again."
        )
