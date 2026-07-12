# RB-05: Triage a failed `queue.job` / `cloud.alert`

**Severity:** critical
**Typical trigger:** `job_failed` alert in the Alerts panel, or a
user reporting that a deploy / rebuild / restore "didn't do
anything".
**Who runs it:** ops, with developer escalation for unfamiliar
errors.

A failed job is the only blocking failure surface in the system —
deploys, rebuilds, backups and tenant provisioning all run as
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
