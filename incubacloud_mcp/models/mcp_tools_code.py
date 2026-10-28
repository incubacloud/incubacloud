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
from ..protocol.tool_spec import OFFSET, ToolSpec, obj, string
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
