from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .cloud_instance_whitelist import _HOSTNAME_RE

DEFAULT_WHITELIST = [
    'cdnjs.cloudflare.com',
    'fonts.googleapis.com',
    'fonts.gstatic.com',
    'www.google.com',
    'www.gravatar.com',
]


class CloudHostWhitelist(models.Model):
    _name = 'cloud.host.whitelist'
    _description = 'Host Whitelist Entry'
    _order = 'sequence, id'

    host_id = fields.Many2one(
        'cloud.host',
        required=True,
        ondelete='cascade',
        index=True,
    )
    hostname = fields.Char(
        required=True,
        help='Hostname allowed for test instances on this host '
             '(e.g. fonts.googleapis.com).',
    )
    sequence = fields.Integer(default=10)

    @api.constrains('hostname')
    def _check_hostname(self):
        """Refuse anything that is not a bare hostname, as an instance's list.

        Each entry becomes a service and a quoted alias in the compose
        file of the host's proxies (``build_whitelist_compose``): a quote
        or a line break in one rewrote that file. Checked when an entry
        is created or its hostname written, never on an update of the
        module: an entry stored before the check stays until it is
        changed, and the compose file refuses it.

        :raises ValidationError: when an entry could not be a hostname
        """
        for entry in self:
            if not _HOSTNAME_RE.fullmatch((entry.hostname or '').strip()):
                raise ValidationError(
                    _(
                        "'%(value)s' is not a hostname. Write it as "
                        "api.example.com, with no scheme, port or path.",
                        value=entry.hostname or '',
                    )
                )
