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
from odoo.addons.incubacloud_mcp.models.mcp_tools_code import MAX_COMMIT_CHARS

from .common import (
    FORM_CAPABILITIES,
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
        for name, arguments in (
            ("repo_read", {"url": REPO, "branch": "main"}),
            ("repo_create_branch", {"url": REPO, "branch": "assistant/x"}),
        ):
            with self.subTest(name):
                self._assert_refused_by_role(self.consultant_key, name, arguments)

    def _feed(self, instance, branch, rebuild=True, deployed=False):
        """Make *instance* deploy from *branch* of the repository."""
        # No requirements.txt fetched from GitHub: the panel's form does the same.
        instance.sudo().with_context(skip_apply_requirements=True).write(
            {
                "auto_rebuild": rebuild,
                "repo_ids": [(0, 0, {"url": REPO, "branch": branch})],
            }
        )
        if deployed:
            # As a finished deploy leaves it; ``_transition`` needs the deploy.
            instance.sudo().with_context(cloud_state_transition=True).write(
                {"state": "deployed"}
            )

    def _write(self, branch, files=None, capabilities=None, **params):
        """Call ``repo_write_files`` on *branch*; return the result."""
        files = files or [{"path": "acme_code/__init__.py", "content": ""}]
        return self._call_tool(
            self.key,
            "repo_write_files",
            {"url": REPO, "branch": branch, "message": "Add acme_code", "files": files},
            capabilities,
            **params,
        )

    def _asked(self, name, arguments):
        """Call *name* with a form-capable client; return the approval's message."""
        first = self._call_tool(self.key, name, arguments, FORM_CAPABILITIES)
        self.assertEqual(first.get("resultType"), "input_required", first)
        return first["inputRequests"]["confirm"]["params"]["message"]

    def _branch(self, branch="assistant/x"):
        """Create *branch* as the developer's agent (no approval: private)."""
        return self._call_tool(
            self.key, "repo_create_branch", {"url": REPO, "branch": branch}
        )

    def test_a_new_branch_needs_no_approval_and_is_remembered(self):
        """The branch starts at the default branch's head; the agent's from now on."""
        payload = self._payload(self._branch())
        self.assertEqual(
            (payload["branch"], payload["from_branch"], payload["sha"]),
            ("assistant/x", "main", "sha-main"),
        )
        Branch = self.env["incubacloud.mcp.agent.branch"].with_user(self.developer)
        self.assertTrue(Branch._created_by_user("acme/odoo-addons", "assistant/x"))

    def test_an_existing_branch_is_not_created_again(self):
        """GitHub's 422 comes back as a refusal the agent can read."""
        result = self._branch("main")
        # An ``{ok: false, error}`` answer reaches the agent as its error's text.
        self.assertTrue(result["isError"])
        self.assertIn("already exists", result["content"][-1]["text"])

    def test_writing_to_the_agent_s_branch_needs_no_approval(self):
        """One commit, audited as the agent's."""
        self._branch()
        result = self._write("assistant/x", capabilities=FORM_CAPABILITIES)
        self.assertNotEqual(result.get("resultType"), "input_required", result)
        payload = self._payload(result)
        self.assertEqual(payload["commit"], "commit-new")
        audit = (
            self.env["cloud.audit.log"]
            .sudo()
            .search([("action", "=", "Agent: repo_write_files")], limit=1)
        )
        self.assertTrue(audit.via_client)

    def test_a_git_address_in_a_file_is_no_secret(self):
        """``git+ssh://git@github.com/…`` in a requirements file is written."""
        self._branch()
        line = "git+ssh://git@github.com/acme/lib.git@19.0#egg=acme_lib\n"
        result = self._write(
            "assistant/x", files=[{"path": "requirements.txt", "content": line}]
        )
        self.assertFalse(result["isError"], result)

    def test_writing_to_a_branch_that_existed_asks_first(self):
        """``main`` was there before: the approval names the branch."""
        message = self._asked(
            "repo_write_files",
            {
                "url": REPO,
                "branch": "main",
                "message": "Add acme_code",
                "files": [{"path": "acme_code/__init__.py", "content": ""}],
            },
        )
        self.assertIn('"main"', message)
        self.assertNotIn("production", message.lower())
        self.assertFalse([c for c in self.github.calls if c[0] == "POST"])

    def test_a_branch_production_deploys_from_says_so_even_the_agent_s(self):
        """With Auto-Rebuild or without, the approval names the production."""
        self._branch()
        for rebuild in (True, False):
            with self.subTest(rebuild=rebuild):
                self.production.sudo().repo_ids.unlink()
                self._feed(self.production, "assistant/x", rebuild=rebuild)
                first = self._write("assistant/x", capabilities=FORM_CAPABILITIES)
                self.assertEqual(first.get("resultType"), "input_required", first)
                message = first["inputRequests"]["confirm"]["params"]["message"]
                self.assertIn("production", message.lower())
                self.assertIn(self.production.name, message)

    def test_production_is_found_however_its_line_spells_the_address(self):
        """A production line with git@… and other letters still makes the card."""
        self._branch()
        self.production.sudo().with_context(skip_apply_requirements=True).write(
            {
                "repo_ids": [
                    (
                        0,
                        0,
                        {
                            "url": "git@github.com:Acme/Odoo-Addons.git",
                            "branch": "assistant/x",
                        },
                    )
                ]
            }
        )
        message = self._asked(
            "repo_write_files",
            {
                "url": REPO,
                "branch": "assistant/x",
                "message": "Add acme_code",
                "files": [{"path": "acme_code/__init__.py", "content": ""}],
            },
        )
        self.assertIn(self.production.name, message)

    def test_a_branch_nobody_can_tell_about_counts_as_one_that_existed(self):
        """Every credential refused the read of its head: the write asks."""
        self.github.refuse_heads = True
        message = self._asked(
            "repo_write_files",
            {
                "url": REPO,
                "branch": "feature/y",
                "message": "Add acme_code",
                "files": [{"path": "acme_code/__init__.py", "content": ""}],
            },
        )
        self.assertIn('"feature/y"', message)

    def test_a_repository_only_an_instance_uses_is_read_never_written(self):
        """An instance's dependency (OCA): readable, but no branch is made there."""
        other = "https://github.com/oca/web"
        self.staging.sudo().with_context(skip_apply_requirements=True).write(
            {"repo_ids": [(0, 0, {"url": other, "branch": "19.0"})]}
        )
        result = self._call_tool(
            self.key, "repo_create_branch", {"url": other, "branch": "assistant/x"}
        )
        self.assertTrue(result["isError"])
        self.assertIn("writes only there", result["content"][-1]["text"])
        self.assertFalse(self.github.calls)

    def test_a_staging_that_rebuilds_from_an_agent_branch_asks_nothing(self):
        """Only production turns the agent's branch into an approval."""
        self._branch()
        self._feed(self.staging, "assistant/x")
        result = self._write("assistant/x", capabilities=FORM_CAPABILITIES)
        self.assertNotEqual(result.get("resultType"), "input_required", result)

    def test_in_a_public_repository_every_write_asks_first(self):
        """A branch, a commit or a pull request there is public: the user approves."""
        self.github.private = False
        message = self._asked(
            "repo_create_branch", {"url": REPO, "branch": "assistant/x"}
        )
        self.assertIn("public", message)
        self._confirmed_call(
            self.key, "repo_create_branch", {"url": REPO, "branch": "assistant/x"}
        )
        for name, arguments in (
            (
                "repo_write_files",
                {
                    "url": REPO,
                    "branch": "assistant/x",
                    "message": "Add acme_code",
                    "files": [{"path": "acme_code/__init__.py", "content": ""}],
                },
            ),
            (
                "repo_open_pull_request",
                {"url": REPO, "branch": "assistant/x", "title": "Internal code"},
            ),
        ):
            with self.subTest(name):
                self.assertIn("public", self._asked(name, arguments))

    def test_a_pull_request_that_makes_a_preview_asks_first(self):
        """The project copies production for each pull request: the user approves."""
        self.project.sudo().pr_reviews_enabled = True
        self._feed(self.production, "main", deployed=True)
        message = self._asked(
            "repo_open_pull_request",
            {"url": REPO, "branch": "assistant/x", "title": "Internal code"},
        )
        self.assertIn(self.production.name, message)
        self.assertIn("preview", message)

    def test_workflows_paths_out_secrets_and_too_much_are_refused(self):
        """Nothing is written: a workflow, a path out, a panel secret, too much text."""
        self._branch()
        calls = len(self.github.calls)
        half = "x" * (MAX_COMMIT_CHARS // 2 + 1)
        for files in (
            [{"path": ".github/workflows/x.yml", "content": "on: push"}],
            [{"path": ".GitHub/workflows/x.yml", "content": "on: push"}],
            [{"path": "vendor/.Git/config", "content": "[core]"}],
            [{"path": "../x.py", "content": ""}],
            [{"path": "x.py", "content": f"PASSWORD = '{HOST_PASSWORD}'"}],
            [{"path": "a.py", "content": half}, {"path": "b.py", "content": half}],
        ):
            with self.subTest(files[0]["path"]):
                result = self._write("assistant/x", files=files)
                self.assertTrue(result["isError"], result)
                self._assert_no_secret(result["content"][0]["text"])
        self.assertFalse([c for c in self.github.calls[calls:] if c[0] != "GET"])

    def test_the_app_refused_the_token_writes(self):
        """GitHub lets the App only read (403 on writes): the personal token writes."""
        self.env["cloud.settings"]._get_system().github_pat = "ghp_panel-pat-0001"
        self.github.read_only_tokens.add("ghs_app-installation-token")
        payload = self._payload(self._branch("assistant/y"))
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(self.github.branches["assistant/y"], "sha-main")
        self.assertEqual(self.github.tokens[-1], "ghp_panel-pat-0001")

    def test_a_pull_request_is_opened_or_found(self):
        """Opened into the default branch; when one is open already, that one."""
        payload = self._payload(
            self._call_tool(
                self.key,
                "repo_open_pull_request",
                {"url": REPO, "branch": "assistant/x", "title": "Internal code"},
            )
        )
        self.assertEqual((payload["number"], payload["url"]), (7, f"{REPO}/pull/7"))
        posted = [c for c in self.github.calls if c[1].endswith("/pulls")]
        self.assertEqual(posted[0][2]["base"], "main")

    def test_without_a_personal_token_the_repository_is_not_created(self):
        """With the App only, the answer says how to create it on GitHub."""
        arguments = {"project_id": self.project.id, "name": "acme-code"}
        result = self._confirmed_call(self.key, "repo_create", arguments)
        self.assertTrue(result["isError"])
        self.assertIn("New repository", result["content"][-1]["text"])
        self.assertFalse([c for c in self.github.calls if c[1] == "/user/repos"])

    def test_no_repository_is_created_for_a_project_out_of_reach(self):
        """The project is checked before GitHub: nothing is left orphaned there."""
        self.env["cloud.settings"]._get_system().github_pat = "ghp_panel-pat-0001"
        result = self._confirmed_call(
            self.key, "repo_create", {"project_id": 999_999, "name": "acme-code"}
        )
        self.assertTrue(result["isError"])
        self.assertFalse([c for c in self.github.calls if c[1] == "/user/repos"])

    def test_a_repository_is_created_private_and_connected_after_approval(self):
        """Approved, it is created private and added to the project's repositories."""
        self.env["cloud.settings"]._get_system().github_pat = "ghp_panel-pat-0001"
        arguments = {"project_id": self.project.id, "name": "acme-code"}
        first = self._call_tool(self.key, "repo_create", arguments, FORM_CAPABILITIES)
        self.assertEqual(first.get("resultType"), "input_required", first)
        message = first["inputRequests"]["confirm"]["params"]["message"]
        self.assertIn('"acme-code"', message)
        self.assertIn(self.project.name, message)
        payload = self._payload(
            self._confirmed_call(self.key, "repo_create", arguments)
        )
        self.assertTrue(payload["ok"], payload)
        created = [c for c in self.github.calls if c[1] == "/user/repos"]
        self.assertEqual(
            created[-1][2], {"name": "acme-code", "private": True, "auto_init": True}
        )
        self.env.invalidate_all()
        self.assertEqual(
            sorted(self.project.repo_ids.mapped("url")),
            sorted([REPO, "https://github.com/acme/acme-code"]),
        )

    def test_connecting_a_repository_asks_and_keeps_the_others(self):
        """The project keeps its repositories and gains the new line."""
        other = "https://github.com/acme/other"
        payload = self._payload(
            self._confirmed_call(
                self.key,
                "project_connect_repo",
                {"project_id": self.project.id, "url": other, "branch": "main"},
            )
        )
        self.assertTrue(payload.get("ok", True), payload)
        self.env.invalidate_all()
        self.assertEqual(
            sorted(self.project.repo_ids.mapped("url")), sorted([REPO, other])
        )

    def test_a_repository_is_kept_under_one_address(self):
        """Stored as https://github.com/owner/name; another spelling is the same."""
        self._confirmed_call(
            self.key,
            "project_connect_repo",
            {"project_id": self.project.id, "url": "git@github.com:acme/third.git"},
        )
        payload = self._payload(
            self._confirmed_call(
                self.key,
                "project_connect_repo",
                {
                    "project_id": self.project.id,
                    "url": "https://www.github.com/acme/third/",
                },
            )
        )
        self.assertTrue(payload["already_connected"], payload)
        self.env.invalidate_all()
        urls = self.project.repo_ids.mapped("url")
        self.assertIn("https://github.com/acme/third", urls)
        self.assertEqual(len(self.project.repo_ids), 2)
