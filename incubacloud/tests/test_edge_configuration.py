"""Configuring the edge: what is refused, what applies, and where it says so.

Two failures shaped these. A range dropped for a typo narrows who we
believe without saying so, which is the silent narrowing the whole
mechanism exists to prevent — so the write is refused, not trimmed. And
an operator reading the host form could not tell whether they were
protected or about to lock themselves out, because the box shown was the
override while the list applied came from somewhere else entirely.
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase

from ..net.trusted_proxies import invalid_ranges

EDGE = ["198.51.100.0/24", "2001:db8:beef::/48"]


class TestRangeValidation(TransactionCase):
    """A typo is refused where somebody can still fix it."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "edge-cfg-host",
            "ip_address": "192.0.2.70",
            "user": "ubuntu",
            "wildcard_domain": "edge-cfg.example.com",
        })
        self.settings = self.env["cloud.settings"].sudo()._get_system()

    def test_the_helper_names_only_the_unusable_entries(self):
        self.assertEqual(
            invalid_ranges("198.51.100.0/24\n198.51.100.O/24, nope"),
            ["198.51.100.O/24", "nope"],
        )
        self.assertEqual(invalid_ranges(""), [])
        self.assertEqual(invalid_ranges(None), [])

    def test_a_good_list_is_accepted_on_a_host(self):
        self.host.trusted_proxy_ranges = "\n".join(EDGE)
        self.assertEqual(self.host._effective_trusted_proxy_ranges(), EDGE)

    def test_a_typo_is_refused_on_a_host(self):
        with self.assertRaises(ValidationError) as caught:
            self.host.trusted_proxy_ranges = "198.51.100.0/24\n198.51.100.O/24"
        self.assertIn("198.51.100.O/24", str(caught.exception))

    def test_a_typo_is_refused_in_settings(self):
        with self.assertRaises(ValidationError):
            self.settings.trusted_proxy_ranges = "not-a-range"

    def test_clearing_the_field_is_always_allowed(self):
        self.host.trusted_proxy_ranges = "\n".join(EDGE)
        self.host.trusted_proxy_ranges = ""
        self.assertEqual(self.host._effective_trusted_proxy_ranges(), [])

    def test_a_bare_address_and_odd_spacing_are_accepted(self):
        self.host.trusted_proxy_ranges = " 203.0.113.7 ,\n\n 192.0.2.5/24 "
        self.assertEqual(
            self.host._effective_trusted_proxy_ranges(),
            ["203.0.113.7/32", "192.0.2.0/24"],
        )


class TestProvenance(TransactionCase):
    """The form has to say where the applied list came from."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "edge-src-host",
            "ip_address": "192.0.2.71",
            "user": "ubuntu",
            "wildcard_domain": "edge-src.example.com",
        })
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.trusted_proxy_ranges = ""

    def test_a_host_with_nothing_declared_says_so(self):
        self.assertEqual(self.host._trusted_proxy_source(), "none")

    def test_a_host_with_its_own_override_says_so(self):
        self.host.trusted_proxy_ranges = "\n".join(EDGE)
        self.assertEqual(self.host._trusted_proxy_source(), "host")

    def test_settings_report_their_own_source(self):
        Settings = self.env["cloud.settings"].sudo()
        self.assertEqual(Settings._trusted_proxy_source(), "none")
        self.settings.trusted_proxy_ranges = "\n".join(EDGE)
        self.assertEqual(Settings._trusted_proxy_source(), "settings")


class TestProxyPushReanchors(TransactionCase):
    """Applying the proxy settings must clear the drift pill, but only
    for what it actually shipped."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "reanchor-host",
            "ip_address": "192.0.2.74",
            "user": "ubuntu",
            "wildcard_domain": "reanchor.example.com",
        })
        self.host.write(self.host._applied_config_vals())

    def _dirty(self):
        self.host.invalidate_recordset(["config_dirty"])
        return self.host.config_dirty

    def _moved(self):
        from ..models import _config_snapshot_diff as diff
        return set(diff.diff_keys(
            self.host.applied_config_snapshot or {},
            self.host._render_config_snapshot(),
        ))

    def test_a_proxy_only_change_is_recognised_as_such(self):
        from ..models.push_trusted_proxies_executor import _PROXY_KEYS
        self.assertFalse(self._dirty())
        self.host.trusted_proxy_ranges = "\n".join(EDGE)
        self.assertTrue(self._dirty())
        moved = self._moved()
        self.assertTrue(moved)
        self.assertFalse(moved - _PROXY_KEYS)

    def test_a_change_beyond_the_proxy_fields_is_not(self):
        # The job ships two documents; a full setup ships more, so it
        # must not declare somebody else's pending change applied.
        from ..models.push_trusted_proxies_executor import _PROXY_KEYS
        self.host.write({
            "trusted_proxy_ranges": "\n".join(EDGE),
            "wildcard_domain": "moved.example.com",
        })
        self.assertTrue(self._moved() - _PROXY_KEYS)
