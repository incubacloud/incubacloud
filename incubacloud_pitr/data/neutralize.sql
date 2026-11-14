-- Neutralization of continuous recovery in a restored copy of production.
-- The copy must never archive into production's prefix, nor hold the
-- keys that decrypt production's archive.
UPDATE cloud_instance SET pitr_key = NULL, pitr_enabled = false, pitr_state = 'off',
    pitr_cluster_id = NULL, pitr_previous_clusters = NULL;
-- Nor date, purge or alert about production's archive (1.0.16): an
-- instance switched on again in the copy would empty production's prefix
-- once switched off. A copy taken before them lacks the columns.
DO $$ BEGIN
    UPDATE cloud_instance SET pitr_off_at = NULL, pitr_purge_at = NULL,
        pitr_rendered_prefix = NULL, pitr_rendered_backend_id = NULL;
EXCEPTION WHEN undefined_column THEN NULL;
END $$;
