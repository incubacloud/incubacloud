"""GitHub login: authorization code flow on top of stock ``auth_oauth``.

Stock ``auth_oauth`` speaks the implicit flow — it builds every login
button with ``response_type=token`` and expects the provider to hand the
access token straight back to the browser. GitHub does not implement
that: it returns a ``code`` that has to be exchanged server-side with the
app's secret. It is also not an OpenID Connect provider, so there is no
``id_token`` to read an identity from, and it rejects access tokens
passed as a query string — which is exactly how
``res.users._auth_oauth_rpc`` sends them.

So this controller supplies the three GitHub-shaped pieces and nothing
else:

1. ``list_providers`` rewrites the login button for GitHub records to ask
   for a ``code`` and to come back to this module's callback.
2. The callback exchanges that code for a token and reads the identity
   from the GitHub API with a proper ``Authorization`` header.
3. It assembles the ``validation`` dict that
   ``res.users._auth_oauth_signin`` expects and hands over.

From step 3 onwards everything is stock: signup for a new visitor,
linking for a known one, and any override a database has layered on top.

The email is taken from ``/user/emails`` and must be **verified** —
``/user`` alone returns the public profile email, which GitHub never
confirms and may be empty. ``email_verified`` is therefore only ever set
from GitHub's own flag, so code downstream that treats the claim as proof
of mailbox ownership stays honest.

Every server-side request goes to a fixed GitHub URL held in a module
constant, never to a value read from the database, so no configuration
mistake can turn this into an outbound request to somewhere else.
"""
import json
import logging
import secrets

import requests
import werkzeug.urls

from odoo import SUPERUSER_ID, http
from odoo.exceptions import AccessDenied
from odoo.http import request

from odoo.addons.auth_oauth.controllers.main import OAuthLogin
from odoo.addons.web.controllers.utils import _get_login_redirect_url, ensure_db

_logger = logging.getLogger(__name__)

# Fixed by GitHub. Deliberately constants and not provider fields: these
# are the only hosts this module ever talks to, and keeping them out of
# the database means an edited provider record cannot redirect a
# server-side call somewhere else.
GITHUB_TOKEN_URL = 'https://github.com/login/oauth/access_token'
GITHUB_USER_URL = 'https://api.github.com/user'
GITHUB_EMAILS_URL = 'https://api.github.com/user/emails'

CALLBACK_PATH = 'auth_oauth/github/callback'
REQUEST_TIMEOUT = 10

# Stock's login page renders these: 2 is the generic failure, 3 is a
# refused identity. Reusing them keeps the message consistent with every
# other provider instead of inventing a dialect.
ERROR_GENERIC = 2
ERROR_ACCESS_DENIED = 3


def _callback_url():
    """Return this database's absolute GitHub callback URL."""
    return request.httprequest.url_root + CALLBACK_PATH


def build_auth_link(provider, callback_url, state):
    """Return the GitHub authorize URL for a login button.

    Split out of the controller so the part that actually matters — an
    authorization *code* request pointed at this module's callback — can
    be checked without standing up a request and rendering a page.

    :param provider: provider values (the ``search_read`` dict).
    :param callback_url: absolute URL GitHub must come back to.
    :param state: state dict, already carrying its nonce.
    :return: the full authorize URL.
    """
    params = {
        'response_type': 'code',
        'client_id': provider['client_id'],
        'redirect_uri': callback_url,
        'scope': provider.get('scope') or 'read:user user:email',
        'state': json.dumps(state),
    }
    return "%s?%s" % (
        provider['auth_endpoint'], werkzeug.urls.url_encode(params),
    )


def _pick_verified_email(emails):
    """Return the address GitHub has confirmed belongs to the account.

    Prefers the primary address, falls back to any other verified one, and
    returns ``None`` when the account has none — which is a refusal, not a
    detail to paper over: an unverified address proves nothing about who
    is signing in.

    :param emails: decoded ``GET /user/emails`` payload.
    :return: the email address, or ``None``.
    """
    if not isinstance(emails, list):
        return None
    verified = [
        e for e in emails
        if isinstance(e, dict) and e.get('verified') and e.get('email')
    ]
    for entry in verified:
        if entry.get('primary'):
            return entry['email']
    return verified[0]['email'] if verified else None


class GitHubOAuthLogin(OAuthLogin):
    """Login-page half: point GitHub buttons at the code flow."""

    def list_providers(self, *args, **kwargs):
        """Rewrite ``auth_link`` for GitHub providers.

        Stock built the link for the implicit flow. For GitHub records the
        link has to request a ``code``, come back to this module's
        callback, and carry a nonce that the callback checks — without it
        the callback would accept a state that an attacker composed, which
        is the login-CSRF this parameter exists to stop.
        """
        providers = super().list_providers(*args, **kwargs)
        for provider in providers:
            # Stock reads the providers with ``sudo().search_read()`` and no
            # field list, so every field comes back — ``client_secret``
            # included, superuser being exempt from its group. That dict is
            # handed to the login template. Drop the secret here so it never
            # reaches a rendering context, whatever a theme does with it.
            provider.pop('client_secret', None)
            if not provider.get('github_flow'):
                continue
            state = self.get_state(provider)
            nonce = secrets.token_urlsafe(24)
            state['n'] = nonce
            request.session[f'github_oauth_nonce_{provider["id"]}'] = nonce
            provider['auth_link'] = build_auth_link(
                provider, _callback_url(), state,
            )
        return providers


