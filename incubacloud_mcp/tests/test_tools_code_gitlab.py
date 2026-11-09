"""The code tools on a GitLab repository: read with git, write later.

A line of a Git provider is read with the panel's git and the provider's
credential; it is the user's when a line of theirs has the same provider
and path. Writing to one answers that it comes in a next version.
"""

from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.incubacloud.net import git_reader
from odoo.addons.incubacloud_mcp.models.mcp_tools_code import TOOLS

from .common import ConfirmMixin, McpHttpMixin, McpUsersMixin, PanelFixtureMixin

GITLAB_REPO = "https://gitlab.com/acme/odoo-addons.git"


@tagged("post_install", "-at_install")
class TestCodeToolsOnGitLab(
    PanelFixtureMixin, McpUsersMixin, McpHttpMixin, ConfirmMixin, HttpCase
):
    """A developer's agent reads the project's GitLab repository."""

    def setUp(self):
        """A provider of gitlab.com with a deploy token, and the project's line."""
        super().setUp()
        self._resource_here()
        self._panel_fixture()
        # The core's policy, whatever else is installed: the saas CI runs
        # this suite in a tenant's panel too, whose 'public' policy has
        # tests of its own (incubacloud_tenant).
        self.startPatcher(patch.object(
            type(self.env["cloud.git.provider"]), "_network_policy",
            autospec=True, return_value="any",
        ))
        self.provider = self.env["cloud.git.provider"].sudo().create(
            {"name": "GitLab.com", "base_url": "https://gitlab.com"}
        )
        self.env["cloud.git.credential"].sudo().create(
            {
                "provider_id": self.provider.id,
                "namespace": "acme",
                "kind": "deploy",
                "username": "gitlab+deploy-token-9",
                "token": "gldt-mcpread01",
            }
        )
        self.project.sudo().with_context(skip_apply_requirements=True).repo_ids = [
            (0, 0, {"url": "https://gitlab.com/Acme/Odoo-Addons", "branch": "main"})
        ]
        self.developer = self._cloud_user("mcp-gl-dev", group="group_cloud_developer")
        self.project.member_ids = [(4, self.developer.id)]
        self.key = self._api_key(self.developer)

    def _patch_git(self, tree, files=None):
        """Make the panel's git answer *tree* and *files*.

        :return: the ``(tree, read_files)`` mocks
        """
        mocks = []
        for name, value in (("tree", tree), ("read_files", files or {})):
            patcher = patch.object(
                git_reader.GitReader, name, autospec=True, return_value=value
            )
            mocks.append(patcher.start())
            self.addCleanup(patcher.stop)
        return mocks

    def _read(self, path, url=GITLAB_REPO):
        """Call ``repo_read`` on *path* of ``main``; return the result."""
        return self._call_tool(
            self.key, "repo_read", {"url": url, "branch": "main", "path": path}
        )

    def test_a_file_is_read_with_git_and_the_provider_s_credential(self):
        tree, read = self._patch_git(
            ["acme_base/__manifest__.py"],
            {"acme_base/__manifest__.py": "{'name': 'Acme'}\n"},
        )
        result = self._read("acme_base/__manifest__.py")
        self.assertFalse(result["isError"], result)
        payload = self._payload(result)
        self.assertEqual(payload["type"], "file")
        self.assertEqual(payload["text"], "{'name': 'Acme'}\n")
        ref, credential, rev = read.call_args.args[1:4]
        self.assertEqual(ref.path, "acme/odoo-addons")
        self.assertEqual(credential, ("gitlab+deploy-token-9", "gldt-mcpread01"))
        self.assertEqual(rev, "main")
        self.assertNotIn("gldt-mcpread01", result["content"][-1]["text"])

    def test_a_read_counts_against_the_hourly_cap_of_git_reads(self):
        from odoo.addons.incubacloud_mcp.models import mcp_tools_code
        tree, _read = self._patch_git(["acme_base/__manifest__.py"])
        with patch.object(
            mcp_tools_code, "consume_git_read", return_value=False,
        ) as consume:
            result = self._read("")
        consume.assert_called_once()
        self.assertTrue(result["isError"], result)
        self.assertIn("limit", str(result))
        tree.assert_not_called()

    def test_a_folder_is_listed_one_level_deep(self):
        self._patch_git(
            ["README.md", "acme_base/__manifest__.py", "acme_base/models/x.py"]
        )
        payload = self._payload(self._read(""))
        self.assertEqual(
            payload["entries"],
            [
                {"name": "README.md", "type": "file", "size": None},
                {"name": "acme_base", "type": "dir", "size": None},
            ],
        )

    def test_a_gitlab_repository_of_no_project_is_refused_before_git(self):
        tree, read = self._patch_git([])
        result = self._read("README.md", url="https://gitlab.com/other/code.git")
        self.assertTrue(result["isError"])
        self.assertIn("project_connect_repo", result["content"][-1]["text"])
        tree.assert_not_called()

    def test_writing_to_a_gitlab_repository_comes_in_a_next_version(self):
        tree, read = self._patch_git([])
        for name, arguments in (
            ("repo_create_branch", {"branch": "assistant/x"}),
            (
                "repo_write_files",
                {
                    "branch": "assistant/x",
                    "message": "m",
                    "files": [{"path": "a.py", "content": "x"}],
                },
            ),
            ("repo_open_pull_request", {"branch": "assistant/x", "title": "t"}),
        ):
            with self.subTest(name):
                result = self._call_tool(
                    self.key, name, {"url": GITLAB_REPO, **arguments}
                )
                self.assertTrue(result["isError"], result)
                self.assertIn("next version", result["content"][-1]["text"])
        tree.assert_not_called()

    def test_connecting_a_gitlab_repository_keeps_it_canonical_and_once(self):
        other = "gitlab.com/acme/other"
        payload = self._payload(
            self._confirmed_call(
                self.key,
                "project_connect_repo",
                {"project_id": self.project.id, "url": other, "branch": "main"},
            )
        )
        self.assertTrue(payload.get("ok", True), payload)
        again = self._payload(
            self._confirmed_call(
                self.key,
                "project_connect_repo",
                {
                    "project_id": self.project.id,
                    "url": "https://gitlab.com/ACME/Other.git",
                },
            )
        )
        self.assertTrue(again["already_connected"], again)
        self.env.invalidate_all()
        self.assertIn(
            "https://gitlab.com/acme/other.git", self.project.repo_ids.mapped("url")
        )

    def test_a_host_without_provider_is_named(self):
        result = self._confirmed_call(
            self.key,
            "project_connect_repo",
            {"project_id": self.project.id, "url": "https://git.acme.tld/a/b"},
        )
        self.assertTrue(result["isError"], result)
        self.assertIn("git.acme.tld", result["content"][-1]["text"])

    def test_get_project_says_what_the_panel_does_with_each_line(self):
        payload = self._payload(
            self._call_tool(self.key, "get_project", {"project_id": self.project.id})
        )
        caps = payload["repos"][0]["repo_capabilities"]
        self.assertEqual(caps["provider"], "gitlab")
        self.assertEqual(caps["code_tools"], {"read": True, "write": False})

    def test_the_descriptions_name_gitlab(self):
        specs = {spec.name: spec for spec in TOOLS}
        for name in ("repo_read", "project_connect_repo"):
            with self.subTest(name):
                self.assertIn("GitLab", specs[name].description)
        info = self.env["incubacloud.mcp.catalog"]._spec("github_repo_info")
        self.assertIn("GitLab", info.description)
