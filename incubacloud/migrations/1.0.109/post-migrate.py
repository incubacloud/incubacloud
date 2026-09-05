"""Post-migrate for 1.0.109 — let the host decide a domain's certificate.

``cloud.instance.domain.cert_resolver`` used to default to
``letsencrypt``, so every row carries that value whether somebody chose
it or merely accepted the default. The two are indistinguishable now,
and leaving them alone would mean the new ``auto`` applies to rows
created from here on and to nothing else — the existing fleet would keep
asking a certificate authority for names a CDN has started answering
for, which is the failure this release exists to remove.

Moving them is safe because ``auto`` resolves to ``letsencrypt`` for
every host that is not behind a CDN holding a certificate covering the
name, which is every host at the time this runs. Nothing changes on the
next deploy; what changes is that a host moved behind a CDN later takes
its domains with it instead of waiting for somebody to remember.

``custom`` and ``none`` are deliberate — nobody reaches them by leaving
a form alone — so they are left exactly as they are.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Move rows sitting on the old default onto the new one.

    :param cr: database cursor
    :param version: module version being upgraded from, unused
    """
    if not version:
        return
    cr.execute(
        """
        UPDATE cloud_instance_domain
           SET cert_resolver = 'auto'
         WHERE cert_resolver = 'letsencrypt'
        """,
    )
    _logger.info(
        "[1.0.109] %s domain(s) moved from letsencrypt to auto",
        cr.rowcount,
    )