class GitHubOAuthController(http.Controller):
    """Callback half: exchange the code and sign the user in."""

    @http.route(
        '/' + CALLBACK_PATH, type='http', auth='none',
        methods=['GET'], sitemap=False, readonly=False,
    )
    def github_callback(self, **kw):
        """Complete a GitHub login and open a session.

        Returns a redirect in every case: on success to wherever the login
        was headed, on any failure to the login page with the same
        ``oauth_error`` codes stock uses. Nothing here raises into a 500 —
        a provider outage or a revoked app must look like a failed login,
        not a broken database.
        """
        state_raw = kw.get('state') or ''
        code = kw.get('code') or ''
        try:
            state = json.loads(state_raw)
        except (ValueError, TypeError):
            _logger.warning("[github-oauth] unparseable state")
            return self._login_error(ERROR_GENERIC)

        dbname = state.get('d')
        provider_id = state.get('p')
        if not (dbname and provider_id and code):
            _logger.warning("[github-oauth] state missing required claims")
            return self._login_error(ERROR_GENERIC)
        if not http.db_filter([dbname]):
            return self._login_error(ERROR_GENERIC)
        ensure_db(db=dbname)

        session_key = f'github_oauth_nonce_{provider_id}'
        expected_nonce = request.session.pop(session_key, None)
        if not expected_nonce or state.get('n') != expected_nonce:
            _logger.warning(
                "[github-oauth] nonce mismatch — refusing callback",
            )
            return self._login_error(ERROR_GENERIC)

        # Same cursor the session will authenticate on: a token written on
        # another one stays invisible to this request's REPEATABLE READ
        # snapshot, and the credential check would then find no row.
        env = request.env(user=SUPERUSER_ID)
        provider = env['auth.oauth.provider'].browse(int(provider_id))
        if not provider.exists() or not provider.github_flow:
            return self._login_error(ERROR_GENERIC)

        try:
            access_token = self._exchange_code(provider, code)
            validation = self._read_identity(access_token)
        except AccessDenied:
            return self._login_error(ERROR_ACCESS_DENIED)
        except Exception:
            _logger.exception("[github-oauth] login exchange failed")
            return self._login_error(ERROR_GENERIC)

        params = {'access_token': access_token, 'state': state_raw}
        try:
            login = env['res.users']._auth_oauth_signin(
                provider.id, validation, params,
            )
            if not login:
                raise AccessDenied()
            # Commit so the credential check below, which reopens a
            # snapshot on this same cursor, sees the token just written.
            env.cr.commit()
        except AccessDenied:
            return self._login_error(ERROR_ACCESS_DENIED)
        except Exception:
            _logger.exception("[github-oauth] sign-in failed")
            return self._login_error(ERROR_GENERIC)

        credential = {
            'login': login, 'token': access_token, 'type': 'oauth_token',
        }
        auth_info = request.session.authenticate(request.env, credential)
        redirect = state.get('r') or ''
        url = werkzeug.urls.url_unquote_plus(redirect) if redirect else '/odoo'
        resp = request.redirect(
            _get_login_redirect_url(auth_info['uid'], url), 303,
        )
        resp.autocorrect_location_header = False
        return resp

    def _login_error(self, code):
        """Send the visitor back to the login page with stock's error code."""
        return request.redirect(f'/web/login?oauth_error={code}', 303)

    def _exchange_code(self, provider, code):
        """Trade the authorization code for a user access token.

        ``Accept: application/json`` is not optional: without it GitHub
        answers this endpoint in ``application/x-www-form-urlencoded`` and
        the response cannot be read as JSON.

        :raises AccessDenied: if GitHub refuses the exchange.
        :raises ValueError: if the provider is not configured.
        """
        client_secret = provider.sudo().client_secret
        if not (provider.client_id and client_secret):
            raise ValueError("GitHub provider is missing client credentials")
        response = requests.post(
            GITHUB_TOKEN_URL,
            data={
                'client_id': provider.client_id,
                'client_secret': client_secret,
                'code': code,
                'redirect_uri': _callback_url(),
            },
            headers={'Accept': 'application/json'},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get('error') or not payload.get('access_token'):
            _logger.warning(
                "[github-oauth] token exchange refused: %s",
                payload.get('error') or 'no access_token',
            )
            raise AccessDenied()
        return payload['access_token']

    def _read_identity(self, access_token):
        """Build the ``validation`` dict from the GitHub account.

        The subject is the numeric account id — stable across username
        changes, unlike the login. The email comes from ``/user/emails``
        and must be verified.

        :raises AccessDenied: if no verified address is available.
        """
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/vnd.github+json',
        }
        user_response = requests.get(
            GITHUB_USER_URL, headers=headers, timeout=REQUEST_TIMEOUT,
        )
        user_response.raise_for_status()
        user_data = user_response.json()
        subject = user_data.get('id')
        if not subject:
            raise AccessDenied()

        emails_response = requests.get(
            GITHUB_EMAILS_URL, headers=headers, timeout=REQUEST_TIMEOUT,
        )
        emails_response.raise_for_status()
        email = _pick_verified_email(emails_response.json())
        if not email:
            _logger.warning(
                "[github-oauth] account %s has no verified email — refusing",
                subject,
            )
            raise AccessDenied()

        return {
            'user_id': str(subject),
            'email': email,
            'email_verified': True,
            'name': user_data.get('name') or user_data.get('login') or email,
        }
