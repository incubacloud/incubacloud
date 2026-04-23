# RB-03: Clean up orphan terminal subprocesses

**Severity:** warning
**Typical trigger:** host memory alert, or `ps` shows
`python -m odoo.addons.incubacloud.terminal_subprocess` processes
that outlive their sessions.
**Who runs it:** ops.

Each in-browser terminal spawns one `terminal_subprocess.py` via
`start_new_session=True`. On a clean close, the parent tells the
child to exit and deletes its row from `cloud.terminal.route`. If
the parent is killed hard (OOM, `docker kill`, container restart
mid-session), the detached children survive and the routing row is
left pointing at a process that no longer answers on its port.

The GC cron (every 5 minutes) normally reaps these rows, but:

- A subprocess that is **alive but unresponsive** (e.g. its asyncssh
  session hangs) is not killed by the GC — only its stale routing row
  is pruned when `os.kill(pid, 0)` says the PID is gone.
- A subprocess that **survives container restart** keeps its port
  bound but no routing row exists, so nothing in Odoo can talk to it.

## Symptoms / triggers

- Host disk/memory alert on the Odoo container.
- `ps aux | grep terminal_subprocess` returns processes that have
  been alive for > 1h with no corresponding `cloud.terminal.route`.
- Users report "terminal closed" immediately after opening.
- Port exhaustion in the ephemeral range (rare but possible on
  heavily-used control planes).

## Diagnosis

List known routes and their pids:

```sql
db$ SELECT id, session_id, pid, port, create_date
    FROM cloud_terminal_route
    ORDER BY create_date DESC;
```

Inside the Odoo container, cross-check against running processes:

```bash
odoo$ ps -eo pid,etime,cmd | grep terminal_subprocess | grep -v grep
```

- PIDs in `ps` but not in the table → **orphans**, reap them.
- PIDs in the table but not in `ps` → stale routes, GC will handle
  them on its next tick, or trigger it manually (below).
- PIDs in both → healthy active terminal, leave alone.

## Resolution

1. **Trigger the GC cron immediately** to clear stale routes:

   ```python
   # From `odoo shell`:
   env.ref('incubacloud.cron_terminal_route_gc').method_direct_trigger()
   ```

2. **Kill orphan processes** (those with no route row). Collect the
   pids from the diagnosis step and kill them directly — they were
   started with `start_new_session=True`, so they do not respond to
   container signals:

   ```bash
   odoo$ kill -TERM <pid1> <pid2> ...
   ```

   Give them 5s to clean up, then:

   ```bash
   odoo$ kill -KILL <pid1> <pid2> ...
   ```

   Any subprocess that has been idle past `SESSION_TIMEOUT` should
   have self-terminated already; anything still alive is either
   actively used or stuck in its watchdog.

3. **Verify the cleanup**:

   ```bash
   odoo$ ps -eo pid,etime,cmd | grep terminal_subprocess | grep -v grep
   ```

   ```sql
   db$ SELECT COUNT(*) FROM cloud_terminal_route;
   ```

   Both should match (one process per row).

## Rollback

This runbook only kills processes and deletes table rows that have
no corresponding live process. There is no rollback — if you kill an
active terminal, the user just reopens it. No data at risk.

If in doubt about whether a PID is orphan, `strace -p <pid>` for a
few seconds: an active terminal will show `read` / `write` activity,
an orphan will be idle or in a tight loop.

## References

- [`controllers/terminal.py`](../../incubacloud/controllers/terminal.py)
  — proxy spawning the subprocess.
- [`terminal_subprocess.py`](../../incubacloud/terminal_subprocess.py)
  — the subprocess entry point, includes the watchdog.
- [`models/cloud_terminal_route.py`](../../incubacloud/models/cloud_terminal_route.py)
  — routing table + GC helpers.
- [`data/terminal_route_gc_cron.xml`](../../incubacloud/data/terminal_route_gc_cron.xml)
  — the 5-minute GC cron definition.
