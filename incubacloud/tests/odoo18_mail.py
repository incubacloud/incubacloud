"""Leave the mails a test sends where Odoo 19 leaves them.

Odoo 19 skips ``mail.mail._send`` while a test runs, so a mail stays in
the outgoing queue and the tests read it there. Odoo 18 runs it against
a server that sends nothing, marks the mail sent and deletes it when it
is ``auto_delete``, as every alert and job mail is.
"""

from odoo.modules import module

from odoo.addons.mail.models.mail_mail import MailMail

_send = MailMail._send


def _send_outside_tests(self, *args, **kwargs):
    """Skip sending while a test runs, as Odoo 19 does; send otherwise."""
    if module.current_test:
        return True
    return _send(self, *args, **kwargs)


MailMail._send = _send_outside_tests
