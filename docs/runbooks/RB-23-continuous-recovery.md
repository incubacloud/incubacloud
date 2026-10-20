# RB-23 — Continuous recovery: alerts, a stuck restore, a moved instance

**Severity:** critical · **When:** a `pitr_failing`, `pitr_archive_lagging`
or `pitr_base_missing` alert; a restore to a moment that never ended; an
instance that moved host or changed backend and is no longer archiving.

How it works, in one paragraph (the rest is in
[architecture.md](../architecture.md#continuous-recovery-incubacloud_pitr)):
the instance's `db` writes its log segments to `pg_wal/archive_status`, a
`pitr` agent container (rclone, the only one with the bucket's
credentials) uploads them in order with wal-g, encrypted, under the
cluster's own prefix of the instance's backup bucket, and copies the
filestore's changed files every objective. Every ten minutes the panel
reads the host (`pitr.sh status`) and reconciles: switches on what the
plan or the operator wants, raises the alerts below. A restore runs
**detached on the host**, holding `.walg/restore/lock`, and leaves its
verdict in `.walg/restore/result`.

Everything below is under the instance's directory on its host
(`~/<project>-<version>-prod` or as the panel's shell opens it):

```
.walg/                 wal-g and its loader; run, archive
.walg/spool/           done/<segment> marks; fs-ok, the last filestore copy that worked
.walg/restore/         lock, pid, log, result of the restore under way or last run
.docker/pitr.env       the agent's credentials (0600): never printed
```

## 1. Read the alert

Alerts panel → the alert's message, or:

```sql
db$ SELECT i.name, a.code, a.level, a.message, a.create_date
    FROM cloud_alert a JOIN cloud_instance i ON i.id = a.instance_id
    WHERE a.code LIKE 'pitr_%' AND a.state = 'active';
```

| Code | Message starts with | Meaning |
|---|---|---|
| `pitr_failing` | *could not be switched on* | The `pitr_enable` job failed. The watch retries in a day, not before. |
| `pitr_failing` | *is not archiving its database* | `archive_mode` is off or the cluster changed (a move, a new deploy). The watch switches it on again. |
| `pitr_failing` | *agent … is not running* | The `pitr` container is down: nothing uploads. |
| `pitr_failing` | *cannot upload its database log* | Uploads fail (`N failures, M segments waiting`): bucket, credentials or network. PostgreSQL keeps the segments and retries. |
| `pitr_failing` | *A restore … stopped without finishing* | The restore's process is gone and its lock is left. See §3. |
| `pitr_archive_lagging` | *is behind archiving* | The log or the files are more than three objectives late. Usually transient. |
| `pitr_base_missing` | *first base copy failed* / *no base copy for 36 h* | Nothing (or nothing recent) to restore from. |

## 2. Diagnose on the host

From the instance's **Shell** in the panel (or `ssh` as the host's user):

```bash
cd ~/<instance dir>
docker compose ps pitr db                       # the agent must be Up
bash .walg/run status 2>/dev/null || sudo .walg/pitr.sh status "$PWD" odoo
docker compose logs --tail 100 pitr             # upload errors, rclone/wal-g output
ls pg_wal_status 2>/dev/null; docker compose exec -T db \
  psql -U odoo -d postgres -tAc "SELECT archived_count, failed_count, last_failed_wal, last_failed_time FROM pg_stat_archiver"
```

What `status` prints (`IC_PITR_STATUS …`): `mode=on|off`, `last_archived`
(epoch of the last segment uploaded), `failed`, `pending` and
`oldest_pending` (segments waiting), `agent=1|0`, `fs_ok` (epoch of the
last filestore copy), `cluster` (the database's system identifier),
and `restoring=1` or `restoring=lost job=N` while a restore is under way
or was abandoned.

- **Agent down** → `docker compose up -d pitr`; read its log for why.
- **Uploads failing** → the bucket or its credentials. Test from the
  agent, never with the credentials in your shell:
  `docker compose exec pitr rclone lsd bucket:/<prefix>`. A backend whose
  credentials rotated needs the instance rebuilt (the env file is
  rendered at deploy); the watch does it on the next reconcile once the
  backend is right.
- **`mode=off` or a new `cluster`** → the watch switches it on again
  within ten minutes. Not within a day of a switch that failed: fix the
  cause, then **Switch off** and **Switch on** from the card, or
  `instance.action_pitr_enable()` from a shell of the panel.
- **No base copy** → `sudo .walg/pitr.sh base-backup "$PWD" odoo` by
  hand to see the error; the daily one runs from the agent.

## 3. A restore that never ended

The alert *stopped without finishing* means `.walg/restore/lock` holds a
job's id and no process `.walg/restore/pid` names is alive. The restore
was interrupted (host rebooted, process killed) and the instance may be
stopped.

1. Read what it did: `cat .walg/restore/log` and
   `cat .walg/restore/result` (absent if it never got to decide).
2. Check the data directory it was working on: `ls -d data/ data.restore.* 2>/dev/null`.
   The restore fetches the base copy **beside** the live data and swaps
   only once recovery succeeded; the live data is the one `db` mounts.
3. If `db` is down, start the stack: `docker compose up -d`. It starts
   the data as it is — either the previous data (the swap never
   happened) or the restored one (it did).
4. Once Odoo answers, remove the lock: `rm .walg/restore/lock` (and
   `.walg/restore/pid`). Leave `log` and `result`.
5. Run the restore again from the card if the swap never happened. The
   same job run again (**Retry** in the jobs drawer) follows a restore
   under way instead of starting a second one; a new restore starts
   fresh once the lock is gone.

Never remove the lock while `pid` names a live process: `ps -p $(cat
.walg/restore/pid)` first.

## 4. An instance that moved host or changed backend

A move initialises a new cluster on the destination, and a backend
change a new prefix: both start a new archive. The watch takes care of
it (switch on again after the move's cutover, `pitr_enable` after the
rebuild that follows a backend change). What to expect:

- The card says *Switching on…* for a few minutes, then `OK · Archiving`
  with the window starting at the new base copy.
- **The old archive stays in the bucket** under the old cluster's
  prefix until the backup retention expires it. A restore to a moment
  before the move is not offered by the card: do it by hand with
  `wal-g` against the old prefix, on a copy, never on the live data.
- A move that rolled back: the source's archive continues; the
  destination's partial one expires.

## Rollback

Switching continuous recovery off from the card stops archiving and
drops the PostgreSQL settings on the next rebuild; nothing else changes
for the instance. The nightly backups are independent of it.

## See also

- [architecture.md — Continuous recovery](../architecture.md#continuous-recovery-incubacloud_pitr)
- [RB-19 — Get a large backup onto a host and restore it](RB-19-restore-large-backup.md)
- User guide: *Backups → Restore to any minute*
