# RB-29 — A tenant's placement and its moves between hosts

**Severity:** warning · **When:** a tenant's page shows *Moves paused*; a
`move_pending`, `move_postponed`, `move_slow_cut` or `move_stuck` alert; a
customer asks why their panel has not moved, or what the old copy of a moved
panel holds; disk to reclaim on a host a panel moved away from.

Paying a plan applies it where the panel runs, at once: the tenant never
leaves `active` for a plan change. When the plan's pool is not the pool of
the host the panel runs on, the tenant **owes a move**, and the placement
planner moves it when nobody minds. How the chain works is in
[architecture.md](../architecture.md) («Cross-host move»); the SaaS side
(planner, windows, postponements, pin) in the SaaS layer's architecture
notes («Placement on the plan's pool»).

## 1. The states

`cloud.tenant.placement_state`, written by the platform only, shown on the
tenant's page (group **Placement**) and in the **Placement** column of the
tenant list. The **Owe a move** chip of the list keeps every tenant that is
not *Settled*.

| State | Label | Meaning | What happens next |
|---|---|---|---|
| `settled` | Settled | On its plan's pool, pinned by an operator, not active, or closing its account. | Nothing. |
| `owed` | Owes a move | No host of the pool has room (`move_pending` alert), or the panel cannot move yet (the reason is in its history: usually an older tenant module, which the release rollout rebuilds). | The planner books it as soon as there is room. |
| `booked` | Move booked | A window is booked: `move_window_local_hour` (04:00) in the owner's time zone, `move_window_hours` long, at least `move_min_notice_hours` ahead; the customer sees it in the portal and in an announcement of the panel, and may choose another time or «Move now». | Moves earlier if the panel is idle for `move_idle_minutes` (up to three tries), otherwise inside the window. |
| `moving` | Moving | The chain runs (`cloud.instance.move_origin_host_id` is set). Delete is hidden and refused. | `settled` after the cutover; `booked` again if postponed; `paused` if it failed. |
| `paused` | Moves paused | A move failed, was rolled back by hand or by the watchdog (`move_retry_paused`). The panel keeps working on its current host. | Nothing until an operator resumes it (§3). |

The planner is the cron **SaaS: Place tenants on their plan's pool**
(`cloud.tenant._cron_placements`, every 5 minutes). At most
`move_max_concurrent` tenants' moves run at once across the platform. Its
settings are in Settings → SaaS → Placement.

## 2. The buttons of the tenant's page

| Button | Shown when | Does |
|---|---|---|
| **Move now** | Active, *Move booked* or *Owes a move* | `action_move_now`: refused while a job of ours is queued or running on the panel, with no host free in the pool, or within 30 minutes of the last «Move now» (each one deploys the destination whole before it may be postponed). Otherwise the chain starts with trigger `now`; its `move_quiet_check` still postpones it if a job runs on the panel right before the stop. |
| **Cancel booked move** | *Move booked* | Takes the window and the panel's announcement away. The tenant still owes the move: **the next run of the planner books it again** unless it is pinned or paused by then. To keep it where it is, pin it instead. |
| **Resume moves** | *Moves paused* | Clears `move_retry_paused` and works the state out again; the planner books it on its next run if the pool has room. |
| **Pinned to its host** (checkbox, saved with «Save changes») | Always | Pinned: *Settled* where it runs, any booking and its announcement go, the planner leaves it alone. Released: owes the move again and is booked at once if the pool has room. Both are written in the history. |

A «Move to host» from the **instance's** page (core) is immediate, with no
window and no quiet check of requests. If it leaves the tenant outside its
plan's pool, the cutover pins it («Pinned by a manual move to host …» in the
history).

## 3. A paused move

1. Read why in the tenant's **History**: «Move rolled back — Stays on host
   X; moves paused (failed). <step>: <message>», or «Move could not start».
   The step's own log is in the instance's jobs.
2. Fix the cause (RB-05 for a failed job; a host without room, a target not
   `compatible` or without Traefik; a backup destination missing).
3. Press **Resume moves**. The planner books it on its next run, and the
   customer sees the new window in the portal and the panel.

The customer meanwhile reads in the portal: «Your move could not be
completed and our team is looking into it. Your panel keeps working on its
current server.»

## 4. Postponements

`move_quiet_check` runs on the source right before the stop. It postpones
the move, with no alert and without touching the source, when:

- something runs on the panel (`busy`): a job of the master's on the
  instance besides the move's own, or, inside the panel's database, one
  of its own jobs started, or of its queue started or waiting, within the
  last two hours (the hidden types and the `bg` channel do not count);
- for a move started by inactivity (`idle`) or by its window (`window`),
  the panel had requests in the last minutes (`active`); a move started
  `idle` is also postponed when the requests cannot be read. «Move now»
  and an operator's move do not look at requests;
