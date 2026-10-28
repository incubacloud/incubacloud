"""Which tools the model sees, the assistant's rules on top, and its own tools.

The MCP's tools as they are (``incubacloud.mcp.catalog``), each run through
``incubacloud.mcp.server._tools_call`` like an agent's call, and three of
the assistant's own: searching the user documentation, and searching and
reading core's public code. On top of the MCP's rules, the assistant's
(spec, «Seguridad»), by an open list rather than a closed one:

* code and staging deployments need a developer (:data:`_MIN_GROUP`);
* every tool that is not read-only asks the person first, except those of
  :data:`_UNASKED` while every instance their arguments name is a staging;
* so creating an instance, ``run_command`` and changing an instance's or a
  project's repositories always ask, and the card says what changes;
* anything that reaches a production instance, by any id of its
  arguments, asks and says so;
* after the task read content others can write (logs, files, mail, a
  command's output) and until the person writes again, every change asks
  (``tainted``).
"""

import json

from odoo import api, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.incubacloud.models.cloud_security_mixin import _GROUP_HIERARCHY
from odoo.addons.incubacloud_mcp.models.mcp_tools import quoted
from odoo.addons.incubacloud_mcp.models.mcp_tools_code import _repo_key
from odoo.addons.incubacloud_mcp.protocol.limits import fit_answer
from odoo.addons.incubacloud_mcp.protocol.schema import SchemaError, validate
from odoo.addons.incubacloud_mcp.protocol.tool_spec import (
    ToolSpec,
    integer,
    obj,
    string,
)

from ..connectors.base import ToolDef
from ..sources import (
    DOCS_URL,
    SourceError,
    core_root,
    read_source,
    search_code,
    search_docs,
)

_USER = "group_cloud_user"
_DEVELOPER = "group_cloud_developer"
#: The MCP tools the assistant gives to a developer only.
_MIN_GROUP = {
    name: _DEVELOPER
    for name in (
        "create_instance",
        "clone_to_staging",
        "revive_instance",
        "deploy_instance",
        "rebuild_instance",
        "import_project",
    )
}
#: The MCP tools that create an instance.
_CREATES_INSTANCES = (
    "create_instance",
    "clone_to_staging",
    "revive_instance",
    "import_project",
    "import_host_instance",
)
#: The tools that change something and still run without a card, while
#: every instance their arguments name is a staging and the task read
#: nothing others can write since the person's last message: the work of
#: writing a module and trying it on a staging. The code tools keep the
#: MCP's own approvals (a branch that existed, production, a public
#: repository, a preview). Every other tool that is not read-only asks.
_UNASKED = frozenset(
    {
        "deploy_instance",
        "rebuild_instance",
        "keep_staging",
        "create_backup",
        "clear_staging_mailbox",
        "dismiss_alerts",
        "manage_tags",
        "repo_create_branch",
        "repo_write_files",
        "repo_open_pull_request",
    }
)
#: Whether the "tainted" rule (decision 33) covers the agent's own work too:
#: writing to a branch the user's agent created, and deploying or rebuilding a
#: staging that deploys from one. True (the owner's choice of 28-oct): after
#: the task read content others write, every change asks, the agent's own
#: branch included. False would leave that work as the spec's branch rule has
#: it, without a card. The one line that decides it.
TAINT_COVERS_OWN_WORK = True
#: Reads of the MCP that carry text from outside the panel without the
#: ``untrusted`` mark: a configuration compared with what the repository
#: holds. They mark the task as the untrusted ones do (task 15). The names
#: and notes ``search`` and ``get_project`` answer are written by the panel's
#: own users, under their roles: they do not.
THIRD_PARTY_READS = frozenset({"compare_config"})
#: Characters of the arguments a card quotes.
_ARGUMENTS_CHARS = 1500

