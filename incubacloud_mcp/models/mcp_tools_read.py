"""Read-only tools: what the panel shows, as the user may see it."""

from odoo import api, models
from odoo.exceptions import UserError

from ..protocol.limits import (
    MAX_BYTES,
    cap_strings,
    page_answer,
    page_items,
    page_lines,
    size_of,
)
from ..protocol.log_view import clean_lines
from ..protocol.redact import mask_token_urls
from ..protocol.tool_spec import (
    LIMIT,
    OFFSET,
    ToolSpec,
    boolean,
    integer,
    obj,
    record_id,
    string,
)
from .mcp_catalog import tool_policies

_USER = "group_cloud_user"
_CONSULTANT = "group_cloud_consultant"
_DEVELOPER = "group_cloud_developer"
_MANAGER = "group_cloud_manager"

_INSTANCE = record_id("The instance.")
#: Log chunks read at a time while paging a job's log.
_CHUNK_BATCH = 50
#: What a page of a job's log may take: the answer's limit, less room for
#: the job's state and the page's own keys. A fixed size, so the pages of
#: a log are cut at the same lines from one call to the next.
LOG_PAGE_BYTES = MAX_BYTES - 4 * 1024
#: What each text of the job's state and brief keeps beside its log: with
#: their keys, well within the 4 KB the page leaves them, so the answer's
#: last guard never cuts a page of the log (``_tool_get_job``).
_JOB_FIELD_BYTES = 256
#: The Traefik files of a server, which ``get_host`` answers only when asked.
_TRAEFIK_FILES = ("traefik_yml", "traefik_config_yml", "traefik_inverseproxy_yaml")

