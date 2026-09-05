"""Decide which hosts publish the webhook allowlist, and with what in it.

The rendering itself lives in ``github/edge.py``; this is the part that
knows where the endpoint is actually served from and keeps the published
source ranges current. Both halves are deliberately capability, not
policy: nothing here is switched on until an operator sets
``github_webhook_allowlist``, because the allowlist is only correct for
deliveries from github.com — an installation fed by a self-hosted GitHub
Enterprise delivers from its own addresses and would be locked out by
the very control meant to protect it.

The dead-man's switch that notices deliveries have stopped lives with
the event model that would have recorded one, not here.
"""
import hashlib
import logging

from odoo import _, api, fields, models

from ..github.edge import build_webhook_edge_yaml
from ..net.trusted_proxies import parse_ranges
from ..github.meta import GitHubMetaError, fetch_hook_ranges

_logger = logging.getLogger(__name__)

#: Last successfully published list, newline-separated. Stored rather
#: than derived on demand so a failed fetch cannot silently narrow the
#: allowlist, and so a refresh can tell a real change from a re-read.
RANGES_PARAM = 'incubacloud.github_hook_ranges'

WEBHOOK_EDGE_FETCH_ALERT = 'github_hook_ranges_unreadable'
WEBHOOK_EDGE_CHANGED_ALERT = 'github_hook_ranges_changed'

#: Traefik names an instance's service after the compose project it was
#: deployed under, which is the same key the access log and the metric
#: relabelling join on: ``<project>-<version>-prod-main@docker``.
_SERVICE_SUFFIX = 'prod-main@docker'


class CloudInstanceWebhookService(models.Model):
    _inherit = 'cloud.instance'

    def _traefik_service_name(self):
        """Return the Traefik service that serves this instance's Odoo.

        Derived from the compose project name the panel itself forces
        into ``COMPOSE_PROJECT_NAME``, so it survives custom domains and
        redirects and never has to be looked up.

        :return: e.g. ``acme-prod-19-0-prod-main@docker``, or ``''`` when
            the version is unknown and the name cannot be built
        :rtype: str
        """
        self.ensure_one()
        name = self.doodba_project_name or self.name or ''
        version = (self.odoo_version or '').replace('.', '-')
        if not (name and version):
            return ''
        return f'{name}-{version}-{_SERVICE_SUFFIX}'

    def _github_webhook_hostnames(self):
        """Return the hostnames this instance answers the webhook on.

        Redirect-only domains are excluded: they never reach the
        application, so a router pointed at one would answer nothing.

        :rtype: list
        """
        self.ensure_one()
        names = [
            domain.hostname.strip()
            for domain in self.domain_ids
            if domain.hostname and not domain.redirect_to
        ]
        primary = (self.domain or '').strip()
        if primary and primary not in names:
            names.append(primary)
        return [name for name in names if name]


