# RB-10: GitHub App / PAT rate-limited or revoked

**Severity:** critical
**Typical trigger:** webhooks silent, every API call returns
`401`/`403`, repo listing fails in the UI.
**Who runs it:** ops + the GitHub App owner (often the same
person).

The control plane talks to GitHub through a GitHub App (preferred)
or a personal access token (legacy). When the App's installation
token expires unrenewably, when a PAT is revoked, or when we hit
the secondary rate-limit, every outbound call fails — and nothing
in the SPA that lists branches, opens PRs or fetches commits works.

## Symptoms / triggers

- "Failed to load repositories" in the SPA.
- `401 Bad credentials` or `403 API rate limit exceeded` in the
  Odoo log against `api.github.com`.
- Webhook deliveries continue (GitHub sends those regardless), but
  any subsequent API call we make to read the commit fails.

## Diagnosis

1. **Check which credential is in use.** The App lives on the
   `cloud.github.app` singleton; the fallback PAT lives on
   `cloud.settings.github_pat` (not on the app record):

   ```sql
   db$ SELECT id, app_id, installation_id, slug,
              private_key IS NOT NULL AS has_key,
              webhook_secret IS NOT NULL AS has_webhook_secret
       FROM cloud_github_app;
   db$ SELECT github_pat IS NOT NULL AS has_pat FROM cloud_settings;
   ```

   `has_key = true` → the App is configured (installation tokens
   re-mint themselves hourly via the in-memory cache).
   `has_pat = true` and 401s → the PAT is revoked or expired.

2. **Inspect the headers** GitHub returned, from the log:

   ```bash
   odoo$ grep -E 'X-RateLimit|X-GitHub-Request-Id' /var/log/odoo/odoo.log | tail
   ```

   - `X-RateLimit-Remaining: 0` → primary rate-limit, reset time in
     `X-RateLimit-Reset` (epoch seconds).
   - No header + `401` → bad credential.
   - `403` with body `Secondary rate limit` → secondary rate-limit
     (concurrency-based, not request-count).

## Resolution

### Primary rate-limit hit

1. **Read `X-RateLimit-Reset`** to find when the window resets.
2. **Wait it out** — there is no operational fix; the limit is
   per-installation per-hour. Disable any in-flight cron that
   hammers the API:

   ```python
   env.ref('incubacloud.cron_some_github_call').write({'active': False})
   ```

3. **Once recovered**, audit the hot path that exhausted the budget.
   The usual culprit is a polling cron set too aggressive (the
   module-level token cache in `incubacloud/github/token_cache.py`
   auto-refreshes App tokens and shouldn't burn through budget on
   its own).

### Secondary rate-limit hit

This is concurrency-based: too many simultaneous calls. Lower the
queue.job channel capacity for any GitHub-touching job:

```sql
db$ UPDATE queue_job_channel SET capacity = 1 WHERE name = 'root';
```

Then sleep 60s and resume normal capacity. Restart is **not**
required.

### PAT revoked

1. **Issue a fresh PAT** with the same scopes (`repo`, `read:org`).
2. **Store it on `cloud.settings`** — via the SPA (Settings → GitHub,
   which calls `/cloud/save_github_pat`), or from `odoo shell`:

   ```python
   env['cloud.settings'].search([]).write({'github_pat': '<new-pat>'})
   env.cr.commit()
   ```

   Do **not** UPDATE via SQL: the field is `EncryptedChar`, and a raw
   SQL write stores the token in plaintext instead of `enc:…`.

3. **Smoke-test** by listing branches in the SPA for a project
   using that credential.

### App private key revoked / installation deleted

Worst case — the App needs to be re-issued or re-installed on the
target org/repos. Owner runs through GitHub's UI, then:

1. **Update `private_key`, `app_id`, `installation_id`** on the
   `cloud.github.app` record via the SPA Settings → GitHub tab.
2. **Re-run the smoke test**: list repos / branches. Token cache
   re-mints transparently on first use.

## Rollback

Credential rotation is forward-only — the previous PAT or App key
is gone. The "rollback" is to put a known-working credential back
in the `cloud.github.app` record. Always keep a pre-rotation
backup of the values until the new credential is verified working
end-to-end.

## References

- [`models/cloud_github_app.py`](../../incubacloud/models/cloud_github_app.py)
  — token cache + minting logic.
- [`models/cloud_github_event.py`](../../incubacloud/models/cloud_github_event.py)
  — webhook ingest (independent of outbound API).
- [RB-07](RB-07-webhook-replay-investigation.md) — webhook-side
  issues, separate from outbound API.
- GitHub docs: <https://docs.github.com/rest/overview/rate-limits-for-the-rest-api>
