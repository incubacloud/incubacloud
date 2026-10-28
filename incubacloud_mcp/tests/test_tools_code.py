"""The code tools: read, branch, write, pull requests and repositories."""

import base64
import dataclasses
import functools
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.incubacloud.github.client import (
    GitHubAnonymousClient,
    GitHubAPIError,
    GitHubAppClient,
)
from odoo.addons.incubacloud_mcp.github_write import GitHubWriter

from .common import (
    HOST_PASSWORD,
    ConfirmMixin,
    McpHttpMixin,
    McpUsersMixin,
    PanelFixtureMixin,
)

REPO = "https://github.com/acme/odoo-addons"
README = "# Acme addons\n"


@dataclasses.dataclass
class GitHubState:
    """What the tests' GitHub holds, and the calls it got (data only)."""

    branches: dict = dataclasses.field(default_factory=lambda: {"main": "sha-main"})
    private: bool = True
    calls: list = dataclasses.field(default_factory=list)
    #: The token of each call, in order.
    tokens: list = dataclasses.field(default_factory=list)
    #: Tokens GitHub lets read only.
    read_only_tokens: set = dataclasses.field(default_factory=set)
    #: Whether GitHub refuses every credential the read of a branch's head.
    refuse_heads: bool = False


def answer_github(state, writer, method, endpoint, data=None):
    """Answer one ``GitHubWriter.request`` (patched with autospec) from *state*."""
    state.calls.append((method, endpoint, data))
    state.tokens.append(writer._token)
    if method != "GET" and writer._token in state.read_only_tokens:
        raise GitHubAPIError(403, "Resource not accessible by integration")
    prefix = "/repos/acme/odoo-addons"
    if method == "GET" and endpoint == prefix:
        return {"default_branch": "main", "html_url": REPO, "private": state.private}
    if method == "GET" and endpoint.startswith(f"{prefix}/git/ref/heads/"):
        if state.refuse_heads:
            raise GitHubAPIError(403, "Resource not accessible by integration")
        branch = endpoint.rsplit("/git/ref/heads/", 1)[1].replace("%2F", "/")
        if branch not in state.branches:
            raise GitHubAPIError(404, "Not Found")
        return {"object": {"sha": state.branches[branch]}}
    if method == "POST" and endpoint == f"{prefix}/git/refs":
        branch = data["ref"].removeprefix("refs/heads/")
        if branch in state.branches:
            raise GitHubAPIError(422, '{"message": "Reference already exists"}')
        state.branches[branch] = data["sha"]
        return {"ref": data["ref"]}
    if method == "GET" and "/git/commits/" in endpoint:
        return {"tree": {"sha": "tree-base"}}
    if method == "POST" and endpoint.endswith("/git/trees"):
        return {"sha": "tree-new"}
    if method == "POST" and endpoint.endswith("/git/commits"):
        return {"sha": "commit-new", "html_url": f"{REPO}/commit/new"}
    if method == "PATCH" and "/git/refs/heads/" in endpoint:
        return {"object": {"sha": data["sha"]}}
    if method == "POST" and endpoint.endswith("/pulls"):
        return {"number": 7, "html_url": f"{REPO}/pull/7"}
    if method == "GET" and endpoint == "/user":
        return {"login": "acme"}
    if method == "POST" and endpoint == "/user/repos":
        return {
            "html_url": f"https://github.com/acme/{data['name']}",
            "default_branch": "main",
        }
    raise AssertionError(f"unexpected GitHub call {method} {endpoint}")


