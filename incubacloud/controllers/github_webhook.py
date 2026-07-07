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

from psycopg2 import errors as pg_errors

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


def _client_ip():
    """Best-effort client IP.

    Relies on Odoo's ``--proxy-mode`` to translate ``X-Forwarded-For``
    into ``remote_addr`` correctly. Reading XFF manually and taking the
    leftmost entry would let any client spoof their IP via the header,
    defeating the per-IP rate limit on /cloud/github/webhook.

    Note: when this stack runs with ``PROXY_MODE=false`` (Doodba
    common.yaml), ``remote_addr`` is the Traefik container IP and the
    bucket collapses to one global cap. That is over-restrictive but
    not spoofable — the operationally safe degradation.
    """
    try:
        return request.httprequest.remote_addr or "unknown"
    except Exception:
        return "unknown"


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
        # Rate-limit by client IP *before* reading the body or doing
        # HMAC work — a forged-signature flood otherwise forces us to
        # hash every request before we can reject it. GitHub itself
        # tolerates the 429 by backing off and re-delivering, so
        # legitimate traffic is not lost.
        ip = _client_ip()
        rl = request.env['cloud.rate.limit'].sudo()
        if not rl.hit(f'webhook_ip:{ip}',
                      cap_key='rate_limit_webhook_per_min'):
            _logger.warning("webhook_rate_limit_hit ip=%s", ip)
            return request.make_response(
                "Rate limit exceeded. Retry after 60s.\n",
                status=429,
                headers=[
                    ("Content-Type", "text/plain"),
                    ("Retry-After", "60"),
                ],
            )

        payload_bytes: bytes = request.httprequest.get_data()
        event_type: str = request.httprequest.headers.get("X-GitHub-Event", "")
        delivery_id: str = request.httprequest.headers.get("X-GitHub-Delivery", "")
        signature: str = request.httprequest.headers.get("X-Hub-Signature-256", "")

        # GitHub always sends X-GitHub-Delivery. A missing header means
        # this is not a real webhook call — reject before doing any
        # HMAC work so a malformed client cannot pollute the event log
        # or bypass the anti-replay unique constraint (which only
        # applies to non-empty delivery_id).
        if not delivery_id:
            _logger.warning(
                "GitHub webhook missing X-GitHub-Delivery header (event=%s)",
                event_type,
            )
            return request.make_response(
                "Missing X-GitHub-Delivery header.\n",
                status=400,
                headers=[("Content-Type", "text/plain")],
            )

        service = request.env["cloud.github.credential.service"].sudo()

        secret = service.resolve_webhook_secret(payload_bytes, signature)

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

        # Anti-replay: the unique index on delivery_id raises
        # UniqueViolation if GitHub (or an attacker) resends the same
        # delivery. Swallow it and return 200 — returning a non-2xx
        # would make GitHub retry the same delivery forever. The event
        # was already processed on the first insert, so handlers are
        # skipped here to avoid double rebuilds / PR churn.
        try:
            with request.env.cr.savepoint():
                event = request.env["cloud.github.event"].sudo().create({
                    "event_type": event_type,
                    "action": action,
                    "delivery_id": delivery_id,
                    "payload": payload_str,
                })
        except pg_errors.UniqueViolation:
            _logger.info(
                "GitHub webhook replay ignored: delivery=%s event=%s action=%s",
                delivery_id, event_type, action,
            )
            return request.make_response(
                "Duplicate delivery — already processed.\n",
                status=200,
                headers=[("Content-Type", "text/plain")],
            )

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
