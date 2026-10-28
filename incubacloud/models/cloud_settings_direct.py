"""Settings of the direct path (plan-trafico-interno-directo).

The DNS token the direct certificates are asked for with.
"""
from odoo import models

from .encrypted_char import EncryptedChar


class CloudSettings(models.Model):
    _inherit = "cloud.settings"

    acme_dns_cloudflare_token = EncryptedChar(
        string="DNS token for direct certificates (Cloudflare)",
        groups="incubacloud.group_cloud_manager",
        help="Cloudflare API token that may only read the zone and edit its "
             "DNS. Traefik asks Let's Encrypt for the direct names' "
             "certificates with it (DNS-01). Written to each host that needs "
             "it as a root-only file, never in its compose file. Stored "
             "encrypted.",
    )
