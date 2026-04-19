"""GitHub App webhook receiver.

Endpoint: POST /cloud/github/webhook
Auth:     public (signature validated via HMAC-SHA256)

Responsibilities:
- Read raw body bytes before any parsing
- Delegate signature validation to the credential service
- Persist the event in ``cloud.github.event``
- Return 200 OK or 401 Unauthorized

Business logic is intentionally absent — events are processed asynchronously
by other parts of the system that observe ``cloud.github.event``.
"""

import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class GitHubWebhookController(http.Controller):

    @http.route(
        "/cloud/github/webhook",
        type="http",
        auth="public",
        csrf=False,
        methods=["POST"],
        save_session=False,
    )
    def github_webhook(self, **_kwargs):
        payload_bytes: bytes = request.httprequest.get_data()
        event_type: str = request.httprequest.headers.get("X-GitHub-Event", "")
        delivery_id: str = request.httprequest.headers.get("X-GitHub-Delivery", "")
        signature: str = request.httprequest.headers.get("X-Hub-Signature-256", "")

        service = request.env["cloud.github.credential.service"].sudo()

        _, secret = service.resolve_webhook_project(payload_bytes, signature)

        if secret is None:
            # No secret configured — reject to prevent unauthenticated deployments
            _logger.error(
                "GitHub webhook rejected: no webhook secret configured "
                "(delivery=%s, event=%s). "
                "Configure a webhook secret in the GitHub App settings.",
                delivery_id, event_type,
            )
            return request.make_response(
                "Webhook secret not configured.\n",
                status=401,
                headers=[("Content-Type", "text/plain")],
            )
        elif not secret:
            # Secret is configured but signature did not validate
            _logger.warning(
                "GitHub webhook HMAC validation failed "
                "(delivery=%s, event=%s)", delivery_id, event_type
            )
            return request.make_response(
                "Signature validation failed.\n",
                status=401,
                headers=[("Content-Type", "text/plain")],
            )

        try:
            payload_data = json.loads(payload_bytes) if payload_bytes else {}
        except Exception:
            payload_data = {}

        payload_str = payload_bytes.decode("utf-8", errors="replace")
        action = payload_data.get("action", "")

        event = request.env["cloud.github.event"].sudo().create({
            "event_type": event_type,
            "action": action,
            "delivery_id": delivery_id,
            "payload": payload_str,
        })

        _logger.debug(
            "GitHub webhook persisted: event=%s action=%s delivery=%s",
            event_type, action, delivery_id,
        )

        # Auto-detect installation_id on first installation.created event
        if event_type == "installation" and action == "created":
            request.env["cloud.github.app"].sudo()._process_installation_event(
                payload_data
            )

        # Auto-rebuild on push
        if event_type == "push":
            event._process_push_event()

        # PR preview environments
        if event_type == "pull_request":
            event._process_pull_request_event()

        return request.make_response(
            "OK\n",
            status=200,
            headers=[("Content-Type", "text/plain")],
        )
