"""A database without ``database.secret`` gets one when the panel loads it.

Neutralizing a copy deletes production's (``data/neutralize.sql``, section
9): on PostgreSQL 12 the SQL cannot draw a key strong enough without
pgcrypto. Odoo itself creates the parameter only when ``base`` is
installed, so the copy's is created here, in Python, as Odoo creates it.
"""

import logging
import uuid

from odoo import models

_logger = logging.getLogger(__name__)


class IrConfigParameter(models.Model):
    _inherit = "ir.config_parameter"

    def _register_hook(self):
        """Create ``database.secret`` when the database has none.

        Runs once per registry load, before any request is served, so a
        neutralized copy signs its first session with a key of its own.
        The value is a uuid4, as Odoo's own default. ``ON CONFLICT`` lets
        workers that load the registry together create a single one, and
        a database that has its key is only read.
        """
        super()._register_hook()
        cr = self.env.cr
        cr.execute("SELECT 1 FROM ir_config_parameter WHERE key = 'database.secret'")
        if cr.fetchone():
            return
        cr.execute(
            """
            INSERT INTO ir_config_parameter
                   (key, value, create_uid, write_uid, create_date, write_date)
            VALUES ('database.secret', %s, %s, %s,
                    now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC')
            ON CONFLICT (key) DO NOTHING
            """,
            (str(uuid.uuid4()), self.env.uid, self.env.uid),
        )
        if cr.rowcount:
            self.env.registry.clear_cache()
            _logger.info("database.secret was missing (a neutralized copy): created one.")
