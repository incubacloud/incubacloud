# RB-06: Multi-worker deployment checklist

**Severity:** planned
**Typical trigger:** raising `ODOO_WORKERS` above 1 in production.
**Who runs it:** ops + developer review.

Most of IncubaCloud is multi-worker safe — bus, job logs, rate
limits and alerts all live in PostgreSQL, so any worker can serve
any request. The known **non-correct-under-multi-worker** subsystem
is the in-browser terminal: each terminal is a Python subprocess on
*one* worker's host, and only that worker's children can serve its
read/write/resize calls.

This runbook is the gating checklist before flipping the workers
knob.

## Symptoms / triggers

This is preventive; there are no failure symptoms yet — you are
about to introduce the failure mode if you skip the checklist.

## Diagnosis

Confirm the current single-worker setup before changing it:

```bash
$ docker compose exec odoo bash -c 'echo $ODOO_WORKERS'
```

In doodba, `ODOO_WORKERS=0` (threaded) or `=1` is the safe baseline;
anything `>1` is what this runbook gates.

## Resolution

Walk down the checklist before changing `ODOO_WORKERS`. Each item
must be verified or the workers change must be reverted.

### 1. Terminal routing must not assume worker locality

Open [controllers/terminal.py](../../incubacloud/controllers/terminal.py):

- Every read/write/resize call must look up `cloud.terminal.route`
  by `session_id` and proxy to the recorded `port` over the loopback.
- If you see direct `subprocess.Popen` references kept in
  module-level dicts → **STOP**. Those die under multi-worker.

If a route's port is bound on a different worker's process, the
loopback proxy still works (single host). What does **not** work is
multi-host: the moment the Odoo container is split across two
hosts, terminals break. Document this in the deploy doc.

### 2. Bus channels must be Postgres-backed

```bash
$ docker compose exec odoo bash -c 'echo $ODOO_BUS_DRIVER'
```

Must be empty or `postgresql` (default). Anything in-memory loses
events across workers.

### 3. Rate limit windows must hit the DB

[`models/cloud_rate_limit.py`](../../incubacloud/models/cloud_rate_limit.py)
keeps one row per bucket per 60-second window and increments it with
an atomic upsert — workers see each other's hits. Verify by tailing
the table during a burst:

```sql
db$ SELECT bucket, count FROM cloud_rate_limit
    WHERE window_start > NOW() - INTERVAL '60 seconds'
    ORDER BY count DESC;
```

The `count` should grow regardless of which worker took the request.

### 4. Cron bot ownership

Crons run on whichever worker happens to pick them up. Verify the
cron bot user owns every cron from `incubacloud*`:

```sql
db$ SELECT m.name, c.user_id
    FROM ir_cron c
    JOIN ir_model_data d ON d.res_id = c.id AND d.model = 'ir.cron'
    JOIN ir_module_module m ON m.name = d.module
    WHERE m.name LIKE 'incubacloud%';
```

Every row must show the cron-bot uid (not 1). If any show uid=1, run
`_incubacloud_assign_cron_user_id` for that module ([RB-08](RB-08-rotate-cron-bot.md)).

### 5. queue.job channel sizing

```sql
db$ SELECT name, capacity FROM queue_job_channel;
```

Default `root` capacity is 1. Under multi-worker, raise it
deliberately — but understand that two simultaneous deploys to the
same `cloud.instance` is not safe (data race already prevented at
the executor level, see deploy executor `_check_no_active_job`).

### 6. Re-run the test suite under threading

```bash
$ invoke test -m incubacloud
```

Anything that uses module-level mutable state will surface here.
Pay attention to flaky tests — under multi-worker, "flaky" usually
means "race condition in production".

### 7. Roll out gradually

Bump `ODOO_WORKERS` in this order: 1 → 2 → 4. Monitor for 24h at
each step. The signals to watch:

- Terminal sessions: open one, leave it idle, reload the SPA, ensure
  it reattaches.
- Bus: open the SPA in two tabs, trigger a job in one, the other tab
  must update.
- Crons: every cron from §4 must have ticked at least once.

## Rollback

Setting `ODOO_WORKERS` back to its old value and `invoke restart`
fully reverts the change. No data is at risk — the runbook is purely
a configuration toggle on the Odoo runtime.

## References

- [`controllers/terminal.py`](../../incubacloud/controllers/terminal.py)
- [`models/cloud_terminal_route.py`](../../incubacloud/models/cloud_terminal_route.py)
- [`models/cloud_rate_limit.py`](../../incubacloud/models/cloud_rate_limit.py)
- [RB-03](RB-03-orphan-terminal-subprocesses.md) — terminal cleanup,
  more important once workers > 1.
- [RB-08](RB-08-rotate-cron-bot.md) — cron bot ownership audit.
