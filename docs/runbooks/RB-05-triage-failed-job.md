# RB-05: Triage a failed `queue.job` / `cloud.alert`

**Severity:** critical
**Typical trigger:** `job_failed` alert in the Alerts panel, or a
user reporting that a deploy / rebuild / restore "didn't do
anything".
**Who runs it:** ops, with developer escalation for unfamiliar
errors.

A failed job is the only blocking failure surface in the system —
deploys, rebuilds, backups and provisioning all run as
`queue.job` items. When one of these fails, `cloud.alert` opens an
alert and the SPA shows a red badge in the header. The alert auto-
closes when the same job succeeds (e.g. on a retry).

## Symptoms / triggers

- Red "Alerts" badge in the SPA header with `job_failed` rows.
- A user message: "I clicked deploy and the instance didn't come up".
- Email from `queue_job` retry exhaustion.

## Diagnosis

1. **Open the alert** in the SPA. The subject is
   `<job name> on <target> failed[: <excerpt>]`. Click through to
   the job — the alert links to the originating `cloud.job`.

2. **Read the structured logs** for the job:

   ```sql
   db$ SELECT timestamp, source, line
       FROM cloud_job_log_chunk
       WHERE job_id = <cloud_job_id>
       ORDER BY id DESC
       LIMIT 200;
   ```

   `source = 'system'` is our progress log; `stdout` / `stderr` is
   raw remote command output.

3. **Cross-reference with the queue.job record**:

   ```sql
   db$ SELECT id, state, exc_name, exc_message, retry,
              date_created, date_done
       FROM queue_job
       WHERE channel = 'root'
         AND id = <queue_job_id>;
   ```

   Use `exc_name` as the first hint at the failure category:
   - `paramiko.ssh_exception.*` / `asyncssh.misc.*` → host unreachable
     or auth failed → check `cloud.host` and SSH credentials.
   - `subprocess.CalledProcessError` → remote command failed → look at
     the last `stderr` chunk.
   - `IntegrityError` / `psycopg2.errors.*` → schema problem → likely
     needs a developer.
   - `RetryableJobError` with a connection message → a transient SSH
     connection failure being auto-retried (up to 3 attempts, 30 s
     backoff). Only the final failed attempt raises the
     `host_unreachable` alert — a job seen in `pending` with
     `retry > 0` is probably still self-healing; wait before acting.

## Resolution

The right action depends on the job type. In every case, do **not**
manually flip the alert to "dismissed" — it will reopen on next
attempt. Resolve the underlying cause, then retry:

1. **Fix the root cause** based on the exception (often: bad SSH
   key, host out of disk, malformed `cloud.instance` field).
2. **Retry the job** from the queue.job UI:
   - Settings → Technical → Queue → click the job → "Requeue".
   - On success the `queue.job` state write triggers
     `_dismiss_job_failed_alerts()` and the alert closes itself.
3. **For repeatedly-failing jobs** (3+ retries), do not just re-run
   — escalate to a developer. The `exc_message` field is the
   handoff: paste it into the issue along with the `cloud_job` id.

If the job is **stuck in `started`** for hours (executor process
died mid-run, leaving no failure record), force it into a terminal
state so the alert reflects reality. Do it through the ORM — the
alert/state bookkeeping hangs off `queue.job.write()`, so a plain
SQL UPDATE would skip it:

```python
# odoo shell
job = env['queue.job'].browse(<id>)
job.write({'state': 'failed',
           'exc_message': 'Manually marked failed: executor died'})
env.cr.commit()
```

### `archived_purge_chain_continued`: an archived copy whose chain was continued

Deleting an archived instance for good (`purge_archived_backups`)
stopped with exit 23 and deleted nothing: an instance of the same name
that inherited its backup path made an incremental backup on the
archived full, so deleting that full would leave every later set of the
live instance without its base (`lib/purge_prefix.sh`, `IC_PURGE_DEPENDS`
in the job log names the first such object). The record stays. It only
happens to a pair that shared its path before instances that take an
archived one's name got a path of their own.

