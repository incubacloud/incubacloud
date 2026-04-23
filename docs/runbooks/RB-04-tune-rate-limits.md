# RB-04: Tune rate limits

**Severity:** warning
**Typical trigger:** legitimate users hit `429`, or a real abuser
slips past the current cap.
**Who runs it:** ops.

`cloud.rate.limit` (core) and `saas.rate.limit` (SaaS) are
sliding-window counters with per-action caps. Each `hit()` call
records the action; if the count in the window exceeds the cap the
caller sees `RateLimitError` → HTTP 429. Caps are configurable via
`ir.config_parameter`; defaults live in the model.

## Symptoms / triggers

- "Too many requests" toast on the SPA, repeated on retry.
- `cloud.alert` of severity `warning` with subject containing
  "rate-limit exceeded".
- A spike of 429s in the access log on a known legitimate burst
  (e.g. a redeploy script triggering 50 jobs at once).
- Conversely: an audit shows a brute-force pattern that the current
  cap allowed through.

## Diagnosis

Find the actions actually being throttled:

```sql
db$ SELECT action, COUNT(*) AS hits, MAX(hit_at) AS most_recent
    FROM cloud_rate_limit
    WHERE hit_at > NOW() - INTERVAL '1 hour'
    GROUP BY action
    ORDER BY hits DESC;
```

Then look up the current cap for the noisy action. Caps live as
`ir.config_parameter` keyed `incubacloud.rate_limit.<action>` (core)
or `saas.rate_limit.<action>` (SaaS). Falls back to the per-action
default in `_get_cap()`:

```sql
db$ SELECT key, value FROM ir_config_parameter
    WHERE key LIKE '%rate_limit%';
```

The "Rates" tab in Settings shows the same data with current cap
side by side; use it for a quick eyeball check.

## Resolution

There are two scenarios, with opposite remedies.

### Scenario A: legitimate traffic being throttled

Raise the cap for the affected action. From the Settings → "Rates"
tab, edit the row and save — the model writes the parameter. From
SQL, set the parameter directly:

```sql
db$ INSERT INTO ir_config_parameter (key, value)
    VALUES ('incubacloud.rate_limit.deploy_instance', '20')
    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
```

The new cap takes effect on the next `hit()` call — no restart
needed.

### Scenario B: cap was too permissive

Lower the cap the same way. **Then** purge the existing window so
the new cap is enforced from now, not retroactively from N minutes
ago:

```sql
db$ DELETE FROM cloud_rate_limit
    WHERE action = 'login_attempt'
      AND hit_at < NOW() - INTERVAL '1 minute';
```

If the abuse is by a known IP / user, prefer blocking at the proxy
layer first — rate limits are last-line, not first-line.

## Rollback

Caps are single key/value pairs in `ir_config_parameter`. To revert,
either set the previous value back, or delete the row to fall back
to the in-code default:

```sql
db$ DELETE FROM ir_config_parameter
    WHERE key = 'incubacloud.rate_limit.deploy_instance';
```

The GC cron (every hour) prunes window rows older than the longest
configured window; you do not need to rotate state manually.

## References

- [`models/cloud_rate_limit.py`](../../incubacloud/models/cloud_rate_limit.py)
  — `hit()`, `_get_cap()`, `_gc()`.
- `static/src/components/core_rates_tab/` — the Settings UI tab.
- `tests/test_cloud_rate_limit.py` — windows / GC / test-mode bypass.
- `saas.rate.limit` (in `incubacloud_saas_manager`) — same shape,
  separate table, same tuning procedure.
