"""The resource URL agents' tokens are bound to, and the HMAC tokens."""

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud_mcp.protocol.canonical import (
    canonical_resource,
    origin_of,
    resource_metadata_url,
    same_origin,
)
from odoo.addons.incubacloud_mcp.protocol.signing import fingerprint, sign, verify


class TestCanonicalResource(BaseCase):
    """One spelling of the panel's MCP URL, the audience of every token."""

    def test_scheme_and_host_are_lowercased_and_the_default_port_dropped(self):
        """Scheme and host in lower case, no default port, no trailing slash."""
        self.assertEqual(
            canonical_resource("HTTPS://Panel.Example.COM:443/mcp/"),
            "https://panel.example.com/mcp",
        )

    def test_a_port_that_is_not_the_default_stays(self):
        """A port other than the default stays in the URL."""
        self.assertEqual(
            canonical_resource("https://panel.example.com:8443/mcp"),
            "https://panel.example.com:8443/mcp",
        )

    def test_what_is_not_an_absolute_http_url_is_refused(self):
        """No scheme, another scheme, a query, a fragment or credentials: refused."""
        for bad in (
            "",
            "panel.example.com/mcp",
            "ftp://panel.example.com/mcp",
            "https://panel.example.com/mcp#frag",
            "https://panel.example.com/mcp?x=1",
            "https://user:pw@panel.example.com/mcp",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                canonical_resource(bad)

    def test_an_empty_userinfo_query_or_fragment_or_a_non_ascii_host_is_refused(self):
        """An empty @, ? or #, or a host in Unicode: refused too."""
        for bad in (
            "https://@panel.example.com/mcp",
            "https://panel.example.com/mcp?",
            "https://panel.example.com/mcp#",
            "https://pänel.example.com/mcp",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                canonical_resource(bad)

    def test_what_a_shell_would_read_is_refused(self):
        """The URL goes into commands the user pastes in a terminal: a path
        or a host with quotes, spaces, ``$()`` or backticks is refused."""
        for bad in (
            "https://x.example.com/mcp$(id)",
            "https://x.example.com/mcp'x",
            'https://x.example.com/mcp"x',
            "https://x.example.com/mcp x",
            "https://x.example.com/mcp`id`",
            "https://x.example.com/mcp;id",
            "https://x$(id).example.com/mcp",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                canonical_resource(bad)

    def test_a_plain_path_and_an_ip_literal_stay(self):
        """Letters, digits, ``._~%/-`` and an IPv6 literal are a resource."""
        self.assertEqual(
            canonical_resource("https://panel.example.com/a-b_c.d~e%20f/mcp"),
            "https://panel.example.com/a-b_c.d~e%20f/mcp",
        )
        self.assertEqual(
            canonical_resource("http://[::1]:8069/mcp"), "http://[::1]:8069/mcp"
        )

    def test_an_underscore_in_the_host_stays(self):
        """A shell does not read ``_``: a host such as an internal one with
        it is a resource."""
        self.assertEqual(
            canonical_resource("https://My_Panel.example.com/mcp"),
            "https://my_panel.example.com/mcp",
        )

    def test_origin_and_the_rfc_9728_document(self):
        """The origin, and the RFC 9728 document with the path after it."""
        self.assertEqual(
            origin_of("https://panel.example.com/mcp"), "https://panel.example.com"
        )
        self.assertEqual(
            resource_metadata_url("https://panel.example.com/mcp"),
            "https://panel.example.com/.well-known/oauth-protected-resource/mcp",
        )

    def test_same_origin(self):
        """Only the panel's own origin matches; null and others do not."""
        resource = "https://panel.example.com/mcp"
        self.assertTrue(same_origin("https://PANEL.example.com", resource))
        self.assertFalse(same_origin("https://evil.example.com", resource))
        self.assertFalse(same_origin("null", resource))


class TestSigning(BaseCase):
    """The HMAC tokens: the session id and requestState."""

    KEY = bytes(range(32))

    def test_a_token_verifies_with_its_key_and_label(self):
        """A token signed with the key and label gives its payload back."""
        token = sign(self.KEY, "session", {"u": 7, "v": "2025-11-25"})
        self.assertEqual(
            verify(self.KEY, "session", token), {"u": 7, "v": "2025-11-25"}
        )

    def test_another_label_does_not_verify(self):
        """A session token is not a request token."""
        token = sign(self.KEY, "session", {"u": 7})
        self.assertIsNone(verify(self.KEY, "request", token))

    def test_another_key_does_not_verify(self):
        """Another key does not verify."""
        token = sign(self.KEY, "session", {"u": 7})
        self.assertIsNone(verify(bytes(32), "session", token))

    def test_a_tampered_body_does_not_verify(self):
        """A body changed under the signature does not verify."""
        token = sign(self.KEY, "session", {"u": 7})
        forged = sign(bytes(32), "session", {"u": 8}).split(".")[0]
        self.assertIsNone(
            verify(self.KEY, "session", f"{forged}.{token.split('.')[1]}")
        )

    def test_garbage_is_refused(self):
        """Empty, malformed or not a string: None, never an error."""
        for bad in ("", "abc", "a.b.c", "a.ñ", None, 42):
            with self.subTest(bad=bad):
                self.assertIsNone(verify(self.KEY, "session", bad))

    def test_a_token_that_is_not_ascii_is_refused_not_a_crash(self):
        """A lone surrogate or any non-ASCII character: None, not a crash."""
        for bad in ("\ud800.a", "a.\ud800", "é.a"):
            with self.subTest(bad=ascii(bad)):
                self.assertIsNone(verify(self.KEY, "session", bad))

    def test_the_fingerprint_ignores_key_order_but_not_values_or_tool(self):
        """An approval's fingerprint follows tool and values, not key order."""
        self.assertEqual(
            fingerprint("t", {"a": 1, "b": 2}), fingerprint("t", {"b": 2, "a": 1})
        )
        self.assertNotEqual(fingerprint("t", {"a": 1}), fingerprint("t", {"a": 2}))
        self.assertNotEqual(fingerprint("t", {"a": 1}), fingerprint("u", {"a": 1}))
