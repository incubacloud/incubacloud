from . import controllers
from . import models


def _post_load():
    """Correct the visitor address Odoo reads, when a proxy hides it.

    Runs while this module is imported into the server process, before
    any registry exists — which is the only moment early enough, since
    the name it wraps is read on every request from the first one.

    Does nothing unless the configuration says which header states the
    visitor and whose assertion to believe, so an installation reached
    directly is untouched.
    """
    import odoo.http
    from odoo.tools import config

    from .net import real_ip

    real_ip.install(config, odoo.http)


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
