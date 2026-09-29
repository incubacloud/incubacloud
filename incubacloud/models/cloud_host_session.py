"""Immutable audit trail of host-shell sessions.

Every shell opened against a ``cloud.host`` leaves a row here. The row is
created ``state='open'`` and may transition to ``closed`` exactly once
(with ``closed_at`` and ``close_reason``). The ``write`` override refuses
any other mutation; the ACL denies ``unlink`` below ``base.group_system``.

Session input and output are NOT recorded: a shell receives credentials
on stdin (psql, sudo, curl with tokens), and persisting them in the
database would be a worse hole than the one it closes. The audit covers
who, when, from where and for how long — enough for forensics.
"""
from odoo import _, fields, models
from odoo.exceptions import AccessError


class CloudHostSession(models.Model):
    _name = 'cloud.host.session'
    _description = 'Cloud Host Console Session'
    _order = 'opened_at desc'
    _rec_name = 'host_id'

    host_id = fields.Many2one(
        'cloud.host', required=True, ondelete='restrict',
        help="Deleting the host is blocked while sessions exist — "
             "ondelete=restrict keeps the audit trail.",
    )
    state = fields.Selection(
        [('open', 'Open'), ('closed', 'Closed')],
        default='open', required=True, index=True,
    )
    user_id = fields.Many2one(
        'res.users', required=True,
        default=lambda self: self.env.user,
    )
    opened_at = fields.Datetime(default=fields.Datetime.now, required=True)
    closed_at = fields.Datetime()
    session_id = fields.Char(required=True, index=True, readonly=True)
    client_ip = fields.Char(readonly=True)
    user_agent = fields.Char(readonly=True)
    close_reason = fields.Char(readonly=True)

    _ALLOWED_CLOSE_FIELDS = frozenset({'state', 'closed_at', 'close_reason'})

    def write(self, vals):
        """Allow only the open → closed transition; closed rows are frozen."""
        for rec in self:
            if rec.state == 'closed' and set(vals) - self._ALLOWED_CLOSE_FIELDS:
                raise AccessError(_(
                    "Closed host sessions are immutable.",
                ))
            if 'state' in vals and vals['state'] not in ('open', 'closed'):
                raise AccessError(_(
                    "Invalid host session state transition.",
                ))
        return super().write(vals)
