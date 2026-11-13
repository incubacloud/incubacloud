"""Tools that operate the panel: create, change, deploy, back up, resolve."""

from odoo import api, models
from odoo.exceptions import UserError

from odoo.addons.incubacloud.models._odoo_versions import (
    ODOO_VERSION_SELECTION,
    POSTGRES_MAJORS,
)
from odoo.addons.incubacloud.models._repo_requirements import _normalize_url
from odoo.addons.incubacloud.models.cloud_repo_mixin import repo_url_key

from ..protocol.tool_spec import (
    ToolSpec,
    array,
    boolean,
    describe_fields,
    integer,
    nullable,
    obj,
    record_id,
    record_key,
    string,
)
from .mcp_catalog import tool_policies
from .mcp_tools import quoted
from .mcp_tools_code import _repo_key

#: Stands for the commit a branch has when ``freeze_repos`` pins it, read
#: only once the call runs.
_BRANCH_HEAD = object()

_CONSULTANT = "group_cloud_consultant"
_PROJECT_MANAGER = "group_cloud_project_manager"
_DEVELOPER = "group_cloud_developer"

_INSTANCE = record_id("The instance.")
_REPO_MODELS = ("cloud.project.repo", "cloud.instance.repo")

#: The fields a project's and an instance's ``vals`` share.
_SERIES = [version for version, _label in ODOO_VERSION_SELECTION]
_COMMON_FIELDS = {
    "name": string("The name."),
    # As text, or as the number the panel's form sends (19 or 19.0): the
    # routes write it as text.
    "odoo_version": {
        **string("The Odoo series, as text (19.0) or as a number.", enum=_SERIES),
        "type": ["string", "number"],
        "enum": [*_SERIES, *(float(version) for version in _SERIES)],
    },
    "odoo_commit_sha": string(
        "Pin Odoo's core to this commit; empty follows the branch."
    ),
    "odoo_initial_lang": nullable(
        record_key(
            "The language installed on the first deploy, by its code (es_ES, "
            "fr_FR…) or by the id get_project and get_instance answer; null "
            "for English.",
            "res.lang",
            "code",
            or_id=True,
        )
    ),
    "pip_dependencies": string("The pip requirements, one per line."),
    "apt_dependencies": string("The system (APT) packages, one per line."),
    "backup_backend_id": nullable(
        record_id("The backup destination; null for the default.")
    ),
    "tag_ids": array("The tags (manage_tags), all of them.", record_id("A tag.")),
}
_REPO = obj(
    {
        "id": record_id("The repository row to keep, on an update."),
        "url": string("The repository's URL."),
        "branch": string("The branch; main when empty."),
        "addons": string("The modules to use, comma-separated; empty for all."),
        "excludes": string("The modules to leave out, comma-separated."),
        "commit_sha": string("Pin to this commit; empty follows the branch."),
    }
)
_REPOS = array(
    "The repositories, all of them, in order: the list replaces the current one.",
    _REPO,
)
_PROJECT_FIELDS = _COMMON_FIELDS | {
    "description": string("A description."),
    "project_author": string("The author the modules' manifests name."),
    "project_license": string(
        "The license of the project's modules.",
        enum=(
            "no_license",
            "Apache-2.0",
            "BSL-1.0",
            "AGPL-3.0-or-later",
            "LGPL-3.0-or-later",
            "MIT",
            "OEEL-1.0",
            "OPL-1.0",
        ),
    ),
    "pr_reviews_enabled": boolean(
        "Open a preview instance for each pull request of its repositories."
    ),
    "repos": _REPOS,
}
_ENVIRONMENT = string("Staging or production.", enum=("staging", "production"))
_DOMAIN = obj(
    {
        "id": record_id("The domain row to keep, on an update."),
        "hostname": string("The hostname."),
        "redirect_to": string("Redirect to this hostname instead of serving."),
        "cert_resolver": string("How its certificate is obtained."),
        "redirect_permanent": boolean("A permanent (301) redirect."),
    }
)
_INSTANCE_FIELDS = (
    _COMMON_FIELDS
    | {
        "host_id": record_id("The server (list_hosts)."),
        "odoo_proxy": string("The proxy in front of Odoo.", enum=("traefik", "none")),
        "postgres_version": string("The PostgreSQL major.", enum=POSTGRES_MAJORS),
        "postgres_dbname": string("The database's name."),
        "postgres_username": string("The database user."),
        "smtp_relay_host": string("The outgoing mail relay's host."),
        "smtp_relay_port": integer("The relay's port.", minimum=1, maximum=65535),
        "smtp_relay_security": string(
            "The relay's security.", enum=("none", "starttls", "ssl")
        ),
        "smtp_relay_user": string(
            "The relay's user; with OAuth2, the sender mailbox."
        ),
        "smtp_relay_auth": string(
            "How a production's relay signs in: password, or oauth2_microsoft"
            " for Microsoft 365, which sets smtp.office365.com, 587 and"
            " STARTTLS. With OAuth2 the client secret is set by the user in"
            " the panel.",
            enum=("password", "oauth2_microsoft"),
        ),
        "smtp_relay_oauth_directory_id": string(
            "With OAuth2, the directory ID of the Microsoft 365 organisation:"
            " a GUID or one of its domains."
        ),
        "smtp_relay_oauth_client_id": string(
            "With OAuth2, the application (client) ID, a GUID."
        ),
    }
    | {
        f"{service}_memory_limit": string(f"Memory limit of {service}, as 2g or 512m.")
        for service in ("odoo", "db", "backup", "smtp")
    }
    | {
        f"{service}_cpus": {"type": "number", "description": f"CPUs of {service}."}
        for service in ("odoo", "db", "backup", "smtp")
    }
    | {
        "repos": _REPOS,
        "domains": array(
            "The domains, all of them: the list replaces the current one.", _DOMAIN
        ),
    }
)
_CREATE_PROJECT = obj(_PROJECT_FIELDS, required=("name",))
_UPDATE_PROJECT = obj(
    _PROJECT_FIELDS
    | {
        "member_ids": array(
            "The users assigned to the project, all of them.", record_id("A user.")
        ),
    }
)
_CREATE_INSTANCE = obj(
    _INSTANCE_FIELDS
    | {
        "project_id": record_id("The project."),
        "environment": _ENVIRONMENT,
        "domain": string("One domain, instead of domains."),
    }
)
_UPDATE_INSTANCE = obj(
    _INSTANCE_FIELDS
    | {
        "environment": string(
            "Chosen when the instance is created: another value is refused.",
            enum=("staging", "production"),
        ),
        "odoo_conf": string("The [options] of the instance's odoo.conf."),
        "auto_rebuild": boolean("Rebuild on a push to a branch it deploys."),
        "auto_update": boolean("Update the modules on a rebuild."),
        "boot_test": boolean(
            "Boot a rebuild's new image on a copy of the database before"
            " applying it (with auto_update on)."
        ),
        "autopurge_exempt": boolean("Keep this staging from the automatic purge."),
        "website_info_public": boolean(
            "Let anyone open Odoo's /website/info (installed apps and version);"
            " applied on the next rebuild."
        ),
        "whitelist": array(
            "The hostnames it may reach out to, all of them.", string("A hostname.")
        ),
    }
)


