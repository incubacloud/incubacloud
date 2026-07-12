# RB-04: Tune rate limits

**Severity:** warning
**Typical trigger:** legitimate users hit `429`, or a real abuser
slips past the current cap.
**Who runs it:** ops.

`cloud.rate.limit` is a DB-backed **tumbling-window counter**: one row
per bucket (e.g. `webhook_ip:1.2.3.4`, `terminal_user:42`,
`terminal_instance:7`) per 60-second window, incremented with an atomic
upsert. `hit()` returns `True`/`False` — it never raises. When it
returns `False` the caller answers HTTP 429 (HTTP endpoints) or
`{ok: false, error}` (JSON-RPC). Admin-tunable caps live as fields on
`cloud.settings`; two buckets (restore upload = 2/min/user, health =
60/min/IP) are hardcoded.

## Symptoms / triggers

- "Too many requests" toast on the SPA, repeated on retry.
- HTTP 429 on `/cloud/github/webhook` (with `Retry-After: 60`) or
  `/cloud/health`.
- A spike of 429s on a known legitimate burst (e.g. a busy GitHub org
  pushing to many repos at once).
- Conversely: an audit shows a brute-force pattern that the current
  cap allowed through.

## Diagnosis

Find the buckets actually being throttled:

```sql
db$ SELECT bucket, count, window_start
    FROM cloud_rate_limit
    WHERE window_start > NOW() - INTERVAL '1 hour'
    ORDER BY count DESC
    LIMIT 20;
```

Then look at the current caps. The tunable ones are fields on the
`cloud.settings` singleton (defaults in parentheses):

- `rate_limit_webhook_per_min` (300) — GitHub webhook, per IP
- `rate_limit_terminal_per_min` (30) — terminal opens, per instance
- `rate_limit_terminal_user_per_min` (10) — terminal opens, per user

```sql
db$ SELECT rate_limit_webhook_per_min,
           rate_limit_terminal_per_min,
           rate_limit_terminal_user_per_min
    FROM cloud_settings;
```

The "Rates" tab in Settings shows the same values; use it for a quick
eyeball check. A value ≤ 0 falls back to the in-code default.

## Resolution

There are two scenarios, with opposite remedies.

### Scenario A: legitimate traffic being throttled

Raise the cap from the Settings → "Rates" tab (preferred), or from SQL:

```sql
db$ UPDATE cloud_settings SET rate_limit_webhook_per_min = 600;
```

The new cap takes effect on the next `hit()` call — no restart needed.
(If you changed it via SQL, invalidate caches or restart so the ORM
cache doesn't serve the old value.)

The restore-upload (2/min/user) and health (60/min/IP) caps are not
tunable — those limits changing would be a code change, not an ops
action.

### Scenario B: cap was too permissive

Lower the cap the same way. **Then** reset the offending bucket's
current window so the new cap is enforced from now:

```sql
db$ DELETE FROM cloud_rate_limit
    WHERE bucket = 'webhook_ip:203.0.113.7';
```

If the abuse is by a known IP / user, prefer blocking at the proxy
layer first — rate limits are last-line, not first-line.

## Rollback

Set the previous value back on `cloud.settings` (Rates tab or SQL
UPDATE). Setting a tunable cap to `0` falls back to the in-code
default.

The GC cron (daily) prunes counter rows whose window started more than
24 hours ago; you do not need to rotate state manually.

## References

- [`models/cloud_rate_limit.py`](../../incubacloud/models/cloud_rate_limit.py)
  — `hit()`, `_get_cap()`, `_gc()`, `RATE_LIMIT_DEFAULTS`.
- `models/cloud_settings.py` — the tunable cap fields.
- `static/src/components/core_rates_tab/` — the Settings UI tab.
- `tests/test_cloud_rate_limit.py` — windows / GC / test-mode bypass.
