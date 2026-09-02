"""HTTP utility helpers for GitHub API calls.

Centralises a no-redirect opener used by every ``urllib.request.urlopen``
in this codebase that talks to ``api.github.com``. Refusing redirects
closes the SSRF surface where a future API change (or a man-in-the-
middle on the route) could redirect the call to ``http://169.254.169.254``
or another internal address.
"""
import json
import time
import urllib.error
import urllib.request


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse all HTTP redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            "Redirects not allowed", headers, fp,
        )


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


class HTTPBudgetExceeded(Exception):
    """Raised when a bounded GitHub operation exhausts its budget."""


class HTTPBudget:
    """Track request count, transferred bytes and a shared deadline.

    One instance is shared by every GitHub request made for an import so
    per-request timeouts cannot multiply into an unbounded operation.

    :param max_requests: maximum number of attempted HTTP requests
    :param max_bytes: maximum response-body bytes read in total
    :param timeout_seconds: wall-clock budget for the whole operation
    :param clock: monotonic clock override used by deterministic tests
    """

    def __init__(self, max_requests, max_bytes, timeout_seconds, clock=None):
        """Initialize counters and calculate the shared monotonic deadline."""
        self.max_requests = int(max_requests)
        self.max_bytes = int(max_bytes)
        self.requests = 0
        self.bytes_read = 0
        self._clock = clock or time.monotonic
        self.deadline = self._clock() + float(timeout_seconds)

    def begin_request(self):
        """Count one request and fail before opening it when exhausted."""
        self._check_deadline()
        if self.requests >= self.max_requests:
            raise HTTPBudgetExceeded("HTTP request budget exhausted")
        self.requests += 1

    def remaining_timeout(self, per_request_timeout):
        """Return the smaller of the per-request and shared timeouts."""
        remaining = self.deadline - self._clock()
        if remaining <= 0:
            raise HTTPBudgetExceeded("HTTP deadline exhausted")
        return max(0.001, min(float(per_request_timeout), remaining))

    def check(self):
        """Raise when the shared operation deadline has expired."""
        self._check_deadline()

    def read(self, response, max_response_bytes):
        """Read a response body without exceeding per-response/total caps.

        Bytes are charged before decoding, so base64 transport overhead and
        GitHub error bodies count exactly like successful JSON responses.

        :param response: file-like urllib response or ``HTTPError``
        :param max_response_bytes: cap for this individual response
        :return: raw response bytes
        """
        self._check_deadline()
        remaining = self.max_bytes - self.bytes_read
        if remaining <= 0:
            raise HTTPBudgetExceeded("HTTP byte budget exhausted")
        limit = min(int(max_response_bytes), remaining)
        headers = getattr(response, "headers", None) or {}
        content_length = headers.get("Content-Length")
        if content_length:
            try:
                declared = int(content_length)
            except (TypeError, ValueError):
                declared = 0
            if declared > limit:
                raise HTTPBudgetExceeded("HTTP response is too large")
        raw = response.read(limit + 1)
        self.bytes_read += len(raw)
        if len(raw) > limit:
            raise HTTPBudgetExceeded("HTTP response is too large")
        self._check_deadline()
        return raw

    def _check_deadline(self):
        """Raise when the shared operation deadline has expired."""
        if self._clock() >= self.deadline:
            raise HTTPBudgetExceeded("HTTP deadline exhausted")


def read_response_limited(response, budget=None, max_bytes=None):
    """Read *response* with optional shared and per-response bounds.

    Existing callers that pass neither option retain urllib's ordinary
    unbounded read. Security-sensitive import callers always pass both.

    :param response: file-like urllib response or ``HTTPError``
    :param HTTPBudget budget: shared budget, when applicable
    :param int max_bytes: maximum bytes for this response
    :return: raw response bytes
    """
    if budget is not None:
        if max_bytes is None:
            raise ValueError("A bounded request requires max_bytes")
        return budget.read(response, max_bytes)
    if max_bytes is None:
        return response.read()
    raw = response.read(int(max_bytes) + 1)
    if len(raw) > int(max_bytes):
        raise HTTPBudgetExceeded("HTTP response is too large")
    return raw


def read_json_limited(response, budget=None, max_bytes=None):
    """Decode a JSON response after applying transport-size limits."""
    raw = read_response_limited(
        response, budget=budget, max_bytes=max_bytes,
    )
    return json.loads(raw) if raw else {}


def safe_urlopen(req, timeout=10):
    """``urllib.request.urlopen`` equivalent that refuses redirects.

    Used for every fixed-host GitHub API call in this module so the
    target URL cannot be silently redirected to an attacker-controlled
    or internal-network address.
    """
    return _OPENER.open(req, timeout=timeout)
