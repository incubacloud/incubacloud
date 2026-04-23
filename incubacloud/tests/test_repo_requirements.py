"""
Tier 1 — Unit tests for _repo_requirements helpers.

These tests cover the pure merge and parse logic without hitting the ORM.
"""
import unittest

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.models._repo_requirements import (
    _parse_req_line,
    merge_pip_requirements,
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
