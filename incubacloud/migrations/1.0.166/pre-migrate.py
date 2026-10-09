"""Let the backup quota alert template follow the code from now on.

It was loaded with ``noupdate`` set, which makes Odoo skip it on every
update, so it would never pick up the company mail layout this version
gives it, nor any later change of text. Its data file no longer sets the
flag, but Odoo only applies a file's flag to records it updates — this
lifts the old one so the update below reaches the record.
"""


def migrate(cr, version):
    """Clear ``noupdate`` on the existing template's XMLID."""
    if not version:
        return
    cr.execute(
        """
        UPDATE ir_model_data
           SET noupdate = FALSE
         WHERE module = 'incubacloud'
           AND name = 'mail_template_backup_usage_alert'
        """
    )
