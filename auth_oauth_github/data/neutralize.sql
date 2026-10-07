-- auth_oauth_github: what a restored copy of production must not keep.
-- Loaded by odoo neutralize / click-odoo-neutralize (no manifest entry needed).
--
-- Stock auth_oauth only disables the providers. For GitHub that leaves two
-- live credentials in the copy: the OAuth App's client secret, which works
-- from anywhere, and the token stored on every user who signed in with
-- GitHub (res_users.oauth_access_token, plain text), which is a real GitHub
-- token that does not expire. Both go; the client id is public and stays.

UPDATE auth_oauth_provider SET client_secret = NULL WHERE github_flow;
UPDATE res_users SET oauth_access_token = NULL
 WHERE oauth_provider_id IN (SELECT id FROM auth_oauth_provider WHERE github_flow);
