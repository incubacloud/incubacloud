from datetime import timedelta

from odoo import api, fields, models


class CloudAuditLog(models.Model):
    _name = 'cloud.audit.log'
    _description = 'Cloud Audit Log'
    _order = 'id desc'
    _log_access = False

    user_id = fields.Many2one(
        'res.users',
        required=True,
        default=lambda self: self.env.uid,
        readonly=True,
    )
    action = fields.Char(required=True, readonly=True)
    instance_id = fields.Many2one(
        'cloud.instance',
        ondelete='set null',
        index=True,
        readonly=True,
    )
    host_id = fields.Many2one(
        'cloud.host',
        ondelete='set null',
        index=True,
        readonly=True,
    )
    project_id = fields.Many2one(
        'cloud.project',
        ondelete='set null',
        index=True,
        readonly=True,
    )
    job_id = fields.Many2one(
        'cloud.job',
        ondelete='set null',
        readonly=True,
    )
    details = fields.Char(readonly=True)
    create_date = fields.Datetime(readonly=True, default=fields.Datetime.now)

    @api.model
    def _purge_old(self, days):
        """Delete audit log entries older than `days` days. 0 = keep all."""
        if not days or int(days) <= 0:
            return 0
        cutoff = fields.Datetime.now() - timedelta(days=int(days))
        # Purge with sudo: the model is deliberately append-only (the
        # manager ACL grants no unlink), so the scheduled retention purge
        # — the only sanctioned deletion path — runs elevated, mirroring
        # cloud.job.log.chunk._purge_old. Without this the cron and the
        # manual endpoint raise AccessError once rows age past retention.
        old = self.sudo().search([('create_date', '<', cutoff)])
        count = len(old)
        old.unlink()
        return count

    def _format(self):
        return [
            {
                'id': entry.id,
                'user_name': entry.user_id.name or '',
                'action': entry.action,
                'details': entry.details or '',
                'create_date': entry.create_date,
                'job_id': entry.job_id.id if entry.job_id else None,
            }
            for entry in self
        ]