class CloudHostGitHubWebhookEdge(models.Model):
    _inherit = 'cloud.host'

    trusted_proxies_shipped = fields.Text(
        copy=False,
        readonly=True,
        help="The proxy ranges this host's Traefik was last given. Written "
             "by the jobs that ship them, and compared against what the "
             "panel currently intends, so nothing downstream is published "
             "against a posture the host is not running yet.",
    )

    github_webhook_edge_hash = fields.Char(
        copy=False,
        readonly=True,
        help="Digest of the webhook allowlist document last published to "
             "this host. Compared against a fresh render so the refresh "
             "only ships a host whose document actually moved.",
    )

    # ── Published source ranges ───────────────────────────────────────

    @api.model
    def _github_hook_ranges(self):
        """Return the last published allowlist, or an empty list.

        :rtype: list
        """
        raw = self.env['ir.config_parameter'].sudo().get_param(
            RANGES_PARAM, '',
        )
        return [line.strip() for line in raw.splitlines() if line.strip()]

    @api.model
    def _ensure_github_hook_ranges(self):
        """Return the stored allowlist, reading it once if never stored.

        A freshly provisioned host would otherwise wait for the refresh
        before its webhook router existed. Best effort: a failed read
        returns nothing and leaves the router unbuilt, which is the safe
        direction — no allowlist is open, a wrong one is closed.

        :rtype: list
        """
        stored = self._github_hook_ranges()
        if stored:
            return stored
        try:
            ranges = fetch_hook_ranges()
        except GitHubMetaError as exc:
            _logger.warning('GitHub hook ranges unreadable: %s', exc)
            return []
        self.env['ir.config_parameter'].sudo().set_param(
            RANGES_PARAM, '\n'.join(ranges),
        )
        return ranges

    # ── What each host should publish ─────────────────────────────────

    def _github_webhook_routes(self):
        """Return the routers this host should carry for the webhook.

        Two sources. The panel's own endpoint, when settings say which
        host serves it and under what name -- it is not an instance, so
        nothing else here can find it. And one route per hostname of
        every deployed instance on the host, because an instance running
        this module answers the webhook path on each of its own domains.

        Empty while the feature is off, which is what keeps a plain
        installation's routing exactly as it was.

        :rtype: list
        """
        self.ensure_one()
        settings = self.env['cloud.settings'].sudo()._get_system()
        if not settings.github_webhook_allowlist:
            return []
        routes = []
        # The panel itself is not a cloud.instance, so nothing walking
        # this host's instances would ever find it -- and it is the one
        # thing that certainly serves the endpoint. It is described in
        # settings instead, and only its own host publishes it.
        panel_host = settings._github_panel_host()
        if panel_host and panel_host.id == self.id:
            panel = settings._github_panel_route()
            if panel:
                routes.append(panel)
        for instance in self.instance_ids:
            if instance.state != 'deployed':
                continue
            service = instance._traefik_service_name()
            if not service:
                continue
            routes.extend(
                {'hostname': hostname, 'service': service}
                for hostname in instance._github_webhook_hostnames()
            )
        # Decided here rather than in the renderer so every route on
        # this host answers the question the same way, the panel's own
        # included -- it is served by the same proxy and reached the
        # same way as everything else here.
        for route in routes:
            route['tls_mode'] = self._router_tls_mode(route.get('hostname'))
        return routes

    def _github_webhook_document(self):
        """Return the allowlist document for this host, or ``''``.

        Empty means "publish nothing and remove whatever is there": no
        routes to protect, or no ranges to protect them with. Never a
        document with an empty allowlist, which would reject every
        delivery instead of letting them through unprotected.

        :rtype: str
        """
        self.ensure_one()
        routes = self._github_webhook_routes()
        ranges = self._github_hook_ranges()
        if not (routes and ranges):
            return ''
        intended = self._effective_trusted_proxy_ranges()
        shipped = parse_ranges(self.trusted_proxies_shipped)
        if intended != shipped:
            # The allowlist has to compare against whatever this host's
            # Traefik is *actually* running, and it is not running what
            # the panel currently intends. Publishing early is the exact
            # failure this exists to prevent: a forwarded-chain allowlist
            # on a host that still strips the header rejects every
            # delivery, and so does an address-based one on a host that
            # is already behind a CDN.
            _logger.info(
                'Host %s has not been shipped its proxy ranges yet; '
                'holding the webhook allowlist back.', self.display_name,
            )
            return ''
        return build_webhook_edge_yaml(
            routes, ranges, trusted_proxy=bool(shipped),
        )

    @staticmethod
    def _github_webhook_digest(document):
        """Return the digest a published document is recognised by.

        :param str document: a rendered allowlist document
        :rtype: str
        """
        return hashlib.sha256((document or '').encode()).hexdigest()

    def _github_webhook_needs_push(self):
        """Return whether this host's document differs from what it has.

        :rtype: bool
        """
        self.ensure_one()
        digest = self._github_webhook_digest(self._github_webhook_document())
        return digest != (self.github_webhook_edge_hash or '')

    # ── Refresh ───────────────────────────────────────────────────────

    @api.model
    def _cron_refresh_github_hook_ranges(self):
        """Re-read the published ranges and republish the hosts that moved.

        Fail-safe by construction: a fetch that errors leaves the stored
        list untouched and raises an alert, because narrowing an
        allowlist on bad data is how deliveries stop without a trace.

        Runs the per-host comparison whether or not the ranges moved, so
        a host that gained or lost an instance is republished too.

        :return: how many hosts were enqueued for a push
        :rtype: int
        """
        Alert = self.env['cloud.alert'].sudo()
        ICP = self.env['ir.config_parameter'].sudo()
        settings = self.env['cloud.settings'].sudo()._get_system()
        if not settings.github_webhook_allowlist:
            return 0
        try:
            ranges = fetch_hook_ranges()
        except GitHubMetaError as exc:
            _logger.warning('GitHub hook ranges unreadable: %s', exc)
            Alert.raise_alert(
                WEBHOOK_EDGE_FETCH_ALERT,
                _("Could not read GitHub's published webhook source ranges "
                  "(%s). The edge allowlist keeps the ranges it already "
                  "has; if GitHub has since added one, deliveries from it "
                  "are being rejected.", exc),
                level='warning',
            )
            return 0
        Alert.resolve_alert(WEBHOOK_EDGE_FETCH_ALERT)

        previous = self._github_hook_ranges()
        if previous != ranges:
            ICP.set_param(RANGES_PARAM, '\n'.join(ranges))
            if previous:
                Alert.raise_alert(
                    WEBHOOK_EDGE_CHANGED_ALERT,
                    _("GitHub changed its published webhook source ranges "
                      "(was: %(before)s; now: %(after)s). The edge "
                      "allowlists are being republished to match.",
                      before=', '.join(previous), after=', '.join(ranges)),
                    level='warning',
                )
        return self.sudo()._push_github_webhook_edge_where_changed()

    @api.model
    def _push_github_webhook_edge_where_changed(self):
        """Enqueue a publish on every host whose document moved.

        :return: how many hosts were enqueued
        :rtype: int
        """
        Job = self.env['cloud.job'].sudo()
        enqueued = 0
        hosts = self.search([('traefik_deployed', '=', True)])
        for host in hosts:
            if not host._github_webhook_needs_push():
                continue
            Job.enqueue(host.id, False, 'push_github_webhook_edge')
            enqueued += 1
        return enqueued
