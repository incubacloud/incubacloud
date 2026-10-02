"""Post-migrate for 1.0.147 — undo what the save endpoint did to ``auto``.

1.0.109 moved every domain onto ``auto`` so the host decides its
certificate. The endpoint that saves an instance did not know the value
and rewrote it to ``letsencrypt`` whenever the Networking tab was saved
for any reason. On a host a CDN answers for, that asks a certificate
authority for a name it can never reach — the exact failure 1.0.109
removed. Those rows go back to ``auto``.

Only hosts behind a CDN: there ``letsencrypt`` cannot be a deliberate
choice that works. Elsewhere the two values deploy the same today, and a
row somebody chose on purpose is left alone. ``custom`` and ``none`` are
never touched.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Move clobbered rows on CDN-fronted hosts back to ``auto``.

    :param cr: database cursor
    :param version: module version being upgraded from; None on install
    """
    if not version:
        return
    cr.execute(
        """
        UPDATE cloud_instance_domain d
           SET cert_resolver = 'auto'
          FROM cloud_instance i
          JOIN cloud_host h ON h.id = i.host_id
         WHERE d.instance_id = i.id
           AND d.cert_resolver = 'letsencrypt'
           AND h.behind_cdn
        """,
    )
    _logger.info(
        "[1.0.147] %s domain(s) on CDN-fronted hosts moved back to auto",
        cr.rowcount,
    )
