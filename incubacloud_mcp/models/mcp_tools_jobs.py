"""What the panel's buttons queue or call on the models, as tools.

Start, Stop and Restart of an instance, its Export, the server's Check,
Full Setup and the actions its page offers, the jobs' lists, Cancel and
Retry, the clean-up of a move's frozen copy, the users' picker and the
user's own notification preferences. Each calls what the button calls,
as the user: ``cloud.job.enqueue`` and the job's own methods hold the role
of each job type (``_check_job_type_allowed``) and the record rules, as
for the panel. What may cut a service, end someone's work or remove a way
back asks the user first, and the confirmation method runs the same
checks before anyone is asked.
"""

from odoo import api, models
from odoo.exceptions import UserError

from ..protocol.limits import page_items
from ..protocol.tool_spec import (
    LIMIT,
    OFFSET,
    ToolSpec,
    array,
    boolean,
    obj,
    record_id,
    string,
)
from .mcp_catalog import tool_policies
from .mcp_tools import quoted

_USER = "group_cloud_user"
_CONSULTANT = "group_cloud_consultant"
_PROJECT_MANAGER = "group_cloud_project_manager"
_DEVELOPER = "group_cloud_developer"
_MANAGER = "group_cloud_manager"

_INSTANCE = record_id("The instance.")
_HOST = record_id("The server.")
_JOB = record_id("The job.")

#: The instance's power buttons, by the word an agent uses.
INSTANCE_POWER = {
    "start": "start_instance",
    "stop": "stop_instance",
    "restart": "restart_instance",
}
#: The power buttons that cut the service: on production they ask.
_CUTS_SERVICE = frozenset({"stop_instance", "restart_instance"})
#: The server's own buttons, by the word an agent uses.
HOST_BUTTONS = {"check": "host_probe", "full_setup": "full_setup"}
#: Job types whose retry repeats something that destroys, cuts a service
#: or rewrites a server: the retry asks the user, as the first run did.
RETRY_ASKS = frozenset(
    {
        "stop_instance",
        "restart_instance",
        "delete_instance",
        "delete_host",
        "backup_restore",
        "restore_instance",
        "move_cutover",
        "move_cleanup_source",
        "move_rollback_cleanup",
        "move_freeze_source",
        "purge_archived_backups",
        "full_setup",
        "host_hardening",
        "push_trusted_proxies",
        "sync_direct_path",
        "pitr_restore",
        "pitr_disable",
    }
)
#: What ``save_user_preferences`` takes besides its secrets, by the names
#: the tool gives them.
PREFERENCE_FIELDS = {
    "notification_level": "cloud_notification_level",
    "notification_mode": "cloud_notification_mode",
    "email_enabled": "cloud_email_enabled",
    "muted_project_ids": "cloud_muted_project_ids",
    "telegram_chat_id": "cloud_telegram_chat_id",
}

