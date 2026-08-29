"""Provider fields GitHub needs that stock ``auth_oauth`` does not carry.

Two additions, both deliberately narrow:

``client_secret``
    The authorization-code exchange is a server-to-server call and GitHub
    authenticates it with the app's secret. Stock ``auth_oauth`` only ever
    ran the implicit flow, which has no such step, so the field does not
    exist there. The name matches OCA's ``auth_oidc``, which adds the same
    ``Char`` for the same reason: when both modules are installed the two
    definitions merge instead of fighting.

``github_flow``
    Marks the records this module is responsible for. A boolean rather
    than a lookup by XML ID, because an administrator may well create the
    provider by hand (or a second one for another GitHub org) and a flag
    keeps working where an XML ID would silently not match. The name is
    module-specific on purpose: ``auth_oidc`` already defines a ``flow``
    selection, and reusing it would force its values on this module.
"""
from odoo import fields, models


class AuthOauthProvider(models.Model):
    _inherit = 'auth.oauth.provider'

    client_secret = fields.Char(
        groups='base.group_system',
        help="OAuth App client secret, used server-side to exchange the "
             "authorization code for an access token. Never sent to the "
             "browser.",
    )
    github_flow = fields.Boolean(
        string="GitHub authorization code flow",
        help="Handle this provider with the GitHub authorization code "
             "flow instead of the stock implicit flow: the login button "
             "requests a code, and the callback exchanges it server-side "
             "and reads the account's verified email from the GitHub API.",
    )
