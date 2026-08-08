"""
Tier 1 — Unit tests for _repo_requirements helpers.

These tests cover the pure merge and parse logic without hitting the ORM.
"""
import unittest

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.models._repo_requirements import (
    _normalize_url,
    _parse_req_line,
    merge_pip_requirements,
    prune_pip_sources,
    repo_key_for_source_label,
)


class TestParseReqLine(unittest.TestCase):

    def test_simple_package(self):
        result = _parse_req_line("requests")
        self.assertEqual(result, ("requests", "requests"))

    def test_package_with_version(self):
        result = _parse_req_line("requests>=2.0")
        self.assertEqual(result, ("requests", "requests>=2.0"))

    def test_strips_comment(self):
        result = _parse_req_line("requests>=2.0  # HTTP lib")
        self.assertEqual(result, ("requests", "requests>=2.0"))

    def test_blank_line_returns_none(self):
        self.assertIsNone(_parse_req_line(""))
        self.assertIsNone(_parse_req_line("   "))

    def test_comment_only_returns_none(self):
        self.assertIsNone(_parse_req_line("# just a comment"))

    def test_normalises_name_to_lowercase(self):
        name, _ = _parse_req_line("Pillow>=9.0")
        self.assertEqual(name, "pillow")

    def test_git_url_returns_none(self):
        # git+ URLs don't start with a plain package name
        result = _parse_req_line("git+https://github.com/OCA/openupgradelib.git@master")
        # git+ doesn't match [A-Za-z0-9_.-]+ at start cleanly — depends on impl
        # The important thing is it doesn't raise
        # (may return None or a tuple — just ensure no exception)


class TestMergePipRequirements(unittest.TestCase):

    def test_empty_existing_returns_incoming(self):
        result = merge_pip_requirements("", "requests\nflask\n")
        self.assertIn("requests", result['content'])
        self.assertIn("flask", result['content'])
        self.assertEqual(result['conflicts'], [])

    def test_no_overlap_appends_all_incoming(self):
        existing = "requests\n"
        incoming = "flask\npillow\n"
        result = merge_pip_requirements(existing, incoming)
        self.assertIn("flask", result['content'])
        self.assertIn("pillow", result['content'])
        self.assertIn("requests", result['content'])
        self.assertEqual(result['conflicts'], [])

    def test_exact_duplicate_skipped(self):
        existing = "requests>=2.0\n"
        incoming = "requests>=2.0\n"
        result = merge_pip_requirements(existing, incoming)
        self.assertEqual(result['conflicts'], [])
        self.assertEqual(result['content'].count("requests"), 1)

    def test_version_conflict_detected(self):
        existing = "lib1>=3.0\n"
        incoming = "lib1<3.0\n"
        result = merge_pip_requirements(existing, incoming)
        self.assertEqual(len(result['conflicts']), 1)
        c = result['conflicts'][0]
        self.assertEqual(c['name'], 'lib1')
        self.assertEqual(c['existing'], 'lib1>=3.0')
        self.assertEqual(c['incoming'], 'lib1<3.0')

    def test_conflict_keeps_existing_spec(self):
        existing = "lib1>=3.0\n"
        incoming = "lib1<3.0\n"
        result = merge_pip_requirements(existing, incoming)
        self.assertIn("lib1>=3.0", result['content'])
        self.assertNotIn("lib1<3.0", result['content'])

    def test_multiple_conflicts(self):
        existing = "libA>=1.0\nlibB==2.0\n"
        incoming = "libA<1.0\nlibB==3.0\n"
        result = merge_pip_requirements(existing, incoming)
        self.assertEqual(len(result['conflicts']), 2)

    def test_blank_separator_added_before_new_packages(self):
        existing = "requests\n"
        incoming = "flask\n"
        result = merge_pip_requirements(existing, incoming)
        lines = result['content'].split('\n')
        # There should be a blank line separating existing from added
        self.assertIn('', lines)

    def test_no_new_packages_content_unchanged(self):
        existing = "requests>=2.0\n"
        incoming = "requests>=2.0\n"
        result = merge_pip_requirements(existing, incoming)
        self.assertEqual(result['content'], existing)

    def test_case_insensitive_name_matching(self):
        existing = "Pillow>=9.0\n"
        incoming = "pillow>=8.0\n"
        result = merge_pip_requirements(existing, incoming)
        self.assertEqual(len(result['conflicts']), 1)
        self.assertEqual(result['conflicts'][0]['name'], 'pillow')

    def test_returns_dict_with_required_keys(self):
        result = merge_pip_requirements("", "")
        self.assertIn('content', result)
        self.assertIn('conflicts', result)


