"""Forget the health an archived instance was archived with.

Archiving now drops the services and status of the last health check:
nothing checks an archived instance, so they were shown for ever (a
deleted tenant's instance showed its database and mail relay in green,
and "error", after its containers had been removed). This clears the
instances archived before this version.
"""


def migrate(cr, version):
    """Clear the last check's services and status on archived instances."""
    if not version:
        return
    cr.execute(
        """
        UPDATE cloud_instance
           SET service_states = NULL,
               status = 'ok'
         WHERE active IS NOT TRUE
           AND archived_at IS NOT NULL
        """
    )
