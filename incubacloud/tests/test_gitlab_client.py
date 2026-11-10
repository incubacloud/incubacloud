"""The GitLab API client.

Only the provider's API URL, a path built from encoded segments, no
redirects, the token in ``PRIVATE-TOKEN`` and nowhere else, a private CA
verified, never switched off, the address pinned under the ``public``
policy, and bounded answers.
"""
import http.client
import json
import socket
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
    # ``read1`` hands out the body in blocks, as the socket does.
    left = [data]

    def read1(n=-1):
        """Return the next block of at most *n* bytes of the body."""
        block, left[0] = left[0][:n], left[0][n:]
        return block

    response.read1.side_effect = read1
    return response


def _connection(response=None):
    """Return a connection mock that has a socket and answers *response*.

    :rtype: MagicMock
    """
    conn = MagicMock(spec=http.client.HTTPSConnection)
    conn.sock = MagicMock(spec=socket.socket)
    if response is not None:
        conn.getresponse.return_value = response
    return conn


class _ClientCase(BaseCase):

    def _call(self, response, method="token_self", pin_ip=None, ca_pem=None,
              api_url="https://gitlab.example/api/v4"):
        """Call *method* of a client whose connection answers *response*.

        :return: ``(result or exception, connection class mock, connection)``
        :rtype: tuple
        """
        conn = _connection(response)
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
        """A TLS failure of the handshake is ``tls``: nothing was sent."""
        for error, code in (
            (ssl.SSLCertVerificationError("bad"), "tls"),
            (TimeoutError(), "timeout"),
            (ConnectionRefusedError(), "unreachable"),
        ):
            conn = _connection()
            conn.connect.side_effect = error
            with self.subTest(code=code), patch.object(
                client_mod.http.client, "HTTPSConnection", autospec=True,
                return_value=conn,
            ), self.assertRaises(client_mod.GitLabError) as caught:
                client_mod.GitLabClient(
                    "https://gitlab.example/api/v4", _TOKEN,
                ).current_user()
            self.assertEqual(caught.exception.code, code)
            conn.request.assert_not_called()

    def test_a_tls_failure_after_the_request_is_sent_is_not_tls(self):
        """A dropped session sending or reading may hide a write GitLab took."""
        for step, error in (
            ("request", ssl.SSLError("bad record")),
            ("getresponse", ssl.SSLEOFError("EOF")),
            ("getresponse", ssl.SSLZeroReturnError("closed")),
        ):
            conn = _connection()
            getattr(conn, step).side_effect = error
            with self.subTest(step=step, error=type(error).__name__), patch.object(
                client_mod.http.client, "HTTPSConnection", autospec=True,
                return_value=conn,
            ), self.assertRaises(client_mod.GitLabError) as caught:
                client_mod.GitLabClient(
                    "https://gitlab.example/api/v4", _TOKEN,
                ).update_hook("projects", 1, 2, "https://panel/x", "s")
            self.assertEqual(caught.exception.code, "unreachable")

    def test_a_spent_budget_fails_before_dialling(self):
        """A registration's calls share one budget: past it, no call is made.

        The code is ``budget``, not ``timeout``: nothing was sent.
        """
        with patch.object(
            client_mod.http.client, "HTTPSConnection", autospec=True,
        ) as made, self.assertRaises(client_mod.GitLabError) as caught:
            client_mod.GitLabClient(
                "https://gitlab.example/api/v4", _TOKEN,
                deadline=client_mod.time.monotonic() - 1,
            ).current_user()
        self.assertEqual(caught.exception.code, "budget")
        made.assert_not_called()

    def test_a_call_waits_no_longer_than_the_budget_left(self):
        conn = _connection(_response(body={}))
        with patch.object(
            client_mod.http.client, "HTTPSConnection", autospec=True,
            return_value=conn,
        ) as made:
            client_mod.GitLabClient(
                "https://gitlab.example/api/v4", _TOKEN,
                deadline=client_mod.time.monotonic() + 5,
            ).current_user()
        self.assertLessEqual(made.call_args.kwargs["timeout"], 5)
        self.assertGreater(made.call_args.kwargs["timeout"], 0)

    def _budgeted(self, conn, deadline):
        """Call ``current_user`` on a client with *deadline* over *conn*.

        :return: the error it raised, or None
        :rtype: GitLabError
        """
        client = client_mod.GitLabClient(
            "https://gitlab.example/api/v4", _TOKEN, deadline=deadline,
        )
        with patch.object(
            client_mod.http.client, "HTTPSConnection", autospec=True,
            return_value=conn,
        ):
            try:
                client.current_user()
            except client_mod.GitLabError as exc:
                return exc
        return None

    def test_each_wait_is_bounded_by_what_the_budget_has_left(self):
        """The socket's timeout is reset before the answer and before each block."""
        calls = []
        body = b'{"id": 1, "name": "' + b"x" * 40000 + b'"}'
        response = _response(raw=body)
        read1 = response.read1.side_effect

        def block(n=-1):
            """Record the read, then hand out the block."""
            calls.append("read1")
            return read1(n)

        response.read1.side_effect = block
        conn = _connection(response)
        conn.sock.settimeout.side_effect = lambda t: calls.append(("settimeout", t))
        conn.getresponse.side_effect = lambda: calls.append("getresponse") or response
        error = self._budgeted(conn, client_mod.time.monotonic() + 8)
        self.assertIsNone(error)
        self.assertEqual(calls[:3], [calls[0], "getresponse", calls[2]])
        self.assertEqual(calls[0][0], "settimeout")
        reads = [i for i, c in enumerate(calls) if c == "read1"]
        self.assertGreater(len(reads), 1, "the body is read in blocks")
        for i in reads:
            self.assertEqual(calls[i - 1][0], "settimeout", "each block resets the wait")
        for call in calls:
            if call not in ("getresponse", "read1"):
                self.assertLessEqual(call[1], 8)
                self.assertGreater(call[1], 0)

    def test_a_budget_spent_while_the_answer_is_read_is_a_timeout(self):
        """The request was sent: not ``budget``, and no block is read past it."""
        response = _response(raw=b'{"a": "' + b"x" * 200000 + b'"}')
        conn = _connection(response)
        blocks = []
        client = client_mod.GitLabClient(
            "https://gitlab.example/api/v4", _TOKEN,
            deadline=client_mod.time.monotonic() + 30,
        )
        read1 = response.read1.side_effect

        def block(n=-1):
            """Spend the budget after the first block."""
            blocks.append(n)
            client.deadline = client_mod.time.monotonic() - 1
            return read1(n)

        response.read1.side_effect = block
        with patch.object(
            client_mod.http.client, "HTTPSConnection", autospec=True,
            return_value=conn,
        ), self.assertRaises(client_mod.GitLabError) as caught:
            client.current_user()
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(len(blocks), 1)

    def test_a_budget_spent_after_the_request_is_sent_is_a_timeout(self):
        """Sent, so GitLab may have applied it: ``timeout``, which is unclear."""
        conn = _connection(_response(body={}))
        client = client_mod.GitLabClient(
            "https://gitlab.example/api/v4", _TOKEN,
            deadline=client_mod.time.monotonic() + 30,
        )

        def send(*_args, **_kwargs):
            """Spend the budget as the request goes out."""
            client.deadline = client_mod.time.monotonic() - 1

        conn.request.side_effect = send
        with patch.object(
            client_mod.http.client, "HTTPSConnection", autospec=True,
            return_value=conn,
        ), self.assertRaises(client_mod.GitLabError) as caught:
            client.update_hook("projects", 1, 2, "https://panel/x", "s")
        self.assertEqual(caught.exception.code, "timeout")
        conn.getresponse.assert_not_called()