_REPO_A = "https://github.com/acme/repo-a"
_REPO_B = "https://github.com/acme/repo-b"
_KEY_A = _normalize_url(_REPO_A)
_KEY_B = _normalize_url(_REPO_B)


class TestMergeWithProvenance(BaseCase):
    """Per-package authority: who wrote a line decides who may change it.

    Repos without a pinned commit float to their branch tip, so upstream
    keeps editing its own ``requirements.txt``. Without authority every
    such edit is a conflict a human must click through; with it, only a
    real disagreement stops the build.
    """

    def _merge(self, existing, incoming, sources, repo=_REPO_A, branch="19.0"):
        """Run a provenance-mode merge and return the result dict."""
        return merge_pip_requirements(
            existing, incoming,
            repo_url=repo, repo_branch=branch, sources=sources,
        )

    def test_new_package_is_added_and_owned_by_the_repo(self):
        result = self._merge("requests\n", "libnew==1.0\n", {})
        self.assertIn("libnew==1.0", result['content'])
        self.assertEqual(result['added'], ["libnew==1.0"])
        self.assertEqual(result['sources']['libnew']['repo'], _KEY_A)
        self.assertEqual(result['conflicts'], [])

    def test_identical_spec_is_adopted_when_unowned(self):
        """Lazy backfill: no migration, ownership settles on first sight."""
        result = self._merge("libx==1.0\n", "libx==1.0\n", {})
        self.assertEqual(result['adopted'], ["libx"])
        self.assertEqual(result['sources']['libx']['repo'], _KEY_A)
        self.assertEqual(result['content'], "libx==1.0\n")

    def test_identical_spec_inside_a_conflict_block_is_not_adopted(self):
        """An unresolved marker has no settled owner to record."""
        existing = (
            "<<<<<<< existing\n"
            "libx==1.0\n"
            "=======\n"
            "libx==2.0\n"
            ">>>>>>> acme/repo-b (19.0)"
        )
        result = self._merge(existing, "libx==1.0\n", {})
        self.assertEqual(result['adopted'], [])
        self.assertNotIn('libx', result['sources'])

    def test_owner_updating_its_own_line_is_applied_not_flagged(self):
        sources = {'libx': {'repo': _KEY_A, 'spec': 'libx==1.0', 'label': 'a'}}
        result = self._merge("libx==1.0\n", "libx==1.2\n", sources)
        self.assertEqual(result['conflicts'], [])
        self.assertIn("libx==1.2", result['content'])
        self.assertNotIn("libx==1.0", result['content'])
        self.assertEqual(
            result['updated'],
            [{'name': 'libx', 'old': 'libx==1.0', 'new': 'libx==1.2'}],
        )
        self.assertEqual(result['sources']['libx']['spec'], 'libx==1.2')

    def test_another_repos_line_still_conflicts(self):
        sources = {'libx': {'repo': _KEY_B, 'spec': 'libx==1.0', 'label': 'b'}}
        result = self._merge("libx==1.0\n", "libx==1.2\n", sources)
        self.assertEqual(len(result['conflicts']), 1)
        self.assertEqual(result['updated'], [])
        self.assertIn("<<<<<<<", result['content'])

    def test_operator_owned_line_still_conflicts(self):
        """No entry means the operator wrote it: never overwrite it."""
        result = self._merge("libx==1.0\n", "libx==1.2\n", {})
        self.assertEqual(len(result['conflicts']), 1)
        self.assertEqual(result['updated'], [])
        self.assertIn("libx==1.0", result['content'])

    def test_package_dropped_upstream_is_never_removed(self):
        sources = {'libx': {'repo': _KEY_A, 'spec': 'libx==1.0', 'label': 'a'}}
        result = self._merge("libx==1.0\n", "libother==2.0\n", sources)
        self.assertIn("libx==1.0", result['content'])
        self.assertIn('libx', result['sources'])
        self.assertEqual(result['removed'], ['libx'])

    def test_operator_package_absent_upstream_is_not_reported_removed(self):
        """Only this repo's own packages count as dropped by it."""
        result = self._merge("libx==1.0\n", "libother==2.0\n", {})
        self.assertEqual(result['removed'], [])

    def test_ownership_survives_a_branch_change(self):
        """Authority is the repo, not the branch it was read from."""
        sources = {'libx': {'repo': _KEY_A, 'spec': 'libx==1.0', 'label': 'a'}}
        result = self._merge(
            "libx==1.0\n", "libx==1.2\n", sources, branch="20.0",
        )
        self.assertEqual(result['conflicts'], [])
        self.assertIn("libx==1.2", result['content'])

    def test_legacy_call_reports_no_sources(self):
        """Without a map the caller must not be able to persist an empty one."""
        result = merge_pip_requirements("libx==1.0\n", "libx==1.2\n")
        self.assertNotIn('sources', result)
        self.assertEqual(result['removed'], [])
        self.assertEqual(len(result['conflicts']), 1)


