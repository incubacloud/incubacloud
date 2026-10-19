-- Neutralization of continuous recovery in a restored copy of production.
-- The copy must never archive into production's prefix, nor hold the
-- keys that decrypt production's archive.
UPDATE cloud_instance SET pitr_key = NULL, pitr_enabled = false, pitr_state = 'off';
