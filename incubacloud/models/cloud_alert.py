from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError


class CloudAlert(models.Model):
    _name = 'cloud.alert'
    _description = 'Cloud Infrastructure Alert'
    _order = 'create_date desc'

    host_id = fields.Many2one(
        'cloud.host',
        ondelete='cascade',
        string='Host',
        index=True,
    )
    instance_id = fields.Many2one(
        'cloud.instance',
        ondelete='cascade',
        string='Instance',
        index=True,
    )
    project_id = fields.Many2one(
        'cloud.project',
        ondelete='cascade',
        string='Project',
        index=True,
    )
    conflict_data = fields.Json(
        string='Conflict Data',
        help='List of pip dependency conflicts: [{name, existing, incoming}]',
    )
    payload = fields.Json(
        string='Alert Payload',
        help=(
            'Generic structured payload attached to the alert. '
            'Used by instance_error_logs (deduped log line groups), '
            'and reserved for any future alert that needs to ship '
            'structured detail beyond the human-readable message.'
        ),
    )
    job_id = fields.Many2one(
        'cloud.job',
        ondelete='set null',
        string='Originating Job',
        index=True,
    )
    code = fields.Char(
        required=True,
        index=True,
        help='Machine-readable identifier for deduplication (e.g. disk_critical).',
    )
    message = fields.Char(required=True)
    level = fields.Selection(
        [('warning', 'Warning'), ('critical', 'Critical')],
        default='warning',
        required=True,
    )
    state = fields.Selection(
        [('active', 'Active'), ('dismissed', 'Dismissed')],
        default='active',
        required=True,
    )
    create_date = fields.Datetime(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._broadcast_overview(
            critical=records.filtered(
                lambda r: r.level == 'critical' and r.state == 'active',
            ),
        )
        return records

    def write(self, vals):
        res = super().write(vals)
        # Only broadcast on state transitions — field-level edits
        # (message tweaks, metadata) do not change the overview badge.
        if 'state' in vals:
            self._broadcast_overview()
        return res

    def unlink(self):
        # Capture the env before the rows go away so the broadcast
        # can still hit all active internal users.
        env = self.env
        res = super().unlink()
        self.browse()._broadcast_overview_from(env)
        return res

    # NOTE: alerts may be global (no host / instance / project) — used
    # for system-wide security or platform events (OIDC code reuse,
    # GitHub App credentials nearing expiry, JWKS rotation overdue,
    # etc.). Visibility of those rows is restricted at the record-rule
    # layer: ``rule_alert_member`` excludes targetless rows from
    # stakeholders/consultants, so only project-managers+ see them.

    @api.model
    def _gc(self, older_than_days=60):
        """Drop dismissed alerts older than N days. Cron-invoked.

        Active alerts are never touched — active means unresolved, no
        matter how old. Dismissed rows are resolved history and would
        otherwise grow without bound (nothing else deletes them).
        """
        cutoff = fields.Datetime.now() - timedelta(days=older_than_days)
        self.sudo().search([
            ('state', '=', 'dismissed'),
            ('write_date', '<', cutoff),
        ]).unlink()

    def _broadcast_overview(self, critical=None):
        """Notify every active internal user that the alert overview may
        have changed — they will refetch ``/cloud/get_alert_count`` and
        ``/cloud/get_dashboard`` through the normal ACL path.

        The payload is empty by default: the bus does not respect
        ``ir.rule`` filters, so we never ship alert data blindly through
        it. The one exception is *critical* alerts: the client toasts
        them live, so each user's event carries the subset of new
        critical alerts that user is allowed to read — visibility is
        checked per user before the send, keeping the record rules
        authoritative.
        """
        self._broadcast_overview_from(self.env, critical=critical)

    @api.model
    def _broadcast_overview_from(self, env, critical=None):
        users = env['res.users'].search([
            ('share', '=', False),
            ('active', '=', True),
        ])
        for user in users:
            payload = {}
            if critical:
                visible = critical.filtered(
                    lambda a, u=user: self._alert_visible_to(a, u)
                    and not self._alert_muted_for(a, u),
                )
                if visible:
                    payload = {'critical': [
                        {'id': a.id, 'message': a.message}
                        for a in visible.sudo()
                    ]}
            user._bus_send('cloud_overview', payload)

    @api.model
    def _alert_visible_to(self, alert, user):
        """Return True when *user* passes ACL + record rules to read
        *alert* — gates what travels on their bus channel.
        """
        try:
            alert.with_user(user).check_access('read')
        except AccessError:
            return False
        return True

    @api.model
    def _alert_muted_for(self, alert, user):
        """Return True when the alert's project is muted by *user* —
        muted projects never toast, though they stay in the panel.
        """
        project = alert.project_id or (
            alert.instance_id.project_id if alert.instance_id else None
        )
        return user._cloud_project_muted(project)

    @api.model
    def action_dismiss_all(self, level_filter=None):
        """Dismiss every active, non-blocking alert visible to the
        current user and return how many were affected.

        Conflict alerts (``pip_conflict`` / ``addon_conflict``) are
        excluded — they gate deploys and have their own Resolve flow,
        so a bulk sweep must never silence them. Record rules scope
        the search; the write access check covers the rest.
        """
        domain = [
            ('state', '=', 'active'),
            ('code', 'not in', ('pip_conflict', 'addon_conflict')),
        ]
        if level_filter in ('warning', 'critical'):
            domain.append(('level', '=', level_filter))
        alerts = self.search(domain)
        if alerts:
            alerts.check_access('write')
            alerts.write({'state': 'dismissed'})
        return len(alerts)