LOCAL_TOOLS = (
    ToolSpec(
        "search_docs",
        "Search the panel's user documentation by keywords (it is in English): "
        "answers the sections that match best, with their page's link.",
        _USER,
        obj(
            {
                "query": string("Keywords, in English.", max_length=200),
                "limit": integer("How many sections, at most.", minimum=1, maximum=10),
            },
            required=("query",),
        ),
        handler="_tool_search_docs",
        read_only=True,
    ),
    ToolSpec(
        "search_code",
        "Search the panel's public code (IncubaCloud core) for a text, ignoring "
        "case: answers file, line and the line's text.",
        _USER,
        obj(
            {
                "text": string(
                    "What to look for, two characters at least.", max_length=200
                ),
                "path": string(
                    "Only under this folder, e.g. incubacloud/models.", max_length=300
                ),
                "limit": integer("How many lines, at most.", minimum=1, maximum=100),
            },
            required=("text",),
        ),
        handler="_tool_search_code",
        read_only=True,
    ),
    ToolSpec(
        "read_code",
        "Read a file of the panel's public code by lines (pass next_line back for "
        "more), or list a folder; empty path for the top.",
        _USER,
        obj(
            {
                "path": string("The file or folder.", max_length=300),
                "start_line": integer("First line.", minimum=1),
                "lines": integer("How many lines.", minimum=1, maximum=2000),
            }
        ),
        handler="_tool_read_code",
        read_only=True,
    ),
)


def _ids(value):
    """Return the record ids in *value*: an id, or a list of them."""
    values = value if isinstance(value, list) else [value]
    return [
        item for item in values if isinstance(item, int) and not isinstance(item, bool)
    ]


def _repo_label(url):
    """Return ``owner/name`` of a repository address, or the address."""
    key = _repo_key(url)
    return f"{key[0]}/{key[1]}" if key else (url or "").strip()