TOOLS = (
    ToolSpec(
        "run_instance_action",
        "Start, stop or restart an instance (action start, stop or restart), "
        "or run one of the actions get_instance lists in custom_actions (its "
        "code). Answers the job_id; follow it with get_job. Stopping or "
        "restarting production, or an action with a warning, asks the user.",
        _CONSULTANT,
        obj(
            {
                "instance_id": _INSTANCE,
                "action": string(
                    "start, stop, restart, or the code of one of the instance's "
                    "custom_actions.",
                    max_length=64,
                ),
            },
            required=("instance_id", "action"),
        ),
        handler="_tool_run_instance_action",
        confirm="conditional",
    ),
    ToolSpec(
        "export_instance",
        "Export an instance as the panel's Export does: a sanitized development "
        "copy (no database dump, no environment files, no keys) packed as a "
        "tar.gz on its job. Answers the job_id; when the job is done, "
        "download_backup with that job_id gives the download link.",
        _DEVELOPER,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        handler="_tool_export_instance",
    ),
    ToolSpec(
        "run_host_action",
        "Run a server's button: check (probe its state and resources), "
        "full_setup (reinstall its tooling and its proxy), or the code of one "
        "of the actions get_host lists in custom_actions (push_trusted_proxies, "
        "sync_direct_path…). Answers the job_id; follow it with get_job. All "
        "but check ask the user.",
        _MANAGER,
        obj(
            {
                "host_id": _HOST,
                "action": string(
                    "check, full_setup, or the code of one of the server's "
                    "custom_actions.",
                    max_length=64,
                ),
            },
            required=("host_id", "action"),
        ),
        handler="_tool_run_host_action",
        confirm="conditional",
    ),
    ToolSpec(
        "list_jobs",
        "The jobs the user can see, newest first, in pages: of one instance "
        "(instance_id), of one server (host_id) or of the whole panel, filtered "
        "by state, dates or category (operational by default; admin are the "
        "background ones). The panel reads at most the newest 200 that match.",
        _USER,
        obj(
            {
                "instance_id": _INSTANCE,
                "host_id": _HOST,
                "states": array(
                    "Only jobs in these states.",
                    string(
                        "A state.",
                        enum=(
                            "queued",
                            "pending",
                            "enqueued",
                            "wait_dependencies",
                            "started",
                            "done",
                            "failed",
                            "cancelled",
                        ),
                    ),
                ),
                "category": string(
                    "Which jobs.", enum=("operational", "admin", "all")
                ),
                "date_from": string("Created from this ISO date."),
                "date_to": string("Created up to this ISO date."),
                "include_pre_claim": boolean(
                    "With instance_id: also the jobs from before the instance "
                    "was handed over to this panel."
                ),
                "offset": OFFSET,
                "limit": LIMIT,
            }
        ),
        handler="_tool_list_jobs",
        read_only=True,
    ),
    ToolSpec(
        "cancel_job",
        "Cancel a job that waits or runs. A job of a chain ends with the rest "
        "of its chain; the approval lists every job it ends. A running "
        "restore or move stopped half-way may leave its instance to be "
        "repaired.",
        _CONSULTANT,
        obj({"job_id": _JOB}, required=("job_id",)),
        handler="_tool_cancel_job",
        confirm="always",
    ),
    ToolSpec(
        "retry_job",
        "Retry a failed job: a new job repeats it at the head of its queue. "
        "Answers the new job_id. Retrying what deletes, restores, moves, stops "
        "or rewrites a server asks the user.",
        _CONSULTANT,
        obj({"job_id": _JOB}, required=("job_id",)),
        handler="_tool_retry_job",
        confirm="conditional",
    ),
    ToolSpec(
        "clean_frozen_copy",
        "Remove the copy the last move of an instance left frozen on its old "
        "server. After it the move cannot be undone by going back to that "
        "copy: check the instance on its new server first.",
        _MANAGER,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        route="/cloud/clean_frozen_copy",
        destructive=True,
        confirm="always",
        enqueues=True,
    ),
    ToolSpec(
        "find_users",
        "Find the panel's internal users by name (20 at most), for member_ids "
        "of update_project.",
        _PROJECT_MANAGER,
        obj({"search": string("Part of the name.", max_length=200)}),
        route="/cloud/get_users",
        read_only=True,
    ),
    ToolSpec(
        "get_notification_preferences",
        "The signed-in user's notification preferences: level, delivery mode, "
        "email, muted projects and Telegram chat. The bot token and the webhook "
        "secret show only as *_configured flags.",
        _USER,
        obj(),
        route="/cloud/get_user_preferences",
        read_only=True,
    ),
    ToolSpec(
        "update_notification_preferences",
        "Change the signed-in user's notification preferences (only the values "
        "given change), or with action detect_telegram_chat read the chat of the "
        "last message sent to their Telegram bot, or with send_telegram_test "
        "send it a test message. The bot token and the webhook are set by the "
        "user in the panel.",
        _USER,
        obj(
            {
                "action": string(
                    "What to do; save by default.",
                    enum=("save", "detect_telegram_chat", "send_telegram_test"),
                ),
                "notification_level": string(
                    "Which job notifications: failures, important or all.",
                    max_length=32,
                ),
                "notification_mode": string(
                    "When they are sent.", enum=("immediate", "daily_digest")
                ),
                "email_enabled": boolean("Send them by email."),
                "muted_project_ids": array(
                    "The projects to mute, all of them.", record_id("A project.")
                ),
                "telegram_chat_id": string("The Telegram chat.", max_length=64),
            }
        ),
        handler="_tool_update_notification_preferences",
    ),
)

