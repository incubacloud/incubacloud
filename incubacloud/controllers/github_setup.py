"""GitHub App Manifest setup flow.

Routes:
    GET /cloud/github/setup    — generates manifest, auto-POSTs to GitHub
    GET /cloud/github/callback — exchanges code, stores credentials, redirects

GitHub App Manifest docs:
https://docs.github.com/en/apps/sharing-github-apps/
registering-a-github-app-from-a-manifest
"""

import base64
import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from odoo import _, http
from odoo.http import request

from ..github.http_utils import safe_urlopen

_logger = logging.getLogger(__name__)

_GITHUB_APP_NEW = "https://github.com/settings/apps/new"
_GITHUB_MANIFEST_CONVERT = (
    "https://api.github.com/app-manifests/{code}/conversions"
)
_SESSION_STATE_KEY = "_github_app_setup_state"
_STATE_TTL_SECONDS = 600  # 10 minutes


class GitHubSetupController(http.Controller):

    @staticmethod
    def _github_app_name(base_url, fallback=""):
        """Derive a fleet-unique GitHub App name from the tenant's host.

        GitHub App names must be globally unique and at most 34 chars.
        Tenants share the same company name, so we use the host
        *subdomain* (unique across the IncubaCloud fleet) rather than the
        company name: take the first DNS label, sanitise non-alphanumerics
        to hyphens, and cap the result at 34 chars. Falls back to ``app``
        when no usable label is present.

        :param base_url: the tenant's base URL (e.g. ``https://x.foo.io``).
        :param fallback: hostname to use when ``base_url`` has none.
        """
        host_label = urllib.parse.urlparse(base_url).hostname or fallback or ""
        slug = re.sub(
            r"[^a-zA-Z0-9]+", "-", host_label.split(".")[0],
        ).strip("-") or "app"
        return f"IncubaCloud-{slug}"[:34]

    @http.route(
        "/cloud/github/setup", type="http", auth="user", methods=["GET"]
    )
    def github_setup(self):
        """Redirect user to GitHub App creation via manifest flow.

        Builds a GitHub App manifest pre-filled with this Odoo instance's
        webhook URL and redirects the user to GitHub for confirmation.
        A CSRF-style ``state`` token is stored in the session to validate
        the callback.
        """
        request.env['cloud.security.mixin']._check_can_manage_settings()
        # Build base URL — respect X-Forwarded-Proto from reverse proxies
        # (cloudflared, ngrok, etc.) and fall back to ir.config_parameter.
        proto = request.httprequest.headers.get(
            "X-Forwarded-Proto",
            request.httprequest.scheme,
        )
        host = request.httprequest.host
        base_url = f"{proto}://{host}"
        if not base_url.startswith("https://"):
            base_url = (
                request.env["ir.config_parameter"]
                .sudo()
                .get_param("web.base.url", "")
                .rstrip("/")
            )

        state = secrets.token_urlsafe(16)
        request.session[_SESSION_STATE_KEY] = {
            'state': state,
            'created_at': time.time(),
        }

        # GitHub requires HTTPS for redirect_url — the manifest flow will 500
        # on their side if the base URL is HTTP (e.g. local development).
        if not base_url.startswith("https://"):
            return request.redirect(
                "/cloud/settings?setup_error=requires_https"
            )

        app_name = self._github_app_name(base_url, host)

        manifest = {
            "name": app_name,
            "url": base_url,
            "redirect_url": f"{base_url}/cloud/github/callback",
            "public": True,
            "default_permissions": {
                "contents": "read",
                "metadata": "read",
                "pull_requests": "read",
            },
            "default_events": [
                "push",
                "pull_request",
                "create",
                "delete",
            ],
        }
        # GitHub rejects http:// webhook URLs in the manifest (500 error).
        # Only include hook_attributes over HTTPS (production). In dev,
        # configure the webhook URL manually in the GitHub App settings.
        if base_url.startswith("https://"):
            manifest["hook_attributes"] = {
                "url": f"{base_url}/cloud/github/webhook",
                "active": True,
            }

        # GitHub requires a POST form.  We render a branded intermediate page
        # (instead of auto-submitting) so the operator can sign in to GitHub
        # first if needed \u2014 an unauthenticated POST makes GitHub return an
        # opaque 500.  The manifest JSON is base64-encoded and carried in a
        # data attribute; a tiny script decodes it into the hidden field.
        manifest_b64 = base64.b64encode(
            json.dumps(manifest).encode()
        ).decode()
        # Translatable strings live here (not in the QWeb template): Odoo's
        # makepot does not extract terms from a standalone full-HTML template,
        # so we pass already-translated text into the template as values.
        return request.render('incubacloud.github_setup_redirect', {
            'app_name': app_name,
            'action': f"{_GITHUB_APP_NEW}?state={state}",
            'manifest_b64': manifest_b64,
            'title': _("Create GitHub App"),
            'heading': _("Create your GitHub App"),
            'warn': _(
                "Make sure you are signed in to GitHub first in this "
                "browser, otherwise GitHub shows a generic error instead "
                "of the app creation page."
            ),
            'signin': _("Open GitHub sign-in"),
            'cont': _("Continue to GitHub"),
        })

    @http.route(
        "/cloud/github/callback",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def github_callback(self, code=None, state=None, **_kwargs):
        """Handle GitHub's redirect after App creation.

        Validates the ``state`` token, exchanges ``code`` for full app
        credentials (no auth required), stores them in ``cloud.github.app``.
        """
        request.env['cloud.security.mixin']._check_can_manage_settings()
        session_data = request.session.pop(_SESSION_STATE_KEY, None)
        if not isinstance(session_data, dict):
            _logger.warning("GitHub App setup callback: no state in session")
            return request.redirect(
                "/cloud/settings?setup_error=invalid_state"
            )
        expected = session_data.get('state')
        created_at = session_data.get('created_at') or 0
        if time.time() - created_at > _STATE_TTL_SECONDS:
            _logger.warning(
                "GitHub App setup callback: state expired (age=%ss)",
                int(time.time() - created_at),
            )
            return request.redirect(
                "/cloud/settings?setup_error=state_expired"
            )
        if not code or not state or state != expected:
            _logger.warning(
                "GitHub App setup callback: invalid state "
                "(got=%r, expected=%r)",
                state,
                expected,
            )
            return request.redirect(
                "/cloud/settings?setup_error=invalid_state"
            )

        # Refuse to overwrite an existing app via the manifest callback —
        # the operator must explicitly /cloud/reset_github_app first. This
        # closes the only path where the callback would silently rotate
        # production credentials.
        App = request.env["cloud.github.app"].sudo()
        if App.search([], limit=1):
            _logger.warning(
                "GitHub App setup callback: refusing to overwrite "
                "existing app; reset required first"
            )
            return request.redirect(
                "/cloud/settings?setup_error=app_exists"
            )

        # Exchange code for credentials (public GitHub API — no auth needed)
        try:
            url = _GITHUB_MANIFEST_CONVERT.format(code=code)
            req = urllib.request.Request(
                url,
                method="POST",
                headers={
                    "Accept": "application/vnd.github.v3+json",
                    "Content-Length": "0",
                },
                data=b"",
            )
            with safe_urlopen(req, timeout=15) as resp:  # nosec B310 — hardcoded https://api.github.com
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            _logger.error(
                "GitHub App manifest conversion HTTP %s: %s",
                exc.code,
                body,
            )
            return request.redirect(
                "/cloud/settings?setup_error=github_api"
            )
        except Exception:
            _logger.exception(
                "GitHub App manifest conversion failed unexpectedly"
            )
            return request.redirect(
                "/cloud/settings?setup_error=github_api"
            )

        app_id = str(data.get("id", ""))
        private_key = data.get("pem", "")
        webhook_secret = data.get("webhook_secret", "")

        if not app_id or not private_key:
            _logger.error(
                "GitHub manifest response missing id or pem: keys=%s",
                list(data),
            )
            return request.redirect(
                "/cloud/settings?setup_error=missing_credentials"
            )

        slug = data.get("slug", "")
        # installation_id is not known yet; will be auto-set when the user
        # installs the app and the installation webhook fires.
        App.create({
            "app_id": app_id,
            "private_key": private_key,
            "webhook_secret": webhook_secret,
            "slug": slug,
            "installation_id": "",
        })

        request.env['cloud.audit.log'].sudo().create({
            'action': 'GitHub App created via manifest',
            'details': f'app_id={app_id}, slug={slug or "?"}',
        })

        _logger.info(
            "GitHub App created via manifest flow: app_id=%s slug=%s",
            app_id,
            slug or "?",
        )
        return request.redirect("/cloud/settings?setup_ok=1")
