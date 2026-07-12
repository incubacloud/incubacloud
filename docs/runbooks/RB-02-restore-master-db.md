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
