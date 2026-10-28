"""The assistant's approvals: a card in the chat instead of a link.

``incubacloud.mcp.server._tools_call`` asks ``_gate`` whether a call may run.
For a call of the assistant (its context names :data:`ASSISTANT_VERSION`,
which no MCP client can: the version of a client's request is checked
against the protocol's before any context exists) the answer is a card:
the MCP's confirmation and the assistant's own rules, one approval bound to
the user, the conversation, the tool, the arguments and whether production
is reached, ten minutes, once.
The model has no tool that approves; the card is answered by the person's
own request (``incubacloud.assistant.message._confirm_card``).
"""

from odoo import api, models

from odoo.addons.incubacloud_mcp.protocol.signing import fingerprint

from ..request_scope import ASSISTANT_VERSION

#: The credential of an assistant's call: ``assistant:<conversation id>``.
_CLIENT_PREFIX = "assistant:"


class McpConfirm(models.AbstractModel):
    """A card for the assistant's calls; the MCP's channels for anyone else."""

    _inherit = "incubacloud.mcp.confirm"

    @api.model
    def _gate(self, spec, args, ctx, params):
        """Return None when the call may run now, else what stops it.

        For the assistant, ``{"assistant_confirmation": approval id,
        "message": what the person confirms}``.
        """
        if ctx.version != ASSISTANT_VERSION:
            return super()._gate(spec, args, ctx, params)
        texts = [
            text
            for text in (
                self.env["incubacloud.mcp.tools"]._confirmation(spec, args),
                self.env["incubacloud.assistant.tools"]._extra_confirmation(
                    spec, args, tainted=self._tainted(ctx)
                ),
            )
            if text
        ]
        if not texts:
            return None
        # Bound to whether production is reached too: an approval given
        # while it was not never covers the call once it is.
        reaches = self.env["incubacloud.assistant.tools"]._acts_on_production(
            spec, args
        )
        digest = fingerprint(spec.name, {"arguments": args, "production": reaches})
        Approval = self.env["incubacloud.mcp.approval"]
        approved = Approval._find_approved(spec.name, digest, ctx.principal.client)
        if approved and approved._consume("approved"):
            return None
        message = (
            self.env["incubacloud.mcp.secrets"]._scrubber().text("\n\n".join(texts))
        )
        approval, _token = Approval._open(
            spec.name, digest, message, "assistant", ctx.principal.client
        )
        return {"assistant_confirmation": approval.id, "message": message}

    @api.model
    def _tainted(self, ctx):
        """Return whether the call's conversation read content others can write
        since the person's last message (the task asks before every change)."""
        client = ctx.principal.client or ""
        if not client.startswith(_CLIENT_PREFIX):
            return False
        try:
            conversation_id = int(client.removeprefix(_CLIENT_PREFIX))
        except ValueError:
            return True
        conversation = (
            self.env["incubacloud.assistant.conversation"]
            .sudo()
            .browse(conversation_id)
        )
        return bool(conversation.exists().tainted)
