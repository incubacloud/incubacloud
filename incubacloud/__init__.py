from . import controllers
from . import models


def _post_init_hook(env):
    """Runs after ``data/*.xml`` of this module is fully loaded.

    By this point every other installed module (``account``, etc.)
    has already registered its ``_inherit`` extensions on
    ``res.partner``, so defaults for required fields like
    ``autopost_bills`` are applied by the ORM when we create the
    cron bot. Also promotes every ``ir.cron`` owned by this module
    from the implicit OdooBot owner to the dedicated cron user.
    """
    env['res.users']._incubacloud_ensure_cron_bot()
    env['res.users']._incubacloud_assign_cron_user_id(
        module_name='incubacloud',
    )