TOOLS = (
    ToolSpec(
        "panel_overview",
        "What this panel holds at a glance: the user's role and permissions, counts, "
        "recent jobs, active alerts, the Odoo versions it can deploy, the "
        "languages an instance can be installed in (odoo_initial_lang takes the "
        "code) and the backup destinations a project or an instance can use "
        "(backup_backend_id takes the id).",
        _USER,
        obj(),
        handler="_tool_panel_overview",
        read_only=True,
        untrusted=True,
    ),
    ToolSpec(
        "search",
        "Find projects, instances and (for managers) servers by name or domain.",
        _USER,
        obj(
            {"query": string("At least two characters.", max_length=200)},
            required=("query",),
        ),
        route="/cloud/global_search",
        read_only=True,
    ),
    ToolSpec(
        "list_projects",
        "List the projects the user can see, in pages.",
        _USER,
        obj({"offset": OFFSET, "limit": LIMIT}),
        handler="_tool_list_projects",
        read_only=True,
    ),
    ToolSpec(
        "get_project",
        "One project: its settings and, with include_instances, its instances.",
        _USER,
        obj(
            {
                "project_id": record_id("The project."),
                "include_instances": boolean("Also return the project's instances."),
            },
            required=("project_id",),
        ),
        handler="_tool_get_project",
        read_only=True,
    ),
    ToolSpec(
        "list_instances",
        "List the instances of one project (its archived ones with archived=true) "
        "or of one server: give project_id or host_id.",
        _USER,
        obj(
            {
                "project_id": record_id("The project."),
                "host_id": record_id("The server."),
                "archived": boolean("The project's archived instances instead."),
                "offset": OFFSET,
                "limit": LIMIT,
            }
        ),
        handler="_tool_list_instances",
        read_only=True,
        requires="project_id or host_id, one of the two.",
    ),
    ToolSpec(
        "get_instance",
        "One instance: environment, state, domain, versions, repositories and "
        "resources. Secrets show only as has_<field> flags.",
        _USER,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        route="/cloud/get_instance",
        read_only=True,
    ),
    ToolSpec(
        "list_hosts",
        "List the servers the user can see, with their capacity, in pages.",
        _USER,
        obj({"offset": OFFSET, "limit": LIMIT}),
        handler="_tool_list_hosts",
        read_only=True,
    ),
    ToolSpec(
        "get_host",
        "One server: address, state, resources, hardening, its SSH key "
        "fingerprint, the actions its page offers (custom_actions, for "
        "run_host_action) and, while a job runs or waits on it, that job "
        "(active_job: its type, state and last step). Its three Traefik files "
        "(traefik_yml, traefik_config_yml, traefik_inverseproxy_yaml) only with "
        "include_traefik_files=true.",
        _MANAGER,
        obj(
            {
                "host_id": record_id("The server."),
                "include_traefik_files": boolean("Also return its Traefik files."),
            },
            required=("host_id",),
        ),
        handler="_tool_get_host",
        read_only=True,
    ),
    ToolSpec(
        "get_job",
        "A job: its state, what it waits behind and its log, in pages. The log "
        "is a clean view of the panel's: terminal colour codes are taken out, "
        "and consecutive lines that differ only in their numbers (download or "
        "extraction progress) show as the first and the last of them, with "
        "'(+N similar lines)' between. Lines are counted on that view: total says how "
        "many lines, total_pages how many pages and page which one this is. Ask "
        "for any page, the last one too, with page; or start at a line with "
        "offset (next_offset continues). limit makes pages of at most that many "
        "lines.",
        _USER,
        obj(
            {
                "job_id": record_id("The job."),
                "offset": integer(
                    "The line of the log's view where the page starts: the "
                    "next_offset of the previous answer.",
                    minimum=0,
                ),
                "page": integer(
                    "The page to show, from 1 to total_pages; instead of offset.",
                    minimum=1,
                ),
                "limit": LIMIT,
            },
            required=("job_id",),
        ),
        handler="_tool_get_job",
        read_only=True,
        untrusted=True,
        requires="a job the user can see (list_jobs).",
    ),
    ToolSpec(
        "list_alerts",
        "The alerts the user can see, newest first, in pages.",
        _USER,
        obj(
            {
                "state_filter": string(
                    "Which alerts.", enum=("all", "active", "dismissed")
                ),
                "level_filter": string(
                    "Which levels.", enum=("all", "warning", "critical")
                ),
                "offset": OFFSET,
                "limit": LIMIT,
            }
        ),
        handler="_tool_list_alerts",
        read_only=True,
        untrusted=True,
    ),
    ToolSpec(
        "get_audit_log",
        "The audit log of one instance or one server, in pages.",
        _MANAGER,
        obj(
            {
                "instance_id": _INSTANCE,
                "host_id": record_id("The server."),
                "q": string("Filter by user or action."),
                "action_filter": string("One action."),
                "date_from": string("ISO date."),
                "date_to": string("ISO date."),
                "offset": OFFSET,
                "limit": LIMIT,
            }
        ),
        handler="_tool_get_audit_log",
        read_only=True,
        untrusted=True,
    ),
    ToolSpec(
        "get_monitoring",
        "How monitoring is set up on this panel: collection, dashboards, links.",
        _DEVELOPER,
        obj(),
        route="/cloud/monitoring/config",
        read_only=True,
    ),
    ToolSpec(
        "compare_config",
        "Compare an instance's configuration with its project's: what apply_config "
        "would change.",
        _CONSULTANT,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        route="/cloud/compare_sync",
        read_only=True,
        requires="an instance of a project.",
    ),
    ToolSpec(
        "github_repo_info",
        "Read a GitHub repository: its branches, a branch's head, its Odoo modules or "
        "its Python requirements.",
        _CONSULTANT,
        obj(
            {
                "url": string("The repository's https URL.", max_length=500),
                "branch": string("The branch (all but what=branches)."),
                "what": string(
                    "What to read.",
                    enum=("branches", "head", "modules", "requirements"),
                ),
            },
            required=("url", "what"),
        ),
        handler="_tool_github_repo_info",
        read_only=True,
        untrusted=True,
        requires=(
            "a repository the panel's GitHub App or token can read; branch for "
            "all but branches."
        ),
    ),
    ToolSpec(
        "list_backups",
        "An instance's backups, in pages (refresh=true asks the server again, as a "
        "job), or with job_id the result of a backup listing job.",
        _DEVELOPER,
        obj(
            {
                "instance_id": _INSTANCE,
                "job_id": record_id("A backup listing job."),
                "refresh": boolean("Ask the server again."),
                "offset": OFFSET,
                "limit": LIMIT,
            }
        ),
        handler="_tool_list_backups",
        read_only=True,
        requires=(
            "a deployed instance with a backup destination; job_id from a "
            "listing this tool queued."
        ),
    ),
    ToolSpec(
        "read_access_log",
        "The last lines of an instance's proxy access log, in pages.",
        _DEVELOPER,
        obj(
            {
                "instance_id": _INSTANCE,
                "tail": integer("How many lines.", minimum=1, maximum=5000),
                "offset": OFFSET,
                "limit": LIMIT,
            },
            required=("instance_id",),
        ),
        handler="_tool_read_access_log",
        read_only=True,
        untrusted=True,
        requires="a deployed instance behind the panel's proxy.",
    ),
)

