"""Pre-migrate for 1.0.18 — normalise ``cloud.instance.domain.cert_resolver``.

The field was a free ``Char`` defaulting to ``'letsencrypt'`` and is now a
Selection over ``letsencrypt`` / ``custom`` / ``none``. The column type
does not change (both are varchar), but any row holding a value outside
the new domain would make the field unreadable in the UI and unwritable
through the ORM.

In practice every row should already say ``letsencrypt`` — nothing ever
wrote anything else, because the value was never editable. This runs
anyway: "should already be" is not a guarantee about a column that has
been writable over RPC for as long as it has existed.

NULLs are included: the field is ``required=True`` now, and a NULL would
fail the constraint on the first write to that row.
"""
import logging

_logger = logging.getLogger(__name__)

_VALID = ('letsencrypt', 'custom', 'none')


def migrate(cr, version):
    """Clamp out-of-domain cert_resolver values to the historical default."""
    cr.execute(
        """
        UPDATE cloud_instance_domain
           SET cert_resolver = 'letsencrypt'
         WHERE cert_resolver IS NULL
            OR cert_resolver NOT IN %s
        """,
        (_VALID,),
    )
    if cr.rowcount:
        _logger.info(
            "incubacloud 1.0.18: normalised %d cert_resolver value(s) "
            "to 'letsencrypt'",
            cr.rowcount,
        )
