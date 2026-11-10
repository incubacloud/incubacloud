"""Post-migrate for 1.0.223 — personal defaults on the panel's models go.

From this version only an Administrator sets a default of a single
user's on the panel's models (``ir_default``), and those of other users
are not read. The ones saved before are deleted: a user sets them again
from the form if they were an Administrator's.

Idempotent: a second run finds none.
"""
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.incubacloud.models.ir_default import PANEL_MODEL_PREFIXES

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Delete the personal rows of ``ir_default`` on the panel's models.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    cr.execute(
        """
        DELETE FROM ir_default d
         USING ir_model_fields f
         WHERE d.field_id = f.id
           AND d.user_id IS NOT NULL
           AND f.model LIKE ANY(%s)
        """,
        ([f"{prefix}%" for prefix in PANEL_MODEL_PREFIXES],),
    )
    if cr.rowcount:
        _logger.info(
            "1.0.223: %d personal default(s) on the panel's models deleted",
            cr.rowcount,
        )
        api.Environment(cr, SUPERUSER_ID, {}).registry.clear_cache()
