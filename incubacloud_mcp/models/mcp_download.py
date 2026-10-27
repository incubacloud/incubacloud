"""Single-use download links of backups, for agents.

The panel's own link (``/web/content``) needs the browser's cookie, which
an agent does not have. This one carries a random token instead: one use,
fifteen minutes, bound to the user who approved it and to the job that
prepared the file, audited when issued (the tool call's row) and when
used. A request that carries another user's session is refused, and so
is a user who, since, lost the role or the access to the job.
"""

import secrets
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError

from ..protocol.canonical import origin_of
from .cloud_audit_log import agent_mark
from .mcp_approval import token_hash

DOWNLOAD_TTL = timedelta(minutes=15)
#: Expired links are kept this long, then the vacuum removes them.
_KEEP_EXPIRED = timedelta(days=1)


class McpDownload(models.Model):
    """One link to one backup file, for the user whose agent asked."""

    _name = "incubacloud.mcp.download"
    _description = "Single-use backup download link for an agent"
    _order = "id desc"

    user_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="cascade", readonly=True
    )
    job_id = fields.Many2one(
        "cloud.job", required=True, ondelete="cascade", readonly=True
    )
    attachment_id = fields.Many2one(
        "ir.attachment", required=True, ondelete="cascade", readonly=True
    )
    token_hash = fields.Char(required=True, index=True, readonly=True)
    expires_at = fields.Datetime(required=True, readonly=True)
    used_at = fields.Datetime(readonly=True)
    via_client = fields.Char(readonly=True)

    @api.model
    def _issue(self, job, attachment):
        """Create a link to *attachment* for the current user.

        :param job: the finished download job that prepared the file
        :param attachment: the file
        :return: the link's URL; only the token's hash is stored
        :rtype: str
        """
        token = secrets.token_urlsafe(32)
        self.sudo().create(
            {
                "user_id": self.env.uid,
                "job_id": job.id,
                "attachment_id": attachment.id,
                "token_hash": token_hash(token),
                "expires_at": fields.Datetime.now() + DOWNLOAD_TTL,
                "via_client": agent_mark(self.env),
            }
        )
        origin = origin_of(self.env["cloud.settings"].sudo()._mcp_resource())
        return f"{origin}/cloud/mcp/download/{token}"

    @api.model
    def _redeem(self, token, session_uid):
        """Use the link of *token* once; return its grant, or an empty recordset.

        Another user's session leaves the link as it was. Otherwise the
        link is spent first, in one statement, then its user's role and
        access are checked again: a refusal spends it too.

        :param str token: the link's token (attacker-controlled)
        :param session_uid: the uid of the request's session, if any
        """
        if not token:
            return self.browse()
        grant = self.sudo().search([("token_hash", "=", token_hash(token))], limit=1)
        if not grant or (session_uid and session_uid != grant.user_id.id):
            return self.browse()
        self.env.cr.execute(
            "UPDATE incubacloud_mcp_download SET used_at = (now() at time zone 'UTC') "
            "WHERE id = %s AND used_at IS NULL "
            "AND expires_at > (now() at time zone 'UTC') RETURNING id",
            (grant.id,),
        )
        if not self.env.cr.fetchone():
            return self.browse()
        grant.invalidate_recordset(["used_at"])
        user = grant.user_id
        sec = self.env["cloud.security.mixin"].with_user(user)
        if not user.active or not sec._has_cloud_group("group_cloud_developer"):
            return self.browse()
        try:
            grant.job_id.with_user(user).check_access("read")
        except AccessError:
            return self.browse()
        self.env["cloud.audit.log"].sudo().create(
            {
                "user_id": user.id,
                "action": "Agent: backup downloaded",
                "instance_id": grant.job_id.instance_id.id or False,
                "job_id": grant.job_id.id,
                "details": grant.attachment_id.name or "",
                "via_client": grant.via_client or "",
            }
        )
        return grant

    @api.autovacuum
    def _gc_expired(self):
        """Remove links expired more than a day ago."""
        self.sudo().search(
            [("expires_at", "<", fields.Datetime.now() - _KEEP_EXPIRED)]
        ).unlink()
