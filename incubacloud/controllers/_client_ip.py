"""Who is asking, when something sits between them and us.

Every per-client control in these controllers — rate limits, audit
lines, lockouts — is only as good as the address it keys on. Behind a
proxy that address is the proxy's, so a single edge becomes one shared
bucket for everybody behind it: the limit stops protecting against the
attacker and starts penalising the crowd they are hiding in.

Odoo's own ``--proxy-mode`` does not settle this. It applies
``ProxyFix(x_for=1)``, which takes the last entry of the forwarded
chain — correct with exactly one proxy in front, and wrong with two,
where the last entry is the inner proxy and the client is further left.
A host behind a CDN always has two: the CDN, and the reverse proxy on
the host itself.

So the address is resolved here instead, against the ranges the
installation has declared as its own proxies. With none declared the
answer is the connecting address, exactly as before.
"""
from odoo.http import request

from ..net.trusted_proxies import client_ip as _resolve

_UNKNOWN = "unknown"


def client_ip():
    """Return the caller's address, seen through the declared proxies.

    Never raises: this is called on the refusal path of public
    endpoints, where an exception would turn a rate-limit check into a
    500 and hand the caller a better outcome than being throttled.

    :return: the client address, or ``'unknown'`` when there is no
        request to read one from
    :rtype: str
    """
    try:
        httprequest = request.httprequest
        ranges = request.env['cloud.settings'].sudo(
        )._effective_trusted_proxy_ranges()
        resolved = _resolve(
            httprequest.remote_addr,
            list(httprequest.access_route or []),
            ranges,
        )
        return resolved or _UNKNOWN
    except Exception:  # noqa: BLE001 - see docstring
        try:
            return request.httprequest.remote_addr or _UNKNOWN
        except Exception:  # noqa: BLE001
            return _UNKNOWN