def _deployed_code(env, url, branch, commit_sha):
    """Return what a repository line deploys, as two spellings of it compare.

    The branch and the commit as ``/cloud/save_instance`` stores them: an
    empty branch is ``main``.

    :param env: an Odoo environment
    :param str url: the repository's address, in any spelling the panel takes
    :param str branch: the branch
    :param str commit_sha: the pinned commit; empty follows the branch
    :return: ``(owner/name, branch, commit)``; for a repository that is
        not GitHub's, the address every spelling of it shares
        (``repo_url_key``)
    :rtype: tuple
    """
    url = (url or "").strip()
    key = _repo_key(url)
    return (
        "/".join(key) if key else repo_url_key(env, url) or url,
        (branch or "main").strip() or "main",
        (commit_sha or "").strip(),
    )


def _with_vals(description, vals):
    """Return the description of a tool that takes *vals*, with its fields.

    :param str description: what the tool does
    :param dict vals: the schema of its ``vals``
    """
    return (
        f"{description} vals takes these fields, and refuses any other; "
        f"secrets are set by the user in the panel: {describe_fields(vals)}."
    )


def _vals(schema):
    """Return the ``vals`` property declared by *schema*, described."""
    return {
        **schema,
        "description": "The fields to set; only those given change.",
    }


TOOLS = (
    ToolSpec(
        "create_project",
        _with_vals("Create a project.", _CREATE_PROJECT),
        _PROJECT_MANAGER,
        obj({"vals": _vals(_CREATE_PROJECT)}, required=("vals",)),
        route="/cloud/create_project",
        requires=(
            "a series this panel offers (panel_overview lists odoo_versions); a "
            "backup destination id from panel_overview's backup_destinations."
        ),
    ),
    ToolSpec(
        "update_project",
        _with_vals(
            "Change a project's settings. Answers the project's repositories as "
            "they are once saved.",
            _UPDATE_PROJECT,
        ),
        _CONSULTANT,
        obj(
            {"project_id": record_id("The project."), "vals": _vals(_UPDATE_PROJECT)},
            required=("project_id", "vals"),
        ),
        handler="_tool_update_project",
        requires=(
            "a project the user is a member of; member_ids from find_users; a "
            "series this panel offers."
        ),
    ),
    ToolSpec(
        "import_project",
        "Create a project and its production instance from a GitHub repository.",
        _PROJECT_MANAGER,
        obj(
            {
                "url": string("The repository's https URL.", max_length=500),
                "branch": string("The branch.", max_length=200),
            },
            required=("url",),
        ),
        route="/cloud/import_project",
        requires=(
            "a GitHub repository the panel's GitHub App or token can read, laid "
            "out by Doodba's copier (its .copier-answers.yml); a series this "
            "panel offers."
        ),
    ),
    ToolSpec(
        "create_instance",
        _with_vals(
            "Create an instance in a project. Its name is 1 to 63 lowercase "
            "letters, digits, hyphens and underscores, starting with a letter or "
            "a digit, and unique in the project, archived instances included.",
            _CREATE_INSTANCE,
        ),
        _CONSULTANT,
        obj({"vals": _vals(_CREATE_INSTANCE)}, required=("vals",)),
        route="/cloud/create_instance",
        requires=(
            "a project the user can change, and a server from list_hosts; a "
            "production instance needs a backup destination (its own "
            "backup_backend_id, its project's or the panel's default) before it "
            "is deployed; one production instance per project; the series one "
            "the panel offers."
        ),
    ),
    ToolSpec(
        "update_instance",
        _with_vals("Change an instance's settings.", _UPDATE_INSTANCE),
        _CONSULTANT,
        obj(
            {"instance_id": _INSTANCE, "vals": _vals(_UPDATE_INSTANCE)},
            required=("instance_id", "vals"),
        ),
        route="/cloud/save_instance",
        # A production's repositories, branches and pinned commits are the
        # code it deploys: changing them asks, as a write to a branch a
        # production deploys from does (_confirm_update_instance).
        confirm="conditional",
        requires=(
            "an instance the user can see; the environment chosen at creation "
            "cannot change."
        ),
    ),
    ToolSpec(
        "keep_staging",
        "Keep a staging from its automatic purge.",
        _CONSULTANT,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        route="/cloud/keep_instance",
        requires="a staging instance.",
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
        requires="an archived instance (list_instances with archived=true).",
    ),
    ToolSpec(
        "deploy_instance",
        "Deploy an instance. Answers the job_id; follow it with get_job.",
        _CONSULTANT,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        route="/cloud/deploy_instance",
        enqueues=True,
        requires=(
            "a server on the instance (host_id); not deployed yet (a deployed "
            "one is rebuilt with rebuild_instance); a production instance needs "
            "a backup destination; a deploy held by a dependency conflict "
            "starts once resolve_conflict resolves it."
        ),
    ),
    ToolSpec(
        "rebuild_instance",
        "Rebuild an instance's image and restart it with a boot test.",
        _CONSULTANT,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        route="/cloud/rebuild_instance",
        enqueues=True,
        requires="a deployed instance on a server.",
    ),
    ToolSpec(
        "send_relay_test_mail",
        "Send one test mail through a production's mail relay to the user's "
        "own address. Answers the job_id; get_job then says whether the "
        "relay's server took it (result.delivered, result.message).",
        _CONSULTANT,
        obj({"instance_id": _INSTANCE}, required=("instance_id",)),
        route="/cloud/instance_relay_test_mail",
        enqueues=True,
        requires=(
            "a deployed production instance with a mail relay and no changes "
            "left to apply (get_instance answers relay_test_refusal); five "
            "test mails an hour per instance."
        ),
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
        requires=(
            "a deployed production instance, and a staging name not used in the "
            "project."
        ),
    ),
    ToolSpec(
        "create_backup",
        "Back up an instance now. Answers backup_job_id, the job that takes the "
        "copy, and job_id, the last job of its chain, which lists the "
        "instance's backups once the copy is taken: when job_id is done, "
        "list_backups shows the new backup.",
        _DEVELOPER,
        obj(
            {
                "instance_id": _INSTANCE,
                "with_filestore": boolean(
                    "Include the filestore (default true)."
                ),
            },
            required=("instance_id",),
        ),
        handler="_tool_create_backup",
        requires=(
            "a deployed instance on a server; a production one needs a backup "
            "destination."
        ),
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
        requires=(
            "an active pip or addon conflict alert (list_alerts) and a choice "
            "among the ones it lists."
        ),
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
        # Taking the project's repositories or branches gives a production
        # code it did not deploy from (_confirm_apply_config).
        confirm="conditional",
        requires="the actions compare_config proposed for this instance, unchanged.",
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
        # Pinning or releasing a production's commit changes the code it
        # deploys (_confirm_freeze_repos).
        confirm="conditional",
        requires=(
            "repositories of a project or an instance the user can change, with "
            "a branch to read the commit of."
        ),
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
        requires="a deployed staging instance.",
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
    "/cloud/instance_relay_test_mail": "send_relay_test_mail",
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
    def _tool_update_project(self, args):
        """Save the project; answer its repositories as saved, all of them.

        The route answers the rows it wrote, and none when the call sent no
        repositories: the agent reads what the project holds either way.
        """
        result = self._route(
            "/cloud/save_project", project_id=args["project_id"], vals=args["vals"]
        )
        if not isinstance(result, dict) or result.get("ok") is False:
            return result
        project = self.env["cloud.project"].browse(args["project_id"])
        return {
            **result,
            "repos": [
                {
                    "id": repo.id,
                    "url": repo.url,
                    "branch": repo.branch or "",
                    "addons": repo.addons or "",
                    "excludes": repo.excludes or "",
                    "commit_sha": repo.commit_sha or "",
                }
                for repo in project.repo_ids
            ],
        }

    @api.model
    def _confirm_update_instance(self, args):
        """Ask before a production deploys from code it did not deploy from.

        A repository, a branch or a pinned commit that none of the
        production's lines holds yet is code that reaches production at
        its next rebuild, or at once with Auto-Rebuild: the same outcome a
        write to a branch it deploys from asks for (repo_write_files).
        Another spelling of the same address, other modules of the same
        repository, or a line taken out bring no new code and ask nothing,
        and neither does a staging.

        :param dict args: the validated arguments: ``instance_id`` and ``vals``
        :return: what the user approves, or None
        """
        repos = args["vals"].get("repos")
        if repos is None:
            return None
        instance = self.env["cloud.instance"].browse(args["instance_id"]).exists()
        return self._ask_production_code(
            instance,
            [
                _deployed_code(
                    self.env,
                    repo["url"],
                    repo.get("branch"),
                    repo.get("commit_sha"),
                )
                for repo in repos
                if (repo.get("url") or "").strip()
            ],
        )

    @api.model
    def _confirm_apply_config(self, args):
        """Ask before the project's configuration changes a production's code.

        The repositories the actions take from the project (``pull``) and
        the conflicts the project's line wins give the instance their
        branch and pinned commit: asked as :meth:`_confirm_update_instance`
        asks. When the project has several lines of one repository, the
        first is the one the actions apply, as ``apply_sync`` takes it.
        Dependencies, the instance's lines copied to the project and a
        staging ask nothing.

        :param dict args: the validated arguments: ``instance_id`` and ``actions``
        :return: what the user approves, or None
        """
        actions = args.get("actions")
        repo_actions = actions.get("repos") if isinstance(actions, dict) else None
        if not isinstance(repo_actions, dict):
            return None
        instance = self.env["cloud.instance"].browse(args["instance_id"]).exists()
        # The line apply_sync takes for an address: the first of the project's.
        project_lines = {}
        for line in instance.project_id.repo_ids:
            project_lines.setdefault(_normalize_url(line.url), line)
        taken = [url for url in repo_actions.get("pull") or () if isinstance(url, str)]
        conflicts = repo_actions.get("conflicts")
        if isinstance(conflicts, dict):
            # As apply_sync reads it: any winner but the instance is the project.
            taken += [
                url
                for url, winner in conflicts.items()
                if isinstance(url, str) and winner != "instance"
            ]
        codes = []
        for url in taken:
            line = project_lines.get(_normalize_url(url))
            if line:
                codes.append(
                    _deployed_code(
                        self.env, line.url, line.branch, line.commit_sha
                    )
                )
        return self._ask_production_code(instance, codes)

    @api.model
    def _confirm_freeze_repos(self, args):
        """Ask before pinning or releasing the commit of a production's line.

        Pinned, the line deploys its branch's commit of the moment;
        released, whatever its branch holds at the next rebuild. Asked
        for the lines of a production the call changes, as the routes
        read them: one line is pinned again even when it already is,
        several only where none is; releasing changes a pinned line
        alone. A project's lines and a staging's ask nothing.

        :param dict args: the validated arguments: ``model``, ``repo_ids``
            and ``freeze``
        :return: what the user approves, or None
        """
        if args["model"] != "cloud.instance.repo" or not args["repo_ids"]:
            return None
        freeze = args.get("freeze", True)
        lines = self.env["cloud.instance.repo"].search(
            [("id", "in", args["repo_ids"])]
        )
        if freeze and len(args["repo_ids"]) > 1:
            lines = lines.filtered(
                lambda line: line.url and line.branch and not line.commit_sha
            )
        elif not freeze:
            lines = lines.filtered("commit_sha")
        questions = [
            self._ask_production_code(
                instance,
                [
                    (
                        *_deployed_code(self.env, line.url, line.branch, "")[:2],
                        _BRANCH_HEAD if freeze else "",
                    )
                    for line in lines.filtered(
                        lambda line, inst=instance: line.instance_id == inst
                    )
                ],
            )
            for instance in lines.instance_id
        ]
        return "\n\n".join(question for question in questions if question) or None

    @api.model
    def _ask_production_code(self, instance, codes):
        """Return what to approve before *instance* deploys *codes*, or None.

        Only a production asks, and only for code none of its lines holds
        yet; each entry is listed once.

        :param instance: the instance whose lines would change
        :param list codes: ``(repository, branch, commit)`` as
            :func:`_deployed_code` gives them; ``_BRANCH_HEAD`` for the
            commit the branch has when the call runs
        :return: the question, or None
        """
        if instance.environment != "production":
            return None
        held = {
            _deployed_code(self.env, line.url, line.branch, line.commit_sha)
            for line in instance.repo_ids
        }
        new = [code for code in dict.fromkeys(codes) if code not in held]
        if not new:
            return None
        lines = "\n".join(
            "- " + self._code_entry(repo, branch, sha) for repo, branch, sha in new
        )
        return self.env._(
            "Change the code production %(name)s deploys from. It reaches "
            "production at its next rebuild, or at once when it rebuilds on "
            "every push. New in it:\n%(lines)s",
            name=quoted(instance.name),
            lines=lines,
        )

    @api.model
    def _code_entry(self, repo, branch, sha):
        """Return one entry of :meth:`_ask_production_code`'s list.

        :param str repo: the repository, as :func:`_deployed_code` names it
        :param str branch: the branch
        :param sha: the pinned commit, empty to follow the branch, or
            ``_BRANCH_HEAD``
        :rtype: str
        """
        if sha is _BRANCH_HEAD:
            return self.env._(
                "%(repo)s, branch %(branch)s, pinned at its current commit",
                repo=quoted(repo),
                branch=quoted(branch),
            )
        if sha:
            return self.env._(
                "%(repo)s, branch %(branch)s, commit %(sha)s",
                repo=quoted(repo),
                branch=quoted(branch),
                sha=quoted(sha),
            )
        return self.env._(
            "%(repo)s, branch %(branch)s",
            repo=quoted(repo),
            branch=quoted(branch),
        )

    @api.model
    def _tool_create_backup(self, args):
        """Queue the backup; answer the job that takes it and the chain's last.

        The route answers the chain's last job, the listing that records the
        copy; the copy's own job is the chain's ``backup_create``. Without
        ``with_filestore`` the copy keeps the filestore, as the panel's does.
        """
        result = self._with_job_state(
            self._route(
                "/cloud/create_backup",
                instance_id=args["instance_id"],
                with_filestore=bool(args.get("with_filestore", True)),
            )
        )
        if not isinstance(result, dict) or not result.get("job_id"):
            return result
        Job = self.env["cloud.job"]
        last = Job.browse(result["job_id"])
        backup = Job.search(
            [
                ("instance_id", "=", args["instance_id"]),
                ("job_type_id.code", "=", "backup_create"),
                ("id", "<", last.id),
            ],
            order="id desc",
            limit=1,
        )
        return {**result, "backup_job_id": backup.id or None}

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