ROUTES = {
    "/cloud/clean_frozen_copy": "clean_frozen_copy",
    "/cloud/get_users": "find_users",
    "/cloud/get_user_preferences": "get_notification_preferences",
    "/cloud/save_user_preferences": "update_notification_preferences",
    "/cloud/telegram_detect_chat_id": "update_notification_preferences",
    "/cloud/telegram_send_test": "update_notification_preferences",
}


class McpCatalogJobs(models.AbstractModel):
    """The tools of the buttons that queue jobs, and the routes they call."""

    _inherit = "incubacloud.mcp.catalog"

    @api.model
    def _tool_specs(self):
        """Add the tools of the job buttons."""
        return super()._tool_specs() + list(TOOLS)

    @api.model
    def _route_policies(self):
        """Classify the routes these tools call."""
        return {**super()._route_policies(), **tool_policies(ROUTES)}


class McpToolsJobs(models.AbstractModel):
    """What the job buttons check, ask and run."""

    _inherit = "incubacloud.mcp.tools"

    # ── Instance and server buttons ───────────────────────────────────────

    @api.model
    def _action_type(self, code, apply_to):
        """Return the job type *code* that the pages offer as an action.

        :param str code: the type's code
        :param str apply_to: ``instance`` or ``host``
        :raises UserError: when no page offers it
        """
        job_type = self.env["cloud.job.type"].search(
            [
                ("code", "=", code),
                ("show_as_action", "=", True),
                ("apply_to", "=", apply_to),
            ],
            limit=1,
        )
        if not job_type:
            raise UserError(
                self.env._("No such action: %(action)s.", action=quoted(code))
            )
        return job_type

    @api.model
    def _prepare_run_instance_action(self, args):
        """Resolve the action to the job type the button queues, and check it.

        The instance must be one the user may read, on a server; the type,
        one the user may queue on it. The type's code joins the arguments:
        the approval is for it.

        :raises UserError: for an instance without a server, or an action
            the instance does not offer
        :raises AccessError: below the type's role, or an instance the
            user may not read
        """
        instance = self._instance(args["instance_id"])
        action = args["action"]
        code = INSTANCE_POWER.get(action) or self._action_type(action, "instance").code
        if not instance.host_id:
            raise UserError(self.env._("Instance has no host"))
        self.env["cloud.job"]._check_job_type_allowed(code, instance.id)
        return {**args, "code": code}

    @api.model
    def _confirm_run_instance_action(self, args):
        """Ask for a stop or a restart of production, and for an action whose
        type warns before it runs (``action_confirm``), with that warning."""
        instance = self._instance(args["instance_id"])
        code = args["code"]
        job_type = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        values = {
            "action": job_type.name or code,
            "name": quoted(instance.name),
            "project": quoted(instance.project_id.name or ""),
        }
        if job_type.action_confirm:
            return self.env._(
                "%(action)s on the instance %(name)s (project %(project)s). "
                "%(warning)s",
                warning=job_type.action_confirm,
                **values,
            )
        if code in _CUTS_SERVICE and instance.environment == "production":
            if code == "stop_instance":
                return self.env._(
                    "Stop the production instance %(name)s (project %(project)s). "
                    "It is unavailable until it is started again.",
                    **values,
                )
            return self.env._(
                "Restart the production instance %(name)s (project %(project)s). "
                "It is unavailable while it restarts.",
                **values,
            )
        return None

    @api.model
    def _tool_run_instance_action(self, args):
        """Queue the job the button queues, as the user."""
        instance = self._instance(args["instance_id"])
        job_id = self.env["cloud.job"].enqueue(
            instance.host_id.id, instance.id, args["code"]
        )
        return self._with_job_state({"ok": True, "job_id": job_id})

    @api.model
    def _tool_export_instance(self, args):
        """Queue the export the Export button queues, as the user."""
        instance = self._instance(args["instance_id"])
        if not instance.host_id:
            raise UserError(self.env._("Instance has no host"))
        job_id = self.env["cloud.job"].enqueue(
            instance.host_id.id, instance.id, "export_instance"
        )
        result = self._with_job_state({"ok": True, "job_id": job_id})
        result["next_step"] = (
            "When the job is done, call download_backup with this job_id to get "
            "the download link."
        )
        return result

    @api.model
    def _prepare_run_host_action(self, args):
        """Resolve the action to the job type the button queues, and check it.

        :raises UserError: for an action the server's page does not offer
        :raises AccessError: below manager, or the type's role
        """
        self.env["cloud.security.mixin"]._check_can_manage_hosts()
        host = self._record("cloud.host", args["host_id"], self.env._("Host not found"))
        action = args["action"]
        code = HOST_BUTTONS.get(action)
        if not code:
            code = self._action_type(action, "host").code
            if code not in host._offered_actions().mapped("code"):
                raise UserError(
                    self.env._(
                        "The server %(name)s does not offer %(action)s.",
                        name=quoted(host.name),
                        action=quoted(action),
                    )
                )
        self.env["cloud.job"]._check_job_type_allowed(code)
        return {**args, "code": code}

    @api.model
    def _confirm_run_host_action(self, args):
        """Ask for all but a check, with what the job does to the server."""
        code = args["code"]
        if code == "host_probe":
            return None
        host = self._record(
            "cloud.host", args["host_id"], self.env._("Host not found")
        ).sudo()
        values = {"name": quoted(host.name), "ip": host.ip_address}
        if code == "full_setup":
            return self.env._(
                "Run the full setup of the server %(name)s (%(ip)s): it checks the "
                "system, reinstalls the panel's tooling and redeploys its proxy. "
                "Every instance on it may be unreachable for a while.",
                **values,
            )
        job_type = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        message = self.env._(
            "Run %(action)s on the server %(name)s (%(ip)s).",
            action=quoted(job_type.name or code),
            **values,
        )
        detail = job_type.action_confirm or job_type.description
        return f"{message} {detail}" if detail else message

    @api.model
    def _tool_run_host_action(self, args):
        """Queue the job the button queues, as the user."""
        job_id = self.env["cloud.job"].enqueue(args["host_id"], False, args["code"])
        return self._with_job_state({"ok": True, "job_id": job_id})

    # ── Jobs ──────────────────────────────────────────────────────────────

    @api.model
    def _tool_list_jobs(self, args):
        """A page of the jobs the history reads, as the user may see them."""
        filters = {
            key: args[key]
            for key in (
                "instance_id",
                "host_id",
                "states",
                "date_from",
                "date_to",
                "include_pre_claim",
            )
            if args.get(key)
        }
        filters["job_category"] = args.get("category") or "operational"
        history = self.env["cloud.job"].load_history(filters)
        page = page_items(history["jobs"], args.get("offset"), args.get("limit"))
        if history.get("preClaim"):
            page["pre_claim"] = history["preClaim"]
        return page

    @api.model
    def _job(self, job_id):
        """Return the job *job_id* the user may read."""
        return self._record("cloud.job", job_id, self.env._("Job not found"))

    @api.model
    def _job_label(self, job):
        """Return how an approval names *job*: its number, type and target."""
        target = (
            self.env._("instance %(name)s", name=quoted(job.instance_id.name))
            if job.instance_id
            else self.env._("server %(name)s", name=quoted(job.host_id.name or ""))
        )
        return self.env._(
            "#%(id)s %(type)s (%(target)s)",
            id=job.id,
            type=job.job_type_id.name or job.name,
            target=target,
        )

    @api.model
    def _confirm_cancel_job(self, args):
        """Name the job and every other one cancelling it ends.

        Refused before anyone is asked when it is not active, or when the
        user may not end it or one of those (``_first_unstoppable``).
        """
        job = self._job(args["job_id"])
        Job = self.env["cloud.job"]
        if job.state not in Job._active_states and not job.blocked_alert_id:
            raise UserError(self.env._("Only active jobs can be cancelled."))
        ended = job._ended_with()
        stopped, reason = Job._first_unstoppable(job | ended)
        if stopped:
            raise UserError(reason)
        message = self.env._("Cancel the job %(job)s.", job=self._job_label(job))
        if ended:
            message += " " + self.env._(
                "It also ends: %(jobs)s.",
                jobs=", ".join(self._job_label(other.sudo()) for other in ended),
            )
        if job.state == "started":
            message += " " + self.env._(
                "It is running: what it has done so far stays as it is."
            )
        return message

    @api.model
    def _tool_cancel_job(self, args):
        """Cancel the job as the panel's Cancel does."""
        job = self._job(args["job_id"])
        job.cancel_job()
        return {"ok": True, "job_id": job.id, "state": job.state}

    @api.model
    def _retry_asks_types(self):
        """Return the job types whose retry asks the user (modules extend it)."""
        return set(RETRY_ASKS)

    @api.model
    def _confirm_retry_job(self, args):
        """Ask for the retry of what destroys, cuts a service or rewrites a server.

        Refused before anyone is asked when the job did not fail, waits for
        an alert, or is of a type the user may not queue.
        """
        job = self._job(args["job_id"])
        if job.blocked_alert_id:
            raise UserError(
                self.env._(
                    "This job waits for an alert and starts by itself once it is "
                    "resolved. Resolve it in Alerts."
                )
            )
        if job.state != "failed":
            raise UserError(self.env._("Only failed jobs can be retried."))
        self.env["cloud.job"]._check_job_type_allowed(
            job.job_type_id.code, job.instance_id.id or None
        )
        if job.job_type_id.code not in self._retry_asks_types():
            return None
        return self.env._(
            "Retry the failed job %(job)s: it does again what it was doing.",
            job=self._job_label(job),
        )

    @api.model
    def _tool_retry_job(self, args):
        """Retry the job as the panel's Retry does."""
        new_id = self._job(args["job_id"]).retry_job()
        return self._with_job_state({"ok": True, "job_id": new_id})

    # ── Moves ─────────────────────────────────────────────────────────────

    @api.model
    def _confirm_clean_frozen_copy(self, args):
        """Name the instance and the server whose frozen copy goes."""
        self.env["cloud.security.mixin"]._check_can_manage_hosts()
        instance = self._instance(args["instance_id"])
        old = instance.sudo().move_frozen_source_host_id
        if not old:
            raise UserError(self.env._("This instance has no frozen copy to clean."))
        return self.env._(
            "Remove the frozen copy of the instance %(name)s from its old server "
            "%(host)s. Its move can then no longer be undone by going back to "
            "that copy.",
            name=quoted(instance.name),
            host=quoted(old.name),
        )

    # ── Notification preferences ──────────────────────────────────────────

    @api.model
    def _writes_update_notification_preferences(self, args):
        """Detecting the Telegram chat writes nothing: only the rest is audited."""
        return args.get("action") != "detect_telegram_chat"

    @api.model
    def _tool_update_notification_preferences(self, args):
        """Save the preferences given, keeping the rest; or run a Telegram check."""
        action = args.get("action") or "save"
        if action == "detect_telegram_chat":
            return self._route("/cloud/telegram_detect_chat_id")
        if action == "send_telegram_test":
            return self._route("/cloud/telegram_send_test")
        current = self._route("/cloud/get_user_preferences")
        params = {
            "cloud_notification_level": current.get("cloud_notification_level"),
            "cloud_notification_mode": current.get("cloud_notification_mode"),
        }
        params.update(
            {
                field: args[name]
                for name, field in PREFERENCE_FIELDS.items()
                if name in args
            }
        )
        return self._route("/cloud/save_user_preferences", **params)
