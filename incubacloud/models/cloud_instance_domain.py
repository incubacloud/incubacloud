from odoo import api, fields, models
from odoo.exceptions import ValidationError


class CloudInstanceDomain(models.Model):
    _name = 'cloud.instance.domain'
    _description = 'Instance Domain'
    _order = 'sequence, id'

    instance_id = fields.Many2one(
        'cloud.instance', required=True, ondelete='cascade', index=True,
    )
    hostname = fields.Char(required=True)
    redirect_to = fields.Char(
        help='Redirect this hostname to another (e.g. example.com → www.example.com)',
    )
    cert_resolver = fields.Char(default='letsencrypt')
    sequence = fields.Integer(default=10)

    @api.constrains('hostname')
    def _check_hostname_unique(self):
        hostnames = [r.hostname for r in self if r.hostname]
        if not hostnames:
            return
        all_with_same = self.search([('hostname', 'in', hostnames)])
        by_host = {}
        for rec in all_with_same:
            by_host.setdefault(rec.hostname, []).append(rec)
        for hostname, recs in by_host.items():
            if len(recs) > 1:
                other = next(
                    (r for r in recs if r not in self), recs[0],
                )
                raise ValidationError(
                    f"Domain '{hostname}' is already used by"
                    f" instance '{other.instance_id.name}'."
                )
