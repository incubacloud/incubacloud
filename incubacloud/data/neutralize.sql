-- IncubaCloud core — neutralize environment-specific data after prod restore.
-- Run, with every installed module's own file, by `odoo neutralize -d <db>`
-- and by `click-odoo-restoredb --neutralize` (no manifest entry needed).
-- Each file is one statement: one error aborts the whole neutralization.
--
-- Usage:
--   click-odoo-restoredb --neutralize devel <backup>
--   docker compose exec -T db psql -U odoo -d devel -c "DELETE FROM ir_mail_server
--       WHERE name = 'neutralization - disable emails' AND smtp_host = 'invalid'"
--   invoke restart
--
-- The DELETE drops the placeholder server Odoo's own neutralization adds,
-- so mail falls back to the stack's MailHog instead of failing. Only on a
-- stack whose SMTP is a mail catcher (devel, staging): on prod.yaml the
-- fallback is the real relay.

-- 1. Nullify all EncryptedChar fields (prevents decrypt crashes with wrong key)
UPDATE cloud_host SET password = '', key_file = NULL, traefik_panel_password = '';
UPDATE cloud_backup_backend SET s3_secret_access_key = NULL, passphrase = NULL;
UPDATE cloud_github_app SET webhook_secret = NULL, private_key = '';
UPDATE cloud_instance SET odoo_admin_password = NULL, odoo_admin_user_password = NULL,
    postgres_password = NULL, smtp_relay_password = NULL;
UPDATE cloud_settings SET github_pat = NULL, metrics_remote_write_token = NULL,
    metrics_operator_token = NULL, grafana_admin_password = NULL,
    metrics_delete_auth_key = NULL;
UPDATE cloud_host SET tls_default_key = NULL, tls_default_cert = NULL;
UPDATE cloud_job SET secret_payload = NULL;
UPDATE res_users SET cloud_telegram_bot_token = NULL, cloud_webhook_secret = NULL;
-- A column added after a copy was taken is missing from it, and a copy is
-- neutralized before it is upgraded: one unknown column aborts the whole
-- neutralization, Odoo's own included. Such a column is cleared on its own,
-- skipped where the copy predates it (secret_key: 1.0.189).
DO $$ BEGIN
    UPDATE cloud_instance SET secret_key = NULL;
EXCEPTION WHEN undefined_column THEN NULL;
END $$;

-- 2. Clear known_hosts fingerprints (prod-specific)
UPDATE cloud_host SET known_hosts_key = NULL;

-- 3. Deactivate infra records to prevent accidental SSH/provisioning
UPDATE cloud_host SET active = false;
UPDATE cloud_instance SET active = false;
-- Nothing else leaves the copy: its jobs and alerts would call the users'
-- webhooks, and its metrics would go to production's central.
UPDATE res_users SET cloud_webhook_url = NULL;
UPDATE cloud_settings SET metrics_enabled = false;
-- Production's queue travels with the copy, and what has not finished
-- would run there: cancelled, with the cloud.job that mirrors it. So is
-- what waits in an instance's queue (``queued``, no queue.job yet), which
-- the copy's dispatcher would start. A job waiting for an alert has no
-- state and is left as it is: only resolving that alert would start it.
WITH live AS (
    UPDATE queue_job SET state = 'cancelled'
     WHERE state IN ('wait_dependencies', 'pending', 'enqueued', 'started')
    RETURNING id
)
UPDATE cloud_job SET state = 'cancelled'
 WHERE queue_job_id IN (SELECT id FROM live) OR state = 'queued';

-- 4. Delete ephemeral session data; a console open in production is not
-- open in the copy (the host's console is younger than some copies).
DELETE FROM cloud_terminal_route;
DELETE FROM cloud_host_terminal_route;
DELETE FROM cloud_restore_upload_grant;
DELETE FROM cloud_rate_limit;
DELETE FROM mail_mail;
UPDATE cloud_instance_session SET state = 'closed',
    closed_at = now() AT TIME ZONE 'UTC' WHERE state = 'open';
DO $$ BEGIN
    UPDATE cloud_host_session SET state = 'closed',
        closed_at = now() AT TIME ZONE 'UTC' WHERE state = 'open';
EXCEPTION WHEN undefined_table THEN NULL;
END $$;

-- 5. Clear environment-specific ir.config_parameters
DELETE FROM ir_config_parameter WHERE key = 'incubacloud.backup_backend_id';
DELETE FROM ir_config_parameter WHERE key = 'incubacloud.host_autoassign';
UPDATE ir_config_parameter SET value = '' WHERE key = 'web.base.url';
