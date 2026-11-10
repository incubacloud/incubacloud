"""The issuer of an OAuth provider: how an agent's token finds its user."""

from odoo import fields, models


class AuthOauthProvider(models.Model):
    _inherit = "auth.oauth.provider"

    issuer = fields.Char(
        help="Issuer (``iss``) of the tokens this provider signs. The MCP "
        "endpoint finds the provider of an agent's token by it, then the "
        "user by the token's ``sub``; a panel entered through single "
        "sign-on may also check its id_token against it.",
    )
