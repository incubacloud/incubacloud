"""Post-migrate for 1.0.8 — fix the proxy's ephemeral ACME storage path.

The Traefik template shipped ``storage: "acme.json"`` (a *relative*
path). Traefik resolves it against the container's working dir (``/``),
so it wrote ``/acme.json`` to the container's ephemeral layer instead of
the mounted ``acme`` volume (``/etc/traefik/acme``). Every proxy
recreation lost the ACME state and re-issued all certificates from
scratch — churning the wildcard and risking Let's Encrypt rate limits.

The template fix (absolute ``/etc/traefik/acme/acme.json``) only helps
*new* hosts: ``init_traefik_templates`` fills ``traefik_yml`` only when
empty, so already-provisioned hosts keep the buggy value stored on their
``cloud.host`` record. Rewrite it here so the next Traefik redeploy
uploads a config that persists ACME state to the volume.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

_OLD = 'storage: "acme.json"'
_NEW = 'storage: "/etc/traefik/acme/acme.json"'


def migrate(cr, version):
    """Repoint stored Traefik ACME storage to the persistent volume on
    every host whose ``traefik_yml`` still carries the relative path."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    # ``active_test=False`` so archived hosts are patched too — an
    # archived host that gets reactivated would otherwise redeploy the
    # buggy relative storage path.
    hosts = env['cloud.host'].with_context(active_test=False).search(
        [('traefik_yml', '!=', False)]
    )
    for host in hosts:
        if _OLD in host.traefik_yml:
            host.traefik_yml = host.traefik_yml.replace(_OLD, _NEW)
            _logger.info(
                "1.0.8: repointed Traefik ACME storage to the acme "
                "volume for host: %s", host.name,
            )
