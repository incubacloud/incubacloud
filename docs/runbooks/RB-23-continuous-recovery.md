# RB-23 — Continuous recovery: alerts, a stuck restore, a moved instance

**Severity:** critical · **When:** a `pitr_failing`, `pitr_archive_lagging`
or `pitr_base_missing` alert; a restore to a moment that never ended; an
instance that moved host or changed backend and is no longer archiving; a
`pitr_prefix_changed` alert, an archive a backend change left behind; a
`pitr_prefix_left` alert, an archive a delete left in the bucket; a
`pitr_purge_failed` alert, an archive switched off that could not be
emptied.

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

`pitr_prefix_left` hangs on no instance (§5):

```sql
db$ SELECT dedup_key, audience, message, create_date FROM cloud_alert
    WHERE code = 'pitr_prefix_left' AND state = 'active';
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
| `pitr_prefix_left` (warning, on no server) | *The continuous recovery archive of … was left in the bucket* / *… was left behind* | The instance was deleted while its host had no `.docker/pitr.env`: its archive is still in the bucket, at the prefix named or, with no destination, at one to find. See §5. |
| `pitr_prefix_left` (warning, on no server) | *… is not emptied with its archived copy* | An instance archived before 1.0.229 whose chain path does not say where its archive is: deleting it empties the chain only. See §5. |
| `pitr_prefix_changed` (warning) | *… now goes to … what it archived before stays at …* | A rebuild rendered the archive at another prefix or destination (a backend changed, the project's folder renamed, a bucket edited). The old prefix keeps what was archived there and nothing empties it. See §4. |
| `pitr_purge_failed` (warning) | *… switched off on … could not be emptied* | The purge of an archive switched off (`pitr_purge`) failed: no destination, credentials refused, a prefix that would reach other copies, or the job's log says why. The watch tries again in a day. See §6. |

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
- **A move: the old cluster's archive stays** under its own prefix
  (`<prefix>/<old system id>`) until it leaves the backup window; the
  daily base copy's housekeeping then purges it (`pitr_previous_clusters`).
  A restore to a moment before the move is not offered by the card: do it
  by hand with `wal-g` against the old prefix, on a copy, never on the
  live data.
- A move that rolled back: the source's archive continues; the
  destination's partial one expires.
- **A change of backend, or of the project's folder: the old prefix stays
  where it was, and nothing empties it.** The housekeeping reaches only the
  prefix the instance archives to now, with the destination it has now,
  and a delete or a purge empties that one. Every rebuild that writes
  `pitr.env` while the instance archives keeps the prefix and destination
  it rendered (`pitr_rendered_prefix`, `pitr_rendered_backend_id`); one
  that renders another raises `pitr_prefix_changed` on the instance, keyed
  by the old prefix, naming the destination it is on and the day from
  which no restore needs it (the change plus the backup window). From that
  day, empty the old prefix by hand as in §5, with that destination's
  credentials, then resolve the alert. A destination edited in place
  (bucket or path) shows as another prefix; its endpoint or keys changed
  in place do not. Switched off, a rebuild leaves the prefix kept as it
  is and raises nothing: nothing archives at the new one, and the purge
  of §6 empties the one it archived at; the alert comes with the rebuild
  that switches it on again. The alert hangs on the instance: deleting
  the instance takes it away, so note the old prefix before deleting one
  with the alert active, and empty it from that note.

## 5. An archive left in the bucket

A delete empties the instance's recovery prefix (`<folder>/<name>-pitr`):
a live instance's from its host, with `.docker/pitr.env`; an archived
one's in the job that deletes it for good (`purge_archived_backups`),
after its duplicity chain, at the prefix frozen when it was archived or,
for one archived before 1.0.229, the one its frozen chain path gives
(`boto3+s3://<bucket>/<path>/<name>` → `s3://<bucket>/<path>/<name>-pitr`).
`pitr_prefix_left` (warning) is raised when that cannot happen and the
instance had recorded a cluster:

- a live delete found no `pitr.env` on the host: the alert names the
  prefix (`s3://<bucket>/<path>`), or says it is unknown when the instance
  had no backup destination by then;
- an archived instance's chain path does not end in its name (imported
  by hand): nothing computed is purged, as it may be another instance's,
  and the alert gives the path to start looking from.