class AssistantTools(models.AbstractModel):
    """The assistant's view of the tools."""

    _name = "incubacloud.assistant.tools"
    _description = "Tools the assistant offers the model"

    @api.model
    def _local_spec(self, name):
        """Return the spec of the assistant's own tool *name*, or None."""
        return next((spec for spec in LOCAL_TOOLS if spec.name == name), None)

    @api.model
    def _min_group(self, spec):
        """Return the least role that may call *spec* through the assistant."""
        extra = _MIN_GROUP.get(spec.name)
        if not extra:
            return spec.min_group
        return max(spec.min_group, extra, key=_GROUP_HIERARCHY.index)

    @api.model
    def _allowed(self, spec):
        """Return whether the current user may call *spec* through the assistant."""
        return self.env["cloud.security.mixin"]._has_cloud_group(self._min_group(spec))

    @api.model
    def _visible_specs(self):
        """Return the specs the current user's model sees, by name."""
        mcp = [
            s
            for s in self.env["incubacloud.mcp.catalog"]._tool_specs()
            if self._allowed(s)
        ]
        local = [s for s in LOCAL_TOOLS if self._allowed(s)]
        return sorted(mcp + local, key=lambda spec: spec.name)

    @api.model
    def _definitions(self):
        """Return the tools for the provider, in a stable order (prompt cache)."""
        return tuple(
            ToolDef(listed["name"], listed["description"], listed["inputSchema"])
            for listed in (spec.listed() for spec in self._visible_specs())
        )

    @api.model
    def _instances_named(self, args):
        """Return every instance *args* name, read past the record rules.

        By their id, by an alert's, by an instance repository line's
        (``freeze_repos``), and by a server's (every instance on it): the
        card must name a production the user does not see.
        """
        Instance = self.env["cloud.instance"].sudo()
        found = Instance
        for key, value in args.items():
            ids = _ids(value)
            if not ids:
                continue
            if key.endswith("instance_id"):
                found |= Instance.browse(ids).exists()
            elif key == "alert_id":
                found |= self.env["cloud.alert"].sudo().browse(ids).exists().instance_id
            elif key == "repo_ids" and args.get("model") == "cloud.instance.repo":
                lines = self.env["cloud.instance.repo"].sudo().browse(ids).exists()
                found |= lines.instance_id
            elif key in ("host_id", "target_host_id"):
                found |= Instance.search([("host_id", "in", ids)])
        return found

    @api.model
    def _production_target(self, args):
        """Return the production instances *args* name, or an empty recordset."""
        try:
            return self._instances_named(args).filtered(
                lambda inst: inst.environment == "production"
            )
        except AccessError:
            return self.env["cloud.instance"]

    @api.model
    def _acts_on_production(self, spec, args):
        """Return whether calling *spec* with *args* changes production."""
        if spec.read_only:
            return False
        vals = args.get("vals") if isinstance(args.get("vals"), dict) else {}
        if spec.name in ("import_project", "import_host_instance"):
            return True
        if spec.name in _CREATES_INSTANCES and vals.get("environment") == "production":
            return True
        if spec.name == "repo_write_files":
            return bool(
                self.env["incubacloud.mcp.tools"]._production_fed_by(
                    args.get("url") or "", args.get("branch") or ""
                )
            )
        return bool(self._production_target(args))

    @api.model
    def _repos_change(self, current, lines):
        """Say what a new list of repository lines adds, removes and moves.

        :param current: the repository lines there now
        :param list lines: the new list, as the route takes it
        """
        before = {_repo_label(line.url): line.branch for line in current}
        after = {
            _repo_label(line.get("url")): line.get("branch") or "main"
            for line in lines
            if isinstance(line, dict) and line.get("url")
        }
        nothing = self.env._("nothing")
        added = [
            f"{quoted(name)} ({quoted(branch)})"
            for name, branch in after.items()
            if name not in before
        ]
        removed = [quoted(name) for name in before if name not in after]
        moved = [
            f"{quoted(name)} → {quoted(branch)}"
            for name, branch in after.items()
            if name in before and before[name] != branch
        ]
        return {
            "added": ", ".join(added) or nothing,
            "removed": ", ".join(removed) or nothing,
            "moved": ", ".join(moved) or nothing,
        }

    @api.model
    def _call_detail(self, spec, args):
        """Return the card's line for a call: the tool and its arguments, quoted."""
        arguments = json.dumps(args, sort_keys=True, ensure_ascii=False)
        return self.env._(
            "Run %(tool)s with %(arguments)s.",
            tool=spec.name,
            arguments=quoted(arguments[:_ARGUMENTS_CHARS]),
        )

    @api.model
    def _what_changes(self, spec, args):
        """Return what the card says the call changes, or None for no own rule."""
        vals = args.get("vals") if isinstance(args.get("vals"), dict) else {}
        if spec.name in _CREATES_INSTANCES:
            if self._acts_on_production(spec, args):
                return self.env._(
                    "Create a production instance. It takes CPU, memory and disk on "
                    "the server."
                )
            name = (
                vals.get("name") or args.get("staging_name") or args.get("instance_id")
            )
            return self.env._(
                "Create the instance %s. It takes CPU, memory and disk on the server.",
                quoted(name),
            )
        if spec.name == "update_project" and isinstance(vals.get("repos"), list):
            project = self.env["cloud.project"].sudo().browse(args.get("project_id"))
            return self.env._(
                "Change the repositories of project %(project)s: it adds %(added)s, "
                "removes %(removed)s and moves %(moved)s. Instances created from the "
                "project afterwards use the new list.",
                project=quoted(project.exists().name or args.get("project_id")),
                **self._repos_change(project.exists().repo_ids, vals["repos"]),
            )
        if spec.name == "update_instance" and isinstance(vals.get("repos"), list):
            instance = self.env["cloud.instance"].sudo().browse(args.get("instance_id"))
            return self.env._(
                "Change the repositories of instance %(instance)s: it adds %(added)s, "
                "removes %(removed)s and moves %(moved)s. Its next deploy uses them.",
                instance=quoted(instance.exists().name or args.get("instance_id")),
                **self._repos_change(instance.exists().repo_ids, vals["repos"]),
            )
        return None

    @api.model
    def _extra_confirmation(self, spec, args, tainted=False):
        """Return what the assistant's own rules make the person confirm, or None.

        :param bool tainted: the task read content others can write since
            the person's last message
        """
        if spec.read_only:
            return None
        texts = []
        changes = self._what_changes(spec, args)
        if changes:
            texts.append(changes)
        production = self._production_target(args)
        if production:
            texts.append(self._production_text(production))
        if tainted and (TAINT_COVERS_OWN_WORK or not self._own_work(spec, args)):
            texts.append(
                self.env._(
                    "Since your last message the assistant read content other people "
                    "can write (logs, files, mail, a command's output): check that "
                    "this is what you asked for."
                )
            )
        if texts or spec.name not in _UNASKED:
            texts.append(self._call_detail(spec, args))
        return "\n\n".join(texts) if texts else None

    @api.model
    def _production_text(self, production):
        """Say which production a call reaches: by name if the user reads it.

        A production found through a server or a repository line may be one
        the user does not see: the card says there is one, not its name.
        """
        seen = production.with_env(self.env).filtered(
            lambda inst: inst.has_access("read")
        )
        if seen == production:
            return self.env._(
                "This acts on the production instance %s.",
                ", ".join(quoted(inst.name) for inst in seen),
            )
        return self.env._(
            "This acts on a production instance, one you may not see in the panel."
        )

    @api.model
    def _own_work(self, spec, args):
        """Return whether the call is the agent's work on a branch it created.

        Writing to a branch the user's agent created (the MCP still asks
        when production deploys from it or the repository is public), and
        deploying or rebuilding a staging that deploys from such a branch:
        what :data:`TAINT_COVERS_OWN_WORK` decides about.
        """
        Branch = self.env["incubacloud.mcp.agent.branch"]
        if spec.name in ("repo_create_branch", "repo_write_files"):
            key = _repo_key(args.get("url") or "")
            if not key or spec.name == "repo_create_branch":
                return bool(key)
            repository = f"{key[0]}/{key[1]}"
            return Branch._created_by_user(repository, args.get("branch") or "")
        if spec.name not in ("deploy_instance", "rebuild_instance"):
            return False
        instance = (
            self.env["cloud.instance"].sudo().browse(_ids(args.get("instance_id")))
        ).exists()
        if len(instance) != 1 or instance.environment != "staging":
            return False
        return any(
            (key := _repo_key(line.url))
            and Branch._created_by_user(f"{key[0]}/{key[1]}", line.branch)
            for line in instance.repo_ids
        )

    @api.model
    def _run_local(self, spec, args):
        """Run one of the assistant's own tools; return ``(text, is_error)``.

        The answer is cut to 64 KB and its secrets masked, as the MCP's are.
        """
        try:
            args = validate(spec.input_schema, args)
            result = getattr(self, spec.handler)(args)
        except (SchemaError, SourceError, UserError) as exc:
            return str(exc), True
        result, _cut = fit_answer(result)
        scrubbed = self.env["incubacloud.mcp.secrets"]._scrubber().value(result)
        return json.dumps(scrubbed, ensure_ascii=False), False

    @api.model
    def _root(self):
        """Return core's repository folder, or say there is none here."""
        root = core_root()
        if not root:
            raise UserError(
                f"The panel's code is not on this installation; the documentation is "
                f"at {DOCS_URL}."
            )
        return root

    @api.model
    def _tool_search_docs(self, args):
        """Search the user documentation; its link when nothing matches."""
        found = search_docs(self._root(), args["query"], args.get("limit") or 5)
        if not found["sections"]:
            found["documentation"] = DOCS_URL
        return found

    @api.model
    def _tool_search_code(self, args):
        """Search core's public code."""
        return search_code(
            self._root(), args["text"], args.get("path") or "", args.get("limit") or 30
        )

    @api.model
    def _tool_read_code(self, args):
        """Read a file or list a folder of core's public code."""
        return read_source(
            self._root(),
            args.get("path") or "",
            args.get("start_line") or 1,
            args.get("lines") or 400,
        )
