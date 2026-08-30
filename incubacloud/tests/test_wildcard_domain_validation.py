r"""Tests for wildcard-domain validation and the config it feeds.

``cloud.host.wildcard_domain`` was ``required=True`` and nothing else, and
the value is spliced into the Traefik configuration of the machine. Two
guarantees are pinned here: nothing that is not a DNS hostname reaches the
column, and whatever does reach ``_build_inverseproxy`` is emitted verbatim
rather than through ``re.sub``'s replacement-string escapes.

The shape/policy split matters and is asserted: the model checks shape only
(it also holds hosts an operator named, where ``h.local`` is fair), while
the internal blocklist and the reserved list are the callers' to apply.
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.models.full_setup_executor import (
    _build_inverseproxy,
)
from odoo.addons.incubacloud.net.hostname import (
    InvalidHostname,
    validate_wildcard_domain,
)


class TestValidateWildcardDomain(BaseCase):
    """The pure validator, with no ORM in the way."""

    def _code(self, value, **kw):
        """Return the ``code`` of the rejection *value* provokes."""
        with self.assertRaises(InvalidHostname) as caught:
            validate_wildcard_domain(value, **kw)
        return caught.exception.code

    def test_accepts_plain_and_wildcard_forms(self):
        self.assertEqual(
            validate_wildcard_domain("app.example.com"), "app.example.com",
        )
        self.assertEqual(
            validate_wildcard_domain("*.example.com"), "*.example.com",
        )

    def test_normalises_case_and_whitespace_without_rewriting_storage(self):
        """Production holds ``Tenants1.incubacloud.io``.

        The comparison is case-insensitive so that row stays writable; the
        model constraint only reads this result, it never writes it back.
        """
        self.assertEqual(
            validate_wildcard_domain("  Tenants1.Incubacloud.IO  "),
            "tenants1.incubacloud.io",
        )

    def test_rejects_malformed_shapes(self):
        self.assertEqual(self._code(""), "empty")
        self.assertEqual(self._code(None), "empty")
        self.assertEqual(self._code("café.example.com"), "non_ascii")
        self.assertEqual(self._code("a" * 250 + ".example.com"), "too_long")
        for bad in (
            "example",              # single label
            "example..com",         # empty label
            "example.com.",         # trailing dot
            "-lead.example.com",    # leading hyphen
            "trail-.example.com",   # trailing hyphen
            ".example.com",         # leading dot
            "$(rm -rf /);",         # command substitution
            "a`) || PathPrefix(`/",  # Traefik rule breakout
            "with space.example.com",
            "under_score.example.com",
            "a" * 64 + ".example.com",  # label over 63
            # A single label never reaches the blocklist: it is not a
            # hostname in the first place.
            "localhost",
        ):
            with self.subTest(bad=bad):
                self.assertEqual(self._code(bad), "shape")

    def test_internal_blocklist_is_opt_in(self):
        """Shape-only accepts ``.local``; the BYOD doors do not.

        ~25 test hosts and any operator-named box on an internal network
        live under ``.local``, so the model must keep taking them.
        """
        self.assertEqual(
            validate_wildcard_domain("guard.test.local"), "guard.test.local",
        )
        self.assertEqual(
            self._code("guard.test.local", check_internal=True), "internal",
        )
        for bad in (
            "127.0.0.1",
            "10.1.2.3",
            "192.168.1.1",
            "172.20.0.5",
            "169.254.169.254",
            "metadata.google.internal",
            "svc.cluster.local",
        ):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self._code(bad, check_internal=True), "internal",
                )

    def test_reserved_matches_the_domain_and_anything_under_it(self):
        reserved = ("incubacloud.io", "incubacloud.com")
        self.assertEqual(
            self._code("incubacloud.io", reserved=reserved), "reserved",
        )
        self.assertEqual(
            self._code("evil.incubacloud.io", reserved=reserved), "reserved",
        )
        self.assertEqual(
            self._code("*.incubacloud.io", reserved=reserved), "reserved",
        )
        # Case and stray whitespace in the configured list must not be a
        # way past it — the list is operator-typed.
        self.assertEqual(
            self._code("x.incubacloud.io", reserved=("  Incubacloud.IO ",)),
            "reserved",
        )
        # A domain that merely ends with the same letters is not under it.
        self.assertEqual(
            validate_wildcard_domain(
                "notincubacloud.io", reserved=reserved,
            ),
            "notincubacloud.io",
        )
        # An empty entry in the list must not reserve everything.
        self.assertEqual(
            validate_wildcard_domain("app.example.com", reserved=("", "  ")),
            "app.example.com",
        )


class TestHostWildcardConstraint(TransactionCase):

    def _create(self, domain):
        return self.env["cloud.host"].create(
            {
                "name": "wildcard-guard",
                "ip_address": "192.0.2.77",
                "user": "ubuntu",
                "wildcard_domain": domain,
            }
        )

    def test_create_refuses_a_non_hostname(self):
        with self.assertRaises(ValidationError):
            self._create("$(rm -rf /);")

    def test_write_refuses_a_non_hostname(self):
        host = self._create("ok.example.com")
        with self.assertRaises(ValidationError):
            host.write({"wildcard_domain": "a`) || PathPrefix(`/"})

    def test_mixed_case_host_stays_writable(self):
        """The row shape production actually holds must survive an edit."""
        host = self._create("Tenants1.incubacloud.io")
        host.write({"wildcard_domain": "Tenants2.incubacloud.io"})
        self.assertEqual(host.wildcard_domain, "Tenants2.incubacloud.io")

    def test_model_does_not_apply_the_internal_blocklist(self):
        """Shape only here — see the validator's docstring for why."""
        host = self._create("guard.test.local")
        self.assertEqual(host.wildcard_domain, "guard.test.local")


class TestBuildInverseproxy(BaseCase):

    TEMPLATE = (
        '      - "traefik.http.routers.api.rule=Host(`traefik.example.com`)"\n'
    )

    def test_backslash_in_domain_is_emitted_verbatim(self):
        r"""``\1`` used to splice a capture group in; ``\d`` used to raise.

        The constraint keeps such a domain out of the column, but this is
        the layer that has to be harmless on its own — it is a plain
        function that anything can call.
        """
        out = _build_inverseproxy(self.TEMPLATE, r"a\1b\d.example.com", "")
        self.assertIn(r"Host(`traefik.a\1b\d.example.com`)", out)

    def test_wildcard_prefix_is_stripped(self):
        out = _build_inverseproxy(self.TEMPLATE, "*.example.org", "")
        self.assertIn("Host(`traefik.example.org`)", out)

    def test_empty_domain_falls_back_to_localhost(self):
        out = _build_inverseproxy(self.TEMPLATE, "", "")
        self.assertIn("Host(`traefik.localhost`)", out)

    def test_password_hash_replaces_the_placeholder_label(self):
        template = (
            '      - "traefik.http.middlewares.auth.basicauth.users='
            'traefik-admin:$$2y$$12$$panel-disabled"\n'
        )
        out = _build_inverseproxy(template, "h.example.com", "s3cret")
        self.assertIn("basicauth.users=traefik-admin:$$2y$$", out)
        self.assertNotIn("panel-disabled", out)