- what runs inside the panel could not be counted (`unknown`): the count
  failed for a moment, and the panel had not stopped, so the move waits
  rather than fail and be paused.

The tenant goes back to *Move booked* in the same window while it is ahead,
`move_postponed_count` goes up by one, the reason stays in
`move_postponed_reason`, and the history says «Move postponed: busy (N jobs
running)», «…: active (N requests in M min)» or «…: unknown (…)». The
destination the move had let into the panel's servers is taken out again. From the third
postponement in a row the planner stops trying by inactivity and waits for
the window. A window that closes after three or more postponements raises
`move_postponed` (warning) and the move is booked for the next one; the
alert resolves at the cutover. Look at what the panel was running: a job of the customer's that
runs every night through the window needs another time chosen with the
customer, or a «Move now» when it is idle.

## 5. How long the cut was

`move_last_cut_seconds` (tenant and instance) is the time the panel was
unavailable: from the start of the drain of the source's `odoo` to the
destination answering healthy. The history says «Moved to host X for plan Y
in N s», and the panel shows its customer an announcement for a day: «Your
panel is on its new server — The move took N seconds».

- Under 45 s is the target.
- Over `move_cut_alert_seconds` (90 s by default, Settings → Jobs) the
  cutover raises `move_slow_cut` (warning) on the instance; the next cut
  under the budget resolves it.
- A long cut is usually the restore: a large database or filestore, a slow
  link through the master's disk, or a destination host under load. The
  job log of the `restore_instance` step has the timings of each command.

## 6. The frozen copy on the old host

After the cutover the old copy is **frozen, not removed**
(`move_freeze_source`): every container of its stack stopped with its
restart policy set to `no`, its directory and volumes kept, no route in the
host's Traefik. The instance's page shows **Frozen copy until** and the
host (`move_frozen_source_host_id`, `move_frozen_until`), for
`move_frozen_source_hours` (48 by default, Settings → Jobs).

It is the operator's copy for a manual restore if the move went wrong in a
way nobody noticed in time. There is no automatic way back.

### Take a dump of it

On the old host, as its user, in the instance's directory (the same
`~/<project>-<version>-prod` it had there):

```bash
cd ~/<instance dir>
docker compose run --rm -v /tmp:/host-tmp odoo \
    click-odoo-backupdb --filestore <dbname> /host-tmp/frozen-<instance>.zip
docker compose stop db          # the run started it; leave the copy stopped
```

`<dbname>` is the instance's database (`cloud_instance.postgres_dbname`,
`prod` unless set). On Odoo 20 the tool is `odoo db dump <dbname>
/host-tmp/frozen-<instance>.zip`. The zip is Odoo's own backup (dump and
filestore) and restores with the instance's «Restore» from a file, or by
RB-19 for a large one. Never `docker compose up` the frozen copy: its crons
and its mail would run beside the instance that moved.

### Remove it now

The instance's page → **Clean frozen copy now** (`action_clean_frozen_copy`),
which queues `move_cleanup_source` on the old host; its log ends with «✓ Old
host copy removed after move.». Without the button, the hourly cron **Cloud:
clean frozen move sources** removes it once its time is up. It is also
removed, first in the queue, when the instance moves again (to that host or
another), when it is deleted, and when it is archived. A copy on a host that
is archived, out of service or no longer compatible is forgotten instead,
with an audit entry «Frozen copy forgotten» naming the host: reclaim that
disk by hand.

## 7. Pinned tenants

`placement_pinned` means an operator wants the panel where it runs, outside
its plan's pool: a test, a customer with a special arrangement, a host being
drained by hand. The planner never books a pinned tenant, and the portal
shows no «Server» card. Review the pinned ones from time to time:

```sql
db$ SELECT t.id, t.name, p.code AS plan, h.name AS host
    FROM cloud_tenant t
    JOIN cloud_plan p ON p.id = t.plan_id
    JOIN cloud_instance i ON i.id = t.instance_id
    JOIN cloud_host h ON h.id = i.host_id
    WHERE t.placement_pinned AND t.state = 'active';
```

Releasing one (uncheck, «Save changes») books its move at once when the
pool has room.

## Rollback

- A booking made by mistake: **Cancel booked move** and pin the tenant, or
  let the customer choose another time from the portal.
- A move that is running and must stop: the instance's **Roll back move**
  (before the cutover only). The tenant ends *Moves paused*.
- A move that finished and must be undone: move the instance back with
  «Move to host» (it will be pinned if that host is outside the plan's
  pool), or restore the dump of the frozen copy (§6) while it still exists.
- A pin set by mistake: uncheck it and «Save changes».
