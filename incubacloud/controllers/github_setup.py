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
from odoo.tools.misc import consteq
from odoo.tools.misc import hmac as hmac_sign

from ..github.http_utils import safe_urlopen

_logger = logging.getLogger(__name__)

_GITHUB_APP_NEW = "https://github.com/settings/apps/new"
_GITHUB_ORG_APP_NEW = "https://github.com/organizations/{org}/settings/apps/new"
# A GitHub login: letters, digits and single hyphens, neither first nor
# last, 39 characters at most. ASCII only, as GitHub's own rule.
_GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
_GITHUB_MANIFEST_CONVERT = (
    "https://api.github.com/app-manifests/{code}/conversions"
)
# GitHub's account types, as cloud.github.app keeps them.
_OWNER_TYPES = {"User": "user", "Organization": "organization"}
_STATE_SCOPE = "incubacloud.github_app_setup"
_STATE_TTL_SECONDS = 600  # 10 minutes


def _state_signature(env, uid, issued_at, nonce):
    """Sign what a setup ``state`` carries, with the database's secret.

    :param env: any environment; the secret is read as superuser.
    :param int uid: the user who started the setup.
    :param int issued_at: when, in epoch seconds.
    :param str nonce: the state's random part.
    :returns: the hex signature.
    """
    return hmac_sign(env(su=True), _STATE_SCOPE, f"{uid}:{issued_at}:{nonce}")


def _make_state(env, uid, now=None):
    """Return a ``state`` for *uid* that the callback can check by itself.

    The state used to live in the session, and Odoo rewrites a session
    whole whenever it records a new device for it — behind the CDN, any
    request arriving from another edge address. One of those landing on
    another worker while the setup page saved the state erased it, and
    the customer came back from GitHub to "invalid state" (QA,
    12-oct-2026; 3 in 30 on three workers). Signed, it needs no storage.

    :param env: any environment.
    :param int uid: the user starting the setup.
    :param float now: epoch seconds; the current time when None.
    :returns: ``<issued_at>.<nonce>.<signature>``.
    """
    issued_at = int(time.time() if now is None else now)
    nonce = secrets.token_urlsafe(12)
    return f"{issued_at}.{nonce}.{_state_signature(env, uid, issued_at, nonce)}"


def _state_problem(env, uid, state, now=None):
    """Say why the callback must refuse *state*, or nothing.

    :param env: any environment.
    :param int uid: the user GitHub sent back.
    :param str state: what GitHub sent back.
    :param float now: epoch seconds; the current time when None.
    :returns: None, ``"invalid_state"`` (not ours, or not this user's)
        or ``"state_expired"`` (ours, older than ten minutes).
    """
    try:
        issued_raw, nonce, signature = (state or "").split(".")
        issued_at = int(issued_raw)
    except ValueError:
        return "invalid_state"
    if not consteq(signature, _state_signature(env, uid, issued_at, nonce)):
        return "invalid_state"
    age = (time.time() if now is None else now) - issued_at
    if age > _STATE_TTL_SECONDS or age < -60:
        return "state_expired"
    return None


def _github_app_new_url(state, org=None):
    """Return GitHub's page for a new App from a manifest, carrying *state*.

    Without *org*, the App is created under the account signed in to
    GitHub in the browser; with it, under that organization, where GitHub
    lets only its owners and App managers create one.

    :param str state: the setup's state (see :func:`_make_state`).
    :param str org: the organization's login, as in its GitHub URL.
    :returns: the URL the setup page posts the manifest to.
    :raises ValueError: when *org* is given and is not a GitHub login, so
        that nothing but the organization's segment can come from it.
    """
    if org is None:
        base = _GITHUB_APP_NEW
    elif isinstance(org, str) and _GITHUB_LOGIN.fullmatch(org):
        base = _GITHUB_ORG_APP_NEW.format(org=urllib.parse.quote(org, safe=""))
    else:
        raise ValueError(f"not a GitHub organization name: {org!r}")
    return f"{base}?{urllib.parse.urlencode({'state': state})}"


