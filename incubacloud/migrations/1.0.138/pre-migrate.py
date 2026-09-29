"""Pre-migrate for 1.0.138 — take over the host shell's records.

The host shell moved into this module. Its two models keep their
``_name`` and their tables, but every record Odoo keeps for them — the
models, their fields and selection values, the inheritance row, the ACL
lines, the GC cron and the server action behind it, the rate-limit field
on ``cloud.settings`` and the page template — is identified by an
``ir.model.data`` row that still carries the name of the module that
shipped them before.

Left there, the end of that module's next upgrade finds records it no
longer declares and deletes them. For the ACL lines, the cron and its
action that is a delete outright; this module would then recreate them
as new rows with the cron back on uid 1. Re-pointing the rows here, in
this module's pre-migrate, happens before either module loads its code:
this module then finds them as its own and updates them in place, and the
other finds nothing left to clean up. The audit rows in
``cloud_host_session`` never move — only the identifiers do.

The foreign keys of the two tables are reflected in
``ir_model_constraint`` under the old owner too. Reflection looks them up
by (name, module), so this module would add a second row for each; they
are re-pointed with the rest.

``noupdate`` is cleared on the way: the rows arrive from a ``noupdate``
data file, and this module ships the same records as ordinary data, so
its own definition (the cron's name included) applies from now on.

Idempotent: a row is only taken over when this module does not already
hold one with the same name, so a second run matches nothing. It names no
other module on purpose — whichever one owned the rows, they are ours now.
"""
import logging

_logger = logging.getLogger(__name__)

#: Record models the takeover is confined to, so a same-named identifier
#: of some unrelated kind can never be swept along.
_MODELS = (
    'ir.model',
    'ir.model.fields',
    'ir.model.fields.selection',
    'ir.model.inherit',
    'ir.model.access',
    'ir.cron',
    'ir.actions.server',
    'ir.ui.view',
)

#: Identifiers taken over by exact name.
_NAMES = (
    'model_cloud_host_session',
    'model_cloud_host_terminal_route',
    'model_inherit__cloud_host_terminal_route__cloud_terminal_route_mixin',
    'access_cloud_host_session_manager',
    'access_cloud_host_terminal_route_manager',
    'cron_cloud_host_terminal_route_gc',
    'cron_cloud_host_terminal_route_gc_ir_actions_server',
    'host_terminal_page',
    'field_cloud_settings__rate_limit_host_console_per_min',
)

#: Identifiers taken over by prefix: every field and selection value of
#: the two models, whatever the fields are called.
_PREFIXES = (
    'field_cloud_host_session__',
    'field_cloud_host_terminal_route__',
    'selection__cloud_host_session__',
)

#: Models whose reflected foreign keys move with them.
_CONSTRAINT_MODELS = ('cloud.host.session', 'cloud.host.terminal.route')


def migrate(cr, version):
    """Re-point the host shell's identifiers and constraints at this module."""
    if not version:
        return
    cr.execute(
        """
        UPDATE ir_model_data imd
           SET module = 'incubacloud', noupdate = false
         WHERE imd.module != 'incubacloud'
           AND imd.model IN %s
           AND (imd.name IN %s
                OR EXISTS (SELECT 1 FROM unnest(%s::varchar[]) p
                            WHERE starts_with(imd.name, p)))
           AND NOT EXISTS (
                 SELECT 1 FROM ir_model_data own
                  WHERE own.module = 'incubacloud'
                    AND own.name = imd.name
               )
        """,
        (_MODELS, _NAMES, list(_PREFIXES)),
    )
    _logger.info(
        "1.0.138: took over %s host shell identifier(s)", cr.rowcount,
    )
    cr.execute(
        """
        UPDATE ir_model_constraint c
           SET module = own_module.id
          FROM ir_module_module own_module, ir_model m
         WHERE own_module.name = 'incubacloud'
           AND m.id = c.model
           AND m.model IN %s
           AND c.module != own_module.id
           AND NOT EXISTS (
                 SELECT 1 FROM ir_model_constraint own
                  WHERE own.module = own_module.id
                    AND own.name = c.name
               )
        """,
        (_CONSTRAINT_MODELS,),
    )
    _logger.info(
        "1.0.138: took over %s host shell constraint(s)", cr.rowcount,
    )
