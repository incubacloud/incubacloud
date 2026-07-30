# RB-02: Restore master DB from off-site backup

**Severity:** critical
**Typical trigger:** control-plane DB lost, corrupted, or returning
garbage after a bad migration.
**Who runs it:** ops lead (requires off-site backup credentials).

The master database holds every `cloud.host`, `cloud.project`,
`cloud.instance`, `cloud.job`, `cloud.alert` and encrypted secret.
Losing it means losing the inventory used to reach every tenant —
tenants keep running, but we can no longer drive them from the UI.

The doodba production stack has duplicity writing full + incremental
backups to the off-site S3 bucket every night. This runbook restores
from that bucket.

## Symptoms / triggers

- `/cloud/ui` returns 500 on every request.
- `psql` complains about missing relations or corrupted indexes.
- `pg_isready` returns "rejecting connections".
- A migration was interrupted by OOM / power loss mid-upgrade.
- Someone `DROP TABLE`d on the wrong shell.

## Diagnosis

Before restoring, confirm that the current DB is actually
unrecoverable:

```bash
$ docker compose exec db psql -U odoo -d prod -c '\dt' | head
$ docker compose exec db pg_dump -U odoo -d prod --schema-only \
    > /tmp/schema-snapshot.sql  # keep for forensics
```

If the schema reads fine and only a few rows are wrong, prefer a
targeted fix over a full restore — restore is lossy (you lose every
change since the last backup, typically 0–24h).

Then verify the backup bucket has a fresh chain:

```bash
$ docker compose run --rm backup \
    duplicity collection-status $BACKUP_DST
```

You need to see a "chain end time" within the last 24h. If the last
backup is older, call this out in the incident channel — data loss
window is larger than usual.

## Knowing the backup ran at all

The backup container emails on failure, which covers *"it ran and it
broke"*. It cannot cover *"it never ran"* — a stopped container, a
misconfigured schedule or a powered-off host produce no email precisely
because nothing executed. That silence is indistinguishable from success.

Close it with an external dead-man's switch. The doodba backup image
(`docker-duplicity-postgres-s3`) already speaks the healthchecks.io
protocol: give any job a ping URL and its runner pings `/start` before
the job, the plain URL on success, and `/fail` on failure — and, because
the runner carries a failure flag forward, a job that failed earlier in
the same run marks the later ones as failed too.

Two schedules need watching, because they fail independently:

| Job | What it does | Runs |
|---|---|---|
| `JOB_200` | `pg_dump` of the databases into `$SRC` | daily **and** weekly |
| `JOB_300` | uploads the incremental to `$DST` | daily |
| `JOB_500` | writes a fresh full chain | weekly |
| `JOB_800` | prunes chains older than the retention | weekly |

Watching only the daily job leaves the **weekly full unmonitored**, and
that is the one holding the chain up: if fulls stop running, incrementals
keep stacking on an ever-older base and the restore quietly degrades
while every daily ping stays green.

Create two checks and add their URLs to the backup service's env file
(`.docker/backup.env`, never committed):

```
JOB_300_HEALTHCHECKS_URL=https://hc-ping.com/<uuid>   # period 1 day,  grace ~6h
JOB_500_HEALTHCHECKS_URL=https://hc-ping.com/<uuid>   # period 7 days, grace ~1 day
```

Each check covers its whole chain, not just its own step: `JOB_200` runs
first in both batches, and the runner carries a failure forward — so a
failed dump sends the later job's ping to `/fail` too.

Leave a variable out entirely to disable its pinging; setting it to an
empty string instead makes the runner attempt a request to an empty URL.

Now no ping within the grace window raises an alert from outside the
infrastructure, which is the only place that can notice the whole host
is gone. Verify it once by pinging the URL by hand and watching the
check go green, then by stopping the container for a cycle.

Related: the restore procedure itself should be rehearsed, not assumed —
see the restore drill in the deployment repo (`drill-restore/`).

## Resolution

1. **Put the stack in maintenance mode** (so no new writes land
   before the restore completes):

   ```bash
   $ docker compose stop odoo odoo_proxy
   ```

   Leave `db` and `backup` running — we need them for the restore.

2. **Move the corrupted DB aside**, do not drop it. If the restore
   fails we may need to forensically pull rows out of it:

   ```bash
   db$ ALTER DATABASE prod RENAME TO prod_corrupt_YYYYMMDD;
   db$ CREATE DATABASE prod OWNER odoo;
   ```

3. **Restore from duplicity** into the fresh `prod`:

   ```bash
   $ docker compose run --rm backup \
       duplicity restore $BACKUP_DST /restore
   $ docker compose run --rm backup \
       pg_restore -d prod -U odoo -j 4 /restore/prod.dump
   ```

4. **Run Odoo with `--update all`** on the restored DB in a one-shot
   container before bringing the stack up — if the backup pre-dates
   a migration we've already shipped, this reapplies it:

   ```bash
   $ invoke install -m all
   ```

5. **Verify key invariants** before exposing the UI:

   ```sql
   db$ SELECT COUNT(*) FROM cloud_host;
   db$ SELECT COUNT(*) FROM cloud_instance WHERE state = 'production';
   db$ SELECT MAX(create_date) FROM cloud_job;
   ```

   Counts should be close to the pre-incident numbers. `max(create_date)`
   tells you the actual data loss window.

6. **Bring the stack back up**:

   ```bash
   $ invoke restart
   ```

7. **Drop the corrupt copy** only after 24h of clean operation:

   ```bash
   db$ DROP DATABASE prod_corrupt_YYYYMMDD;
   ```

## Rollback

If step 3 fails partway (restore aborts, restore completes but the
DB is still broken), the renamed `prod_corrupt_YYYYMMDD` is
untouched — rename it back and investigate with the original data:

```sql
db$ DROP DATABASE prod;
db$ ALTER DATABASE prod_corrupt_YYYYMMDD RENAME TO prod;
```

If you are mid-`pg_restore`, you can cancel it safely — `prod` is
the new empty database at that point, so no user data is at risk.

## References

- [RB-01](RB-01-rotate-secret-key.md) — after a restore, consider
  rotating the Fernet key (the off-site backup was encrypted with
  the old key but its transit integrity is now uncertain).
- `doodba.yaml` — `backup` service definition.
