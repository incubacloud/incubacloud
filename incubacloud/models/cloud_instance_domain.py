import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

# RFC 1123-style hostname: each label is 1-63 chars, starts/ends with
# alphanumeric, may contain hyphens. Labels separated by dots. Whole
# string lowercase. This regex is the primary defense against SQL/shell
# injection through the hostname value (it flows into psql -c "..." via
# _base_url() in deploy/rebuild executors). With this constraint the
# hostname can never contain quotes, dollars, backticks, semicolons or
# whitespace — values that would break either the SQL or the wrapping
# shell command.
_HOSTNAME_RE = re.compile(
    r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?'
    r'(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
)


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
    # The explicit options map onto the three shapes doodba's Traefik
    # macro accepts (see macros/_traefik2_labels.yml.jinja): a resolver
    # *name* emits ACME, boolean true serves whatever certificate the
    # host's Traefik already holds, false leaves the router without TLS
    # config. A closed Selection rather than a free Char: the only ACME
    # resolver our hosts define is ``letsencrypt``, and this value flows
    # into the tenant's copier answers file, so an open domain would be
    # both a way to break the tenant's routing and a needless injection
    # surface.
    #
    # ``auto`` is the default because the right answer is a property of
    # the host, not of the domain: asking a certificate authority only
    # works while the authority can still reach the host, and once a CDN
    # answers for the name it cannot. Leaving that to be maintained by
    # hand means every row has to be revisited whenever a host moves,
    # and the failure for missing one is silent.
    cert_resolver = fields.Selection(
        selection=[
            ('auto', "Automatic (decided by the host)"),
            ('letsencrypt', "Let's Encrypt"),
            ('custom', "Existing certificate (Traefik store)"),
            ('none', "No TLS"),
        ],
        default='auto',
        required=True,
    )
    redirect_permanent = fields.Boolean(
        default=False,
        help="Use a permanent (301) redirect instead of a temporary (302). "
             "Only relevant when Redirect To is set.",
    )
    sequence = fields.Integer(default=10)

    # Value emitted to copier for each explicit Selection option. Kept
    # next to the field so the mapping and the domain can never drift
    # apart. ``auto`` is absent on purpose: it resolves per host, and a
    # constant here would be a second answer able to disagree.
    _CERT_RESOLVER_ANSWERS = {
        'letsencrypt': 'letsencrypt',
        'custom': True,
        'none': False,
    }

    def _cert_resolver_answer(self):
        """Return the ``cert_resolver`` value for this domain's copier entry.

        An explicit choice is honoured as typed — including ``none``,
        which is how somebody turns TLS off for a hostname on purpose.
        ``auto`` asks the host serving this domain, which is the only
        place that knows whether a certificate authority can still reach
        this name and whether the certificate already on disk covers it.

        Falls back to Let's Encrypt for a missing value so a row written
        before the Selection existed still deploys the way it used to.

        :rtype: str | bool
        """
        self.ensure_one()
        choice = self.cert_resolver or 'letsencrypt'
        if choice != 'auto':
            return self._CERT_RESOLVER_ANSWERS.get(choice, 'letsencrypt')
        host = self.instance_id.host_id
        if host and host._router_tls_mode(self.hostname) == 'default':
            return True
        return 'letsencrypt'


    @api.constrains('hostname', 'redirect_to')
    def _check_hostname_format(self):
        for rec in self:
            for field, value in (
                ('hostname', rec.hostname),
                ('redirect_to', rec.redirect_to),
            ):
                v = (value or '').strip().lower()
                if v and not _HOSTNAME_RE.match(v):
                    raise ValidationError(_(
                        "Invalid %(field)s '%(value)s'. Use lowercase letters, "
                        "digits, dots and hyphens only; each label must "
                        "start and end with an alphanumeric character.",
                    ) % {'field': field, 'value': value})

    @api.constrains('hostname')
    def _check_hostname_unique(self):
        hostnames = [r.hostname for r in self if r.hostname]
        if not hostnames:
            return
        # Only active instances reserve a domain. A deployed instance that
        # is deleted gets archived (active=False) rather than unlinked, so
        # its domain rows survive; those must not block a new instance from
        # reusing the same hostname.
        all_with_same = self.search([
            ('hostname', 'in', hostnames),
            ('instance_id.active', '=', True),
        ])
        by_host = {}
        for rec in all_with_same:
            by_host.setdefault(rec.hostname, []).append(rec)
        for hostname, recs in by_host.items():
            if len(recs) > 1:
                other = next(
                    (r for r in recs if r not in self), recs[0],
                )
                raise ValidationError(_(
                    "Domain '%(hostname)s' is already used by instance '%(instance)s'.",
                ) % {'hostname': hostname, 'instance': other.instance_id.name})
