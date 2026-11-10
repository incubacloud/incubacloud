"""A client of GitLab's REST API.

- Only the provider's ``api_url``; a path is built from segments, each
  percent-encoded, never from text a user typed.
- No redirects: a 3xx is the error ``redirect``.
- With the ``public`` network policy, the address the panel checked is
  dialled (``net.outbound._PinnedHTTPSConnection``), the name kept for
  SNI and the certificate.
- A private CA is trusted with ``ssl.create_default_context(cadata=…)``,
  verification never switched off.
- The token goes in ``PRIVATE-TOKEN`` and nowhere else: not the URL, a
  log or an error.
- Answers are bounded: 4 MB, an error's body 64 KB, of which 200
  characters of GitLab's message are kept.
- With a ``deadline``, the socket's timeout is reset to what the budget
  has left before the answer and before each block of its body.

It reads the token's introspection (``token_self``), its user
(``current_user``) and the webhooks of a project or group, and writes
the panel's note on a merge request.
"""
import http.client
import json
import ssl
import time
from urllib.parse import quote, urlencode, urlsplit

from ..net.outbound import _PinnedHTTPSConnection

#: Seconds a request to the API may take: shorter than
#: ``GitHubWriter``'s 30 s, as it also runs inside panel routes.
REQUEST_TIMEOUT = 20

#: Bytes of an answer read at most.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

#: Bytes of an error's body read at most.
MAX_ERROR_BYTES = 64 * 1024

#: Characters of GitLab's message an error keeps.
MAX_MESSAGE_CHARS = 200

#: Bytes asked of the socket at a time reading an answer: each block
#: gets its own wait, bounded by what the budget has left.
READ_BLOCK_BYTES = 16 * 1024

#: Notes of a merge request on each page read: GitLab's largest page.
NOTES_PER_PAGE = 100

#: The code of an HTTP status that is not a success.
_STATUS_CODES = {401: "auth", 403: "forbidden", 404: "not_found", 429: "rate_limited"}


def _hook_kind(kind):
    """Return *kind* if it is ``projects`` or ``groups``.

    :raises ValueError: for anything else
    """
    if kind not in ("projects", "groups"):
        raise ValueError(f"no webhooks on {kind!r}")
    return kind


def _hook_body(url, token):
    """Return the body of a webhook the panel creates or rewrites.

    :rtype: dict
    """
    return {
        "url": url, "token": token, "push_events": True,
        "merge_requests_events": True, "tag_push_events": False,
        "enable_ssl_verification": True,
    }


class GitLabError(Exception):
    """A call that failed, with its code, its status and GitLab's message.

    ``code``: ``auth``, ``forbidden``, ``not_found``, ``rate_limited``,
    ``redirect``, ``tls``, ``budget``, ``timeout``, ``unreachable``,
    ``too_large`` or ``http``. ``tls`` is a handshake that failed, so
    nothing was sent; ``budget`` is a shared budget spent before the
    request was sent. A TLS error after that is ``unreachable`` and a
    budget spent after it ``timeout``: the server may have applied the
    request. Nothing in it holds the token or a header.
    """

    def __init__(self, code, status=None, message=""):
        """:param str code: what failed
        :param int status: the HTTP status, when there was one
        :param str message: GitLab's message, already cut and cleaned
        """
        super().__init__(f"{code} {status or ''} {message}".strip())
        self.code = code
        self.status = status
        self.message = message