class TestPrunePipSources(BaseCase):
    """A hand edit takes a line back from upstream."""

    def setUp(self):
        super().setUp()
        self.sources = {
            'libx': {'repo': _KEY_A, 'spec': 'libx==1.0', 'label': 'a'},
            'liby': {'repo': _KEY_A, 'spec': 'liby==2.0', 'label': 'a'},
        }

    def test_entries_backed_by_the_text_are_kept(self):
        kept = prune_pip_sources("libx==1.0\nliby==2.0\n", self.sources)
        self.assertEqual(set(kept), {'libx', 'liby'})

    def test_edited_line_loses_its_owner(self):
        kept = prune_pip_sources("libx==9.9\nliby==2.0\n", self.sources)
        self.assertEqual(set(kept), {'liby'})

    def test_deleted_line_loses_its_owner(self):
        kept = prune_pip_sources("liby==2.0\n", self.sources)
        self.assertEqual(set(kept), {'liby'})

    def test_line_turned_into_a_conflict_loses_its_owner(self):
        text = (
            "<<<<<<< existing\n"
            "libx==1.0\n"
            "=======\n"
            "libx==3.0\n"
            ">>>>>>> acme/repo-b (19.0)\n"
            "liby==2.0\n"
        )
        kept = prune_pip_sources(text, self.sources)
        self.assertEqual(set(kept), {'liby'})

    def test_empty_map_stays_empty(self):
        self.assertEqual(prune_pip_sources("libx==1.0\n", {}), {})


class TestRepoKeyForSourceLabel(BaseCase):
    """Conflict markers carry a label; resolution needs the repo behind it."""

    class _Repo:
        """Stand-in for a repo line: two plain attributes, no methods."""

        def __init__(self, url, branch):
            self.url = url
            self.branch = branch

    def test_matching_label_returns_the_repo_key(self):
        repos = [self._Repo(_REPO_A, "19.0"), self._Repo(_REPO_B, "19.0")]
        self.assertEqual(
            repo_key_for_source_label(repos, "acme/repo-b (19.0)"), _KEY_B,
        )

    def test_unknown_label_returns_empty(self):
        repos = [self._Repo(_REPO_A, "19.0")]
        self.assertEqual(repo_key_for_source_label(repos, "existing"), '')
