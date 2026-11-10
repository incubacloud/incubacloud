"""Approval of agents' destructive and security-sensitive actions.

The channel depends on what the client can do:

* 2026-07-28 with form elicitation: ``input_required`` with a yes/no form;
* 2026-07-28 with URL elicitation only: ``input_required`` pointing at the
  panel's approval page, and the retry waits until it is approved;
* 2025-11-25 that declared URL elicitation: error ``-32042`` with the page;
* anything else: an ``isError`` result whose text has the page's link.

Every approval is bound to the user and the very credential the agent
calls with (the spec's "bind to the client and user identity": the
token's client, or the API key itself, not its name), the tool and
the fingerprint of the arguments, lasts ten minutes and is used once,
before the action runs.
Whatever does not hold — a state that does not verify, another user,
tool or arguments, an approval expired, cancelled or used — runs
nothing. The confirmation protects from the model's mistakes and from
instructions injected into logs or mail; it does not protect from a
malicious client, which can answer ``confirm: true`` by itself (see the
user documentation).

An auto-approval rule of the very user and credential
(``incubacloud.mcp.auto.approval``) answers instead of the person: the
call runs at once, after every check that comes before the question.
Whoever approved it — the person or a rule — is left on the request
(``set_approval_mark``), and the audit rows and jobs the call writes
carry it.
"""

import logging
import secrets
import time

from odoo import api, models

from ..protocol.errors import URL_ELICITATION_REQUIRED, McpError
from ..protocol.signing import fingerprint, sign, verify
from .cloud_audit_log import set_approval_mark
from .mcp_approval import APPROVAL_TTL

_logger = logging.getLogger(__name__)

#: The key of the single input request the server makes.
INPUT_KEY = "confirm"


def elicitation_modes(capabilities):
    """Return ``(form, url)``: which elicitation modes the client declared.

    An empty ``elicitation`` object means form only (both revisions say so).

    :param capabilities: the client's capabilities (attacker-controlled)
    :rtype: tuple[bool, bool]
    """
    elicitation = (
        capabilities.get("elicitation") if isinstance(capabilities, dict) else None
    )
    if not isinstance(elicitation, dict):
        return False, False
    return ("form" in elicitation or not elicitation), "url" in elicitation


def answered_action(params):
    """Return the ``action`` the client answered the question with, or None.

    :param dict params: the ``tools/call`` params (attacker-controlled)
    :return: ``accept``, ``decline``, ``cancel``, anything else the client
        sent, or None without an answer
    """
    responses = params.get("inputResponses")
    answer = responses.get(INPUT_KEY) if isinstance(responses, dict) else None
    return answer.get("action") if isinstance(answer, dict) else None


