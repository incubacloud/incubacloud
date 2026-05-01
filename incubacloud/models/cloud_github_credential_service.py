"""Abstract credential service for GitHub App authentication.

This model acts as a *service* (no database table — ``_auto = False``) that
the rest of the system calls via ``self.env['cloud.github.credential.service']``.

Default implementation (BYO mode):
    Reads credentials from the ``cloud.github.app`` singleton.

Extension point (PaaS mode):
    ``incubacloud_paas`` inherits this model and overrides ``get_credentials``
    and ``resolve_webhook_project`` to pull credentials from system parameters
    instead.  The engine and webhook controller remain unchanged.
"""

import logging

from odoo import _, models
from odoo.exceptions import UserError

from ..github.client import GitHubAppClient
from ..github.webhook_utils import validate_hmac_sha256

_logger = logging.getLogger(__name__)


class GitHubCredentialService(models.AbstractModel):
    _name = "cloud.github.credential.service"
    _description = "GitHub Credential Service"

    # ── Public interface ────────────────────────────────────────────────────

    def get_pat(self):
        """Return the configured Personal Access Token, or None.

        Stored in ``cloud.settings`` (EncryptedChar) so it never
        appears as plain text in the database.
        """
        settings = self.env["cloud.settings"]._get_system()
        pat = (settings.github_pat or "").strip()
        return pat or None

    def get_credentials(self):
        """Return ``GitHubAppCredentials`` for the current system.

        Raises ``UserError`` if no GitHub App is configured.
        Override in ``incubacloud_paas`` for platform-managed credentials.
        """
        app = self.env["cloud.github.app"].sudo().search([], limit=1)
        if not app:
            raise UserError(
                _("GitHub App is not configured. Go to Settings → GitHub to set it up.")
            )
        return app._get_credentials()

    def resolve_webhook_project(self, payload_bytes: bytes, signature: str):
        """Validate an incoming webhook signature and identify its source.

        Returns:
            ``(project_or_None, secret_or_None)`` — ``secret`` is the matching
            webhook secret if validation succeeded; both are ``None`` if no
            match was found or no secret is configured.

        Override in ``incubacloud_paas`` to use the platform-wide secret.
        """
        app = self.env["cloud.github.app"].sudo().search([], limit=1)
        if app and app.webhook_secret:
            if validate_hmac_sha256(payload_bytes, signature, app.webhook_secret):
                return None, app.webhook_secret
        return None, None

    def test_connection(self) -> dict:
        """Test current credentials by calling the GitHub API.

        Returns a dict with ``ok`` (bool) and either connection details or
        an ``error`` string.
        """
        try:
            credentials = self.get_credentials()
        except UserError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            _logger.exception("Unexpected error fetching GitHub credentials")
            return {"ok": False, "error": str(exc)}

        return GitHubAppClient(credentials).test_connection()
