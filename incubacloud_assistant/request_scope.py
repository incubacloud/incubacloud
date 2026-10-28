"""Who the assistant's tool calls are, and the request they run in.

The assistant runs its tools from a queue job. There ``odoo.http.request``
is the job runner's own request (``/queue_job/runjob``): queue_job 2.1.6
makes its environment the superuser's (``update_env(user=SUPERUSER_ID)``),
earlier versions leave it without a user, and the panel's routes read
``request.env``: called like that, a route would run as the superuser, or
fail. So each tool call runs
inside a request of the user's own, bound for as long as the call lasts:
its environment is the user's (their role, their record rules, never the
superuser), on the call's own cursor, and it carries the mark the audit
and the jobs read (``VIA_REQUEST_ATTR``), as ``/mcp`` sets it.
"""

from contextlib import contextmanager

from werkzeug.test import EnvironBuilder

from odoo import http

from odoo.addons.incubacloud_mcp.protocol.context import (
    VIA_REQUEST_ATTR,
    CallContext,
    Principal,
)

#: The version an assistant's call context names: no MCP client speaks it, and
#: ``incubacloud.mcp.confirm._gate`` answers it with a card in the chat.
ASSISTANT_VERSION = "assistant"
#: The path the user's request names. No route serves it.
TOOL_PATH = "/cloud/assistant/tool"


def call_context(uid, conversation_id):
    """Return the MCP call context of a tool call of a conversation.

    The conversation is the credential: an approval asked for in one
    conversation is used by that conversation only.

    :rtype: CallContext
    """
    return CallContext(
        version=ASSISTANT_VERSION,
        modern=False,
        capabilities={},
        client_name="",
        principal=Principal(
            uid=uid,
            credential=f"Assistant (conversation {conversation_id})",
            client=f"assistant:{conversation_id}",
        ),
    )


@contextmanager
def tool_request(env, via):
    """Bind a request of *env*'s user while a tool runs; yield it.

    The request is never served nor saved: its session lives in memory,
    with the user and the database of *env*.

    :param env: the user's environment, on the cursor of the call
    :param str via: what the audit says the action came through
    :raises ValueError: for a superuser environment
    """
    if env.su:
        raise ValueError("A tool call runs as the user, never as the superuser.")
    base_url = (
        env["ir.config_parameter"].sudo().get_param("web.base.url")
        or "http://localhost"
    )
    # The routes a tool reaches limit by user, never by this address: every
    # step of every user shares it.
    environ = EnvironBuilder(
        path=TOOL_PATH,
        base_url=base_url,
        method="POST",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ).get_environ()
    bound = http.Request(http.HTTPRequest(environ))
    session = http.root.session_store.new()
    session.update(
        http.get_default_session(),
        db=env.cr.dbname,
        uid=env.uid,
        login=env.user.login,
        context=dict(env.context),
    )
    bound.session = session
    bound.db = env.cr.dbname
    bound.registry = env.registry
    bound.env = env
    bound.params = {}
    setattr(bound, VIA_REQUEST_ATTR, via)
    http._request_stack.push(bound)
    try:
        if "website" in env.registry:
            # The website module keys the routing map by the request's website,
            # which its ``ir.http._match`` sets when a request is dispatched:
            # this one never is.
            bound.website_routing = (
                env["website"].sudo().with_context(lang=None).get_current_website().id
            )
        yield bound
    finally:
        http._request_stack.pop()
