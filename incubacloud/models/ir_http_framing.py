"""Who may put a page of this database in a frame.

Every response Odoo routes through ``ir.http`` leaves with a
``frame-ancestors`` directive, added to whatever Content-Security-Policy
the response already carries instead of replacing it: a file Odoo serves
keeps its ``default-src 'none'`` and gains ``frame-ancestors``. A
response that already says who may frame it is left alone. One that
only sends ``X-Frame-Options`` gets the same policy written as
``frame-ancestors`` (``DENY`` as ``'none'``, ``SAMEORIGIN`` as
``'self'``), so the browsers that read only the CSP are told the same
thing. Any other response may be framed by the database's own origin,
with ``X-Frame-Options: SAMEORIGIN`` for browsers without CSP.

It lives in the application and not in a reverse proxy because a proxy
header replaces the application's: Traefik's ``contentSecurityPolicy``
sets the whole header, and its ``customFrameOptionsValue`` overrides a
page's ``DENY``. Being Odoo's, it holds the same behind any proxy, on
Odoo.sh and on a server of one's own.

Requests Odoo serves without ``ir.http`` are left as Odoo sends them:
the static files of the modules and the routes of a request without a
database.
"""

from odoo import models

CSP = "Content-Security-Policy"
XFO = "X-Frame-Options"

#: The ``frame-ancestors`` source written for each ``X-Frame-Options``
#: value a response may already carry. Any other value (``ALLOW-FROM``,
#: which browsers no longer read) counts as none.
_SOURCE_FOR_FRAME_OPTIONS = {"DENY": "'none'", "SAMEORIGIN": "'self'"}


def _declares_frame_ancestors(policy):
    """Tell whether a Content-Security-Policy value names ``frame-ancestors``.

    :param str policy: one header value, directives separated by ``;``
    :return: True when one of its directives is ``frame-ancestors``
    """
    for directive in policy.split(";"):
        name = directive.strip().split(None, 1)[:1]
        if name and name[0].lower() == "frame-ancestors":
            return True
    return False


def add_frame_ancestors(headers):
    """Add the ``frame-ancestors`` directive *headers* lack, keeping the rest.

    :param werkzeug.datastructures.Headers headers: a response's headers,
        changed in place
    :return: None. When a policy of *headers* already names
        ``frame-ancestors``, nothing changes. Otherwise the directive is
        appended to the first Content-Security-Policy, or becomes the
        whole header when there is none; its source follows
        ``X-Frame-Options`` when the response sends one Odoo knows, and
        is ``'self'`` with ``X-Frame-Options: SAMEORIGIN`` added when it
        sends none.
    """
    policies = headers.getlist(CSP)
    if any(_declares_frame_ancestors(policy) for policy in policies):
        return
    frame_options = headers.get(XFO)
    if frame_options is None:
        source = "'self'"
        headers[XFO] = "SAMEORIGIN"
    else:
        source = _SOURCE_FOR_FRAME_OPTIONS.get(
            frame_options.strip().upper(), "'self'"
        )
    directive = f"frame-ancestors {source}"
    kept = policies[0].strip().rstrip(";").strip() if policies else ""
    value = f"{kept}; {directive}" if kept else directive
    if policies:
        headers.setlist(CSP, [value, *policies[1:]])
    else:
        headers[CSP] = value


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _post_dispatch(cls, response):
        """Say who may frame the response, after Odoo set its own headers.

        Runs after ``super()``, whose ``set_csp`` gives an image without a
        policy its ``default-src 'none'``; the directive is added to that.

        :param response: the response being sent
        """
        super()._post_dispatch(response)
        add_frame_ancestors(response.headers)
