"""Pre-migrate for 1.0.198 — add ``cloud.job.asked_as_superuser`` in place.

A job runs as its creator now, with superuser rights only when it was
asked for with them. Every job before this version was, as far as the
field goes: its default is the environment's ``su``, and Odoo fills a
new column with it under the superuser. Left to Odoo, that fill is one
``UPDATE`` of every row of ``cloud_job`` (about 315,000 in production,
14 seconds in devel), which rewrites each of them and leaves the table
twice its size until a vacuum catches up.

Added here first with a constant default, PostgreSQL (11 and later)
records the value once in the catalogue instead of writing it into the
rows, and dropping the default afterwards leaves those rows with it.
Odoo then finds the column there and fills nothing. Jobs created from
now on get the value from the field's default, as before.
"""


def migrate(cr, version):
    """Add the column with every existing job asked for with superuser rights."""
    if not version:
        return
    cr.execute(
        """
        ALTER TABLE cloud_job
            ADD COLUMN IF NOT EXISTS asked_as_superuser boolean DEFAULT true
        """
    )
    cr.execute(
        """
        ALTER TABLE cloud_job
            ALTER COLUMN asked_as_superuser DROP DEFAULT
        """
    )
