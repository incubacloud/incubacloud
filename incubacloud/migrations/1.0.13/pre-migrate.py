"""Pre-migrate for 1.0.13.

Seed ``cloud_instance.state`` from the old ``deployed`` flag *before*
the ORM loads the new field definitions.

This has to run in pre-migrate. In 1.0.13 ``deployed`` stops being a
plain boolean and becomes a stored computed field derived from
``state``. If we waited for post-migrate, Odoo would have created the
``state`` column with its default ('draft') and recomputed ``deployed``
from it — turning every deployed instance into a draft and destroying
the very information we need to migrate. Creating and filling the
column here means the ORM finds it already populated and leaves it
alone.

Idempotent: the column is created only if missing and rows are only
filled while ``state`` is NULL, so re-running changes nothing.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Create ``state`` and derive it from the pre-1.0.13 ``deployed``."""
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'cloud_instance' AND column_name = 'deployed'
        """
    )
    if not cr.fetchone():
        # Fresh install rather than an upgrade: nothing to derive from.
        return

    cr.execute(
        'ALTER TABLE cloud_instance ADD COLUMN IF NOT EXISTS state varchar'
    )
    cr.execute(
        """
        UPDATE cloud_instance
           SET state = CASE WHEN deployed THEN 'deployed' ELSE 'draft' END
         WHERE state IS NULL
        """
    )
    _logger.info(
        "incubacloud 1.0.13: seeded lifecycle state for %s instance(s)",
        cr.rowcount,
    )
