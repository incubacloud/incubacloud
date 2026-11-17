"""End-migrate, on every version change — the cron bot, when it is missing.

A post-migration that creates the bot runs before the modules loaded after
this one, whose required columns it cannot fill, and then leaves the bot
for later (``res.users._incubacloud_ensure_cron_bot``). Here every module is
loaded: the bot is created, and the crons of every ``incubacloud%`` module
still on uid 1 are given to it, as their post-migrations would have done.

With the bot there it does nothing. It never breaks the update: a failure
is logged and the crons keep running as uid 1.
"""
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.incubacloud.models.res_users_ext import CRON_BOT_XML_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Create the missing cron bot and give it the crons left on uid 1.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    try:
        with cr.savepoint():
            env = api.Environment(cr, SUPERUSER_ID, {})
            if env.ref(CRON_BOT_XML_ID, raise_if_not_found=False):
                return
            Users = env["res.users"]
            Users._incubacloud_ensure_cron_bot()
            modules = env["ir.model.data"].search([
                ("model", "=", "ir.cron"), ("module", "=like", "incubacloud%"),
            ]).mapped("module")
            for module in sorted(set(modules)):
                Users._incubacloud_assign_cron_user_id(module_name=module)
    except Exception:  # the update goes on; the crons stay on uid 1
        _logger.exception(
            "incubacloud cron bot: creating it at the end of the update failed"
        )
