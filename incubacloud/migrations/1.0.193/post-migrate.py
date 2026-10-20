"""Post-migrate for 1.0.193 — open alerts take the identity raise_alert gives.

Every alert now goes through ``cloud.alert.raise_alert``. Three kinds kept
their identity outside it, and an open one must match what the engine
looks for, or its next sighting files a twin and its resolution misses it:

* ``job_failed``: the type of the failed job, now its ``dedup_key``;
* a backup backend's alerts: the backend, read from the payload;
* an alert about a project alone: the project, as ``project:<id>``.

A duplicate the old searches left behind is dismissed first, keeping the
newest, so the unique index never sees two open alerts with one identity.
"""

_KEYS = """
    SELECT a.id,
           CASE
               WHEN a.code = 'job_failed' THEN 'job_type:' || t.code
               WHEN a.code IN ('backup_retention_undeclared',
                               'managed_backend_unused')
                   THEN 'backup_backend:' || (a.payload->>'backup_backend_id')
               ELSE 'project:' || a.project_id
           END AS key
      FROM cloud_alert a
      LEFT JOIN cloud_job j ON j.id = a.job_id
      LEFT JOIN cloud_job_type t ON t.id = j.job_type_id
     WHERE a.state = 'active'
       AND a.dedup_key IS NULL
       AND (
            (a.code = 'job_failed' AND t.code IS NOT NULL)
         OR (a.code IN ('backup_retention_undeclared', 'managed_backend_unused')
             AND a.payload->>'backup_backend_id' IS NOT NULL)
         OR (a.code <> 'job_failed' AND a.project_id IS NOT NULL
             AND a.host_id IS NULL AND a.instance_id IS NULL)
       )
"""


def migrate(cr, version):
    """Give open alerts their key, dismissing duplicates of one identity."""
    cr.execute(
        f"""
        WITH keyed AS ({_KEYS}),
        ranked AS (
            SELECT k.id, ROW_NUMBER() OVER (
                       PARTITION BY a.code, COALESCE(a.instance_id, 0),
                                    CASE WHEN a.instance_id IS NULL
                                         THEN COALESCE(a.host_id, 0) END,
                                    k.key
                       ORDER BY a.id DESC
                   ) AS rank
              FROM keyed k JOIN cloud_alert a ON a.id = k.id
        )
        UPDATE cloud_alert a
           SET state = 'dismissed', resolution = 'auto'
          FROM ranked r
         WHERE a.id = r.id AND r.rank > 1
        """
    )
    cr.execute(
        f"""
        UPDATE cloud_alert a
           SET dedup_key = k.key
          FROM ({_KEYS}) k
         WHERE a.id = k.id
        """
    )
