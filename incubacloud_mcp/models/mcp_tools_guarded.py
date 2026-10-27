"""Security controls meant for a person, offered with confirmation (D2).

Each message says exactly what changes: the key's old and new
fingerprints, the user an agent signs in as and whether the instance is
production, how much of the audit goes, which backup leaves.
``update_settings`` asks only for its security settings
(``mcp_tools_settings``); ``run_host_command`` is in ``mcp_tools_command``.
"""

from odoo import api, models
from odoo.exceptions import UserError

from ..protocol.tool_spec import ToolSpec, boolean, integer, obj, record_id, string
from .mcp_catalog import tool_policies

_USER = "group_cloud_user"
_DEVELOPER = "group_cloud_developer"
_MANAGER = "group_cloud_manager"
#: The jobs that prepare a backup for download.
_DOWNLOAD_JOBS = ("backup_download", "backup_download_neutralized")

TOOLS = (
    ToolSpec(
        "trust_host_key",
        "Trust the SSH key a server presents now. The user compares the old and "
        "the new fingerprint before approving; a server that changes its key in "
        "between is not trusted.",
        _MANAGER,
        obj({"host_id": record_id("The server.")}, required=("host_id",)),
        handler="_tool_trust_host_key",
        confirm="always",
    ),
    ToolSpec(
        "instance_login_link",
        "Without user_id, list an instance's users. With user_id, get a one-time "
        "link, valid 60 seconds, to sign in to the instance as that user.",
        _USER,
        obj(
            {
                "instance_id": record_id("The instance."),
                "user_id": integer("The instance's user (from the list).", minimum=1),
                "user_name": string(
                    "Their name, for the approval message.", max_length=200
                ),
            },
            required=("instance_id",),
        ),
        handler="_tool_instance_login_link",
        confirm="conditional",
    ),
    ToolSpec(
        "purge_audit_logs",
        "Delete audit log entries older than days (the retention setting by default).",
        _MANAGER,
        obj({"days": integer("Keep this many days.", minimum=1)}),
        route="/cloud/purge_audit_logs",
        destructive=True,
        confirm="always",
    ),
    ToolSpec(
        "download_backup",
        "Without job_id, prepare a download of a backup as a job (neutralized=true "
        "for a neutralized copy). With the job_id of a finished download, get a "
        "single-use link, valid 15 minutes, which the user approves.",
        _DEVELOPER,
        obj(
            {
                "instance_id": record_id("The instance."),
                "time": string(
                    "The backup's time; 'live' for a neutralized copy taken now.",
                    max_length=64,
                ),
                "download_type": string(
                    "What to download: the database (dump) or with its filestore "
                    "(all).",
                    enum=("dump", "all"),
                ),
                "neutralized": boolean("A neutralized copy."),
                "with_filestore": boolean(
                    "Include the filestore in a neutralized copy."
                ),
                "job_id": record_id("A finished download job."),
            }
        ),
        handler="_tool_download_backup",
        confirm="conditional",
    ),
)

ROUTES = {
    "/cloud/trust_host_key": "trust_host_key",
    "/cloud/get_instance_users": "instance_login_link",
    "/cloud/prepare_instance_connect": "instance_login_link",
    "/cloud/purge_audit_logs": "purge_audit_logs",
    "/cloud/download_backup": "download_backup",
    "/cloud/download_backup_neutralized": "download_backup",
}


class McpCatalogGuarded(models.AbstractModel):
    """The guarded tools, and the routes they call."""

    _inherit = "incubacloud.mcp.catalog"

    @api.model
    def _tool_specs(self):
        """Add the guarded tools."""
        return super()._tool_specs() + list(TOOLS)

    @api.model
    def _route_policies(self):
        """Classify the guarded routes."""
        return {**super()._route_policies(), **tool_policies(ROUTES)}


