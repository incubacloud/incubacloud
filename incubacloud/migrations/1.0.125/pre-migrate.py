"""Pre-migrate for 1.0.125 — keep the retired job type while jobs use it.

Core stops declaring ``push_github_webhook_edge``, and at the end of the
upgrade Odoo deletes every record whose identifier a module no longer
loads. ``cloud.job.job_type_id`` is a required many2one, so its foreign
key restricts: on a database holding even one job of that type the
delete is refused, and the refusal aborts the whole upgrade. That is any
installation that switched the allowlist on without the SaaS module
above it — a tenant panel included.

Flagging the identifier ``noupdate`` takes it out of that cleanup, so
the type survives for as long as history points at it. With no job using
it nothing is flagged and it is deleted as usual. Where the SaaS module
is installed, its own migration takes the identifier over — and clears
the flag — before the cleanup runs.
"""


def migrate(cr, version):
    """Flag the retired type's identifier while any job still uses it."""
    cr.execute("""
        UPDATE ir_model_data d
           SET noupdate = TRUE
         WHERE d.module = 'incubacloud'
           AND d.model = 'cloud.job.type'
           AND d.name = 'push_github_webhook_edge'
           AND EXISTS (
                 SELECT 1 FROM cloud_job j WHERE j.job_type_id = d.res_id
               )
    """)
