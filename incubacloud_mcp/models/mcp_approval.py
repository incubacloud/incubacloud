"""Approvals: a person says yes to what an agent asked for.

Bound to the user who asked and the credential the agent used, the tool
and the fingerprint of its arguments; ten minutes; used once. Every
change of state is one atomic
``UPDATE … WHERE state = … AND expires_at > now() RETURNING id``: of two
retries racing for the same approval, the second waits for the first and
then finds it used (or fails to serialise), so the action runs once. Its
use is committed at once, in a transaction of its own.
"""

import hashlib
import secrets
from datetime import timedelta

from odoo import api, fields, models
from odoo.service.model import PG_CONCURRENCY_EXCEPTIONS_TO_RETRY

from ..protocol.canonical import origin_of
from .cloud_audit_log import agent_mark

APPROVAL_TTL = timedelta(minutes=10)
#: Expired approvals are kept this long, then the vacuum removes them.
_KEEP_EXPIRED = timedelta(days=1)


def token_hash(token):
    """Return the stored form of a page token: its SHA-256, hex.

    :param str token: the token of an approval page (attacker-controlled)
    :rtype: str
    """
    return hashlib.sha256(str(token or "").encode()).hexdigest()


class McpApproval(models.Model):
    """One action an agent asked for, waiting for its user's approval."""

    _name = "incubacloud.mcp.approval"
    _description = "Agent action awaiting a person's approval"
    _order = "id desc"

    user_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="cascade", readonly=True
    )
    tool = fields.Char(required=True, readonly=True)
    args_hash = fields.Char(required=True, index=True, readonly=True)
    message = fields.Text(required=True, readonly=True)
    channel = fields.Selection(
        [
            ("mrtr_form", "Form in the agent's client"),
            ("mrtr_url", "Link in the agent's client"),
            ("url_error", "Link (earlier clients)"),
            ("text", "Link in the agent's answer"),
        ],
        required=True,
        readonly=True,
    )
    token_hash = fields.Char(index=True, readonly=True)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("approved", "Approved"),
            ("used", "Used"),
            ("cancelled", "Cancelled"),
        ],
        default="pending",
        required=True,
        readonly=True,
    )
    expires_at = fields.Datetime(required=True, readonly=True)
    via_client = fields.Char(readonly=True)
    credential = fields.Char(
        readonly=True,
        help="The verified credential of the agent that asked: only it uses "
        "the approval.",
    )

    @api.model
    def _open(self, tool, args_hash, message, channel, credential=""):
        """Open a pending approval for the current user.

        :param str tool: the tool's name
        :param str args_hash: the fingerprint of its arguments
        :param str message: what the user approves
        :param str channel: how the user is asked (see ``channel``)
        :param str credential: the verified credential of the agent asking
            (the token's client id, or ``API key <name>``)
        :return: ``(approval, page token)``; only the token's hash is stored
        """
        token = secrets.token_urlsafe(32)
        approval = self.sudo().create(
            {
                "user_id": self.env.uid,
                "tool": tool,
                "args_hash": args_hash,
                "message": message,
                "channel": channel,
                "token_hash": token_hash(token),
                "expires_at": fields.Datetime.now() + APPROVAL_TTL,
                "via_client": agent_mark(self.env),
                "credential": credential,
            }
        )
        return approval, token

    @api.model
    def _by_token(self, token):
        """Return the approval of a page token, or an empty recordset."""
        if not token:
            return self.sudo().browse()
        return self.sudo().search([("token_hash", "=", token_hash(token))], limit=1)

    @api.model
    def _find_approved(self, tool, args_hash, credential=""):
        """Return the current user's live approved approval for this call.

        :param str credential: the credential the agent calls with; an
            approval asked for by another one does not count
        """
        return self.sudo().search(
            [
                ("user_id", "=", self.env.uid),
                ("credential", "=", credential),
                ("tool", "=", tool),
                ("args_hash", "=", args_hash),
                ("state", "=", "approved"),
                ("expires_at", ">", fields.Datetime.now()),
            ],
            limit=1,
        )

    def _transition(self, from_state, to_state):
        """Move from *from_state* to *to_state* if still live.

        One statement, which takes the row's lock: a second caller waits
        for the first and does not move it again.

        :return: whether it moved
        :rtype: bool
        """
        self.ensure_one()
        # What the ORM still holds for the row reaches the database first.
        self.flush_recordset(["state", "expires_at"])
        self.env.cr.execute(
            "UPDATE incubacloud_mcp_approval SET state = %s, write_uid = %s, "
            "write_date = (now() at time zone 'UTC') "
            "WHERE id = %s AND state = %s "
            "AND expires_at > (now() at time zone 'UTC') RETURNING id",
            (to_state, self.env.uid, self.id, from_state),
        )
        moved = bool(self.env.cr.fetchone())
        self.invalidate_recordset(["state", "write_uid", "write_date"])
        return moved

    def _consume(self, from_state):
        """Use the approval once, from *from_state* (``pending`` or ``approved``).

        In a transaction of its own, committed at once, before the action
        runs: a worker that dies while it runs leaves the approval spent,
        so its ``requestState`` cannot run it again. A transaction that
        meets another one using it at the same time does not get it.

        :return: whether this call used it
        :rtype: bool
        """
        self.ensure_one()
        # What the ORM still holds for the row reaches the database first.
        self.flush_recordset(["state", "expires_at"])
        with self.env.registry.cursor() as cr:
            try:
                moved = self.with_env(self.env(cr=cr))._transition(from_state, "used")
                cr.commit()
            except PG_CONCURRENCY_EXCEPTIONS_TO_RETRY:
                cr.rollback()
                moved = False
        self.invalidate_recordset(["state", "write_uid", "write_date"])
        return moved

    def _decide(self, approve):
        """Approve or cancel a pending approval.

        :param bool approve: True approves, False cancels
        :return: whether it was pending and live
        :rtype: bool
        """
        return self._transition("pending", "approved" if approve else "cancelled")

    def _withdraw(self):
        """Cancel the approval, pending or already approved: the client said no.

        A no from the agent's client outweighs a yes given on the page: the
        approval is not left for another call to use.

        :return: whether it was live and is now cancelled
        :rtype: bool
        """
        return self._transition("pending", "cancelled") or self._transition(
            "approved", "cancelled"
        )

    def _approve_url(self, token):
        """Return the panel's approval page for *token*.

        :raises ValueError: when the panel has no absolute MCP URL
        """
        origin = origin_of(self.env["cloud.settings"].sudo()._mcp_resource())
        return f"{origin}/cloud/mcp/approve/{token}"

    @api.autovacuum
    def _gc_expired(self):
        """Remove approvals expired more than a day ago."""
        self.sudo().search(
            [("expires_at", "<", fields.Datetime.now() - _KEEP_EXPIRED)]
        ).unlink()
