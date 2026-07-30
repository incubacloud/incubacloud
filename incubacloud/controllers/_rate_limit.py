"""Shared rate-limit gate for HTTP controllers (decision log P13).

This collapses the hit → log → shaped-deny block that used to be
copy-pasted across the connect, terminal, webhook, upload and health
routes. It is a helper rather than a route decorator on purpose: the
position of the check inside each endpoint is a decision, not noise —
the webhook limits *before* reading the body (a forged-signature flood
must not cost HMAC work), while connect-as limits *after* the instance
and permission checks (the required role depends on the environment).
A route-entry decorator would force one ordering on all of them.

Usage::

    limited = rate_gate_json(
        Rule(f'connect_user:{uid}',
             cap_key='rate_limit_connect_user_per_min',
             log_tag=f'connect user={uid}',
             message=_('Too many connect-as requests recently.')),
    )
    if limited:
        return limited

HTTP (non-jsonrpc) endpoints call :func:`first_tripped` directly and
keep building their own 429 response, which is shape they own.
"""
import logging

from odoo.http import request

_logger = logging.getLogger(__name__)


class Rule:
    """One tumbling-window rule evaluated by the gate.

    :param bucket: bucket key passed to ``cloud.rate.limit.hit`` (the
        caller interpolates its own identifiers, e.g. ``f"x:{uid}"``).
    :param message: user-facing text for the jsonrpc deny dict. May be
        ``None`` for endpoints that build their own response.
    :param cap_key: ``cloud.settings`` key resolving the per-minute cap.
    :param max_per_window: literal cap for rules without a setting.
        Exactly one of ``cap_key`` / ``max_per_window`` should be set;
        both being ``None`` falls back to the model's default cap.
    :param log_tag: stable, grep-able suffix for the warning log line.
    """

    __slots__ = ('bucket', 'message', 'cap_key', 'max_per_window', 'log_tag')

    def __init__(self, bucket, message=None, *, cap_key=None,
                 max_per_window=None, log_tag=None):
        self.bucket = bucket
        self.message = message
        self.cap_key = cap_key
        self.max_per_window = max_per_window
        self.log_tag = log_tag


def first_tripped(*rules):
    """Return the first :class:`Rule` whose window is over its cap.

    Rules are evaluated in order and the first one over its cap is
    returned (its warning already logged); ``None`` means every rule
    passed and the endpoint body may proceed. Evaluating a rule counts
    the request against its window, matching the previous inline code.
    """
    rl = request.env['cloud.rate.limit'].sudo()
    for rule in rules:
        kwargs = {}
        if rule.cap_key:
            kwargs['cap_key'] = rule.cap_key
        if rule.max_per_window is not None:
            kwargs['max_per_window'] = rule.max_per_window
        if not rl.hit(rule.bucket, **kwargs):
            _logger.warning(
                "rate_limit_hit %s", rule.log_tag or rule.bucket,
            )
            return rule
    return None


def rate_gate_json(*rules):
    """Evaluate *rules*; return the jsonrpc deny dict or ``None``.

    The deny shape ``{'ok': False, 'error': message}`` is the contract
    every jsonrpc route in this module already exposes to the SPA.
    """
    rule = first_tripped(*rules)
    if rule is None:
        return None
    return {'ok': False, 'error': rule.message}
