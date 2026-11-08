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
.walg/restore/         lock, pid, armed, log, result of the restore under way or last run;
                       cancel, the job a cancel was asked for
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
| `pitr_failing` (warning) | *could not be switched on … keeps no log meanwhile* | The `pitr_enable` job failed or was stopped, and the host parked archiving (`pitr.sh settle`: `archive_command = '/bin/true'`). The watch retries in a day, not before; a failure holds no queue. |
| `pitr_failing` (warning) | *could not be switched on again … goes on as it was* | A switch on again (a new objective) failed before it rewrote the archive command: the archiving the instance had goes on, at the objective it had. The new one is not applied, and nothing applies it later: the watch switches on and off only. Retry the job (**Retry** in the jobs drawer); the card already shows the new objective, and choosing it again does nothing. |
| `pitr_failing` | *could not be switched on … uploads nothing* | The archive command is set but the upload test failed (bucket, credentials): PostgreSQL keeps every segment until it works. As *cannot upload its database log* below. |
| `pitr_failing` | *could not be switched on, and the database may be keeping its log* | The host could not be asked to park archiving (unreachable, no answer, a restore holding it). Check the disk and `SHOW archive_command` (§2); park it by hand in `db` (`ALTER SYSTEM SET archive_command = '/bin/true'`, then `SELECT pg_reload_conf()`), or switch off from the card. |
| `pitr_failing` | *is not archiving its database* | `archive_mode` is off or the cluster changed (a move, a new deploy). The watch switches it on again. |
| `pitr_failing` | *agent … is not running* | The `pitr` container is down: nothing uploads. |
| `pitr_failing` | *cannot upload its database log* | Uploads fail (`N failures, M segments waiting`): bucket, credentials or network. PostgreSQL keeps the segments and retries. |
| `pitr_failing` | *A restore … stopped without finishing* | The restore's process is gone and its lock is left. See §3. |
| `pitr_failing` | *The cancelled restore … ended in an unknown state* | A cancel could not put the previous data back. See §3a. |
| `pitr_failing` | *… was cancelled, but its host could not be asked to stop it* | The cancel never reached the host; the next job follows the restore. See §3a. |
| `pitr_failing` (warning) | *The cancelled restore … is still being undone* | The host had not finished undoing within the job's wait; a job follows it. Leave the instance alone until it ends. |
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

## 3a. A cancelled restore

Cancelling a restore stops it and puts back what it changed (6-nov-2026).
The job runs `pitr.sh restore-cancel`, which writes `.walg/restore/cancel`
and sends a TERM to the process in `pid`; the process puts the previous
data back, starts the stack and writes `result` with `cancelled=1`. Once
the restored database is promoted the restore is no longer undone: it
goes on to the end, and the job says it had already been applied.

- **Unknown state** (`state=failed-unknown`): the previous data could not
  be put back. Do not start or rebuild the instance. Read
  `.walg/restore/log`, then look in the database's data directory
  (`docker compose run --rm --no-deps -u root --entrypoint sh db -c 'ls -a "$PGDATA"'`):
  `.pre-pitr-<stamp>` holds the data from before the restore,
  `.pitr-failed-<stamp>` the restored copy moved aside, `.pitr-new` a copy
  fetched and never swapped in. With the database stopped, move the files
  of `.pre-pitr-<stamp>` back to the top of the data directory (what is
  there now aside first), then `docker compose up -d`.
- **The host could not be asked**: whether it got the TERM is unknown. A
  job follows the restore (*Wait for the restore*); if the host stays
  away, the alert stays. Once it is back, `cat .walg/restore/result` says
  how it ended, or run the cancel by hand (it is safe to run again):
  `bash .walg/restore/bin/pitr.sh restore-cancel "$PWD" <job> 300`.
- A cancel that came before the restore started on the host leaves only
  `.walg/restore/cancel`: harmless, and the start of that job refuses.

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
