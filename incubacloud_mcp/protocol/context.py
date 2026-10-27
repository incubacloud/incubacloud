"""Who is calling, in which protocol, and what the HTTP layer answers."""

from dataclasses import dataclass, field

#: Context key that marks what is written as done through an agent, where
#: there is no request (tests calling the models).
VIA_CONTEXT_KEY = "incubacloud_mcp_client"
#: Attribute of the HTTP request that marks what it writes as done through
#: an agent. Only the MCP endpoint sets it: a context key could be sent by
#: any panel user through ``call_kw``.
VIA_REQUEST_ATTR = "incubacloud_mcp_via"


@dataclass(frozen=True)
class Principal:
    """The panel user a credential resolved to.

    ``credential`` is what the server verified: the ``client_id`` of an
    OAuth token, or ``API key <name>``.
    """

    uid: int
    credential: str


@dataclass(frozen=True)
class CallContext:
    """What one request said about itself."""

    version: str
    modern: bool
    capabilities: dict
    client_name: str
    principal: Principal

    def via(self):
        """Return what the audit says the action came through.

        The verified credential, then the client's own (unverified) name.
        """
        if self.client_name:
            return f"{self.principal.credential} ({self.client_name})"
        return self.principal.credential


@dataclass
class Reply:
    """An HTTP answer of the endpoint: status, JSON body, extra headers."""

    status: int = 200
    body: dict | None = None
    headers: dict = field(default_factory=dict)
