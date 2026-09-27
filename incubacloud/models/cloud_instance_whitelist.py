"""One hostname a staging is allowed to reach on the open internet.

Until now egress was decided per *host*: ``cloud.host.whitelist`` builds
one proxy container per hostname in ``~/globalwhitelist`` and every test
instance on that machine joins the same network. That answers "what may
stagings on this box reach", which is never the question a customer
asks — theirs is "this staging has to reach *their* API".

From template v9.6.0 the doodba template answers the per-instance
question directly: a non-empty ``whitelisted_hosts_test`` makes
``test.yaml`` render a NAT gateway (``proxy_general``) plus a sidecar
(``odoo_net_setup``) that shares the odoo container's network namespace
and points its default route at the gateway. This model is that list.

An empty list is not "no egress": it means this staging falls back to
the host's central whitelist, which is what every staging did before —
the template keeps declaring and joining ``globalwhitelist_shared``
either way.
"""
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

#: A hostname and nothing else. The list is space-joined into the
#: sidecar's ``ALLOWED_HOSTS``, so an entry carrying a space would
#: quietly become two rules, and one carrying a newline would be a
#: rule the panel cannot show. No wildcards: the image matches
#: names literally, so a ``*`` would be an allow-list entry that
#: allows nothing while looking generous.
_HOSTNAME_RE = re.compile(
    r'^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$'
)


class CloudInstanceWhitelist(models.Model):
    _name = 'cloud.instance.whitelist'
    _description = 'Instance Egress Whitelist Entry'
    _order = 'sequence, id'

    instance_id = fields.Many2one(
        'cloud.instance',
        required=True,
        ondelete='cascade',
        index=True,
    )
    hostname = fields.Char(
        required=True,
        help='Hostname this staging is allowed to reach through its own '
             'egress gateway (e.g. api.example.com).',
    )
    sequence = fields.Integer(default=10)

    @api.constrains('hostname')
    def _check_hostname(self):
        """Refuse anything that is not a bare hostname.

        :raises ValidationError: when an entry could not be a hostname
        """
        for entry in self:
            if not _HOSTNAME_RE.match((entry.hostname or '').strip()):
                raise ValidationError(
                    _(
                        "'%(value)s' is not a hostname. Write it as "
                        "api.example.com, with no scheme, port or path.",
                        value=entry.hostname or '',
                    )
                )