class McpConfirm(models.AbstractModel):
    """Whether a call may run now, or waits for the user's approval."""

    _name = "incubacloud.mcp.confirm"
    _description = "Approval of agents' sensitive actions"

    @api.model
    def _gate(self, spec, args, ctx, params):
        """Return None when the call may run now, or the result that stops it.

        A call that needs an approval runs only after using one, here,
        before the tool runs: whatever the tool does afterwards, the
        approval is spent.

        :param spec: the tool's :class:`ToolSpec`
        :param dict args: the validated (and prepared) arguments
        :param ctx: the request's :class:`CallContext`
        :param dict params: the ``tools/call`` params, as the client sent them
        :raises McpError: -32042 for an earlier client with URL elicitation
        """
        set_approval_mark(None)
        if spec.confirm == "never":
            return None
        message = self.env["incubacloud.mcp.tools"]._confirmation(spec, args)
        if not message:
            return None
        digest = fingerprint(spec.name, args)
        resuming = ctx.modern and params.get("requestState")
        # The person's "no" to a question asked before the rule was granted
        # stands: it withdraws that approval and runs nothing.
        if resuming and answered_action(params) in ("decline", "cancel"):
            return self._resume(spec, digest, ctx, params)
        # Before any approval of the person: one asked for before the rule
        # was granted expires by itself.
        if self._auto_approved(spec, ctx, ctx.principal.client):
            return None
        if resuming:
            stop = self._resume(spec, digest, ctx, params)
            if stop is None:
                self._mark_person()
            return stop
        approved = self.env["incubacloud.mcp.approval"]._find_approved(
            spec.name, digest, ctx.principal.client
        )
        if approved and approved._consume("approved"):
            self._mark_person()
            return None
        return self._ask(spec, digest, message, ctx)

    @api.model
    def _auto_approved(self, spec, ctx, credential):
        """Return whether a rule of the user and *credential* approves the call.

        It is counted, and the call marked ``auto``, before the action runs;
        a line in the server's log says so, where it stays even if the
        transaction is rolled back.

        :param spec: the tool's :class:`ToolSpec`
        :param ctx: the request's :class:`CallContext`
        :param str credential: the rule's credential key
        :rtype: bool
        """
        Rule = self.env["incubacloud.mcp.auto.approval"]
        rule = Rule._active_for(ctx.principal.uid, credential)
        if not rule or not rule._use():
            return False
        set_approval_mark({"approval": "auto", "rule": rule.id})
        _logger.info(
            "[mcp] auto-approved %s uid=%s via=%s rule=%s",
            spec.name,
            ctx.principal.uid,
            ctx.via(),
            rule.id,
        )
        return True

    @api.model
    def _mark_person(self):
        """Mark the call as approved by the person, for its audit and jobs."""
        set_approval_mark({"approval": "person", "rule": 0})

    @api.model
    def _channel(self, ctx):
        """Return how to ask the client of *ctx* (see the module's docstring)."""
        form, url = elicitation_modes(ctx.capabilities)
        if ctx.modern and form:
            return "mrtr_form"
        if ctx.modern and url:
            return "mrtr_url"
        if not ctx.modern and url and ctx.version == "2025-11-25":
            return "url_error"
        return "text"

    @api.model
    def _ask(self, spec, digest, message, ctx):
        """Open an approval and ask for it through the client's best channel.

        The message is masked like any answer before it is kept or sent.

        :raises McpError: -32042 for an earlier client with URL elicitation
        """
        message = self.env["incubacloud.mcp.secrets"]._scrubber().text(message)
        channel = self._channel(ctx)
        approval, token = self.env["incubacloud.mcp.approval"]._open(
            spec.name, digest, message, channel, ctx.principal.client
        )
        if channel == "mrtr_form":
            # The page's link is never handed out: the form is the channel.
            return self._input_required(
                ctx,
                self._form_request(message),
                self._state(approval, spec, digest, ctx, "form", ""),
            )
        link = approval._approve_url(token)
        if channel == "mrtr_url":
            return self._input_required(
                ctx,
                self._url_request(link, message),
                self._state(approval, spec, digest, ctx, "url", link),
            )
        if channel == "url_error":
            raise McpError(
                200,
                URL_ELICITATION_REQUIRED,
                self.env._("This action needs your approval."),
                {
                    "elicitations": [
                        {
                            "mode": "url",
                            "elicitationId": str(approval.id),
                            "url": link,
                            "message": message,
                        }
                    ]
                },
            )
        text = self.env._(
            "This action needs your approval: open %(link)s and ask for it again.",
            link=link,
        )
        return self.env["incubacloud.mcp.server"]._error_result(
            ctx, f"{text}\n\n{message}"
        )

    @api.model
    def _form_request(self, message):
        """Return the yes/no form elicitation for *message*."""
        return {
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": message,
                "requestedSchema": {
                    "type": "object",
                    "properties": {
                        INPUT_KEY: {
                            "type": "boolean",
                            "title": self.env._("Approve"),
                            "description": message,
                        },
                    },
                    "required": [INPUT_KEY],
                },
            },
        }

    @api.model
    def _url_request(self, link, message):
        """Return the URL elicitation that opens the approval page."""
        return {
            "method": "elicitation/create",
            "params": {"mode": "url", "url": link, "message": message},
        }

    @api.model
    def _state(self, approval, spec, digest, ctx, mode, link):
        """Return the signed ``requestState`` of an approval (label ``request``).

        It holds the approval, a nonce, the user and the credential the
        agent calls with, the tool, the fingerprint of the arguments, when
        it expires, how the user is asked and the page's link (only in
        ``url`` mode).
        """
        return sign(
            self.env["cloud.settings"].sudo()._mcp_secret(),
            "request",
            {
                "a": approval.id,
                "n": secrets.token_hex(8),
                "u": ctx.principal.uid,
                "c": ctx.principal.client,
                "t": spec.name,
                "h": digest,
                "e": int(time.time() + APPROVAL_TTL.total_seconds()),
                "m": mode,
                "l": link,
            },
        )

    @api.model
    def _input_required(self, ctx, request, state):
        """Return an ``input_required`` result asking *request*."""
        return self.env["incubacloud.mcp.server"]._finish(
            {
                "resultType": "input_required",
                "inputRequests": {INPUT_KEY: request},
                "requestState": state,
            },
            ctx,
        )

    @api.model
    def _resume(self, spec, digest, ctx, params):
        """Check a retry's ``requestState`` and answer; None runs the call.

        The state is the client's to send back, so it is read as an
        attacker's: its signature, its user, tool and arguments, its
        expiry and its approval's must all hold.
        """
        server = self.env["incubacloud.mcp.server"]
        state = verify(
            self.env["cloud.settings"].sudo()._mcp_secret(),
            "request",
            params.get("requestState"),
        )
        if not state:
            return server._error_result(
                ctx,
                self.env._("This confirmation is not valid. Ask for the action again."),
            )
        if (
            state.get("u") != ctx.principal.uid
            or state.get("c") != ctx.principal.client
            or state.get("t") != spec.name
            or state.get("h") != digest
        ):
            return server._error_result(
                ctx,
                self.env._(
                    "This confirmation was given for another action. "
                    "Ask for the action again."
                ),
            )
        approval_id = state.get("a")
        approval = self.env["incubacloud.mcp.approval"].sudo()
        if isinstance(approval_id, int) and not isinstance(approval_id, bool):
            approval = approval.browse(approval_id).exists()
        if (
            not approval
            or approval.user_id.id != ctx.principal.uid
            or approval.credential != ctx.principal.client
            or approval.tool != spec.name
            or approval.args_hash != digest
            or not isinstance(state.get("e"), int)
            or state["e"] < time.time()
        ):
            return server._error_result(
                ctx,
                self.env._("This confirmation has expired. Ask for the action again."),
            )
        responses = params.get("inputResponses")
        answer = responses.get(INPUT_KEY) if isinstance(responses, dict) else None
        action = answered_action(params)
        if action in ("decline", "cancel"):
            approval._withdraw()
            return server._error_result(ctx, self.env._("Operation cancelled."))
        if action != "accept":
            return self._reask(approval, state, params, ctx)
        if state.get("m") == "form":
            content = answer.get("content")
            if not isinstance(content, dict) or content.get(INPUT_KEY) is not True:
                approval._withdraw()
                return server._error_result(ctx, self.env._("Operation cancelled."))
            if approval._consume("pending"):
                return None
        elif approval._consume("approved"):
            return None
        elif approval.state == "pending":
            return self._reask(approval, state, params, ctx)
        return server._error_result(
            ctx,
            self.env._(
                "This confirmation has already been used or has expired. "
                "Ask for the action again."
            ),
        )

    @api.model
    def _reask(self, approval, state, params, ctx):
        """Ask again for the same approval, echoing the same ``requestState``."""
        if state.get("m") == "form":
            request = self._form_request(approval.message)
        else:
            request = self._url_request(state.get("l"), approval.message)
        return self._input_required(ctx, request, params.get("requestState"))
