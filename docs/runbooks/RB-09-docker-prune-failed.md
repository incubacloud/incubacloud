# RB-09: `docker_prune` failed for a host

**Severity:** warning (critical if disk < 10% free)
**Typical trigger:** `docker_prune` cron job fails with non-zero
exit, host disk usage climbs past the alert threshold.
**Who runs it:** ops.

`docker_prune` is a scheduled `cloud.job` per host that runs
`docker system prune -af` to reclaim space from unused images,
stopped containers, networks and build cache. It does **not** touch
volumes (no `--volumes`). When it fails, the host stops reclaiming
space and can eventually fill up — killing running containers.
Manually enqueuing it requires the Administrator role (server-side
manager gate on the job type).

## Symptoms / triggers

- Host disk alert (OS-level or `cloud.alert` disk-threshold rule).
- `job_failed` alert for a `docker_prune` job.
- Users reporting that deploys to a specific host fail with
  "no space left on device".

## Diagnosis

1. **Identify the failing host** from the alert or the failed job:

   ```sql
   db$ SELECT j.id, j.name, h.name AS host, j.create_date
       FROM cloud_job j
       JOIN cloud_host h ON h.id = j.host_id
       WHERE j.name = 'docker_prune' AND j.state = 'failed'
       ORDER BY j.create_date DESC LIMIT 10;
   ```

2. **Read the job's stderr** — prune failures usually have a clear
   message (disk already full, container in use, permission
   denied):

   ```sql
   db$ SELECT line FROM cloud_job_log_chunk
       WHERE job_id = <id> AND source = 'stderr'
       ORDER BY id DESC LIMIT 100;
   ```

3. **SSH onto the host** and verify disk state:

   ```bash
   $ ssh <host> 'df -h / && docker system df'
   ```

   The `docker system df` split tells you whether the space is in
   images, containers, volumes or build cache — different reclaim
   strategies apply.

## Resolution

### Common case: prune failed because a container was starting

Nothing to do beyond **retrying the job** from the queue.job UI —
the condition is racy and usually self-heals on the next tick.

### Host is disk-full and prune can't even start

1. **SSH to the host** and reclaim emergency space manually:

   ```bash
   $ ssh <host>
   host$ docker container prune -f
   host$ docker image prune -af
   host$ docker volume prune -f
   host$ docker builder prune -af
   ```

   Do these one at a time and watch `df -h /` — the first one that
   moves the needle is enough.

2. **Once disk is breathing** (≥ 15% free), retry the cron from
   Odoo so the control-plane record reflects a successful prune.

3. **Investigate the root cause** of the fill-up. Common culprits:
   - Dangling images from failed builds (fixed by the prune above).
   - A container with a runaway log file → rotate:
     `truncate -s 0 /var/lib/docker/containers/.../*-json.log`.
   - A dedicated data volume for a DB that grew past its allocation
     → separate capacity planning question.

### Prune fails with permission error

The SSH user on the host must be in the `docker` group. Verify:

```bash
$ ssh <host> 'groups $USER | grep docker'
```

If missing, fix on the host (outside scope of Odoo — don't try to
`usermod` from the cron, it needs root and a re-login).

## Rollback

There is no destructive action in this runbook — prune only removes
objects Docker itself considers unused. If a running workload
disappeared after a manual prune, the root cause is Docker labelling
/ user error, not the prune command itself.

## References

- [RB-05](RB-05-triage-failed-job.md) — general failed-job triage.
- `cron_docker_prune` ("Docker Prune — All Hosts", in
  `incubacloud/data/docker_prune_cron.xml`) — the cron definition;
  entrypoint `cloud_host.cron_docker_prune`.
- `models/docker_prune_executor.py` — the SSH-side command.
