"""Code tools: read a repository, branch, commit, pull requests, repositories.

They work on a repository of the user's projects or instances only, with
the panel's GitHub access. Reading goes as every repository read of the
panel goes: the App, then the personal access token, then anonymously.
Writing goes with the App and, when GitHub refuses it (the App the panel
creates may only read contents), with the personal access token. A
developer's tools; the branch rule: a new branch needs no approval,
writing to one that existed before does, and the approval says when
production deploys from it. Writing to a public repository, or opening a
pull request in one, asks too.
"""

import base64
import logging
import re
import urllib.parse
from contextlib import suppress

from odoo import api, models
from odoo.exceptions import UserError

from odoo.addons.incubacloud.controllers._data_load._routes_github import (
    _github_import_clients,
)
from odoo.addons.incubacloud.github.client import GitHubAPIError, GitHubAppClient
from odoo.addons.incubacloud.models._repo_requirements import (
    _parse_github_repo_path,
    is_safe_git_ref,
)

from ..github_write import GitHubWriter, path_of, segment
from ..protocol.limits import cap_text
from ..protocol.tool_spec import (
    OFFSET,
    ToolSpec,
    boolean,
    obj,
    record_id,
    string,
)
from .mcp_tools import quoted

_logger = logging.getLogger(__name__)

_DEVELOPER = "group_cloud_developer"
_URL = string("The repository's https URL on GitHub.", max_length=500)
_BRANCH = string("The branch.", max_length=200)
#: Files one commit may write.
MAX_FILES = 50
#: Characters one commit may write, all its files together: the call fits
#: in the 256 KB an agent's request to ``/mcp`` may weigh.
MAX_COMMIT_CHARS = 180_000
#: A repository's name, as GitHub accepts it.
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
#: The SSH user of git addresses (``git+ssh://git@github.com/…`` in a
#: requirements file): shaped like a credential, never one.
_GIT_SSH_USER = "://git@"

TOOLS = (
    ToolSpec(
        "repo_read",
        "Read a file of a GitHub repository's branch, or list a folder (path empty "
        "for the top). Only repositories of the user's projects and instances. Long "
        "files come in pages: pass the next_offset back.",
        _DEVELOPER,
        obj(
            {
                "url": _URL,
                "branch": _BRANCH,
                "path": string("The file or folder.", max_length=500),
                "offset": OFFSET,
            },
            required=("url", "branch"),
        ),
        handler="_tool_repo_read",
        read_only=True,
        untrusted=True,
    ),
    ToolSpec(
        "repo_create_branch",
        "Create a branch of a GitHub repository from another one (by default the "
        "repository's default branch). A new branch needs no approval, unless the "
        "repository is public; name it assistant/<short-description> unless the user "
        "names it.",
        _DEVELOPER,
        obj(
            {"url": _URL, "branch": _BRANCH, "from_branch": _BRANCH},
            required=("url", "branch"),
        ),
        handler="_tool_repo_create_branch",
        confirm="conditional",
    ),
    ToolSpec(
        "repo_write_files",
        "Write files to a branch in one commit: each file with its path and its whole "
        "new content, or delete=true. At most 50 files and 180,000 characters in all. "
        "Writing to a branch this user's agent did not create, to a branch a "
        "production instance deploys from, or to a public repository asks the user "
        "first.",
        _DEVELOPER,
        obj(
            {
                "url": _URL,
                "branch": _BRANCH,
                "message": string("The commit message.", max_length=500),
                "files": {
                    "type": "array",
                    "description": "The files.",
                    "items": obj(
                        {
                            "path": string("Path in the repository.", max_length=300),
                            "content": string(
                                "The whole new text.", max_length=MAX_COMMIT_CHARS
                            ),
                            "delete": boolean("Delete the file instead."),
                        },
                        required=("path",),
                    ),
                },
            },
            required=("url", "branch", "message", "files"),
        ),
        handler="_tool_repo_write_files",
        confirm="conditional",
    ),
    ToolSpec(
        "repo_open_pull_request",
        "Open a pull request from a branch into another (by default the repository's "
        "default branch); when one is open already, answer that one. In a public "
        "repository, or when the project makes a preview of production for each pull "
        "request, the user approves it first.",
        _DEVELOPER,
        obj(
            {
                "url": _URL,
                "branch": _BRANCH,
                "base": _BRANCH,
                "title": string("The title.", max_length=200),
                "body": string("The description.", max_length=20_000),
            },
            required=("url", "branch", "title"),
        ),
        handler="_tool_repo_open_pull_request",
        confirm="conditional",
    ),
    ToolSpec(
        "repo_create",
        "Create a private GitHub repository with the panel's personal access token "
        "and add it to a project's repositories (branch main). The user approves it "
        "first. Without a token that may create repositories, the answer says how "
        "the user creates it on GitHub; then connect it with project_connect_repo.",
        _DEVELOPER,
        obj(
            {
                "project_id": record_id("The project."),
                "name": string("The repository's name.", max_length=100),
                "owner": string(
                    "An organization; empty for the token's own account.",
                    max_length=100,
                ),
                "private": boolean("False makes it public. Private by default."),
            },
            required=("project_id", "name"),
        ),
        handler="_tool_repo_create",
        confirm="always",
    ),
    ToolSpec(
        "project_connect_repo",
        "Add an existing GitHub repository to a project's repositories. The user "
        "approves it first: instances created from the project afterwards clone it.",
        _DEVELOPER,
        obj(
            {
                "project_id": record_id("The project."),
                "url": _URL,
                "branch": _BRANCH,
                "addons": string(
                    "Comma-separated modules to install from it; empty for all.",
                    max_length=500,
                ),
            },
            required=("project_id", "url"),
        ),
        handler="_tool_project_connect_repo",
        confirm="always",
    ),
)


