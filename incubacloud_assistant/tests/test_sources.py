"""The public sources: core's repository only, its closed list, never out of it."""

import os
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from odoo.modules.module import get_module_path
from odoo.tests import tagged
from odoo.tests.common import BaseCase, TransactionCase

from .. import sources


class TestSourcesOnATree(BaseCase):
    """On a tree of the test's own: what is read, what is refused."""

    def setUp(self):
        """A core-like tree: a module, docs, a private folder, a link out, a binary."""
        super().setUp()
        self.root = tempfile.mkdtemp()
        self.outside = tempfile.mkdtemp()
        self._write("incubacloud/__manifest__.py", "{'name': 'IncubaCloud'}\n")
        self._write("incubacloud/models/cloud_x.py", "class CloudX:\n    needle = 1\n")
        self._write("incubacloud/static/lib/vendor.js", "needle")
        self._write("incubacloud/scripts/lib/common.sh", "# needle of the scripts\n")
        self._write(
            "docs/user/docs/backups/restore.md",
            "# Restore a backup\n\nOpen the instance.\n\n## From another server\n\n"
            "Choose the backup and press Restore.\n",
        )
        self._write("thoughts/private.md", "needle private notes")
        self._write("AGENTS.md", "needle")
        self._write("incubacloud/.hidden.py", "needle")
        Path(self.root, "incubacloud", "blob.py").write_bytes(b"needle\x00\x01")
        Path(self.outside, "secret.py").write_text("needle outside")
        os.symlink(self.outside, os.path.join(self.root, "incubacloud", "out"))
        os.symlink(
            os.path.join(self.outside, "secret.py"),
            os.path.join(self.root, "incubacloud", "linked.py"),
        )

    def _write(self, relative, text):
        """Write *text* at *relative* under the tree."""
        path = Path(self.root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_search_finds_only_public_text(self):
        """Not the private folder, a file out of the list, a hidden one, a binary,
        a link, a vendored library."""
        found = sources.search_code(self.root, "NEEDLE", "", 50)
        self.assertEqual(
            [(hit["path"], hit["line"]) for hit in found["matches"]],
            [
                ("incubacloud/models/cloud_x.py", 2),
                ("incubacloud/scripts/lib/common.sh", 1),
            ],
        )

    def test_paths_that_climb_or_leave_are_refused(self):
        """``..``, an entry out of the list, a hidden part, a link out."""
        for path in (
            "../x",
            "incubacloud/../../etc/passwd",
            "thoughts/private.md",
            "AGENTS.md",
            "incubacloud/.hidden.py",
            "incubacloud/out/secret.py",
            "incubacloud/linked.py",
        ):
            with self.subTest(path), self.assertRaises(sources.SourceError):
                sources.read_source(self.root, path, 1, 50)

    def test_a_file_is_read_by_lines_and_a_folder_listed(self):
        """Numbered lines; the top lists only the public entries."""
        read = sources.read_source(self.root, "incubacloud/models/cloud_x.py", 2, 1)
        self.assertEqual(read["lines"], ["2: " + "    needle = 1"])
        top = sources.read_source(self.root, "", 1, 50)
        self.assertEqual(top["entries"], ["docs/", "incubacloud/"])

    def test_docs_answer_sections_with_their_page_links(self):
        """The section that matches, its heading, and the page on the docs site."""
        found = sources.search_docs(self.root, "restore from another server", 3)
        self.assertEqual(found["sections"][0]["heading"], "From another server")
        self.assertEqual(
            found["sections"][0]["url"],
            "https://docs.incubacloud.io/backups/restore/#from-another-server",
        )


class TestSourcesPublished(BaseCase):
    """Without a docs folder, as on a built image: the site's search index."""

    INDEX = {
        "docs": [
            {"location": "agents/", "title": "AI agents", "text": "<p>Connect an agent.</p>"},
            {
                "location": "agents/#connect-an-mcp-client",
                "title": "Connect an MCP client",
                "text": "<p>Copy the panel&#39;s <code>MCP</code> address.</p>",
            },
        ]
    }

    def setUp(self):
        """A tree with the module and no docs; nothing read from the site yet."""
        super().setUp()
        self.root = tempfile.mkdtemp()
        Path(self.root, "incubacloud").mkdir()
        patcher = patch.dict(sources._published, {"at": 0.0, "sections": None})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _site(self, *answers):
        """Patch the site's answers in turn: an index, or an exception to raise."""
        responses = []
        for answer in answers:
            if isinstance(answer, Exception):
                responses.append(answer)
                continue
            response = MagicMock(spec=requests.Response)
            response.json.return_value = answer
            responses.append(response)
        return patch.object(sources.requests, "get", side_effect=responses)

    def test_the_published_index_answers_with_its_own_links(self):
        """Its section, its text without markup, and the site's address of it."""
        with self._site(self.INDEX) as get:
            found = sources.search_docs(self.root, "connect MCP client", 1)
        get.assert_called_once_with(
            sources.PUBLISHED_INDEX_URL, timeout=sources.SEARCH_SECONDS
        )
        self.assertEqual(
            found["sections"],
            [
                {
                    "page": "agents/",
                    "heading": "Connect an MCP client",
                    "url": "https://docs.incubacloud.io/agents/#connect-an-mcp-client",
                    "text": "Copy the panel's MCP address.",
                }
            ],
        )

    def test_the_index_is_read_once_an_hour(self):
        """A second search reuses it; past the hour the site is read again."""
        with self._site(self.INDEX, self.INDEX) as get:
            sources.search_docs(self.root, "agent", 3)
            sources.search_docs(self.root, "agent", 3)
            self.assertEqual(get.call_count, 1)
            sources._published["at"] -= sources.PUBLISHED_INDEX_SECONDS
            sources.search_docs(self.root, "agent", 3)
            self.assertEqual(get.call_count, 2)

    def test_an_unreachable_site_serves_the_copy_last_read(self):
        """An old copy rather than nothing; without one, where the docs are."""
        with self._site(requests.ConnectionError("down")):
            with self.assertRaisesRegex(sources.SourceError, "docs.incubacloud.io"):
                sources.search_docs(self.root, "agent", 3)
        with self._site(self.INDEX, requests.Timeout("slow")):
            sources.search_docs(self.root, "agent", 3)
            sources._published["at"] -= sources.PUBLISHED_INDEX_SECONDS
            found = sources.search_docs(self.root, "agent", 3)
        self.assertEqual(found["sections"][0]["page"], "agents/")

    def test_a_docs_folder_is_searched_without_asking_the_site(self):
        """A checkout, as on devel or in the CI, reads its own files."""
        Path(self.root, *sources.DOCS_DIR).mkdir(parents=True)
        Path(self.root, *sources.DOCS_DIR, "agents.md").write_text("# Agents\n\nConnect.\n")
        with self._site() as get:
            found = sources.search_docs(self.root, "connect", 3)
        get.assert_not_called()
        self.assertEqual(found["sections"][0]["url"], "https://docs.incubacloud.io/agents/#agents")


@tagged("post_install", "-at_install")
class TestSourcesOnThisPanel(TransactionCase):
    """On the panel's own installation: core's checkout, and nothing else."""

    def test_the_root_is_core_s_repository(self):
        """The folder above the incubacloud module, which holds its manifest."""
        root = sources.core_root()
        self.assertTrue(
            os.path.isfile(os.path.join(root, "incubacloud", "__manifest__.py"))
        )

    def test_no_installed_module_out_of_the_list_is_read(self):
        """Saas's modules on the master or a tenant panel, or any other, stay out."""
        root = sources.core_root()
        installed = self.env["ir.module.module"].search([("state", "=", "installed")])
        for module in installed.mapped("name"):
            path = get_module_path(module)
            if not path or module in sources.PUBLIC_ENTRIES:
                continue
            relative = os.path.relpath(os.path.realpath(path), root)
            with self.subTest(module), self.assertRaises(sources.SourceError):
                sources.read_source(root, relative, 1, 10)

    def test_the_closed_list_covers_every_entry_of_the_repository(self):
        """Every tracked top-level entry is either read or left out on purpose."""
        root = sources.core_root()
        # A worktree's .git is a file that points to a path of the host: git
        # answers only where that path is mounted too (the test script does).
        if not Path(root, ".git").exists():
            self.skipTest("no git checkout here (a built image)")
        listed = subprocess.run(
            ["git", "-C", root, "ls-files"], capture_output=True, text=True, check=False
        )
        if listed.returncode:
            self.skipTest("no usable git checkout here")
        top = {path.split("/", 1)[0] for path in listed.stdout.split()}
        self.assertEqual(top - sources.PUBLIC_ENTRIES - sources.NOT_READ, set())

    def test_a_search_of_the_real_repository_is_quick(self):
        """A word every module has, in a few seconds at most."""
        start = time.monotonic()
        found = sources.search_code(sources.core_root(), "def ", "incubacloud", 20)
        self.assertEqual(len(found["matches"]), 20)
        self.assertLess(time.monotonic() - start, sources.SEARCH_SECONDS + 1)
