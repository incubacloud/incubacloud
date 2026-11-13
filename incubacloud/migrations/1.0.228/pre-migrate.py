"""Pre-migrate for 1.0.228 — an instance's name becomes a partial unique index.

``cloud_instance_name_project_uniq`` was the table constraint ``UNIQUE
(project_id, name)``, whose index PostgreSQL counts as a key: moving an
instance to another project then failed every older REPEATABLE READ
transaction that stored a row pointing at it. From this version the same
rule is a unique index with a predicate, under the same name.

The constraint is dropped here, before the module's tables are set up.
Left in place, its index has that same name, and ``CREATE UNIQUE INDEX
IF NOT EXISTS`` would keep it as it finds it, so the rule would stay a
constraint. The update then creates the partial index (``init()`` of
``cloud.instance``, as Odoo 18 has no ``models.UniqueIndex``) in the
same transaction.

Idempotent: once dropped there is no constraint of that name, and an
index of that name is not one.
"""
import logging

_logger = logging.getLogger(__name__)

_NAME = "cloud_instance_name_project_uniq"


def migrate(cr, version):
    """Drop the table constraint ``cloud_instance_name_project_uniq``, if it is one.

    :param cr: database cursor
    :param version: module version being upgraded from
    """
    if not version:
        return
    cr.execute(
        """
        SELECT 1
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
         WHERE c.conname = %s AND t.relname = 'cloud_instance'
           AND t.relnamespace = current_schema::regnamespace
        """,
        (_NAME,),
    )
    if not cr.fetchone():
        return
    cr.execute(f'ALTER TABLE cloud_instance DROP CONSTRAINT "{_NAME}"')
    _logger.info(
        "1.0.228: constraint %s dropped; the partial unique index of that "
        "name takes its place",
        _NAME,
    )
