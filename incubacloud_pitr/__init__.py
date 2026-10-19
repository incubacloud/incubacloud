import secrets

from odoo import _
from odoo.exceptions import UserError

from . import controllers, models


def post_init_hook(env):
    """Give existing instances their archive key; crons run as the bot.

    New instances are born with a key (``cloud.instance.create``); the
    ones that predate the module get theirs here, so nothing that renders
    a stack ever has to write one.

    :param env: the installing environment
    """
    Instance = env["cloud.instance"].with_context(active_test=False).sudo()
    for inst in Instance.search([("pitr_key", "=", False)]):
        inst.pitr_key = secrets.token_hex(32)
    env["res.users"]._incubacloud_assign_cron_user_id(
        module_name="incubacloud_pitr",
    )


def uninstall_hook(env):
    """Refuse to uninstall while an instance still archives.

    Its database would go on running an ``archive_command`` that points at
    a binary nobody updates, and its window would stop being watched.
    Switching continuous recovery off first undoes both on the host.

    :param env: the uninstalling environment
    :raise UserError: when an instance still has it on
    """
    count = env["cloud.instance"].with_context(active_test=False).search_count(
        ["|", ("pitr_enabled", "=", True), ("pitr_state", "not in", (False, "off"))],
    )
    if count:
        raise UserError(_(
            "Switch continuous recovery off on %s instance(s) before "
            "uninstalling this module.", count,
        ))
