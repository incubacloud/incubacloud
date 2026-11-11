-- incubacloud_mcp: what a restored copy of production must not keep.
-- Loaded by odoo neutralize / click-odoo-restoredb --neutralize (no manifest
-- entry needed).
--
-- The module's HMAC key signs what this panel hands its agents (their
-- session ids, the state of an approval they come back with): a copy signs
-- with a key of its own, which _mcp_secret() creates on first use. The URL
-- of /mcp is production's: without it the copy takes web.base.url's, which
-- the next module update stores. The agents' issuer is production's trust:
-- the copy's agents connect with API keys until one is set again.
-- Approvals and download links keep only the hash of their token and live
-- minutes: they stay, already expired. The auto-approval rules still live
-- are revoked (``revoked_reason`` 'manual', by nobody): an agent holding a
-- credential of production, an API key copied with the users, would
-- otherwise act on the copy without asking. A copy made before the rules
-- existed has no table for them.

DELETE FROM ir_config_parameter
 WHERE key IN ('incubacloud.mcp.secret', 'incubacloud.mcp.resource');
UPDATE cloud_settings SET mcp_oauth_issuer = NULL;
DO $$
BEGIN
    IF to_regclass('incubacloud_mcp_auto_approval') IS NOT NULL THEN
        UPDATE incubacloud_mcp_auto_approval
           SET revoked_at = now() AT TIME ZONE 'UTC',
               revoked_by_id = NULL,
               revoked_reason = 'manual'
         WHERE revoked_at IS NULL;
    END IF;
END
$$;