def _repo_key(url):
    """Return GitHub's ``(owner, name)`` of *url*, in lower case, or None.

    Every spelling the panel accepts gives the same key: https or
    ``git@github.com:``, with or without ``www.``, ``.git`` or a final
    slash, in any case.
    """
    try:
        owner, name = _parse_github_repo_path(url)
    except ValueError:
        return None
    return owner.lower(), name.lower()


def _check_repo_path(path):
    """Refuse a path a commit or a read must not use.

    Not empty, relative, without ``..``, backslashes or NUL, at most 300
    characters, no ``.git`` folder at any depth and nothing under
    ``.github/``, in any case: a workflow runs with the repository's
    secrets.

    :param str path: the path as the agent gave it (attacker-controlled)
    :raises UserError: naming the problem
    """
    parts = path.split("/")
    if (
        not path
        or len(path) > 300
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or ".." in parts
        or "" in parts
        or parts[0].lower() == ".github"
        or ".git" in (part.lower() for part in parts)
    ):
        raise UserError(
            "A path is relative to the repository, without '..', and never under "
            f".git/ or .github/: {quoted(path)}."
        )


class McpCatalogCode(models.AbstractModel):
    """The code tools; they call no route of their own."""

    _inherit = "incubacloud.mcp.catalog"

    @api.model
    def _tool_specs(self):
        """Add the code tools."""
        return super()._tool_specs() + list(TOOLS)


