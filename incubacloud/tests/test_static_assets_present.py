"""Every static file a page of this module loads is in the repository.

The terminal pages load xterm.js from ``static/lib/xterm``, and the
repository's ``.gitignore`` ignored every ``lib/`` directory: the files
lived only in working copies. Every image built from GitHub shipped
without them, and the host and container shells never started outside
devel (from v1.0.0 until QA's terminal walks found it, 8-oct-2026).

On CI's fresh clone, a file that is referenced but not committed is
simply not there, which is what this checks.
"""
import pathlib
import re

from odoo.tests.common import BaseCase

_MODULE = pathlib.Path(__file__).resolve().parent.parent
_STATIC_REF = re.compile(r"/incubacloud/(static/[A-Za-z0-9_./-]+)")


class TestStaticAssetsPresent(BaseCase):

    def _referenced(self):
        """Static paths named by the module's QWeb views, each once."""
        found = set()
        for view in (_MODULE / "views").glob("*.xml"):
            found.update(_STATIC_REF.findall(view.read_text()))
        return sorted(found)

    def test_the_terminal_pages_name_their_library(self):
        """The check below must have something to check."""
        self.assertIn("static/lib/xterm/xterm.min.js", self._referenced())

    def test_every_referenced_static_file_exists(self):
        missing = [path for path in self._referenced() if not (_MODULE / path).is_file()]
        self.assertFalse(
            missing,
            "pages load these files, and they are not in the module "
            "(ignored by .gitignore?): %s" % ", ".join(missing),
        )

    def test_the_vendored_library_keeps_its_licence(self):
        """xterm.js is MIT: its notice travels with it."""
        self.assertTrue((_MODULE / "static" / "lib" / "xterm" / "LICENSE").is_file())
