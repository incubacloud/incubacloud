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
import urllib.error
import urllib.request

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_GITHUB_APP_NEW = "https://github.com/settings/apps/new"
_GITHUB_MANIFEST_CONVERT = (
    "https://api.github.com/app-manifests/{code}/conversions"
)
_SESSION_STATE_KEY = "_github_app_setup_state"


class GitHubSetupController(http.Controller):

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
        request.session[_SESSION_STATE_KEY] = state

        # GitHub requires HTTPS for redirect_url — the manifest flow will 500
        # on their side if the base URL is HTTP (e.g. local development).
        if not base_url.startswith("https://"):
            return request.redirect(
                "/cloud/settings?setup_error=requires_https"
            )

        # App name must be globally unique on GitHub; use the company name
        # so it's meaningful and avoids collisions between instances.
        company_name = request.env.company.name or request.env.cr.dbname
        # GitHub App names: letters, digits, hyphens, spaces (max 34 chars).
        # Normalize: strip specials, collapse whitespace/hyphens.
        safe = re.sub(r'[^a-zA-Z0-9 -]', '', company_name).strip()
        safe = re.sub(r'[\s-]+', '-', safe)[:22] or "App"
        app_name = f"IncubaCloud-{safe}"

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

        # GitHub requires a POST form.  We set the manifest value via JS
        # (the approach shown in GitHub's own documentation) to avoid any
        # HTML-attribute encoding issues.  The JSON is base64-encoded so
        # the JS string literal contains only [A-Za-z0-9+/=] characters.
        manifest_b64 = base64.b64encode(
            json.dumps(manifest).encode()
        ).decode()

        action = f"{_GITHUB_APP_NEW}?state={state}"
        html = (
            "<!DOCTYPE html>"
            "<html><head><meta charset='utf-8'/>"
            "<title>Redirecting to GitHub\u2026</title></head>"
            "<body>"
            f"<form id='f' method='post' action='{action}'>"
            "<input type='hidden' name='manifest' id='mi'/>"
            "</form>"
            "<script>"
            f"document.getElementById('mi').value=atob('{manifest_b64}');"
            "document.getElementById('f').submit();"
            "</script>"
            "</body></html>"
        )
        return request.make_response(
            html, headers=[("Content-Type", "text/html; charset=utf-8")]
        )

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
        expected = request.session.pop(_SESSION_STATE_KEY, None)
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
            with urllib.request.urlopen(req, timeout=15) as resp:
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

        App = request.env["cloud.github.app"].sudo()
        app = App.search([], limit=1)
        write_vals = {
            "app_id": app_id,
            "private_key": private_key,
            "webhook_secret": webhook_secret,
            "slug": data.get("slug", ""),
        }
        if app:
            app.write(write_vals)
        else:
            # installation_id is not known yet; will be auto-set when
            # the user installs the app and the installation webhook fires.
            write_vals["installation_id"] = ""
            App.create(write_vals)

        _logger.info(
            "GitHub App created via manifest flow: app_id=%s slug=%s",
            app_id,
            data.get("slug", "?"),
        )
        return request.redirect("/cloud/settings?setup_ok=1")
