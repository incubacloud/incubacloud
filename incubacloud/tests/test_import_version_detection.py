"""Tests for the project-importer Odoo-version detection helpers.

These guard the fix for repos whose ``.gitmodules`` carry stale
version-like submodule branch labels: the project version must come from
the project's own module manifests, and each submodule branch must be the
one the pinned SHA truly lives on.
"""
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.controllers._data_load import (
    _routes_github as rg,
)
from odoo.addons.incubacloud.models._odoo_versions import ODOO_VERSIONS


class TestImportVersionHelpers(BaseCase):
    """Pure-function helpers used to infer a repo's Odoo version."""

    # ── _to_https ────────────────────────────────────────────────────────
    def test_to_https_from_ssh(self):
        """SCP-style git@ URLs are rewritten to HTTPS."""
        self.assertEqual(
            rg._to_https("git@github.com:OCA/partner-contact.git"),
            "https://github.com/OCA/partner-contact.git",
        )

    def test_to_https_passthrough(self):
        """An already-HTTPS URL is returned unchanged."""
        self.assertEqual(
            rg._to_https("https://github.com/OCA/x.git"),
            "https://github.com/OCA/x.git",
        )

    # ── _version_from_manifest_text ──────────────────────────────────────
    def test_version_from_manifest_ok(self):
        """The Odoo series is the first two segments of ``version``."""
        self.assertEqual(
            rg._version_from_manifest_text("{'version': '18.0.1.0.11'}"),
            "18.0",
        )

    def test_version_from_manifest_unsupported(self):
        """An unsupported series yields the empty string."""
        self.assertEqual(
            rg._version_from_manifest_text("{'version': '99.0.1.0.0'}"),
            "",
        )

    def test_version_from_manifest_garbage(self):
        """Unparseable manifest text never raises, returns ''."""
        self.assertEqual(rg._version_from_manifest_text("not <<< a dict"), "")

    def test_version_from_manifest_no_version(self):
        """A manifest without a ``version`` key returns ''."""
        self.assertEqual(rg._version_from_manifest_text("{'name': 'x'}"), "")

    # ── _detect_version_from_disk ────────────────────────────────────────
    def test_detect_disk_prefers_own_module(self):
        """A top-level own module (18.0) wins over a vendored 16.0 one."""
        tmp = tempfile.mkdtemp(prefix="ic_test_")
        try:
            self._write_manifest(tmp, "mtm_mod", "18.0.1.0.0")
            self._write_manifest(
                tmp, "OCA/partner-contact", "16.0.1.0.0",
            )
            self.assertEqual(
                rg._detect_version_from_disk(
                    tmp, skip_paths=["OCA/partner-contact"],
                ),
                "18.0",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_detect_disk_skips_vendored_submodule(self):
        """A stale vendored manifest in ``skip_paths`` is ignored."""
        tmp = tempfile.mkdtemp(prefix="ic_test_")
        try:
            self._write_manifest(
                tmp, "OCA/partner-contact", "16.0.1.0.0",
            )
            self.assertEqual(
                rg._detect_version_from_disk(
                    tmp, skip_paths=["OCA/partner-contact"],
                ),
                "",
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_detect_disk_empty(self):
        """A directory without manifests returns ''."""
        tmp = tempfile.mkdtemp(prefix="ic_test_")
        try:
            self.assertEqual(rg._detect_version_from_disk(tmp), "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ── _normalize_sub_branch ────────────────────────────────────────────
    def test_normalize_matching_label_kept(self):
        """A label equal to the project version is kept (no resolution)."""
        self.assertEqual(
            rg._normalize_sub_branch("18.0", "18.0", "u", "sha", []),
            "18.0",
        )

    def test_normalize_empty_uses_project(self):
        """Empty / '.' branch falls back to the project version."""
        self.assertEqual(
            rg._normalize_sub_branch("", "18.0", "u", "sha", []), "18.0",
        )
        self.assertEqual(
            rg._normalize_sub_branch(".", "18.0", "u", "sha", []), "18.0",
        )

    def test_normalize_empty_no_project_uses_main(self):
        """With no project version, an empty branch defaults to 'main'."""
        self.assertEqual(
            rg._normalize_sub_branch("", "", "u", "sha", []), "main",
        )

    def test_normalize_nonversion_label_kept(self):
        """A non-version label like 'main' is preserved verbatim."""
        self.assertEqual(
            rg._normalize_sub_branch("main", "18.0", "u", "sha", []),
            "main",
        )

    def test_normalize_stale_label_resolves_via_sha(self):
        """A stale version label is replaced by the SHA's real branch."""
        with patch.object(
            rg, "_branch_for_sha", return_value="18.0",
        ) as mocked:
            out = rg._normalize_sub_branch(
                "16.0", "18.0",
                "https://github.com/OCA/x.git", "deadbeef", ["tok"],
            )
        self.assertEqual(out, "18.0")
        mocked.assert_called_once()

    def test_normalize_stale_unresolvable_falls_back(self):
        """If the SHA's branch cannot be resolved, use the project version."""
        with patch.object(rg, "_branch_for_sha", return_value=""):
            out = rg._normalize_sub_branch(
                "16.0", "18.0",
                "https://github.com/OCA/x.git", "deadbeef", ["tok"],
            )
        self.assertEqual(out, "18.0")

    # ── _branch_for_sha guard clauses (no network) ───────────────────────
    def test_branch_for_sha_empty_sha(self):
        """No SHA → no resolution attempt, returns ''."""
        self.assertEqual(
            rg._branch_for_sha(
                "https://github.com/OCA/x.git", "", "18.0", [],
            ),
            "",
        )

    def test_branch_for_sha_rejects_non_github_host(self):
        """A non-allowlisted host is refused before any clone (SSRF)."""
        self.assertEqual(
            rg._branch_for_sha(
                "https://evil.example.com/x.git", "deadbeef", "18.0", [],
            ),
            "",
        )

    @staticmethod
    def _write_manifest(root, rel_dir, version):
        """Create ``<root>/<rel_dir>/__manifest__.py`` with the given version."""
        path = Path(root) / rel_dir
        path.mkdir(parents=True, exist_ok=True)
        (path / "__manifest__.py").write_text(
            "{'name': 'm', 'version': '%s'}" % version
        )


class TestOdooVersionSingleSource(TransactionCase):
    """The version selectors must share one source of truth."""

    def test_models_share_constant(self):
        """cloud.instance / cloud.project selections equal ODOO_VERSIONS."""
        inst = [
            code
            for code, _label
            in self.env['cloud.instance']._fields['odoo_version'].selection
        ]
        proj = [
            code
            for code, _label
            in self.env['cloud.project']._fields['odoo_version'].selection
        ]
        self.assertEqual(inst, list(ODOO_VERSIONS))
        self.assertEqual(proj, list(ODOO_VERSIONS))