The alert hangs on no server and no instance, keyed by the prefix: both
are deleted with their alerts, and the instance is going. On a managed
destination it goes to the service provider, who holds its credentials.
Empty the prefix with the backup destination's credentials, from a
machine that has them (they are never printed):

```
$ export AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=…
$ rclone purge ":s3,provider=Other,env_auth=true,endpoint='<endpoint>',region='<region>':<bucket>/<path>"
```

`<region>` is the destination's Region; without one, `auto` behind an
endpoint (Cloudflare R2) and `us-east-1` without.

Check first that `<path>` ends in `<name>-pitr` and names the deleted
instance: a purge of the bucket, or of the folder, takes every instance on
the destination. Then resolve the alert. Switching continuous recovery
off empties the archive later, by itself (§6). Before deleting a switched
off instance whose folder or destination changed, empty by hand the prefix
in `pitr_rendered_prefix`: the delete reads the one the host's `pitr.env`
names now.

Core refuses an instance name ending in `-pitr` from 1.0.229 on, and
continuous recovery is not switched on for `x` while an instance
`x-pitr` (active or archived) is in the same folder: its chain would be
under `x`'s recovery prefix, and emptying either would empty both.

## 6. An archive switched off

Switching continuous recovery off (the card, or a plan without it) keeps
the archive for the backup window the instance has then, and empties it
after (1.0.229). `pitr_disable`, once it ends well, fixes the dates on
the instance: `pitr_off_at` (archiving stopped) and `pitr_purge_at`
(`pitr_off_at` plus the window). They are not recalculated: a plan that
lowers the retention switches it off too. Switching off again (a switch
on that left them, such as a move's) dates them anew. Switching on again
before `pitr_purge_at` clears them and cancels a purge still waiting; the
archive is kept. Once `pitr_purge_at` has come, a switch on waits for the
purge: the card refuses it with a message, and a plan that wants it on is
switched on by the watch once the archive is emptied, from a new archive.
The purge may have started, and it deletes the oldest base copies first:
one cut short leaves deltas without their base, and a first copy taken
over them would show a window that cannot be restored.

The window is the one the instance has once archiving stopped. A layer
that switches it off with a plan (one that overrides `_pitr_window_days`)
dates it with the window after the change, often its destination's: with
a shorter one before, the oldest data in the archive is that shorter
window plus the new one old when it goes.

Once `pitr_purge_at` has come, the watch queues `pitr_purge` (jobs drawer:
*Empty a Switched-Off Recovery Archive*). It does not use the host's
`pitr.env`: it uploads an environment of its own (0600, in the job's
directory, removed when it ends) with the destination's credentials, the
prefix last rendered and `PURGE_BEFORE` = `pitr_off_at`, and runs
`backup_purge_archived.sh`; then it removes `.docker/pitr.env` from the
instance's directory (not for one wanted on again, whose stack names it),
and the panel forgets the clusters, base copies and dates. Only what was archived before the switch off goes. A purge queued
before the date, or on a server the instance left since, empties nothing.

```sql
db$ SELECT name, pitr_state, pitr_cluster_id, pitr_off_at, pitr_purge_at,
           pitr_rendered_prefix
    FROM cloud_instance
    WHERE pitr_purge_at IS NOT NULL ORDER BY pitr_purge_at;
```

`pitr_purge_failed` (to the service provider on a managed destination)
says why it failed: no backup destination (give it
one, or empty the prefix by hand as in §5), the storage refused the
credentials (fix them on the destination), a prefix the panel refused
because it is the destination's path or holds another instance's copies
(nothing was deleted: correct the instance's backup path or the
destination, then **Retry**), or the job's log. The watch
tries again a day after the failure; **Retry** in the jobs drawer runs it
at once. A run again is harmless: it finds nothing left before the cutoff
and goes on. A stack configured outside the panel is not purged (its
runbook), nor an archived instance, whose archive goes with its record
(§5). An instance switched off before 1.0.229 got its dates from the
update.

## Rollback

Switching continuous recovery off from the card stops archiving and
drops the PostgreSQL settings on the next rebuild; nothing else changes
for the instance, and its archive stays in the bucket for the backup
window, then it is emptied (§6). Switching on again before then keeps it.
The nightly backups are independent of it.

## See also

- [architecture.md — Continuous recovery](../architecture.md#continuous-recovery-incubacloud_pitr)
- [RB-19 — Get a large backup onto a host and restore it](RB-19-restore-large-backup.md)
- User guide: *Backups → Restore to any minute*
