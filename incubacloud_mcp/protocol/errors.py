"""JSON-RPC error codes, and the two exceptions that carry an HTTP status."""

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022
#: 2025-11-25 only: the request needs a URL-mode elicitation first.
URL_ELICITATION_REQUIRED = -32042


class McpError(Exception):
    """A protocol-level failure, answered as a JSON-RPC error."""

    def __init__(self, status, code, message, data=None):
        """Keep what the response needs.

        :param int status: HTTP status of the response
        :param int code: JSON-RPC error code
        :param str message: human-readable text
        :param data: optional ``error.data``
        """
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.data = data

    def to_body(self, request_id):
        """Return the JSON-RPC error response for *request_id*.

        :rtype: dict
        """
        error = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


class AuthError(Exception):
    """The request is refused before any MCP work: 401, 403 or 503."""

    def __init__(self, status, message, *, retry_after=None, token_presented=False):
        """Keep what the response needs.

        :param int status: 401 (no or bad credential), 403 (good credential,
            no usable user) or 503 (the issuer cannot be asked)
        :param str message: what the client is told
        :param retry_after: seconds for ``Retry-After``, or None
        :param bool token_presented: whether a credential was sent, which
            adds ``error="invalid_token"`` to a 401 challenge
        """
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after
        self.token_presented = token_presented