ROUTES = {
    "/cloud/get_config": "panel_overview",
    "/cloud/get_dashboard": "panel_overview",
    "/cloud/get_odoo_versions": "panel_overview",
    "/cloud/get_alert_count": "panel_overview",
    "/cloud/global_search": "search",
    "/cloud/get_projects": "list_projects",
    "/cloud/get_project": "get_project",
    "/cloud/get_project_full": "get_project",
    "/cloud/get_project_instances": "list_instances",
    "/cloud/get_host_instances": "list_instances",
    "/cloud/get_archived_instances": "list_instances",
    "/cloud/get_instance": "get_instance",
    "/cloud/get_hosts": "list_hosts",
    "/cloud/get_host": "get_host",
    "/cloud/get_langs": "panel_overview",
    "/cloud/get_job_state": "get_job",
    "/cloud/get_job_brief": "get_job",
    "/cloud/get_alert_history": "list_alerts",
    "/cloud/get_audit_log": "get_audit_log",
    "/cloud/monitoring/config": "get_monitoring",
    "/cloud/compare_sync": "compare_config",
    "/cloud/get_repo_branches": "github_repo_info",
    "/cloud/get_branch_head": "github_repo_info",
    "/cloud/get_repo_modules": "github_repo_info",
    "/cloud/get_repo_requirements": "github_repo_info",
    "/cloud/list_backups": "list_backups",
    "/cloud/get_backup_result": "list_backups",
    "/cloud/instance/access_log": "read_access_log",
}


_REPO_ROUTES = {
    "branches": "/cloud/get_repo_branches",
    "head": "/cloud/get_branch_head",
    "modules": "/cloud/get_repo_modules",
    "requirements": "/cloud/get_repo_requirements",
}


class McpCatalogRead(models.AbstractModel):
    """The read tools, and the routes they call."""

    _inherit = "incubacloud.mcp.catalog"

    @api.model
    def _tool_specs(self):
        """Add the read tools."""
        return super()._tool_specs() + list(TOOLS)

    @api.model
    def _route_policies(self):
        """Classify the routes the read tools call."""
        return {**super()._route_policies(), **tool_policies(ROUTES)}


