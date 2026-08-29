{
    'name': 'GitHub OAuth Authentication',
    'version': '19.0.1.0.0',
    'summary': 'Sign in with GitHub, using the OAuth authorization code flow',
    'description': """
Adds "Sign in with GitHub" to the Odoo login page.

GitHub cannot be wired through the stock ``auth_oauth`` provider alone: it
does not support the implicit flow that ``auth_oauth`` builds its login
buttons for, it is not an OpenID Connect provider, and it refuses access
tokens passed as a query string. This module supplies the missing half —
an authorization-code exchange and a GitHub-specific identity lookup —
and then hands the result to the ordinary ``res.users`` OAuth pipeline,
so signup, linking and every other behaviour stay stock.

Only verified email addresses are accepted: the address is read from
``/user/emails`` and must be both primary and verified, which is what
lets downstream code treat the address as proof of mailbox ownership.

Setup (once per Odoo instance, by the administrator):

1. Register an OAuth App at https://github.com/settings/developers
   (OAuth Apps > New OAuth App). Set the Authorization callback URL to
   exactly ``https://<your-odoo>/auth_oauth/github/callback``, then
   generate a client secret.
2. Open Settings > Users & Companies > OAuth Providers > GitHub — this
   module ships that record, disabled — paste the Client ID and Client
   Secret, and tick Allowed.

GitHub has no API for creating OAuth Apps, so step 1 is manual by
design; there is nothing to automate. Odoo must be served over HTTPS at
the host named in the callback URL.

See README.md for troubleshooting and for how this differs from a GitHub
App.
""",
    'category': 'Extra Tools',
    'author': 'IncubaCloud',
    'website': 'https://github.com/incubacloud',
    'license': 'LGPL-3',
    'depends': ['auth_oauth'],
    'data': [
        'views/auth_oauth_provider_views.xml',
        'data/auth_oauth_provider.xml',
    ],
    'installable': True,
    'auto_install': False,
}
