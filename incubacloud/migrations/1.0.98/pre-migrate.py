"""Pre-migrate for 1.0.98 — make room for the metric-rule code uniqueness.

``cloud.metric.rule`` declared ``_sql_constraints = [("code_uniq", ...)]``,
which Odoo 19 never reads: ``_add_sql_constraints`` applies
``_table_objects``, populated only by ``models.Constraint``/``Index``
descriptors. The uniqueness therefore did not exist in any database, and
``code`` is the alert dedup key — two active rules sharing one fight over
the same ``cloud.alert`` row, so the one that does not fire resolves the
other's genuine alert.

1.0.98 replaces it with a real partial unique index over active rules.
Creating it fails if a database already holds duplicates, so they have to
go first. ``incubacloud`` is installed on every tenant database and those
cannot be surveyed from here — devel and production were both checked and
are clean (6 rules, 6 codes), so on our own estate this is a no-op. It
exists for the ones we cannot see.

Duplicates are **archived, not deleted**: the rows are operator
configuration, they stay visible and reversible in the panel, and
archiving is enough to satisfy an index that only covers active rows.
Re-activating one without fixing its code hits the constraint and gets
the message explaining why.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Archive every active metric rule whose code another one already uses.

    The survivor per code is the rule carrying an ``ir.model.data`` row
    (the seeded, canonical one) and, failing that, the lowest id — the
    oldest, which is the one whose alerts are already in flight.
    """
    cr.execute(
        """
        WITH ranked AS (
            SELECT r.id,
                   r.code,
                   row_number() OVER (
                       PARTITION BY r.code
                       -- EXISTS, not a JOIN: ir_model_data is only
                       -- unique on (module, name), so one rule can
                       -- carry several xmlids and a join would fan
                       -- out — a second xmlid made a sole-holder rule
                       -- rank as its own "duplicate" and get archived.
                       -- NOT EXISTS sorts false (has xmlid) first, so
                       -- the seeded rule survives; ties break on the
                       -- oldest id.
                       ORDER BY NOT EXISTS (
                           SELECT 1 FROM ir_model_data d
                            WHERE d.model = 'cloud.metric.rule'
                              AND d.res_id = r.id
                       ), r.id
                   ) AS rn
              FROM cloud_metric_rule r
             WHERE r.active IS TRUE
        )
        UPDATE cloud_metric_rule
           SET active = FALSE
          FROM ranked
         WHERE cloud_metric_rule.id = ranked.id
           AND ranked.rn > 1
        RETURNING cloud_metric_rule.id, cloud_metric_rule.code
        """
    )
    archived = cr.fetchall()
    for rule_id, code in archived:
        _logger.warning(
            "[metrics] archived duplicate metric rule id=%s code=%r: another "
            "active rule already raises that alert code. Give it its own "
            "code before re-enabling it.",
            rule_id, code,
        )
    if archived:
        _logger.warning(
            "[metrics] %s duplicate metric rule(s) archived so the new "
            "unique index over active codes can be created.", len(archived),
        )