class GitLabClient:
    """Talk to one GitLab server's API with one token.

    :param str api_url: the provider's API URL (``<base>/api/v4``)
    :param str token: the credential's token
    :param str ca_pem: the server's private CA bundle, or None
    :param str pin_ip: the address to dial (``public`` policy), or None
    :param int timeout: seconds per request
    :param float deadline: a ``time.monotonic()`` past which no request
        starts and none waits, or None: the calls of one operation share
        one budget (a registration holds the webhook's row meanwhile)
    """

    def __init__(self, api_url, token, ca_pem=None, pin_ip=None,
                 timeout=REQUEST_TIMEOUT, deadline=None):
        parts = urlsplit(api_url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("the API URL must be https")
        self.host = parts.hostname
        self.port = parts.port or 443
        self.prefix = parts.path.rstrip("/")
        self.token = token
        self.ca_pem = ca_pem
        self.pin_ip = pin_ip
        self.timeout = timeout
        self.deadline = deadline

    # ── Calls ──────────────────────────────────────────────────────────

    def token_self(self):
        """Return what GitLab says of the token itself.

        ``GET /personal_access_tokens/self`` answers for personal, group
        and project tokens of any scope, ``read_repository`` alone
        included, with ``scopes``, ``expires_at``, ``active`` and
        ``user_id``; a deploy token gets a 401.

        :rtype: dict
        :raises GitLabError: when the call fails
        """
        return self._get(["personal_access_tokens", "self"])

    def current_user(self):
        """Return the user the token acts as (needs ``api`` or ``read_api``).

        :rtype: dict
        :raises GitLabError: when the call fails
        """
        return self._get(["user"])

    def get_project(self, path_or_id):
        """Return a project by its path or id (``GET /projects/:id``).

        :param path_or_id: ``group/sub/project`` or the numeric id
        :rtype: dict
        :raises GitLabError: when the call fails
        """
        return self._get(["projects", path_or_id])

    def list_hooks(self, kind, target):
        """Return the webhooks of a project or a group.

        :param str kind: ``projects`` or ``groups``
        :param target: the project's or group's id or path
        :rtype: list
        :raises GitLabError: when the call fails
        """
        return self._get([_hook_kind(kind), target, "hooks"])

    def get_hook(self, kind, target, hook_id):
        """Return one webhook, with its ``alert_status`` and ``disabled_until``.

        :rtype: dict
        :raises GitLabError: when the call fails
        """
        return self._get([_hook_kind(kind), target, "hooks", hook_id])

    def create_hook(self, kind, target, url, token):
        """Create the panel's webhook: pushes and merge requests, TLS verified.

        :param str url: the panel's receiver URL
        :param str token: the secret GitLab sends in ``X-Gitlab-Token``
        :rtype: dict
        :raises GitLabError: when the call fails
        """
        return self._request(
            "POST", [_hook_kind(kind), target, "hooks"], _hook_body(url, token),
        )

    def update_hook(self, kind, target, hook_id, url, token):
        """Rewrite a webhook, always with its URL and its token together.

        A ``PUT`` that changes the URL without the token wipes the secret
        in GitLab, and every delivery after it is refused.

        :rtype: dict
        :raises GitLabError: when the call fails
        """
        return self._request(
            "PUT", [_hook_kind(kind), target, "hooks", hook_id],
            _hook_body(url, token),
        )

    def delete_hook(self, kind, target, hook_id):
        """Delete a webhook.

        :raises GitLabError: when the call fails (``not_found`` included)
        """
        return self._request("DELETE", [_hook_kind(kind), target, "hooks", hook_id])

    def test_hook(self, kind, target, hook_id):
        """Ask GitLab for a test delivery of a tag push.

        A tag push rebuilds nothing; ``push_events`` would repeat the last
        real push and ``merge_requests_events`` the last merge request. A
        test answered 2xx also re-enables a disabled webhook.

        :raises GitLabError: ``rate_limited`` past GitLab's 5 tests a minute
        """
        return self._request(
            "POST",
            [_hook_kind(kind), target, "hooks", hook_id, "test", "tag_push_events"],
        )

    def list_mr_notes(self, project, iid, page=1):
        """Return one page of a merge request's notes, oldest first.

        The panel's note is written when the preview is up, early in the
        merge request's life: oldest first, the first pages hold it.

        :param project: the project's id or path
        :param int iid: the merge request's number in the project
        :param int page: the page, from 1, of :data:`NOTES_PER_PAGE` notes
        :rtype: list
        :raises GitLabError: when the call fails
        """
        return self._get(
            ["projects", project, "merge_requests", iid, "notes"],
            query={
                "sort": "asc", "order_by": "created_at",
                "per_page": NOTES_PER_PAGE, "page": int(page),
            },
        )

    def create_mr_note(self, project, iid, body):
        """Write a note on a merge request (``api`` scope).

        :param str body: the note's Markdown
        :return: the note, with its ``id``
        :rtype: dict
        :raises GitLabError: when the call fails
        """
        return self._request(
            "POST", ["projects", project, "merge_requests", iid, "notes"],
            {"body": body},
        )

    def update_mr_note(self, project, iid, note_id, body):
        """Rewrite a note of a merge request.

        :rtype: dict
        :raises GitLabError: when the call fails (``not_found`` included)
        """
        return self._request(
            "PUT", ["projects", project, "merge_requests", iid, "notes", note_id],
            {"body": body},
        )

    def delete_mr_note(self, project, iid, note_id):
        """Delete a note of a merge request.

        :raises GitLabError: when the call fails (``not_found`` included)
        """
        return self._request(
            "DELETE", ["projects", project, "merge_requests", iid, "notes", note_id],
        )

    # ── Plumbing ───────────────────────────────────────────────────────

    def _path(self, segments):
        """Return the request path of *segments*, each one encoded whole.

        ``["projects", "group/sub/proj"]`` gives
        ``<prefix>/projects/group%2Fsub%2Fproj``: a segment never adds a
        level, nor climbs one.

        :param segments: the path's segments
        :rtype: str
        """
        return self.prefix + "".join("/" + quote(str(s), safe="") for s in segments)

    def _context(self):
        """Return the TLS context: the system's roots, or the private CA.

        :rtype: ssl.SSLContext
        """
        if self.ca_pem:
            return ssl.create_default_context(cadata=self.ca_pem)
        return ssl.create_default_context()

    def _timeout(self, sent=False):
        """Return the seconds the next wait may last: the timeout, or the budget left.

        :param bool sent: whether the request already went out; a budget
            spent after it is ``timeout`` (unclear), before it ``budget``
        :rtype: float
        :raises GitLabError: ``budget`` or ``timeout`` when the budget is spent
        """
        if self.deadline is None:
            return self.timeout
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise GitLabError("timeout" if sent else "budget")
        return min(self.timeout, left)

    def _connection(self):
        """Return a connection to the server, pinned when an address is given.

        :rtype: http.client.HTTPSConnection
        :raises GitLabError: ``budget`` when the budget is spent
        """
        timeout = self._timeout()
        if self.pin_ip:
            return _PinnedHTTPSConnection(
                self.host, self.pin_ip, port=self.port, timeout=timeout,
                context=self._context(),
            )
        return http.client.HTTPSConnection(
            self.host, port=self.port, timeout=timeout,
            context=self._context(),
        )

    def _get(self, segments, query=None):
        """GET *segments* and return the decoded JSON.

        :param dict query: the query string's parameters, or None
        :rtype: dict or list
        :raises GitLabError: on any failure, with its code
        """
        return self._request("GET", segments, query=query)

    def _request(self, method, segments, body=None, query=None):
        """Send one request and return the decoded JSON answer.

        :param str method: the HTTP method
        :param segments: the path's segments
        :param dict body: a JSON body, or None
        :param dict query: the query string's parameters, or None
        :rtype: dict or list
        :raises GitLabError: on any failure, with its code
        """
        headers = {
            "PRIVATE-TOKEN": self.token,
            "Accept": "application/json",
            "User-Agent": "incubacloud/1.0",
        }
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn = self._connection()
        try:
            try:
                conn.connect()
            except ssl.SSLError as exc:
                # The handshake failed: nothing was sent.
                raise GitLabError("tls") from exc
            sock = conn.sock
            path = self._path(segments)
            if query:
                path += "?" + urlencode(query)
            conn.request(method, path, body=payload, headers=headers)
            self._bound_wait(sock)
            response = conn.getresponse()
            return self._answer(response, sock)
        except TimeoutError as exc:
            raise GitLabError("timeout") from exc
        except OSError as exc:
            # A TLS error from here on (``SSLEOFError``, ``SSLZeroReturnError``
            # reading the answer) may follow a request GitLab applied.
            raise GitLabError("unreachable") from exc
        finally:
            conn.close()

    def _bound_wait(self, sock):
        """Limit the next wait on *sock* to what the budget has left.

        The connection's timeout was set when it was made: without this a
        wait could last the whole of it, after the budget ran out.

        :param socket.socket sock: the connection's socket, taken before
            ``getresponse`` (which detaches it from the connection when
            the server closes after answering)
        :raises GitLabError: ``timeout`` when the budget is spent
        """
        sock.settimeout(self._timeout(sent=True))

    def _read_body(self, response, sock, limit):
        """Read at most *limit* bytes of *response*, a block at a time.

        Each block waits at most what the budget has left.

        :param response: the answer
        :param socket.socket sock: the connection's socket
        :param int limit: the bytes to read at most
        :return: the bytes read, and whether the body goes on past *limit*
        :rtype: tuple
        :raises GitLabError: ``timeout`` when the budget is spent
        """
        blocks, size = [], 0
        while size <= limit:
            self._bound_wait(sock)
            block = response.read1(min(READ_BLOCK_BYTES, limit + 1 - size))
            if not block:
                break
            blocks.append(block)
            size += len(block)
        raw = b"".join(blocks)
        return raw[:limit], len(raw) > limit

    def _answer(self, response, sock):
        """Return the JSON of a successful *response*, or raise its error.

        :param socket.socket sock: the connection's socket
        :rtype: dict or list
        :raises GitLabError: on a redirect, an error status, an answer
            too large or one that is not JSON
        """
        status = response.status
        if 300 <= status < 400:
            raise GitLabError("redirect", status)
        if status >= 400:
            raise GitLabError(
                _STATUS_CODES.get(status, "http"), status,
                self._message(response, sock),
            )
        raw, too_large = self._read_body(response, sock, MAX_RESPONSE_BYTES)
        if too_large:
            raise GitLabError("too_large", status)
        try:
            return json.loads(raw) if raw else {}
        except ValueError as exc:
            raise GitLabError("http", status) from exc

    def _message(self, response, sock):
        """Return GitLab's message of an error *response*, cut and cleaned.

        :param socket.socket sock: the connection's socket
        :rtype: str
        """
        try:
            raw, _more = self._read_body(response, sock, MAX_ERROR_BYTES)
            data = json.loads(raw) if raw else {}
        except (ValueError, OSError, GitLabError):
            return ""
        text = ""
        if isinstance(data, dict):
            text = data.get("message") or data.get("error") or ""
        text = text if isinstance(text, str) else json.dumps(text)
        if self.token:
            text = text.replace(self.token, "***")
        return " ".join(text.split())[:MAX_MESSAGE_CHARS]
