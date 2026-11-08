"""A client of GitLab's REST API (plan-gitlab.md §4.3, 8-nov-2026).

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
- Answers are bounded (§9): 4 MB, an error's body 64 KB, of which 200
  characters of GitLab's message are kept.

G1 needs only the token's introspection (``token_self``) and its user
(``current_user``); the calls of webhooks, merge request notes and
writes arrive with G3 to G5.
"""
import http.client
import json
import ssl
from urllib.parse import quote, urlsplit

from ..github.http_utils import HTTPBudgetExceeded, read_response_limited
from ..net.outbound import _PinnedHTTPSConnection

#: Seconds a request to the API may take (§9): shorter than
#: ``GitHubWriter``'s 30 s, as it also runs inside panel routes.
REQUEST_TIMEOUT = 20

#: Bytes of an answer read at most (§9).
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

#: Bytes of an error's body read at most (§9).
MAX_ERROR_BYTES = 64 * 1024

#: Characters of GitLab's message an error keeps.
MAX_MESSAGE_CHARS = 200

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
    ``redirect``, ``tls``, ``timeout``, ``unreachable``, ``too_large`` or
    ``http``. Nothing in it holds the token or a header.
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
    """

    def __init__(self, api_url, token, ca_pem=None, pin_ip=None,
                 timeout=REQUEST_TIMEOUT):
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

    # ── Calls ──────────────────────────────────────────────────────────

    def token_self(self):
        """Return what GitLab says of the token itself.

        ``GET /personal_access_tokens/self`` answers for personal, group
        and project tokens of any scope, ``read_repository`` alone
        included, with ``scopes``, ``expires_at``, ``active`` and
        ``user_id``; a deploy token gets a 401 (spike, §2).

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
        in GitLab, and every delivery after it is refused (spike §1).

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
        test answered 2xx also re-enables a disabled webhook (§4.7.4).

        :raises GitLabError: ``rate_limited`` past GitLab's 5 tests a minute
        """
        return self._request(
            "POST",
            [_hook_kind(kind), target, "hooks", hook_id, "test", "tag_push_events"],
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

    def _connection(self):
        """Return a connection to the server, pinned when an address is given.

        :rtype: http.client.HTTPSConnection
        """
        if self.pin_ip:
            return _PinnedHTTPSConnection(
                self.host, self.pin_ip, port=self.port, timeout=self.timeout,
                context=self._context(),
            )
        return http.client.HTTPSConnection(
            self.host, port=self.port, timeout=self.timeout,
            context=self._context(),
        )

    def _get(self, segments):
        """GET *segments* and return the decoded JSON.

        :rtype: dict or list
        :raises GitLabError: on any failure, with its code
        """
        return self._request("GET", segments)

    def _request(self, method, segments, body=None):
        """Send one request and return the decoded JSON answer.

        :param str method: the HTTP method
        :param segments: the path's segments
        :param dict body: a JSON body, or None
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
            conn.request(method, self._path(segments), body=payload, headers=headers)
            response = conn.getresponse()
            return self._answer(response)
        except ssl.SSLError as exc:
            raise GitLabError("tls") from exc
        except TimeoutError as exc:
            raise GitLabError("timeout") from exc
        except OSError as exc:
            raise GitLabError("unreachable") from exc
        finally:
            conn.close()

    def _answer(self, response):
        """Return the JSON of a successful *response*, or raise its error.

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
                self._message(response),
            )
        try:
            raw = read_response_limited(response, max_bytes=MAX_RESPONSE_BYTES)
        except HTTPBudgetExceeded as exc:
            raise GitLabError("too_large", status) from exc
        try:
            return json.loads(raw) if raw else {}
        except ValueError as exc:
            raise GitLabError("http", status) from exc

    def _message(self, response):
        """Return GitLab's message of an error *response*, cut and cleaned.

        :rtype: str
        """
        try:
            raw = response.read(MAX_ERROR_BYTES)
            data = json.loads(raw) if raw else {}
        except (ValueError, OSError):
            return ""
        text = ""
        if isinstance(data, dict):
            text = data.get("message") or data.get("error") or ""
        text = text if isinstance(text, str) else json.dumps(text)
        if self.token:
            text = text.replace(self.token, "***")
        return " ".join(text.split())[:MAX_MESSAGE_CHARS]