@tagged("post_install", "-at_install")
class TestCodeTools(
    PanelFixtureMixin, McpUsersMixin, McpHttpMixin, ConfirmMixin, HttpCase
):
    """A developer's agent works on the project's repository."""

    def setUp(self):
        """A developer and a consultant on a project with a repository; their keys."""
        super().setUp()
        self._resource_here()
        self._panel_fixture()
        self.project.sudo().with_context(skip_apply_requirements=True).repo_ids = [
            (0, 0, {"url": REPO, "branch": "main"})
        ]
        self.developer = self._cloud_user("mcp-code-dev", group="group_cloud_developer")
        self.consultant = self._cloud_user(
            "mcp-code-consultant", group="group_cloud_consultant"
        )
        self.project.member_ids = [(4, self.developer.id), (4, self.consultant.id)]
        self.key = self._api_key(self.developer)
        self.consultant_key = self._api_key(self.consultant)
        self.github = GitHubState()
        writer = patch.object(
            GitHubWriter,
            "request",
            autospec=True,
            side_effect=functools.partial(answer_github, self.github),
        )
        writer.start()
        self.addCleanup(writer.stop)
        # The App answers its installation token without reaching GitHub.
        token = patch.object(
            GitHubAppClient,
            "get_installation_token",
            autospec=True,
            return_value="ghs_app-installation-token",
        )
        token.start()
        self.addCleanup(token.stop)

    def _read_as_app(self, answer):
        """Make the App read *answer* (and anonymous reads fail)."""
        reads = patch.object(GitHubAppClient, "get", autospec=True, return_value=answer)
        anonymous = patch.object(
            GitHubAnonymousClient,
            "get",
            autospec=True,
            side_effect=GitHubAPIError(404, "Not Found"),
        )
        for patcher in (reads, anonymous):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _read(self, path, url=REPO):
        """Call ``repo_read`` on *path* of ``main``; return the result."""
        return self._call_tool(
            self.key, "repo_read", {"url": url, "branch": "main", "path": path}
        )

    def test_a_file_of_a_branch_is_read_as_untrusted_text(self):
        """The file comes decoded, marked as untrusted data."""
        self._read_as_app(
            {"type": "file", "content": base64.b64encode(README.encode()).decode()}
        )
        result = self._read("README.md")
        self.assertFalse(result["isError"], result)
        self.assertIn("<untrusted_data>", result["content"][-1]["text"])
        payload = self._payload(result)
        self.assertEqual((payload["type"], payload["text"]), ("file", README))

    def test_a_folder_is_listed(self):
        """A folder answers its entries: name, type and size."""
        self._read_as_app(
            [{"name": "acme_base", "type": "dir", "size": 0, "sha": "x", "url": "y"}]
        )
        payload = self._payload(self._read(""))
        self.assertEqual(
            payload["entries"], [{"name": "acme_base", "type": "dir", "size": 0}]
        )

    def test_a_file_over_a_megabyte_says_so(self):
        """GitHub gives no content past 1 MB: the answer says it is too large."""
        self._read_as_app(
            {"type": "file", "encoding": "none", "content": "", "size": 2_000_000}
        )
        payload = self._payload(self._read("big.sql"))
        self.assertEqual((payload["type"], payload["size"]), ("too_large", 2_000_000))

    def test_a_path_that_climbs_out_is_refused_before_github(self):
        """``..`` in the path is refused; GitHub is never asked."""
        self._read_as_app({})
        result = self._read("a/../b")
        self.assertTrue(result["isError"])
        GitHubAppClient.get.assert_not_called()

    def test_a_repository_of_no_project_is_refused_before_github(self):
        """The panel's access reaches more repositories than the user works on."""
        self._read_as_app({})
        result = self._read("README.md", url="https://github.com/other/private-code")
        self.assertTrue(result["isError"])
        self.assertIn("project_connect_repo", result["content"][-1]["text"])
        GitHubAppClient.get.assert_not_called()

    def test_the_repository_is_known_however_its_address_is_spelt(self):
        """``git@github.com:ACME/Odoo-Addons.git`` is the project's repository."""
        self._read_as_app(
            {"type": "file", "content": base64.b64encode(README.encode()).decode()}
        )
        result = self._read("README.md", url="git@github.com:ACME/Odoo-Addons.git")
        self.assertFalse(result["isError"], result)

    def test_a_file_that_holds_a_panel_secret_is_read_masked(self):
        """Whatever a repository holds, the panel's secrets do not come out."""
        text = f"DB_PASSWORD = '{HOST_PASSWORD}'\n"
        self._read_as_app(
            {"type": "file", "content": base64.b64encode(text.encode()).decode()}
        )
        result = self._read("conf.py")
        self.assertFalse(result["isError"], result)
        self._assert_no_secret(result["content"][-1]["text"])

    def test_the_code_tools_are_a_developer_s(self):
        """A consultant neither sees nor runs them."""
        for name, arguments in (("repo_read", {"url": REPO, "branch": "main"}),):
            with self.subTest(name):
                self._assert_refused_by_role(self.consultant_key, name, arguments)
