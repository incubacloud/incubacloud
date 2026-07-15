-- IncubaCloud core — neutralize environment-specific data after prod restore.
-- Loaded by odoo neutralize / click-odoo-neutralize (no manifest entry needed).
--
-- Usage:
--   click-odoo-copydb restore devel < dump.sql
--   click-odoo-neutralize -d devel
--   invoke restart

-- 1. Nullify all EncryptedChar fields (prevents decrypt crashes with wrong key)
UPDATE cloud_host SET password = NULL, key_file = NULL, traefik_panel_password = NULL;
UPDATE cloud_backup_backend SET s3_secret_access_key = NULL, passphrase = NULL;
UPDATE cloud_github_app SET webhook_secret = NULL, private_key = NULL;
UPDATE cloud_instance SET odoo_admin_password = NULL, odoo_admin_user_password = NULL,
    postgres_password = NULL, smtp_relay_password = NULL;
UPDATE cloud_settings SET github_pat = NULL;
UPDATE res_users SET cloud_telegram_bot_token = NULL, cloud_webhook_secret = NULL;

-- 2. Clear known_hosts fingerprints (prod-specific)
UPDATE cloud_host SET known_hosts_key = NULL;

-- 3. Deactivate infra records to prevent accidental SSH/provisioning
UPDATE cloud_host SET active = false;
UPDATE cloud_instance SET active = false;
UPDATE cloud_backup_backend SET active = false;

-- 4. Delete ephemeral session data
DELETE FROM cloud_terminal_route;
DELETE FROM cloud_rate_limit;
DELETE FROM mail_mail;

-- 5. Clear environment-specific ir.config_parameters
DELETE FROM ir_config_parameter WHERE key = 'incubacloud.backup_backend_id';
DELETE FROM ir_config_parameter WHERE key = 'incubacloud.host_autoassign';
UPDATE ir_config_parameter SET value = '' WHERE key = 'web.base.url';
