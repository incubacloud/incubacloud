"""Who may frame a page: ``frame-ancestors`` added by Odoo, not replaced (#75).

A reverse proxy that sets Content-Security-Policy replaces the one Odoo
sends, so a file Odoo serves lost its ``default-src 'none'`` and a page
that said ``frame-ancestors 'none'`` was framed by its own origin. The
directive is now added in ``ir.http``: the unit tests read the rule, the
HTTP ones the headers a browser gets.
"""

from base64 import b64encode

from werkzeug.datastructures import Headers

from odoo.tests import new_test_user, tagged
from odoo.tests.common import BaseCase, HttpCase

from odoo.addons.incubacloud.models.ir_http_framing import add_frame_ancestors

#: An SVG that runs a script when a browser opens it as a document.
SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1">'
    b"<script>document.title = 'ran'</script></svg>"
)


def _added(**sent):
    """Run the rule on a response that sent *sent*, and return its headers.

    :param sent: header name (``_`` for ``-``) to its value, or to a list
        of values for a header sent more than once
    :return: the headers as a browser would get them
    """
    headers = Headers()
    for name, value in sent.items():
        for one in value if isinstance(value, list) else [value]:
            headers.add(name.replace("_", "-"), one)
    add_frame_ancestors(headers)
    return headers


class TestTheRule(BaseCase):
    """What ``add_frame_ancestors`` writes for each response it is handed."""

    def test_a_response_without_a_policy_is_framed_by_its_origin(self):
        """No CSP and no X-Frame-Options: ``'self'``, and SAMEORIGIN."""
        headers = _added()
        self.assertEqual(headers.getlist("Content-Security-Policy"), ["frame-ancestors 'self'"])
        self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")

    def test_a_policy_keeps_its_directives_and_gains_the_frame(self):
        """Odoo's ``default-src 'none'`` stays; the directive follows it."""
        for sent in ("default-src 'none'", "default-src 'none';", " default-src 'none' ; "):
            with self.subTest(sent=sent):
                headers = _added(Content_Security_Policy=sent)
                self.assertEqual(
                    headers.getlist("Content-Security-Policy"),
                    ["default-src 'none'; frame-ancestors 'self'"],
                )
                self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")

    def test_a_response_that_names_its_ancestors_is_left_alone(self):
        """Its own ``frame-ancestors`` wins, whatever its case or place."""
        for sent in (
            "frame-ancestors 'none'",
            "default-src 'self'; Frame-Ancestors https://a.example",
            ["default-src 'none'", "frame-ancestors 'none'"],
        ):
            with self.subTest(sent=sent):
                headers = _added(Content_Security_Policy=sent)
                self.assertEqual(
                    headers.getlist("Content-Security-Policy"),
                    sent if isinstance(sent, list) else [sent],
                )
                self.assertNotIn("X-Frame-Options", headers)

    def test_x_frame_options_is_kept_and_written_as_the_directive(self):
        """DENY becomes ``'none'`` and SAMEORIGIN ``'self'``; the header stays."""
        for sent, source in (
            ("DENY", "'none'"),
            ("deny", "'none'"),
            ("SAMEORIGIN", "'self'"),
            ("ALLOW-FROM https://a.example", "'self'"),
        ):
            with self.subTest(sent=sent):
                headers = _added(X_Frame_Options=sent)
                self.assertEqual(
                    headers.getlist("Content-Security-Policy"),
                    [f"frame-ancestors {source}"],
                )
                self.assertEqual(headers.getlist("X-Frame-Options"), [sent])

    def test_a_directive_whose_name_only_starts_alike_does_not_count(self):
        """``frame-src`` and a source that reads ``frame-ancestors`` are not it."""
        headers = _added(
            Content_Security_Policy="frame-src 'self'; script-src frame-ancestors.example"
        )
        self.assertEqual(
            headers["Content-Security-Policy"],
            "frame-src 'self'; script-src frame-ancestors.example; "
            "frame-ancestors 'self'",
        )

    def test_only_the_first_of_several_policies_is_extended(self):
        """A second policy sent apart stays as it came."""
        headers = _added(Content_Security_Policy=["default-src 'none'", "img-src 'self'"])
        self.assertEqual(
            headers.getlist("Content-Security-Policy"),
            ["default-src 'none'; frame-ancestors 'self'", "img-src 'self'"],
        )


@tagged("post_install", "-at_install")
class TestTheHeadersSent(HttpCase):
    """The headers a browser gets from the panel, with no proxy in front."""

    def setUp(self):
        """Create an internal user of its own to sign in with.

        The database's admin may not have the password ``admin`` (a copy
        of a real panel is neutralized with another), so the pages behind
        a sign-in are read as a user whose password the test knows.
        """
        super().setUp()
        new_test_user(
            self.env, login="framing-user", password="framing-user-pw",
            groups="base.group_user",
        )

    def _get(self, path):
        """Fetch *path* without following a redirect, and check it answered.

        :param str path: the address on this database
        :return: the response
        """
        response = self.url_open(path, allow_redirects=False)
        self.assertEqual(response.status_code, 200, f"{path}: {response.text[:300]}")
        return response

    def assertFraming(self, response, policy, frame_options):
        """Check the response's Content-Security-Policy and X-Frame-Options.

        :param response: the response read
        :param str policy: the whole Content-Security-Policy expected
        :param str frame_options: the X-Frame-Options expected
        """
        self.assertEqual(response.headers.get("Content-Security-Policy"), policy)
        self.assertEqual(response.headers.get("X-Frame-Options"), frame_options)

    def test_the_panel_is_framed_only_by_its_origin(self):
        """``/cloud`` sets no framing of its own: it gets ``'self'``."""
        self.authenticate("framing-user", "framing-user-pw")
        self.assertFraming(self._get("/cloud"), "frame-ancestors 'self'", "SAMEORIGIN")

    def test_the_sign_in_keeps_odoos_own_policy(self):
        """``/web/login`` names its ancestors already; nothing is added."""
        self.assertFraming(
            self._get("/web/login"), "frame-ancestors 'self'", "SAMEORIGIN"
        )

    def test_the_web_client_is_never_framed(self):
        """``/odoo`` sends X-Frame-Options DENY; its CSP says ``'none'`` too.

        Read as an administrator: a module on top may send any other
        user from the web client to the panel, and the redirect is not
        the page whose headers this test reads.
        """
        new_test_user(
            self.env, login="framing-admin", password="framing-admin-pw",
            groups="base.group_user,base.group_system",
        )
        self.authenticate("framing-admin", "framing-admin-pw")
        self.assertFraming(self._get("/odoo"), "frame-ancestors 'none'", "DENY")

    def test_an_svg_keeps_default_src_none_and_gains_the_frame(self):
        """A file Odoo serves keeps its sandbox, by every address that serves it."""
        attachment = self.env["ir.attachment"].create({
            "name": "drawing.svg",
            "datas": b64encode(SVG),
            "mimetype": "image/svg+xml",
            "public": True,
        })
        for path in (
            f"/web/content/{attachment.id}",
            f"/web/image/{attachment.id}",
        ):
            with self.subTest(path=path):
                response = self._get(path)
                self.assertTrue(
                    response.headers["Content-Type"].startswith("image/svg+xml"),
                    response.headers["Content-Type"],
                )
                self.assertFraming(
                    response,
                    "default-src 'none'; frame-ancestors 'self'",
                    "SAMEORIGIN",
                )