class GitHubSetupController(http.Controller):

    @staticmethod
    def _github_app_name(base_url, fallback=""):
        """Derive a fleet-unique GitHub App name from the panel's host.

        GitHub App names must be globally unique and at most 34 chars.
        Panels may share the same company name, so we use the host
        *subdomain* (unique among panels under one domain) rather than the
        company name: take the first DNS label, sanitise non-alphanumerics
        to hyphens, and cap the result at 34 chars. Falls back to ``app``
        when no usable label is present.

        :param base_url: the panel's base URL (e.g. ``https://x.foo.io``).
        :param fallback: hostname to use when ``base_url`` has none.
        """
        host_label = urllib.parse.urlparse(base_url).hostname or fallback or ""
        slug = re.sub(
            r"[^a-zA-Z0-9]+", "-", host_label.split(".")[0],
        ).strip("-") or "app"
        return f"IncubaCloud-{slug}"[:34]

    @staticmethod
    def _setup_texts():
        """The setup page's words, translated here for the template.

        Odoo's makepot does not extract terms from a standalone full-HTML
        template, so the page receives them already translated.

        :returns: a dict of the template's text values.
        """
        return {
            'title': _("Create GitHub App"),
            'heading': _("Create your GitHub App"),
            'warn': _(
                "Make sure you are signed in to GitHub first in this "
                "browser, otherwise GitHub shows a generic error instead "
                "of the app creation page."
            ),
            'signin': _("Open GitHub sign-in"),
            'where': _("Where to create it"),
            'mine': _("My account"),
            'an_org': _("An organization"),
            'org_label': _("Organization name"),
            'org_hint': _(
                "As it appears in GitHub's URL: github.com/name. You must "
                "be an owner or app manager of the organization; otherwise "
                "GitHub refuses."
            ),
            'cont': _("Continue to GitHub"),
            'going': _("Opening GitHub…"),
        }

    @http.route(
        "/cloud/github/setup",
        type="http",
        auth="user",
        methods=["GET", "POST"],
    )
    def github_setup(self, owner="user", org="", **_kwargs):
        """Ask where to create the GitHub App, then send its manifest there.

        GET shows the setup page: where GitHub creates the App, the
        signed-in account or an organization named as in GitHub's URL.
        Its form comes back here by POST, which checks the organization's
        name and answers a page that posts the manifest, pre-filled with
        this panel's webhook URL, to GitHub's page for new Apps of that
        account. A CSRF-style ``state`` token, signed for this user (see
        :func:`_make_state`), lets the callback validate the return.

        :param str owner: ``"org"`` for an organization; anything else is
            the user's account.
        :param str org: the organization's name, when *owner* is ``"org"``.
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

        # GitHub requires HTTPS for redirect_url — the manifest flow will 500
        # on their side if the base URL is HTTP (e.g. local development).
        if not base_url.startswith("https://"):
            return request.redirect(
                "/cloud/settings/github?setup_error=requires_https"
            )

        app_name = self._github_app_name(base_url, host)
        values = self._setup_texts() | {
            "app_name": app_name,
            "owner": "org" if owner == "org" else "user",
            "org": (org or "").strip(),
            "error": "",
        }

        # GET, or a name to fix: the page asks, and its form comes back
        # here. Signing in to GitHub first is up to the user: an
        # unauthenticated POST makes GitHub return an opaque 500.
        org_name = None
        ready = request.httprequest.method == "POST"
        if ready and values["owner"] == "org":
            org_name = values["org"]
            if not org_name:
                values["error"] = _("Type the organization's name.")
            elif not _GITHUB_LOGIN.fullmatch(org_name):
                values["error"] = _(
                    "This is not a GitHub organization name: use the name "
                    "in the organization's URL, letters, digits and single "
                    "hyphens, at most 39 characters."
                )
            ready = not values["error"]
        if not ready:
            values.update(
                action="/cloud/github/setup",
                csrf_token=request.csrf_token(),
            )
            return request.render('incubacloud.github_setup_redirect', values)

        state = _make_state(request.env, request.env.uid)
        manifest = {
            "name": app_name,
            "url": base_url,
            "redirect_url": f"{base_url}/cloud/github/callback",
            "public": True,
            "default_permissions": {
                "contents": "read",
                "metadata": "read",
                # Write: a pull request preview comments its URL on the
                # pull request, which GitHub refuses to an App that may
                # only read them (B6, 8-oct-2026).
                "pull_requests": "write",
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
        # GitHub requires a POST form. The manifest JSON is base64-encoded
        # and carried in a data attribute; a tiny script decodes it into the
        # hidden field and submits the form, whose button stays for a
        # browser without scripts.
        values.update(
            action=_github_app_new_url(state, org_name),
            manifest_b64=base64.b64encode(json.dumps(manifest).encode()).decode(),
        )
        return request.render('incubacloud.github_setup_redirect', values)

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
        problem = _state_problem(request.env, request.env.uid, state)
        if not problem and not code:
            problem = "invalid_state"
        if problem:
            _logger.warning(
                "GitHub App setup callback refused: %s (uid=%s)",
                problem,
                request.env.uid,
            )
            return request.redirect(
                f"/cloud/settings/github?setup_error={problem}"
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
                "/cloud/settings/github?setup_error=app_exists"
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
                "/cloud/settings/github?setup_error=github_api"
            )
        except Exception:
            _logger.exception(
                "GitHub App manifest conversion failed unexpectedly"
            )
            return request.redirect(
                "/cloud/settings/github?setup_error=github_api"
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
                "/cloud/settings/github?setup_error=missing_credentials"
            )

        slug = data.get("slug", "")
        owner = data.get("owner") or {}
        # installation_id is not known yet: the installation webhook sets it
        # when the account that owns the App installs it; an installation on
        # any other account waits for a manager to choose it.
        App.create({
            "app_id": app_id,
            "private_key": private_key,
            "webhook_secret": webhook_secret,
            "slug": slug,
            "owner_login": owner.get("login") or False,
            "owner_type": _OWNER_TYPES.get(owner.get("type")) or False,
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
        return request.redirect("/cloud/settings/github?setup_ok=1")
