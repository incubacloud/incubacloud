"""Frontend-safe error responses for JSON-RPC controllers.

Controllers used to wrap every database/network call in
``except Exception as exc: return {'ok': False, 'error': str(exc)}``.
That leaks internal detail to the browser: SQL constraint names,
asyncssh paths, boto3 request IDs, Python attribute errors, etc.

``safe_error_response`` applies two rules:

1.  ``UserError`` / ``ValidationError`` / ``AccessError`` are the
    curated, user-facing exceptions Odoo code raises on purpose. Their
    ``str()`` stays in the payload untouched — hiding them would
    break flows like "Password too short" or "Backend already exists".

2.  Anything else is logged with full traceback (via
    ``_logger.exception``) and the frontend only sees the caller's
    ``user_message`` plus a short correlation ID. Ops grep the log for
    ``corr=<id>`` to recover the full traceback.

The helper is intentionally small — new callers copy the same call
site: ``except Exception as exc: return safe_error_response(exc, _("..."))``.
"""
import logging
import uuid

from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

# Exceptions whose message is curated for end users and can pass
# through verbatim. Extend here, not at every call site.
_SAFE_TYPES = (UserError, ValidationError, AccessError)


def safe_error_response(exc, user_message, extra=None):
    """Return a ``{ok: False, error: ...}`` dict safe to hand to the SPA.

    :param exc: the caught exception instance.
    :param user_message: curated text shown to the end user. Should be
        translated at the call site (wrap in ``_(...)``).
    :param extra: optional dict merged into the payload (e.g. to
        preserve shape fields like ``{'modules': []}`` that the
        frontend reads unconditionally).
    :return: dict with ``ok=False``, ``error`` (safe), and any extras.
    """
    payload = {'ok': False}
    if isinstance(exc, _SAFE_TYPES):
        payload['error'] = str(exc)
    else:
        corr = uuid.uuid4().hex[:8]
        # ``_logger.exception`` writes the message + full traceback;
        # the corr id is what ops greps on.
        _logger.exception(
            "ic_error corr=%s msg=%r", corr, user_message,
        )
        payload['error'] = f"{user_message} (ref: {corr})"
    if extra:
        payload.update(extra)
    return payload
