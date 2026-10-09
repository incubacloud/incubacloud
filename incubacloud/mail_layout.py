"""The company layout of this module's emails (``data/mail_layout.xml``)."""
from odoo.tools import html_sanitize

MAIL_LAYOUT = 'incubacloud.mail_layout'


def wrap_in_mail_layout(env, html):
    """Return ``html`` inside the company mail layout.

    For the emails assembled in code — alerts, job notifications, the
    daily digest. A ``mail.template`` gets the same layout by naming
    ``MAIL_LAYOUT`` in its ``email_layout_xmlid``.

    Rendered directly rather than through
    ``mail.render.mixin._render_encapsulate``, which also turns relative
    links into absolute ones on the spot and fails when ``web.base.url``
    is not set. ``mail.mail`` does that conversion when it sends, as it
    always did for these emails, so building one can never fail because
    of it — and these are built while an alert or a job is recorded.

    :param env: environment whose company signs the email.
    :param str html: the message itself, already HTML. It is sanitised,
        not escaped: an alert carries names and messages that come from
        outside, and only harmless markup may reach a mail client.
    :return: the whole email, as HTML.
    """
    return env['ir.qweb']._render(
        MAIL_LAYOUT,
        {'body': html_sanitize(html), 'company': env.company},
        minimal_qcontext=True,
    )