class McpToolsGuarded(models.AbstractModel):
    """What each guarded tool asks the user, and how it runs once approved."""

    _inherit = "incubacloud.mcp.tools"

    @api.model
    def _prepare_trust_host_key(self, args):
        """Read the key the server presents now, and bind the call to it.

        The fingerprint joins the arguments: the approval is for that key,
        and a retry that finds another key does not match it. Only the
        key exchange runs: no credential is sent.

        :raises UserError: when the server presents no key
        """
        self.env["cloud.security.mixin"]._check_can_manage_hosts()
        host = self._record("cloud.host", args["host_id"], self.env._("Host not found"))
        presented = host.sudo()._peek_known_host_key()["fingerprint"]
        if not presented:
            raise UserError(self.env._("The server presented no SSH key."))
        return {**args, "expected_fingerprint": presented}

    @api.model
    def _confirm_trust_host_key(self, args):
        """Show the key trusted until now and the one presented now."""
        host = self._record(
            "cloud.host", args["host_id"], self.env._("Host not found")
        ).sudo()
        old = (
            host.known_hosts_fingerprint
            or host.revoked_key_fingerprint
            or self.env._("none")
        )
        return self.env._(
            'Trust the SSH key the server "%(name)s" (%(ip)s) presents now: '
            "%(new)s. Key trusted until now: %(old)s. Compare them before "
            "approving.",
            name=host.name,
            ip=host.ip_address,
            new=args["expected_fingerprint"],
            old=old,
        )

    @api.model
    def _tool_trust_host_key(self, args):
        """Trust the key, only if the server still presents the approved one."""
        return self._route(
            "/cloud/trust_host_key",
            host_id=args["host_id"],
            expected_fingerprint=args["expected_fingerprint"],
        )

    @api.model
    def _confirm_instance_login_link(self, args):
        """Ask only for a link: name the user and say whether it is production.

        The id is what the link is for; the name is the agent's word, shown
        beside it.
        """
        if not args.get("user_id"):
            return None
        instance = self._instance(args["instance_id"])
        self.env["cloud.security.mixin"]._check_can_connect_as_user(instance=instance)
        user = f"id {args['user_id']}"
        if args.get("user_name"):
            user = f"{args['user_name']} ({user})"
        if instance.environment == "production":
            return self.env._(
                'Sign in to the production instance "%(name)s" as its user %(user)s.',
                name=instance.name,
                user=user,
            )
        return self.env._(
            'Sign in to the instance "%(name)s" as its user %(user)s.',
            name=instance.name,
            user=user,
        )

    @api.model
    def _tool_instance_login_link(self, args):
        """List the instance's users, or prepare the one-time link."""
        if not args.get("user_id"):
            return self._route(
                "/cloud/get_instance_users", instance_id=args["instance_id"]
            )
        return self._route(
            "/cloud/prepare_instance_connect",
            instance_id=args["instance_id"],
            user_id=args["user_id"],
            user_name=args.get("user_name"),
        )

    @api.model
    def _confirm_purge_audit_logs(self, args):
        """Say how much of the audit goes."""
        self.env["cloud.security.mixin"]._check_cloud_group(_MANAGER)
        days = args.get("days") or int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("incubacloud.audit_log_retention_days", "90")
            or 90
        )
        return self.env._(
            "Delete the audit log entries older than %(days)s days. This cannot "
            "be undone.",
            days=days,
        )

    @api.model
    def _download_ready(self, job_id):
        """Return ``(job, attachment)`` of a finished download the user may read.

        :raises UserError: when the job prepared no download, or has not
            finished it
        """
        self.env["cloud.security.mixin"]._check_can_manage_backups()
        job = self._record("cloud.job", job_id, self.env._("Job not found"))
        if job.job_type_id.code not in _DOWNLOAD_JOBS:
            raise UserError(self.env._("This job did not prepare a backup download."))
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .search(
                [("res_model", "=", "cloud.job"), ("res_id", "=", job.id)],
                order="id desc",
                limit=1,
            )
        )
        if job.state != "done" or not attachment:
            raise UserError(
                self.env._(
                    "The download is not ready yet. Ask again when its job is done."
                )
            )
        return job, attachment

    @api.model
    def _confirm_download_backup(self, args):
        """Ask only for the link: name the file and the instance."""
        if not args.get("job_id"):
            return None
        job, attachment = self._download_ready(args["job_id"])
        return self.env._(
            'Get a single-use link, valid 15 minutes, to download "%(file)s" of '
            'the instance "%(instance)s".',
            file=attachment.name,
            instance=job.instance_id.name or "-",
        )

    @api.model
    def _tool_download_backup(self, args):
        """Queue a download, or issue the link of a finished one."""
        if args.get("job_id"):
            job, attachment = self._download_ready(args["job_id"])
            return {
                "ok": True,
                "url": self.env["incubacloud.mcp.download"]._issue(job, attachment),
                "file_name": attachment.name,
                "expires_in_seconds": 900,
                "single_use": True,
            }
        if not args.get("instance_id"):
            raise UserError(
                self.env._("Give instance_id, or the job_id of a finished download.")
            )
        if args.get("neutralized"):
            result = self._route(
                "/cloud/download_backup_neutralized",
                instance_id=args["instance_id"],
                time=args.get("time") or "live",
                with_filestore=bool(args.get("with_filestore")),
            )
        else:
            if not args.get("time"):
                raise UserError(
                    self.env._("Give the backup's time (list_backups lists them).")
                )
            result = self._route(
                "/cloud/download_backup",
                instance_id=args["instance_id"],
                time=args["time"],
                download_type=args.get("download_type") or "dump",
            )
        result = self._with_job_state(result)
        if isinstance(result, dict) and result.get("job_id"):
            result["next_step"] = (
                "When the job is done, call download_backup with this job_id to "
                "get the download link."
            )
        return result