class McpToolsRead(models.AbstractModel):
    """Handlers of the read tools that need more than one route."""

    _inherit = "incubacloud.mcp.tools"

    @api.model
    def _tool_panel_overview(self, args):
        """Role and permissions, dashboard, alert count, Odoo versions, the
        languages and the backup destinations' names, as the forms list them."""
        config = self._route("/cloud/get_config")
        backends = self._route("/cloud/get_backup_backends")
        overview = {
            "role": config.get("role"),
            "permissions": config.get("permissions"),
            "active_alerts": self._route("/cloud/get_alert_count").get("count"),
            "odoo_versions": self._route("/cloud/get_odoo_versions"),
            "languages": [
                {"code": lang.get("code"), "name": lang.get("name")}
                for lang in self._route("/cloud/get_langs") or []
                if isinstance(lang, dict)
            ],
            "backup_destinations": [
                {"id": row.get("id"), "name": row.get("name")}
                for row in (backends.get("items") or [])
                if isinstance(row, dict)
            ],
        }
        # The dashboard's alerts get what the rest leaves of 64 KB; all of
        # them are in list_alerts.
        dashboard = self._route("/cloud/get_dashboard")
        alerts = page_answer(
            dashboard, "alerts", max_bytes=MAX_BYTES - size_of(overview)
        )
        dashboard["alerts"] = alerts["alerts"]
        dashboard["truncated"] = bool(dashboard.get("truncated") or alerts["truncated"])
        return {**overview, "dashboard": dashboard}

    @api.model
    def _tool_get_host(self, args):
        """A server as its page reads it, with the job it runs or waits for.

        Its Traefik files are left out unless asked for: three YAML files
        weigh more than the rest of the answer, and an agent rarely needs
        them.
        """
        host = self._route("/cloud/get_host", host_id=args["host_id"])
        if not isinstance(host, dict) or host.get("ok") is False:
            return host
        if not args.get("include_traefik_files"):
            for key in _TRAEFIK_FILES:
                host.pop(key, None)
        host["active_job"] = self._host_active_job(args["host_id"])
        return host

    @api.model
    def _host_active_job(self, host_id):
        """Return the server's own job that runs, or else the first that waits.

        Its type, state and last step as the panel's log says it, and, for a
        step of a chain (a VPS's provisioning: order, hardening, setup),
        which step of how many.

        :return: a dict, or None when no job of the server is active
        """
        Job = self.env["cloud.job"]
        domain = [
            ("host_id", "=", host_id),
            ("instance_id", "=", False),
            ("state", "in", Job._active_states),
            ("job_type_id.code", "not in", Job._get_hidden_job_types()),
        ]
        job = Job.search(
            domain + [("state", "!=", "queued")], order="id", limit=1
        ) or Job.search(domain, order="id", limit=1)
        if not job:
            return None
        active = {
            "job_id": job.id,
            "job_type": job.job_type_id.code,
            "name": job.job_type_id.name or job.name,
            "state": "blocked" if job.blocked_alert_id else job.state,
            "step": job._get_last_system_message() or "",
        }
        if job.queue_chain_key:
            chain = Job.sudo().search(
                [("queue_chain_key", "=", job.queue_chain_key)], order="id"
            )
            active["chain"] = {
                "position": chain.ids.index(job.id) + 1,
                "steps": [step.job_type_id.code for step in chain],
            }
        return active

    @api.model
    def _tool_list_alerts(self, args):
        """The alerts, a page of the route's fitted to 64 KB."""
        offset = args.get("offset") or 0
        params = {
            key: args[key] for key in ("state_filter", "level_filter") if key in args
        }
        result = self._route(
            "/cloud/get_alert_history",
            offset=offset,
            limit=args.get("limit") or 20,
            **params,
        )
        return page_answer(
            result, "alerts", offset, first=offset, total=result.get("total")
        )

    @api.model
    def _tool_get_audit_log(self, args):
        """The audit log, a page of the route's fitted to 64 KB."""
        offset = args.get("offset") or 0
        params = {key: value for key, value in args.items() if key != "offset"}
        result = self._route("/cloud/get_audit_log", offset=offset, **params)
        return page_answer(
            result, "entries", offset, first=offset, total=result.get("total")
        )

    @api.model
    def _tool_read_access_log(self, args):
        """The proxy log's last lines, in 64 KB pages; the summary on each."""
        result = self._route(
            "/cloud/instance/access_log",
            instance_id=args["instance_id"],
            tail=args.get("tail"),
        )
        if result.get("ok") is False:
            return result
        return page_answer(result, "entries", args.get("offset"), args.get("limit"))

    @api.model
    def _tool_list_projects(self, args):
        """The projects, as the panel caps them, in pages."""
        result = self._route("/cloud/get_projects")
        return {
            **page_items(
                result.get("items") or [], args.get("offset"), args.get("limit")
            ),
            "capped_at": result.get("limit") if result.get("truncated") else None,
        }

    @api.model
    def _tool_list_hosts(self, args):
        """The servers, as the panel caps them, in pages."""
        result = self._route("/cloud/get_hosts")
        return {
            **page_items(
                result.get("hosts") or [], args.get("offset"), args.get("limit")
            ),
            "autoassign_enabled": result.get("autoassign_enabled"),
            "capped_at": result.get("limit") if result.get("truncated") else None,
        }

    @api.model
    def _tool_get_project(self, args):
        """A project and, when asked, its instances."""
        project = self._route("/cloud/get_project", project_id=args["project_id"])
        if project.get("ok") is False or not args.get("include_instances"):
            return project
        full = self._route("/cloud/get_project_full", project_id=args["project_id"])
        return {**project, "instances": list((full.get("instances") or {}).values())}

    @api.model
    def _tool_list_instances(self, args):
        """The instances of one project (or its archived ones) or of one server."""
        project_id, host_id = args.get("project_id"), args.get("host_id")
        if bool(project_id) == bool(host_id):
            raise UserError(self.env._("Give either project_id or host_id."))
        if host_id:
            result = self._route("/cloud/get_host_instances", host_id=host_id)
        elif args.get("archived"):
            result = self._route("/cloud/get_archived_instances", project_id=project_id)
        else:
            result = self._route("/cloud/get_project_instances", project_id=project_id)
        if result.get("ok") is False:
            return result
        return page_items(
            result.get("instances") or [], args.get("offset"), args.get("limit")
        )

    @api.model
    def _tool_get_job(self, args):
        """State, queue and log of a job the user can read.

        The log is read as ``/cloud/log/<id>/download`` reads it: the
        record rules of ``cloud.job.log.chunk`` decide which lines the
        user sees (a stakeholder, only the panel's own messages).
        """
        state = self._route("/cloud/get_job_state", job_id=args["job_id"])
        if not state.get("ok"):
            return {"ok": False, "error": self.env._("Job not found")}
        brief = self._route("/cloud/get_job_brief", job_id=args["job_id"])
        details = (
            {key: value for key, value in brief.items() if key != "ok"}
            if brief.get("ok")
            else {}
        )
        # Its texts cut short, so that with a page of the log the answer
        # stays within the limit: the guard would cut the page, and the
        # next page asked for by number would start past lines never shown.
        company, _cut = cap_strings(
            {**details, **{key: value for key, value in state.items() if key != "ok"}},
            _JOB_FIELD_BYTES,
        )
        return {
            **company,
            "log": self._job_log_page(
                args["job_id"], args.get("offset"), args.get("limit"), args.get("page")
            ),
        }

    @api.model
    def _job_log_lines(self, job_id):
        """Yield the lines of a job's log the user may read, from the first.

        The record rules of ``cloud.job.log.chunk`` decide which; the
        chunks are read a batch at a time, so a long log is never all in
        memory at once, each batch after the last chunk read (``id``): an
        ``OFFSET`` would have the database walk every chunk before it
        again, batch after batch. Each line says the stream it came from.

        :param int job_id: the job
        :return: a generator of ``[source] line`` strings
        """
        Chunk = self.env["cloud.job.log.chunk"]
        last = 0
        while True:
            chunks = Chunk.search(
                [("job_id", "=", job_id), ("id", ">", last)],
                order="id",
                limit=_CHUNK_BATCH,
            )
            if not chunks:
                return
            last = chunks[-1].id
            for chunk in chunks:
                for line in (chunk.content or "").splitlines():
                    yield f"[{chunk.source}] {line}"
            chunks.invalidate_recordset(["content"])

    @api.model
    def _job_log_page(self, job_id, offset=0, limit=None, page=None):
        """Return a page of the agent's view of a job's log.

        The view (``protocol.log_view``) has no terminal codes and folds
        the runs of progress lines; offsets, lines and pages are counted
        on it. The log is read to its end, so the answer always says how
        many lines and pages it has. The panel's log is not changed.

        Each line is masked before it is measured, as the answer masks it
        (the secrets, and a link with a one-time token): a mask may be
        longer than what it hides, and a page measured before masking
        could grow past what the answer takes. Masked first, a page is
        what the answer shows, whole: page N+1 starts where page N ended.

        :param int job_id: the job
        :param int offset: the view's line where the page starts
        :param limit: at most this many lines a page, or None
        :param page: the page to show, from 1, instead of *offset*
        :return: ``{items, offset, page, total, total_pages, truncated,
            next_offset}``
        :raises UserError: for both *offset* and *page*, or a page the
            log does not have
        """
        if page and offset:
            raise UserError(self.env._("Give either offset or page."))
        scrubber = self.env["incubacloud.mcp.secrets"]._scrubber()
        result = page_lines(
            (
                mask_token_urls(scrubber.text(line))
                for line in clean_lines(self._job_log_lines(job_id))
            ),
            offset,
            limit,
            page,
            max_bytes=LOG_PAGE_BYTES,
        )
        if page and page > max(1, result["total_pages"]):
            raise UserError(
                self.env._(
                    "This log has %(pages)s pages: ask for one from 1 to %(pages)s.",
                    pages=result["total_pages"],
                )
            )
        return result

    @api.model
    def _tool_github_repo_info(self, args):
        """Branches, head, modules or requirements of a repository."""
        what = args["what"]
        if what == "branches":
            return self._route(_REPO_ROUTES[what], url=args["url"])
        if not args.get("branch"):
            raise UserError(self.env._("Name the branch."))
        return self._route(_REPO_ROUTES[what], url=args["url"], branch=args["branch"])

    @api.model
    def _tool_list_backups(self, args):
        """An instance's backups, or the result of a listing job."""
        if args.get("job_id"):
            return self._route(
                "/cloud/get_backup_result",
                job_id=args["job_id"],
                offset=args.get("offset") or 0,
                limit=args.get("limit"),
            )
        if not args.get("instance_id"):
            raise UserError(self.env._("Give instance_id or job_id."))
        result = self._route(
            "/cloud/list_backups",
            instance_id=args["instance_id"],
            refresh=bool(args.get("refresh")),
            offset=args.get("offset") or 0,
            limit=args.get("limit"),
        )
        return self._with_job_state(result)
