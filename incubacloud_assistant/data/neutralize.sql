-- incubacloud_assistant: what a restored copy of production must not keep.
-- Loaded by odoo neutralize / click-odoo-restoredb --neutralize (no manifest
-- entry needed).
--
-- The AI provider's key is the only secret the assistant stores: it spends
-- the customer's money from anywhere, so it never travels into a copy. The
-- code tools' GitHub credentials are the panel's own (cloud_settings.github_pat,
-- the App's key), cleared by the core's script.

UPDATE cloud_settings SET assistant_api_key = NULL;