class TestGitLabClientHooks(BaseCase):
    """The webhook calls."""

    def _send(self, call, response=None):
        """Run *call* on a client and return ``(result, method, path, body)``.

        :param call: a function taking the client
        :rtype: tuple
        """
        conn = _connection(response or _response(201, {"id": 7}))
        with patch.object(
            client_mod.http.client, "HTTPSConnection", autospec=True,
            return_value=conn,
        ):
            result = call(client_mod.GitLabClient(
                "https://gitlab.example/api/v4", _TOKEN,
            ))
        method, path = conn.request.call_args.args[:2]
        body = conn.request.call_args.kwargs.get("body")
        return result, method, path, json.loads(body) if body else None

    def test_update_hook_always_sends_the_url_and_the_token_together(self):
        """A PUT that changes the URL without the token wipes the secret in GitLab."""
        _r, method, path, body = self._send(lambda c: c.update_hook(
            "projects", 12, 7, "https://panel/cloud/gitlab/webhook/ab", "s3cret",
        ), _response(200, {"id": 7}))
        self.assertEqual((method, path), ("PUT", "/api/v4/projects/12/hooks/7"))
        self.assertEqual(body["url"], "https://panel/cloud/gitlab/webhook/ab")
        self.assertEqual(body["token"], "s3cret")
        self.assertTrue(body["push_events"])
        self.assertTrue(body["merge_requests_events"])
        self.assertTrue(body["enable_ssl_verification"])

    def test_create_hook_posts_the_events_the_panel_uses(self):
        result, method, path, body = self._send(lambda c: c.create_hook(
            "groups", "acme/sub", "https://panel/cloud/gitlab/webhook/ab", "t",
        ))
        self.assertEqual(result, {"id": 7})
        self.assertEqual((method, path), ("POST", "/api/v4/groups/acme%2Fsub/hooks"))
        self.assertEqual(body["token"], "t")
        self.assertFalse(body["tag_push_events"])

    def test_the_test_delivery_is_a_tag_push(self):
        """Never push_events (repeats the last push) nor merge_requests_events."""
        _r, method, path, _b = self._send(
            lambda c: c.test_hook("projects", 12, 7),
        )
        self.assertEqual(
            (method, path), ("POST", "/api/v4/projects/12/hooks/7/test/tag_push_events"),
        )

    def test_the_other_calls(self):
        for call, expected in (
            (lambda c: c.get_project("Acme/Web"), ("GET", "/api/v4/projects/Acme%2FWeb")),
            (lambda c: c.list_hooks("projects", 12), ("GET", "/api/v4/projects/12/hooks")),
            (lambda c: c.get_hook("projects", 12, 7), ("GET", "/api/v4/projects/12/hooks/7")),
            (lambda c: c.delete_hook("groups", 3, 7), ("DELETE", "/api/v4/groups/3/hooks/7")),
        ):
            with self.subTest(expected=expected):
                _r, method, path, _b = self._send(call, _response(200, {}))
                self.assertEqual((method, path), expected)

    def test_the_notes_of_a_merge_request(self):
        """The note calls: listed oldest first, a body in JSON, the path encoded."""
        notes = "/api/v4/projects/acme%2Fweb/merge_requests/12/notes"
        _r, method, path, _b = self._send(
            lambda c: c.list_mr_notes("acme/web", 12), _response(200, []),
        )
        self.assertEqual(method, "GET")
        self.assertEqual(path, f"{notes}?sort=asc&order_by=created_at&per_page=100&page=1")
        _r, method, path, _b = self._send(
            lambda c: c.list_mr_notes("acme/web", 12, page=3), _response(200, []),
        )
        self.assertEqual(path, f"{notes}?sort=asc&order_by=created_at&per_page=100&page=3")
        for call, expected, body in (
            (lambda c: c.create_mr_note("acme/web", 12, "hi"), ("POST", notes), {"body": "hi"}),
            (lambda c: c.update_mr_note("acme/web", 12, 9, "re"), ("PUT", f"{notes}/9"),
             {"body": "re"}),
            (lambda c: c.delete_mr_note("acme/web", 12, 9), ("DELETE", f"{notes}/9"), None),
        ):
            with self.subTest(expected=expected):
                _r, method, path, sent = self._send(call, _response(200, {}))
                self.assertEqual((method, path), expected)
                self.assertEqual(sent, body)

    def test_an_unknown_kind_is_refused(self):
        client = client_mod.GitLabClient("https://gitlab.example/api/v4", _TOKEN)
        with self.assertRaises(ValueError):
            client.list_hooks("users", 1)
