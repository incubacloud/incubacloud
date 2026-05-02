# Restore a backup

Three flavours:

- **Download exact** — full database with original users, crons, mail servers, credentials.
  For forensic recovery.
- **Download neutralized** — same data, but crons turned off, mail servers archived,
  credentials scrubbed, and a red ribbon on top. Safe to load on a dev laptop.
- **Restore in place** — replaces the live database of an instance with the chosen backup.
  Used to roll back a bad update.

## Pick the right mode

If you're moving data to development or staging, **download neutralized**.
If you're rolling back production after a broken release, **restore in place**.
If you need a forensic copy with original credentials and crons, **download exact**.

## Download a backup

1. Open the instance and go to **Backups**. You see a chronological list of backups
   available in your bucket.
2. Pick a backup, click **Download**.
3. Choose **Exact** or **Neutralized**.
4. The job starts and shows a progress bar.
5. When ready, click the download link. You get a standard ZIP
   (`dump.sql + filestore/`) — restorable on any Odoo of the same version.

## Restore in place

1. Open the instance and go to **Backups**.
2. Pick the backup you want to restore.
3. Click **Restore**.
4. On production, you'll be asked to confirm twice. We want to be very sure.
5. Wait for the restore to finish. The instance is briefly unavailable while
   the database is replaced. When the job ends in green, you're back online with the older data.

!!! warning "In-place restore is destructive"
    The current database is replaced by the backup. Anything created or modified after
    that backup's timestamp is lost. Take a fresh manual backup first if in doubt.

## Restore from an external ZIP

You can also upload a ZIP from your laptop or push it via rsync to the host.

### Upload (web)

Open the instance → **Restore Database** → drop or select the ZIP. Maximum 2 GB.
Larger files time out — use rsync instead.

### Push (rsync)

For dumps larger than 2 GB:

```bash
rsync -P your-dump.zip user@host:/var/lib/incubacloud/incoming/
```

Then in the instance UI, **Restore Database → From host file** and pick the file.

## Verify it worked

For downloads:

- [x] The ZIP opens and contains `dump.sql` + `filestore/`.

For in-place restore:

- [x] The instance is back up.
- [x] The data matches the backup's timestamp.
- [x] The job log ends in green.

## Troubleshooting

??? note "Restore failed: passphrase mismatch"
    The backup was encrypted with a different passphrase than the one currently
    configured on the backend. Check `Backend detail → Passphrase`. If you've
    rotated passphrases, you need the original to read older backups.

??? note "Restore failed: schema version mismatch"
    The backup was taken from a different Odoo version than the instance you're
    restoring to. Deploy a fresh instance with the matching Odoo version, then
    restore there.

??? note "I want to restore to a different instance, not the source"
    Deploy a new instance, configure its backup backend to point to the same
    bucket, and the backup list will include the source's backups. Pick the one
    you want and Restore.

## See also

- [Backups overview](index.md)
- [Migrate from Odoo.sh](../migrations/from-odoosh.md) — uses the same restore flow.
