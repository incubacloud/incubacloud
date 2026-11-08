"""The GitLab API client (plan-gitlab.md §4.3, 8-nov-2026).

Only the provider's API URL, a path built from encoded segments, no
redirects, the token in ``PRIVATE-TOKEN`` and nowhere else, a private CA
verified, never switched off, the address pinned under the ``public``
policy, and bounded answers.
"""
import http.client
import json
import ssl
from unittest.mock import MagicMock, patch

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.gitlab import client as client_mod
from odoo.addons.incubacloud.tests import _certs

_TOKEN = "glpat-secret-token-123"


def _response(status=200, body=None, raw=None):
    """Return a response of *status* with a JSON *body* or *raw* bytes.

    :rtype: MagicMock
    """
    response = MagicMock(spec=http.client.HTTPResponse)
    response.status = status
    data = raw if raw is not None else json.dumps(body or {}).encode()
    response.read.side_effect = lambda n=-1: data if n < 0 else data[:n]
    return response


class _ClientCase(BaseCase):

    def _call(self, response, method="token_self", pin_ip=None, ca_pem=None,
              api_url="https://gitlab.example/api/v4"):
        """Call *method* of a client whose connection answers *response*.

        :return: ``(result or exception, connection class mock, connection)``
        :rtype: tuple
        """
        conn = MagicMock(spec=http.client.HTTPSConnection)
        conn.getresponse.return_value = response
        cls = "_PinnedHTTPSConnection" if pin_ip else "HTTPSConnection"
        target = client_mod if pin_ip else client_mod.http.client
        with patch.object(target, cls, autospec=True, return_value=conn) as made:
            client = client_mod.GitLabClient(
                api_url, _TOKEN, ca_pem=ca_pem, pin_ip=pin_ip,
            )
            try:
                result = getattr(client, method)()
            except client_mod.GitLabError as exc:
                result = exc
        return result, made, conn


class TestGitLabClientRequest(_ClientCase):

    def test_the_token_goes_in_its_header_only(self):
        result, made, conn = self._call(_response(body={"scopes": ["api"]}))
        self.assertEqual(result, {"scopes": ["api"]})
        method, path = conn.request.call_args.args[:2]
        headers = conn.request.call_args.kwargs["headers"]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/api/v4/personal_access_tokens/self")
        self.assertEqual(headers["PRIVATE-TOKEN"], _TOKEN)
        self.assertNotIn(_TOKEN, path)
        self.assertEqual(made.call_args.args[0], "gitlab.example")

    def test_a_path_is_built_from_encoded_segments(self):
        client = client_mod.GitLabClient("https://gitlab.example/api/v4", _TOKEN)
        self.assertEqual(
            client._path(["projects", "group/sub/my proj"]),
            "/api/v4/projects/group%2Fsub%2Fmy%20proj",
        )
        self.assertEqual(
            client._path(["projects", "../../admin"]),
            "/api/v4/projects/..%2F..%2Fadmin",
        )

    def test_a_redirect_is_an_error(self):
        result, _made, _conn = self._call(_response(status=302))
        self.assertIsInstance(result, client_mod.GitLabError)
        self.assertEqual(result.code, "redirect")

    def test_an_error_keeps_a_short_message_and_never_the_token(self):
        body = {"message": "401 Unauthorized " + _TOKEN + " x" * 300}
        result, _made, _conn = self._call(_response(status=401, body=body))
        self.assertEqual(result.code, "auth")
        self.assertEqual(result.status, 401)
        self.assertLessEqual(len(result.message), 200)
        self.assertNotIn(_TOKEN, str(result))
        self.assertNotIn(_TOKEN, result.message)

    def test_an_answer_over_the_limit_is_refused(self):
        raw = b"[" + b" " * client_mod.MAX_RESPONSE_BYTES + b"]"
        result, _made, _conn = self._call(_response(raw=raw))
        self.assertEqual(result.code, "too_large")

    def test_a_pinned_address_is_dialled_with_the_name_kept(self):
        _result, made, _conn = self._call(
            _response(body={}), pin_ip="203.0.113.7",
        )
        self.assertEqual(made.call_args.args[:2], ("gitlab.example", "203.0.113.7"))

    def test_a_private_ca_is_verified_not_switched_off(self):
        _result, made, _conn = self._call(_response(body={}), ca_pem=_certs.ca_pem())
        context = made.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_tls_and_network_failures_have_their_codes(self):
        for error, code in (
            (ssl.SSLCertVerificationError("bad"), "tls"),
            (TimeoutError(), "timeout"),
            (ConnectionRefusedError(), "unreachable"),
        ):
            conn = MagicMock(spec=http.client.HTTPSConnection)
            conn.request.side_effect = error
            with self.subTest(code=code), patch.object(
                client_mod.http.client, "HTTPSConnection", autospec=True,
                return_value=conn,
            ), self.assertRaises(client_mod.GitLabError) as caught:
                client_mod.GitLabClient(
                    "https://gitlab.example/api/v4", _TOKEN,
                ).current_user()
            self.assertEqual(caught.exception.code, code)