The purge has two bounds. It deletes what was stored before the
delete's cutoff (`purge_cutoff_at`, or the creation of the instance that
took the name when earlier: `PURGE_BEFORE`). When the archived instance
had itself taken the name of an earlier one that holds the same path,
it deletes only what was stored since its own creation (`PURGE_SINCE`),
and the earlier one's copy stays for that one's purge.

1. Wait for, or take, a full backup of the live instance: the weekly
   full opens a chain of its own, and so does a backup once duplicity
   finds the archived full too old.
2. From the live instance's directory on its host, check that
   `collection-status` shows that full, after the delete's cutoff
   (`purge_cutoff_at`, or the live instance's `create_date` when
   earlier):

   ```bash
   docker compose run --rm --no-deps --entrypoint sh backup \
       -c 'dup collection-status "$DST"'
   ```

3. Delete all of the live instance's incremental sets older than that
   full: the names with `duplicity-inc.<T1>.to.<T2>` or
   `duplicity-new-signatures.<T1>.to.<T2>` stored after the cutoff whose
   `<T2>` is before the full. Not only the first one, whose `<T1>` is
   before the cutoff and which the job log names: each later one builds
   on the one before it. The restore points they held are lost either
   way.
4. Delete the archived instance again. The purge then removes the
   archived chain and leaves the live one's.

When the instance that continued the chain is archived as well, no full
of its own can be taken: delete it first, once its copy is no longer
wanted (or revive it and follow the steps above). Its purge starts at
its own creation (`PURGE_SINCE`), so it takes its incrementals and
leaves the earlier instance's chain; the earlier one's delete then goes
through.

### `backup_path_shared`: two records keep their copies under one path

Raised once per path by 1.0.232's post-migrate, for records that
already shared a backup path before an instance that takes the name of
another got a path of its own; the log of that update lists each path's
records, live or archived. Their copies are told apart by dates alone:
a purge of one goes on only when one of the two is archived and the
other was created at least a minute after the archive, and any other
pair's purge is refused, naming the other instance. A successor that
continued the archived chain stops the archived one's delete with
`archived_purge_chain_continued` (above).

1. List the records that hold the path now:

   ```python
   # odoo shell
   from odoo.addons.incubacloud.models._backup_dst import dst_key
   key = dst_key('<the path in the alert>')
   [(i.id, i.name, i.active, i.create_date, i.archived_at)
    for i in env['cloud.instance'].with_context(active_test=False).search([])
    if any(dst_key(p) == key for p in i._backup_paths_held())]
   ```

2. A successor and its archived predecessor (the dates prove it):
   nothing is at risk. Delete the archived one once its copy is no longer
   wanted.
3. Any other pair (two live ones, or two that lived at the same time):
   give each live one but one a path of its own (`custom_backup_dst`),
   for example the shared path with `-<id>` after it, and rebuild it:
   until then its server keeps copying to the shared path. Its next
   backup is a full there; what it stored under the shared path stays there.

The alert resolves itself at the next daily check of archived copies
(`Verify archived instance copies`) once one record holds the path, or
none.

## Rollback

Marking a job `failed` is reversible (UPDATE again to `started`),
but the side effects of a partial run (half-deployed instance,
half-restored backup) are usually not. Before retrying, decide
whether to clean up the partial state — the runbook for the
specific job (deploy, rebuild, restore) lists what to undo.

## References

- [`models/cloud_job.py`](../../incubacloud/models/cloud_job.py)
  — `_severe_job_types`, `_create_job_failed_alert`, `_dismiss_job_failed_alerts`.
- [`models/queue_job_ext.py`](../../incubacloud/models/queue_job_ext.py)
  — wires job state transitions to alert lifecycle.
- [`models/cloud_alert.py`](../../incubacloud/models/cloud_alert.py)
  — alert model, broadcasts `cloud_overview` on every change.
- [RB-09](RB-09-docker-prune-failed.md) — specific case for prune jobs.
