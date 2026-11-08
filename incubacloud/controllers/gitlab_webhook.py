"""GitLab webhook receiver (plan-gitlab.md §4.7.1).

Endpoint: POST /cloud/gitlab/webhook/<hook_key>
Auth:     public; the webhook's secret arrives in ``X-Gitlab-Token``

In this order, without reading the body until the token matched:

1. the per-IP bucket (``rate_limit_webhook_per_min``, GitHub's), counted
   at READ COMMITTED by ``consume_quota``; a 429 is a lost delivery,
   because GitLab never retries;
2. 8 MB per route (413 from the header, unread);
3. a ``hook_key`` of 32 hex characters naming a webhook, else 401;
4. ``X-Gitlab-Token`` against the webhook's secret, constant time, else
   401 — a forged delivery is refused without reading nor hashing it,
   which is why GitLab needs no edge allowlist;
5. the event type: one not recorded is answered 200 and dropped;
6. the delivery recorded in a savepoint: a repeated one (same
   ``Idempotency-Key``, or same push) answers 200 «Duplicate delivery»;
7. ``_dispatch_safely`` in the request: database work and enqueues only;
   a dispatch that fails keeps its error on the event and still answers
   200, since an error answer counts towards GitLab disabling the
   webhook (Fable's review of G3).
"""
import json
import logging
import re

from odoo import http
from odoo.http import request
from psycopg2 import errors as pg_errors

from ..models.cloud_gitlab_event import RECORDED_EVENTS
from ..models.cloud_rate_limit import consume_quota
from ..models.res_users_ext import as_platform
from ._client_ip import client_ip

_logger = logging.getLogger(__name__)

#: GitLab caps a push's commits at 20 and merge requests are small.
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024

_HOOK_KEY_RE = re.compile(r"^[0-9a-f]{32}$")


def _text(status, body):
    """Return a plain-text response.

    :param int status: the HTTP status
    :param str body: the text
    """
    return request.make_response(
        body, status=status, headers=[("Content-Type", "text/plain")],
    )


class GitLabWebhookController(http.Controller):

    @http.route(
        "/cloud/gitlab/webhook/<string:hook_key>",
        type="http", auth="public", csrf=False, methods=["POST"],
        save_session=False, max_content_length=_MAX_PAYLOAD_BYTES,
    )
    def gitlab_webhook(self, hook_key, **_kwargs):
        """Receive one GitLab delivery; see the module's docstring for the order.

        :param str hook_key: the routing key of the webhook
        """
        ip = client_ip()
        if not consume_quota(
            request.env, f"webhook_ip:{ip}", cap_key="rate_limit_webhook_per_min",
        ):
            _logger.warning("rate_limit_hit gitlab webhook ip=%s", ip)
            return request.make_response(
                "Rate limit exceeded. Retry after 60s.\n", status=429,
                headers=[("Content-Type", "text/plain"), ("Retry-After", "60")],
            )
        headers = request.httprequest.headers
        hook = request.env["cloud.gitlab.hook"].sudo().browse()
        if _HOOK_KEY_RE.match(hook_key or ""):
            hook = hook.search([("hook_key", "=", hook_key)], limit=1)
        if not hook or not hook._token_matches(headers.get("X-Gitlab-Token", "")):
            return _text(401, "Unauthorized.\n")
        event_type = headers.get("X-Gitlab-Event", "")
        if event_type not in RECORDED_EVENTS:
            return _text(200, "Ignored.\n")
        try:
            body = json.loads(request.httprequest.get_data() or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            _logger.warning("GitLab webhook %s: a body that is not JSON", hook.id)
            return _text(200, "Ignored.\n")
        Event = as_platform(request.env["cloud.gitlab.event"])
        try:
            with request.env.cr.savepoint():
                event = Event._record_delivery(
                    hook, event_type, headers.get("Idempotency-Key", ""), body,
                )
        except pg_errors.UniqueViolation:
            _logger.info("GitLab webhook %s: duplicate delivery ignored", hook.id)
            return _text(200, "Duplicate delivery — already processed.\n")
        event._dispatch_safely()
        return _text(200, "OK\n")
