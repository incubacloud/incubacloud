"""The edge guard: what every host's Traefik refuses before any instance sees it.

Odoo answers ``/website/info`` to anyone, with the list of installed apps
and the exact version, and with the language in front too
(``/es/website/info`` is rerouted to the same page). That tells a stranger
which known flaws to try. On 7-nov-2026 the owner decided it is refused on
every instance of every host, the existing ones included, with an
exception an instance can turn on (``cloud.instance.website_info_public``).

The refusal is a document of the proxy's own, ``~/traefik/dynamic/ic_guard.yml``:
a router above every instance's on the ``https`` entrypoint whose only
middleware admits no address, so Traefik answers 403 itself, the way
``direct-deny`` does. The file provider watches the directory, so the
proxy is never restarted. ``push_edge_guard`` writes it on a host that is
already set up; Full Setup writes it before the proxy starts; the host
Check reads its digest and queues the job only when it is missing or
differs from the render.

Not on the ``direct`` entrypoint: that one admits only the platform's own
addresses and refuses everything without a router of its own.
"""
import hashlib
import logging

import yaml

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

#: Where the document lives on the host. The underscore keeps it apart
#: from every tenant's ``{slug}.yml``: a slug cannot hold one.
EDGE_GUARD_FILE = "~/traefik/dynamic/ic_guard.yml"

#: The name the jobs stage it under, in their own directory.
EDGE_GUARD_STAGED = "ic_guard.yml"

#: Above every router an instance or the saas puts on ``https`` (Sablier's
#: ``-draining`` is the highest, 1000), below the per-instance exception.
EDGE_GUARD_PRIORITY = 2000

#: The exception an instance can turn on: just above the guard, so its own
#: router wins for that instance's names and no one else's.
WEBSITE_INFO_EXCEPTION_PRIORITY = 2010

#: The page, bare, with a trailing path, and behind a language code
#: (``/es/website/info``, ``/sr@latin/website/info``): Odoo reroutes a
#: language prefix to the same page. Traefik matches the decoded, cleaned
#: path, so ``/website/%69nfo`` or ``//website/info`` match too (measured
#: on 2.11.44 and 2.11.61, 7-nov-2026). Traefik v2 syntax.
WEBSITE_INFO_RULE = (
    "Path(`/website/info`) || PathPrefix(`/website/info/`)"
    " || Path(`/{lang:[A-Za-z0-9_@-]{2,20}}/website/info`)"
    " || PathPrefix(`/{lang:[A-Za-z0-9_@-]{2,20}}/website/info/`)"
)

#: The router and middleware names the document declares.
EDGE_GUARD_ROUTER = "ic-guard-website-info"
EDGE_GUARD_MIDDLEWARE = "ic-guard-deny"

#: An address no client ever has: the list admits nobody.
EDGE_GUARD_NOBODY = "255.255.255.255/32"


def edge_guard_digest(document):
    """Return the digest a written guard is recognised by on the host.

    The same ``sha256sum`` prints for the file: the document is uploaded
    byte for byte.

    :param str document: a rendered guard
    :rtype: str
    """
    return hashlib.sha256((document or "").encode()).hexdigest()


class CloudHostEdgeGuard(models.Model):
    _inherit = "cloud.host"

    edge_guard_shipped_hash = fields.Char(
        string="Edge guard applied",
        copy=False,
        readonly=True,
        help="Digest of the edge guard (ic_guard.yml) last written to this "
             "host's proxy and read by it.",
    )
    edge_guard_shipped_at = fields.Datetime(
        string="Edge guard applied on",
        copy=False,
        readonly=True,
        help="When the edge guard was last written to this host's proxy.",
    )

    @api.model
    def _shipped_edge_guard_yml(self):
        """Return the guard every host's proxy reads, the same for all.

        Pure and deterministic: the Check compares its digest with the
        file on the host, and a render that moved would re-queue the job
        on every host for nothing.

        :rtype: str
        """
        document = {
            "http": {
                "middlewares": {
                    EDGE_GUARD_MIDDLEWARE: {
                        "ipWhiteList": {"sourceRange": [EDGE_GUARD_NOBODY]},
                    },
                },
                "routers": {
                    EDGE_GUARD_ROUTER: {
                        "rule": WEBSITE_INFO_RULE,
                        "priority": EDGE_GUARD_PRIORITY,
                        # Explicit: Traefik 2.11 hangs a router without
                        # entry points on every one, ``direct`` and
                        # ``metrics`` included. ``http`` redirects to
                        # ``https`` on the entrypoint itself.
                        "entryPoints": ["https"],
                        "middlewares": [EDGE_GUARD_MIDDLEWARE],
                        "service": "noop@internal",
                        # The ``default`` options, as ``direct-deny``:
                        # the CDN's client certificate still applies.
                        "tls": {},
                    },
                },
            },
        }
        body = yaml.safe_dump(document, sort_keys=False, default_flow_style=False)
        return "# Auto-generated by IncubaCloud. Do not edit by hand.\n" + body

    def _edge_guard_leaves_alone(self):
        """Whether the guard must not be written to this host.

        Never in core: every host the panel set up gets it. A module whose
        hosts include one the panel itself runs on (the saas master's)
        excludes it here; the migration, the Check and the job all ask.

        :rtype: bool
        """
        self.ensure_one()
        return False

    def _edge_guard_wanted(self):
        """Whether this host should carry the guard now.

        Active, with the proxy the panel set up, and not left alone.

        :rtype: bool
        """
        self.ensure_one()
        host = self.sudo()
        return bool(
            host.active and host.traefik_deployed
            and not host._edge_guard_leaves_alone()
        )

    @api.model
    def _edge_guard_targets(self):
        """Return the hosts the guard is written to.

        :rtype: cloud.host
        """
        return self.sudo().search([
            ("active", "=", True), ("traefik_deployed", "=", True),
        ]).filtered(lambda host: not host._edge_guard_leaves_alone())

    def _enqueue_edge_guard(self, reason, chained=False):
        """Queue ``push_edge_guard`` on this host, swallowing failures.

        As ``_enqueue_direct_sync``: the caller's pending writes are
        flushed outside the guard, the queueing runs in a savepoint, and a
        failure of its own is logged and leaves the caller's transaction
        usable.

        :param str reason: for the log.
        :param bool chained: called from a job of this host that is still
            running: the push belongs to it and goes to the head of the
            queue.
        :return: the job, or False.
        """
        self.ensure_one()
        name = self.name
        self.env.flush_all()
        try:
            with self.env.cr.savepoint():
                return self.env["cloud.job"].sudo().enqueue(
                    self.id, False, "push_edge_guard",
                    bypass_running_check=chained,
                )
        except Exception as exc:  # noqa: BLE001 — never fail the caller
            _logger.warning(
                "[edge-guard] could not queue the edge guard on %s (%s): %s",
                name, reason, exc,
            )
            return False

    @api.model
    def _edge_guard_everywhere(self, reason):
        """Queue the guard on every host that should carry it.

        :param str reason: for the log.
        :return: the hosts a job was queued on.
        :rtype: cloud.host
        """
        queued = self.browse()
        for host in self._edge_guard_targets():
            if host._enqueue_edge_guard(reason):
                queued |= host
        _logger.info(
            "[edge-guard] queued on %d host(s) (%s): %s",
            len(queued), reason, queued.ids,
        )
        return queued
