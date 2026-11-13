"""Run manually works on the panel's scheduled actions.

The panel's scheduled actions run as its own user, the cron bot, which
is not a Settings user and so may not write ``ir.cron``. Odoo 18's
``method_direct_trigger`` runs the action as the cron's user and then
writes ``lastcall`` as that user too, so "Run manually" ended in an
access error and rolled back what the action had done.
"""

import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class IrCron(models.Model):
    """Run manually: the action as the cron's user, ``lastcall`` as the caller."""

    _inherit = "ir.cron"

    def method_direct_trigger(self):
        """Run the scheduled action now, as Settings' "Run manually" does.

        Odoo 18's own steps, but ``lastcall`` is written by the person who
        pressed the button, whose write access is checked first, not by the
        cron's user, who may lack it (the cron bot does).

        :raises AccessError: when the caller may not write ``ir.cron``
        :returns: True
        """
        self.ensure_one()
        self.browse().check_access("write")
        self._try_lock()
        _logger.info("Job %r (%s) started manually", self.name, self.id)
        cron, _progress = (
            self.with_user(self.user_id)
            .with_context(lastcall=self.lastcall)
            ._add_progress()
        )
        cron.ir_actions_server_id.run()
        self.lastcall = fields.Datetime.now()
        self.env.flush_all()
        _logger.info("Job %r (%s) done", self.name, self.id)
        return True
