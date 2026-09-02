"""GitHub App API client.

Authentication flow:
  1. Generate a short-lived JWT signed with the App's private key.
  2. Exchange the JWT for an installation access token (POST /app/installations/{id}/access_tokens).
  3. Use the installation token as a Bearer token for subsequent API calls.

Installation tokens are cached in memory (see token_cache.py) and are never
persisted to the database. The private key is used only in step 1 and is
never logged or transmitted.
"""

import datetime
import json
import logging
import time
import urllib.error
import urllib.request

from .credentials import GitHubAppCredentials
from .http_utils import read_json_limited, read_response_limited, safe_urlopen
from .jwt_utils import generate_github_app_jwt
from . import token_cache

_logger = logging.getLogger(__name__)

_BASE_URL = "https://api.github.com"
_GITHUB_API_VERSION = "2022-11-28"
_ACCEPT_JSON = "application/vnd.github+json"
_TIMEOUT = 15  # seconds
_AUTH_RESPONSE_BYTES = 256 * 1024


class GitHubPATClient:
    """GitHub API client authenticated with a Personal Access Token."""

    def __init__(self, pat: str):
        self._pat = pat

    def _request(self, method: str, endpoint: str, *, budget=None,
                 max_bytes=None) -> dict:
        """Perform one authenticated request with optional shared limits."""
        url = f"{_BASE_URL}{endpoint}"
        req = urllib.request.Request(
            url,
            method=method.upper(),
            headers={
                "Authorization": f"Bearer {self._pat}",
                "Accept": _ACCEPT_JSON,
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
        )
        if budget is not None:
            budget.begin_request()
            timeout = budget.remaining_timeout(_TIMEOUT)
        else:
            timeout = _TIMEOUT
        try:
            with safe_urlopen(req, timeout=timeout) as resp:  # nosec B310 — hardcoded https://api.github.com
                return read_json_limited(
                    resp, budget=budget, max_bytes=max_bytes,
                )
        except urllib.error.HTTPError as exc:
            raw = read_response_limited(
                exc, budget=budget, max_bytes=max_bytes,
            )
            raise GitHubAPIError(
                exc.code, raw.decode(errors="replace"),
            ) from exc

    def get(self, endpoint: str, *, budget=None, max_bytes=None) -> dict:
        """Fetch a GitHub API endpoint with optional shared limits."""
        return self._request(
            "GET", endpoint, budget=budget, max_bytes=max_bytes,
        )

    def get_installation_token(self) -> str:
        """PAT is used directly as the clone token."""
        return self._pat


class GitHubAPIError(Exception):
    """Raised when the GitHub API returns a non-2xx status code."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        super().__init__(f"GitHub API error {status_code}: {body[:200]}")


class GitHubAppClient:
    """HTTP client authenticated as a GitHub App installation."""

    def __init__(self, credentials: GitHubAppCredentials):
        self._creds = credentials

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _installation_token(self, *, budget=None, max_bytes=None) -> str:
        """Return a valid installation access token (from cache or freshly issued)."""
        cached = token_cache.get_token(self._creds.installation_id)
        if cached:
            return cached

        jwt = generate_github_app_jwt(self._creds.app_id, self._creds.private_key_pem)
        url = (
            f"{_BASE_URL}/app/installations/"
            f"{self._creds.installation_id}/access_tokens"
        )
        req = urllib.request.Request(
            url,
            method="POST",
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": _ACCEPT_JSON,
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
            data=b"",
        )
        if budget is not None:
            budget.begin_request()
            timeout = budget.remaining_timeout(_TIMEOUT)
        else:
            timeout = _TIMEOUT
        auth_max_bytes = (
            min(max_bytes, _AUTH_RESPONSE_BYTES)
            if max_bytes is not None else (
                _AUTH_RESPONSE_BYTES if budget is not None else None
            )
        )
        try:
            with safe_urlopen(req, timeout=timeout) as resp:  # nosec B310 — hardcoded https://api.github.com
                data = read_json_limited(
                    resp, budget=budget, max_bytes=auth_max_bytes,
                )
        except urllib.error.HTTPError as exc:
            raw = read_response_limited(
                exc, budget=budget, max_bytes=auth_max_bytes,
            )
            raise GitHubAPIError(
                exc.code, raw.decode(errors="replace"),
            ) from exc

        token = data["token"]
        expires_str = data.get("expires_at", "")
        try:
            expires_at = datetime.datetime.fromisoformat(
                expires_str.replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            expires_at = time.time() + 3600

        token_cache.set_token(self._creds.installation_id, token, expires_at)
        _logger.debug(
            "GitHub installation token refreshed (app_id=%s)", self._creds.app_id
        )
        return token

    def _request(self, method: str, endpoint: str, data: dict | None = None,
                 *, budget=None, max_bytes=None) -> dict:
        """Perform one installation request with optional shared limits."""
        token = self._installation_token(
            budget=budget, max_bytes=max_bytes,
        )
        url = f"{_BASE_URL}{endpoint}"
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            url,
            method=method.upper(),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": _ACCEPT_JSON,
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                "Content-Type": "application/json",
            },
            data=body,
        )
        if budget is not None:
            budget.begin_request()
            timeout = budget.remaining_timeout(_TIMEOUT)
        else:
            timeout = _TIMEOUT
        try:
            with safe_urlopen(req, timeout=timeout) as resp:  # nosec B310 — hardcoded https://api.github.com
                return read_json_limited(
                    resp, budget=budget, max_bytes=max_bytes,
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                token_cache.invalidate(self._creds.installation_id)
            if exc.code == 204:  # No Content (e.g. DELETE success)
                return {}
            raw = read_response_limited(
                exc, budget=budget, max_bytes=max_bytes,
            )
            raise GitHubAPIError(
                exc.code, raw.decode(errors="replace"),
            ) from exc

    # ── Public API ─────────────────────────────────────────────────────────────

    def get(self, endpoint: str, *, budget=None, max_bytes=None) -> dict:
        """Fetch a GitHub API endpoint with optional shared limits."""
        return self._request(
            "GET", endpoint, budget=budget, max_bytes=max_bytes,
        )

    def post(self, endpoint: str, data: dict | None = None) -> dict:
        return self._request("POST", endpoint, data)

    def patch(self, endpoint: str, data: dict | None = None) -> dict:
        return self._request("PATCH", endpoint, data)

    def delete(self, endpoint: str) -> dict:
        return self._request("DELETE", endpoint)

    # ── Issue / PR comments ────────────────────────────────────────────────────

    def post_issue_comment(self, owner: str, repo: str,
                           issue_number: int, body: str) -> dict:
        """Create a comment on a GitHub issue or PR."""
        return self.post(
            f'/repos/{owner}/{repo}/issues/{issue_number}/comments',
            {'body': body},
        )

    def patch_issue_comment(self, owner: str, repo: str,
                            comment_id: int, body: str) -> dict:
        """Update an existing comment on a GitHub issue or PR."""
        return self.patch(
            f'/repos/{owner}/{repo}/issues/comments/{comment_id}',
            {'body': body},
        )

    def delete_issue_comment(self, owner: str, repo: str,
                             comment_id: int) -> None:
        """Delete a comment from a GitHub issue or PR."""
        self.delete(f'/repos/{owner}/{repo}/issues/comments/{comment_id}')

    def get_installation_token(self) -> str:
        """Return a valid installation access token (cached)."""
        return self._installation_token()

    def _jwt_get(self, endpoint: str) -> dict:
        """Make a JWT-authenticated GET request (no installation token needed)."""
        jwt = generate_github_app_jwt(
            self._creds.app_id, self._creds.private_key_pem
        )
        req = urllib.request.Request(
            f"{_BASE_URL}{endpoint}",
            method="GET",
            headers={
                "Authorization": f"Bearer {jwt}",
                "Accept": _ACCEPT_JSON,
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            },
        )
        with safe_urlopen(req, timeout=_TIMEOUT) as resp:  # nosec B310 — hardcoded https://api.github.com
            return json.loads(resp.read())

    def list_installations(self) -> list:
        """Return all active installations for this GitHub App.

        Uses JWT auth (no installation token needed). Returns a list of
        ``{id, account_login, account_type}`` dicts.
        """
        data = self._jwt_get("/app/installations")
        return [
            {
                "id": str(item.get("id", "")),
                "account_login": item.get("account", {}).get("login", ""),
                "account_type": item.get("account", {}).get("type", ""),
            }
            for item in (data if isinstance(data, list) else [])
        ]

    def test_connection(self) -> dict:
        """Verify credentials by authenticating as the App and fetching a token.

        Returns a dict with ``ok`` (bool) and either ``app_name``/``app_id``
        (on success) or ``error`` (on failure).
        """
        try:
            app_info = self._jwt_get("/app")

            # Also verify that an installation token can be obtained
            self._installation_token()

            return {
                "ok": True,
                "app_name": app_info.get("name", ""),
                "app_id": str(app_info.get("id", "")),
                "installation_id": self._creds.installation_id,
            }
        except GitHubAPIError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


class GitHubAnonymousClient:
    """Read-only GitHub API client for public repositories."""

    def _request(self, endpoint: str, *, budget=None, max_bytes=None) -> dict:
        """Perform one anonymous request with optional shared limits."""
        req = urllib.request.Request(
            f"{_BASE_URL}{endpoint}",
            method="GET",
            headers={
                "Accept": _ACCEPT_JSON,
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                "User-Agent": "incubacloud/1.0",
            },
        )
        if budget is not None:
            budget.begin_request()
            timeout = budget.remaining_timeout(_TIMEOUT)
        else:
            timeout = _TIMEOUT
        try:
            with safe_urlopen(req, timeout=timeout) as resp:  # nosec B310 — hardcoded https://api.github.com
                return read_json_limited(
                    resp, budget=budget, max_bytes=max_bytes,
                )
        except urllib.error.HTTPError as exc:
            raw = read_response_limited(
                exc, budget=budget, max_bytes=max_bytes,
            )
            raise GitHubAPIError(
                exc.code, raw.decode(errors="replace"),
            ) from exc

    def get(self, endpoint: str, *, budget=None, max_bytes=None) -> dict:
        """Fetch a public GitHub API endpoint with optional shared limits."""
        return self._request(
            endpoint, budget=budget, max_bytes=max_bytes,
        )