class McpToolsCode(models.AbstractModel):
    """Handlers and approvals of the code tools."""

    _inherit = "incubacloud.mcp.tools"

    # ── Helpers ────────────────────────────────────────────────────────────

    @api.model
    def _repo_of(self, url):
        """Return ``(owner, name)`` of a GitHub URL.

        :raises UserError: when it is not one
        """
        try:
            return _parse_github_repo_path(url)
        except ValueError as exc:
            raise UserError(
                self.env._(
                    "%s is not a GitHub repository address "
                    "(https://github.com/<owner>/<name>).",
                    quoted(url),
                )
            ) from exc

    @api.model
    def _authorized_repo(self, url, write=False):
        """Return ``(owner, name)`` of *url*, a repository the user works on.

        The panel's GitHub access reaches more than the user does: any
        repository its App or token may read, other customers' and the
        panel's own. A code tool reads only a repository of a project, or
        of an instance, the user reads under their own record rules, and
        writes only to one of a project: an instance's own lines also
        carry its dependencies (OCA, third parties), read but not changed
        here. ``repo_create`` and ``project_connect_repo`` make that link
        themselves, once the user approves.

        :param bool write: the call writes to the repository
        :raises UserError: when *url* is not a GitHub address, or no
            project (or, to read, instance) of the user's has it
        """
        owner, name = self._repo_of(url)
        key = (owner.lower(), name.lower())
        models_ = ("cloud.project.repo",)
        if not write:
            models_ += ("cloud.instance.repo",)
        for model in models_:
            lines = self.env[model].search([("url", "ilike", name)])
            if any(_repo_key(line.url) == key for line in lines):
                return owner, name
        if write:
            raise UserError(
                self.env._(
                    "%s is not a repository of a project you can see in the panel: "
                    "the assistant writes only there. Connect it to the project "
                    "first (project_connect_repo).",
                    quoted(url),
                )
            )
        raise UserError(
            self.env._(
                "%s is not a repository of a project or an instance you can see in "
                "the panel. Connect it to a project first (project_connect_repo).",
                quoted(url),
            )
        )

    @api.model
    def _check_branch(self, branch):
        """Refuse a branch name git or the panel would not accept."""
        if not is_safe_git_ref(branch):
            raise UserError(
                self.env._("%s is not a valid branch name.", quoted(branch))
            )

    @api.model
    def _repo_get(self, endpoint):
        """GET *endpoint* with the App, then the PAT, then anonymously.

        A credential that fails in any way (GitHub refuses it, the App's
        key is broken, the network) moves on to the next one.

        :raises UserError: when none of them may read it
        """
        for client in _github_import_clients(self.env):
            try:
                return client.get(endpoint)
            except GitHubAPIError:
                continue
            except Exception:  # noqa: BLE001 - a broken credential is skipped
                _logger.warning("code tools: a GitHub client failed", exc_info=True)
        raise UserError(
            self.env._(
                "GitHub did not let the panel read this. Check the address and the "
                "branch, and that the panel's GitHub App or token reaches the "
                "repository."
            )
        )

    @api.model
    def _writers(self):
        """Return the writers to try: the App's, then the PAT's."""
        service = self.env["cloud.github.credential.service"].sudo()
        writers = []
        with suppress(Exception):
            app = GitHubAppClient(service.get_credentials())
            writers.append(GitHubWriter(app.get_installation_token()))
        pat = service.get_pat()
        if pat:
            writers.append(GitHubWriter(pat))
        return writers

    @api.model
    def _with_writer(self, operation):
        """Run ``operation(writer)`` with the first credential GitHub accepts.

        A refusal of access (401, 403, 404) moves on to the next
        credential; any other answer (422: it exists already) is the
        answer. GitHub answers 404 both for what does not exist and for
        what a credential may not see: the message says both.

        :raises GitHubAPIError: GitHub's answer other than a refusal
        :raises UserError: when no credential may write there
        """
        not_found = False
        for writer in self._writers():
            try:
                return operation(writer)
            except GitHubAPIError as exc:
                if exc.status_code not in (401, 403, 404):
                    raise
                not_found = not_found or exc.status_code == 404
        if not_found:
            raise UserError(
                self.env._(
                    "GitHub did not find this repository or branch with the panel's "
                    "access. Check the address and the branch; to write, the panel "
                    "needs a personal access token that can write to it, in "
                    "Settings → GitHub."
                )
            )
        raise UserError(
            self.env._(
                "The panel's GitHub access cannot write to this repository. Save a "
                "personal access token that can write to it in Settings → GitHub: the "
                "panel's GitHub App only reads code."
            )
        )

    # ── repo_read ──────────────────────────────────────────────────────────

    @api.model
    def _prepare_repo_read(self, args):
        """Refuse a repository the user does not work on, before GitHub is asked."""
        self._authorized_repo(args["url"])
        return args

    @api.model
    def _tool_repo_read(self, args):
        """A file's text, in pages, or a folder's entries."""
        owner, name = self._authorized_repo(args["url"])
        self._check_branch(args["branch"])
        path = (args.get("path") or "").strip("/")
        if path:
            _check_repo_path(path)
        ref = urllib.parse.quote(args["branch"], safe="")
        data = self._repo_get(
            f"/repos/{segment(owner)}/{segment(name)}/contents/{path_of(path)}?ref={ref}"
        )
        if isinstance(data, list):
            entries = [
                {key: item.get(key) for key in ("name", "type", "size")}
                for item in data
            ]
            return {"ok": True, "path": path, "type": "dir", "entries": entries}
        if data.get("type") != "file":
            return {"ok": True, "path": path, "type": data.get("type") or "unknown"}
        if data.get("encoding") == "none":
            # Past 1 MB GitHub's contents API sends the file without its text.
            return {
                "ok": True,
                "path": path,
                "type": "too_large",
                "size": data.get("size"),
            }
        raw = base64.b64decode(data.get("content") or "")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {"ok": True, "path": path, "type": "binary", "size": len(raw)}
        return {
            "ok": True,
            "path": path,
            "type": "file",
            **cap_text(text, args.get("offset") or 0),
        }

    # ── Before the approval: the repository, and whether it is public ──────

    @api.model
    def _with_visibility(self, args):
        """Check the repository, and bind the call to whether it is public.

        ``public`` joins the arguments before the approval's fingerprint:
        what goes to a public repository anyone reads, so the user
        approves it. Asked with the writers, which are about to write.
        """
        owner, name = self._authorized_repo(args["url"], write=True)
        about = self._with_writer(lambda writer: writer.repository(owner, name))
        return {**args, "public": about.get("private") is not True}

    @api.model
    def _prepare_repo_create_branch(self, args):
        """The repository is the user's; is it public."""
        return self._with_visibility(args)

    @api.model
    def _prepare_repo_write_files(self, args):
        """The repository is the user's; is it public."""
        return self._with_visibility(args)

    @api.model
    def _prepare_repo_open_pull_request(self, args):
        """The repository is the user's; is it public."""
        return self._with_visibility(args)

    @api.model
    def _repo_label(self, url):
        """Return ``owner/name`` of *url*, quoted for an approval."""
        owner, name = self._repo_of(url)
        return quoted(f"{owner}/{name}")

    # ── repo_create_branch ─────────────────────────────────────────────────

    @api.model
    def _confirm_repo_create_branch(self, args):
        """Ask before a branch in a public repository."""
        if not args["public"]:
            return None
        return self.env._(
            "Create branch %(branch)s in %(repo)s, a public repository: anyone can "
            "see it and what is written to it.",
            branch=quoted(args["branch"]),
            repo=self._repo_label(args["url"]),
        )

    @api.model
    def _tool_repo_create_branch(self, args):
        """Create the branch from another one's head; remember it as the agent's."""
        owner, name = self._authorized_repo(args["url"], write=True)
        branch = args["branch"]
        self._check_branch(branch)
        if args.get("from_branch"):
            self._check_branch(args["from_branch"])

        def create(writer):
            """Create it with *writer*; return the branch it came from and the sha."""
            source = (
                args.get("from_branch")
                or writer.repository(owner, name)["default_branch"]
            )
            sha = writer.head(owner, name, source)
            writer.create_branch(owner, name, branch, sha)
            return source, sha

        try:
            source, sha = self._with_writer(create)
        except GitHubAPIError as exc:
            if exc.status_code != 422 or "already exists" not in str(exc).lower():
                raise UserError(
                    self.env._(
                        "GitHub refused to create the branch (%s).", exc.status_code
                    )
                ) from exc
            return {
                "ok": False,
                "error": self.env._(
                    "Branch %s already exists: pick another name, or write to it "
                    "(the user approves it first).",
                    quoted(branch),
                ),
            }
        self.env["incubacloud.mcp.agent.branch"]._remember(f"{owner}/{name}", branch)
        return {"ok": True, "branch": branch, "from_branch": source, "sha": sha}

    # ── repo_write_files ───────────────────────────────────────────────────

    @api.model
    def _production_fed_by(self, url, branch):
        """Return the production instances that deploy from *branch* of *url*.

        Every active production with a repository line on that branch,
        however its address is spelt, frozen or not, with Auto-Rebuild or
        not: a push deploys at once with it, and at the next rebuild or
        unfreeze without. Read past the record rules on purpose: the
        approval must warn about a production the user does not see.
        """
        key = _repo_key(url)
        if key is None:
            return self.env["cloud.instance"]
        lines = (
            self.env["cloud.instance.repo"]
            .sudo()
            .search(
                [
                    ("branch", "=", branch),
                    ("instance_id.environment", "=", "production"),
                ]
            )
            .filtered(lambda line: _repo_key(line.url) == key)
        )
        return lines.instance_id.filtered("active")

    @api.model
    def _branch_exists(self, owner, name, branch):
        """Return whether *branch* exists; when nobody can tell, that it does.

        A 404 says the branch is missing only from a credential that sees
        the repository: otherwise the next credential is asked. Failing
        closed, an unknown branch counts as one that existed (the user
        approves the write).
        """
        for writer in self._writers():
            try:
                writer.head(owner, name, branch)
                return True
            except GitHubAPIError as exc:
                if exc.status_code not in (401, 403, 404):
                    raise
                if exc.status_code != 404:
                    continue
            try:
                writer.repository(owner, name)
            except GitHubAPIError:
                continue
            return False
        return True

    @api.model
    def _confirm_repo_write_files(self, args):
        """Ask for production, a public repository, or a branch that existed."""
        owner, name = self._repo_of(args["url"])
        branch = args["branch"]
        paths = ", ".join(quoted(item.get("path")) for item in args["files"][:10])
        if len(args["files"]) > 10:
            paths += self.env._(" and %s more", len(args["files"]) - 10)
        what = {
            "count": len(args["files"]),
            "branch": quoted(branch),
            "repo": quoted(f"{owner}/{name}"),
            "paths": paths,
        }
        production = self._production_fed_by(args["url"], branch)
        if production:
            text = self.env._(
                "Write %(count)s file(s) to branch %(branch)s of %(repo)s in one "
                "commit (%(paths)s). Production %(names)s deploys from this branch: "
                "this reaches production, at once when it rebuilds on every push, "
                "otherwise at its next rebuild.",
                names=", ".join(quoted(inst.name) for inst in production),
                **what,
            )
            if args["public"]:
                text += " " + self.env._(
                    "It is a public repository too: anyone can read what is written."
                )
            return text
        if args["public"]:
            return self.env._(
                "Write %(count)s file(s) to branch %(branch)s of %(repo)s in one "
                "commit (%(paths)s). %(repo)s is a public repository: anyone can read "
                "what is written.",
                **what,
            )
        created = self.env["incubacloud.mcp.agent.branch"]._created_by_user(
            f"{owner}/{name}", branch
        )
        if created or not self._branch_exists(owner, name, branch):
            return None
        return self.env._(
            "Write %(count)s file(s) to the existing branch %(branch)s of %(repo)s in "
            "one commit (%(paths)s).",
            **what,
        )

    @api.model
    def _tool_repo_write_files(self, args):
        """Write the files in one commit, after checking paths, size and secrets."""
        owner, name = self._authorized_repo(args["url"], write=True)
        branch = args["branch"]
        self._check_branch(branch)
        files = args["files"]
        if not 1 <= len(files) <= MAX_FILES:
            raise UserError(
                self.env._("One commit writes from 1 to %s files.", MAX_FILES)
            )
        if sum(len(item.get("content") or "") for item in files) > MAX_COMMIT_CHARS:
            raise UserError(
                self.env._(
                    "One commit writes at most %s characters: split it in several.",
                    MAX_COMMIT_CHARS,
                )
            )
        scrubber = self.env["incubacloud.mcp.secrets"]._scrubber()
        for item in files:
            _check_repo_path(item["path"])
            if not item.get("delete") and "content" not in item:
                raise UserError(
                    self.env._("File %s has no content.", quoted(item["path"]))
                )
            content = (item.get("content") or "").replace(_GIT_SSH_USER, "://")
            if scrubber.text(content) != content:
                raise UserError(
                    self.env._(
                        "File %s holds a secret of the panel: it is never written "
                        "to a repository.",
                        quoted(item["path"]),
                    )
                )
        try:
            commit = self._with_writer(
                lambda writer: writer.commit_files(
                    owner, name, branch, args["message"], files
                )
            )
        except GitHubAPIError as exc:
            raise UserError(
                self.env._(
                    "GitHub refused the commit (%s). If the branch moved meanwhile, "
                    "read it again and write once more.",
                    exc.status_code,
                )
            ) from exc
        return {
            "ok": True,
            "branch": branch,
            "commit": commit["sha"],
            "url": commit.get("html_url") or "",
            "files": [item["path"] for item in files],
        }

    # ── repo_open_pull_request ─────────────────────────────────────────────

    @api.model
    def _previews_for(self, url):
        """Return the productions a pull request on *url* would copy to a preview.

        As the pull-request webhook finds them (``cloud.github.event``): an
        unfrozen line of a deployed production whose project makes
        previews. Read past the record rules, as ``_production_fed_by``.
        Without the webhook's own gate per production
        (``_pr_preview_allowed``): a card for a preview the webhook would
        skip is one card too many, never one missing.
        """
        key = _repo_key(url)
        if key is None:
            return self.env["cloud.instance"]
        lines = (
            self.env["cloud.instance.repo"]
            .sudo()
            .search(
                [
                    ("commit_sha", "in", (False, "")),
                    ("instance_id.environment", "=", "production"),
                    ("instance_id.deployed", "=", True),
                    ("instance_id.project_id.pr_reviews_enabled", "=", True),
                ]
            )
            .filtered(lambda line: _repo_key(line.url) == key)
        )
        return lines.instance_id

    @api.model
    def _confirm_repo_open_pull_request(self, args):
        """Ask when the pull request makes a preview, or is public."""
        what = {
            "branch": quoted(args["branch"]),
            "repo": self._repo_label(args["url"]),
        }
        previews = self._previews_for(args["url"])
        if previews:
            return self.env._(
                "Open a pull request from branch %(branch)s in %(repo)s. The panel "
                "then creates a preview staging of production %(names)s, with a "
                "copy of its data.",
                names=", ".join(quoted(inst.name) for inst in previews),
                **what,
            )
        if args["public"]:
            return self.env._(
                "Open a pull request from branch %(branch)s in %(repo)s, a public "
                "repository: anyone can read it.",
                **what,
            )
        return None

    @api.model
    def _tool_repo_open_pull_request(self, args):
        """Open the pull request, or answer the one already open."""
        owner, name = self._authorized_repo(args["url"], write=True)
        branch = args["branch"]
        self._check_branch(branch)

        def open_pull(writer):
            """Open it with *writer*; answer the open one on a 422."""
            base = args.get("base") or writer.repository(owner, name)["default_branch"]
            try:
                return writer.open_pull(
                    owner, name, branch, base, args["title"], args.get("body") or ""
                )
            except GitHubAPIError as exc:
                found = (
                    writer.find_pull(owner, name, f"{owner}:{branch}")
                    if exc.status_code == 422
                    else None
                )
                if not found:
                    raise
                return found

        try:
            pull = self._with_writer(open_pull)
        except GitHubAPIError as exc:
            raise UserError(
                self.env._("GitHub refused the pull request (%s).", exc.status_code)
            ) from exc
        return {"ok": True, "number": pull["number"], "url": pull["html_url"]}

    # ── Connecting a repository ────────────────────────────────────────────

    @api.model
    def _project_label(self, project_id):
        """Return the project's name as an approval shows it, or its id."""
        project = self.env["cloud.project"].browse(project_id).exists()
        return quoted(project.name) if project else f"#{project_id}"

    @api.model
    def _connect_repo(self, project_id, url, branch, addons):
        """Add *url* to the project's repositories through the project's own route.

        ``/cloud/save_project`` replaces the whole list, so the list sent
        is the project's own plus the new line; the route checks the role
        and the project's access as in the panel. A repository already
        there under another spelling of its address is not added again.

        :param str url: the canonical address (``https://github.com/o/n``)
        """
        project = self._route("/cloud/get_project", project_id=project_id)
        if project.get("ok") is False:
            return project
        repos = project.get("repos") or []
        key = _repo_key(url)
        if any(_repo_key(repo["url"]) == key for repo in repos):
            return {"ok": True, "already_connected": True}
        lines = [
            {
                field: repo[field]
                for field in ("id", "url", "branch", "addons", "excludes", "commit_sha")
            }
            for repo in repos
        ]
        lines.append(
            {
                "url": url,
                "branch": branch or "main",
                "addons": addons or "",
                "excludes": "",
                "commit_sha": "",
            }
        )
        return self._route(
            "/cloud/save_project", project_id=project_id, vals={"repos": lines}
        )

    @api.model
    def _confirm_project_connect_repo(self, args):
        """What connecting a repository changes in the project."""
        return self.env._(
            "Add %(url)s (branch %(branch)s) to the repositories of project "
            "%(project)s. Instances created from the project afterwards clone it; "
            "existing instances keep theirs.",
            url=quoted(args["url"]),
            branch=quoted(args.get("branch") or "main"),
            project=self._project_label(args["project_id"]),
        )

    @api.model
    def _tool_project_connect_repo(self, args):
        """Check the address and the branch, then connect it under one spelling."""
        owner, name = self._repo_of(args["url"])
        branch = args.get("branch") or "main"
        self._check_branch(branch)
        return self._connect_repo(
            args["project_id"],
            f"https://github.com/{owner}/{name}",
            branch,
            args.get("addons") or "",
        )

    # ── repo_create ────────────────────────────────────────────────────────

    @api.model
    def _confirm_repo_create(self, args):
        """What creating the repository does on GitHub and in the project."""
        return self.env._(
            "Create the %(visibility)s repository %(name)s on GitHub, in "
            "%(owner)s, with the panel's personal access token, and add it to the "
            "repositories of project %(project)s (branch main).",
            visibility=self.env._("private")
            if args.get("private", True)
            else self.env._("public"),
            name=quoted(args["name"]),
            owner=quoted(args["owner"])
            if args.get("owner")
            else self.env._("the token's own account"),
            project=self._project_label(args["project_id"]),
        )

    @api.model
    def _check_can_connect(self, project_id):
        """Raise unless the user may add a repository to the project.

        Checked before GitHub, so a refusal leaves no repository behind.

        :raises UserError: when the project does not exist
        :raises AccessError: when the user may not change it
        """
        project = self.env["cloud.project"].browse(project_id).exists()
        if not project:
            raise UserError(self.env._("Project not found"))
        project.check_access("write")
        self.env["cloud.project.repo"].check_access("create")

    @api.model
    def _tool_repo_create(self, args):
        """Create the repository with the PAT, then connect it."""
        if not _REPO_NAME_RE.match(args["name"]):
            raise UserError(
                self.env._(
                    "A repository's name holds letters, digits, dots, hyphens and "
                    "underscores only."
                )
            )
        self._check_can_connect(args["project_id"])
        guide = self.env._(
            "This panel's GitHub access cannot create repositories. On GitHub, use "
            "New repository (private, with a README), then ask me to connect it to "
            "the project."
        )
        pat = self.env["cloud.github.credential.service"].sudo().get_pat()
        if not pat:
            return {"ok": False, "error": guide}
        writer = GitHubWriter(pat)
        try:
            login = writer.login()
            owner = (args.get("owner") or login).strip()
            repo = writer.create_repository(
                owner,
                args["name"],
                args.get("private", True),
                own_account=owner.lower() == login.lower(),
            )
        except GitHubAPIError as exc:
            if exc.status_code == 422:
                return {
                    "ok": False,
                    "error": self.env._(
                        "A repository with that name already exists there: connect "
                        "it with project_connect_repo, or pick another name."
                    ),
                }
            return {"ok": False, "error": guide}
        branch = repo.get("default_branch") or "main"
        connected = self._connect_repo(args["project_id"], repo["html_url"], branch, "")
        return {
            "ok": connected.get("ok", True) is not False,
            "url": repo["html_url"],
            "branch": branch,
            "connected": connected,
        }
