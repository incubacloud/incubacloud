"""Pre-migrate for 1.0.102 — adopt the webhook allowlist from the SaaS layer.

The allowlist shipped first as SaaS policy and is now a core capability,
so the records it created have to change owner before core's data loads.
Two of them would otherwise collide rather than merge: ``cloud.job.type``
has a unique index on ``code``, and core is updated *before* the SaaS
module that still declares the old copy, so core would try to insert a
second row with a code the first one already holds.

Re-pointing the external identifier makes core's data load recognise the
existing row and update it in place, which also keeps every
``cloud.job`` already referencing that type pointing at something real.
"""

#: (model, old module, old name, new name) of every record being adopted.
_ADOPTED = [
    (
        'cloud.job.type',
        'incubacloud_saas_manager',
        'push_github_webhook_edge',
        'push_github_webhook_edge',
    ),
    (
        'ir.cron',
        'incubacloud_saas_manager',
        'cron_refresh_github_hook_ranges',
        'cron_refresh_github_hook_ranges',
    ),
]

#: The stored range list moves under core's own namespace with it, so a
#: platform that already has one does not re-fetch and re-alert on the
#: first run after the upgrade.
_OLD_PARAM = 'incubacloud_saas.github_hook_ranges'
_NEW_PARAM = 'incubacloud.github_hook_ranges'


def migrate(cr, version):
    """Move the SaaS-owned allowlist records and parameter into core."""
    for model, old_module, old_name, new_name in _ADOPTED:
        cr.execute(
            """
            UPDATE ir_model_data
               SET module = 'incubacloud', name = %s, noupdate = false
             WHERE module = %s AND name = %s AND model = %s
               AND NOT EXISTS (
                     SELECT 1 FROM ir_model_data
                      WHERE module = 'incubacloud' AND name = %s
                        AND model = %s
                   )
            """,
            (new_name, old_module, old_name, model, new_name, model),
        )
    cr.execute(
        """
        UPDATE ir_config_parameter SET key = %s
         WHERE key = %s
           AND NOT EXISTS (
                 SELECT 1 FROM ir_config_parameter WHERE key = %s
               )
        """,
        (_NEW_PARAM, _OLD_PARAM, _NEW_PARAM),
    )
    # A cron declared in XML also creates an ``ir.actions.server`` with
    # an identifier of its own, ``<cron name>_ir_actions_server``. Left
    # behind, that row keeps the action owned by a module which no
    # longer declares it — an orphan whose removal would take the cron
    # with it. It is flagged ``noupdate`` on creation, so it is cleared
    # here too, or core's own name for the action would never apply.
    cr.execute(
        """
        UPDATE ir_model_data
           SET module = 'incubacloud', noupdate = false
         WHERE module = 'incubacloud_saas_manager'
           AND model = 'ir.actions.server'
           AND name = 'cron_refresh_github_hook_ranges_ir_actions_server'
           AND NOT EXISTS (
                 SELECT 1 FROM ir_model_data
                  WHERE module = 'incubacloud'
                    AND model = 'ir.actions.server'
                    AND name = 'cron_refresh_github_hook_ranges_ir_actions_server'
               )
        """
    )
