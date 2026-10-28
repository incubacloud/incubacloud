# Architecture

IncubaCloud Core is an Odoo module that acts as a **control plane** for remote Odoo infrastructure. It does not run workloads — it orchestrates them on external servers via SSH.

---

## High-level overview

```
┌─────────────────────────────────────────────────────────┐
│                   Odoo (IncubaCloud)                    │
│                                                         │
│  ┌─────────────────┐    ┌──────────────────────────┐   │
│  │   OWL SPA       │    │   JSONRPC Controllers    │   │
│  │  /cloud         │◄──►│   /cloud/*               │   │
│  └─────────────────┘    └──────────┬───────────────┘   │
│                                    │                    │
│  ┌─────────────────────────────────▼──────────────────┐ │
│  │              Odoo ORM (models/)                    │ │
│  │  cloud.host · cloud.project · cloud.instance       │ │
│  │  cloud.job · cloud.backup.backend · cloud.alert    │ │
│  └──────────────────────┬─────────────────────────────┘ │
│                         │                               │
│  ┌──────────────────────▼─────────────────────────────┐ │
│  │         queue_job (OCA) worker process             │ │
│  │                                                    │ │
│  │  cloud.job.execute() → AbstractSSHExecutor.run()  │ │
│  └──────────────────────┬─────────────────────────────┘ │
│                         │ asyncssh                      │
└─────────────────────────┼───────────────────────────────┘
                          │ SSH
              ┌───────────▼──────────┐
              │     Remote VPS       │
              │  Git · Docker · OS   │
              └──────────────────────┘
```

---

## Module structure

```
incubacloud/
├── controllers/
│   ├── main.py               # SPA shell, log pages, restore upload, /cloud/health
│   ├── data_load.py          # Thin shell composing the _data_load mixins
│   ├── _data_load/
│   │   ├── _helpers.py       # Serializers, secret whitelist, job responses
│   │   ├── _routes_crud.py   # CRUD endpoints (projects, hosts, instances, alerts…)
│   │   ├── _routes_ops.py    # Operational endpoints (deploy, backups, move…)
│   │   ├── _routes_github.py # GitHub App + repository endpoints
│   │   └── _routes_backends.py # Backup backend endpoints
│   ├── connect.py            # Connect-as, audit log, notification preferences
│   ├── terminal.py           # Web terminal (PTY session endpoints)
│   ├── host_terminal.py      # Host shell (login shell on the machine, manager only)
│   ├── github_webhook.py     # Webhook receiver (HMAC-SHA256 validated)
│   ├── github_setup.py       # GitHub App manifest flow
│   ├── async_utils.py        # Event-loop helpers for controllers
│   └── _safe_error.py        # Error envelope with correlation ids
├── github/
│   ├── client.py             # GitHub API client (App token + PAT)
│   ├── credentials.py        # GitHubAppCredentials dataclass
│   ├── jwt_utils.py          # RS256 JWT generation (no PyJWT dependency)
│   ├── token_cache.py        # In-memory installation token cache
│   ├── http_utils.py         # Shared HTTP plumbing
│   └── webhook_utils.py      # HMAC-SHA256 signature validation
├── models/
│   ├── abstract_executor.py  # SSH executor base class (asyncssh)
│   ├── registry.py           # Executor registry (auto-populated)
│   ├── transport.py          # Transport abstraction (SSH today)
│   ├── cloud_host.py / cloud_project.py / cloud_instance.py
│   ├── cloud_job.py          # Job engine + notifications fan-out
│   ├── cloud_job_type.py / cloud_job_log_chunk.py / cloud_job_log_message.py
│   ├── cloud_alert.py        # Alerts (dedup, auto-resolve, GC)
│   ├── cloud_audit_log.py    # Audit trail
│   ├── cloud_backup_backend.py
│   ├── cloud_settings.py     # Singleton settings (incl. rate-limit caps)
│   ├── cloud_rate_limit.py   # DB-backed rate limiting
│   ├── cloud_security_mixin.py # RBAC gate helpers
│   ├── encrypted_char.py / password_utils.py  # Fernet field + key handling
│   ├── res_users_ext.py      # Per-user notification preferences
│   ├── queue_job_ext.py      # queue.job bridge (state sync, chain failure)
│   ├── *_executor.py         # One file per job type (23 executors)
│   └── …                     # repos, domains, tags, whitelist, tokens, sessions
├── terminal_session.py       # PTY session manager (module root)
├── terminal_subprocess.py    # Spawned PTY worker process
├── host_terminal_session.py / host_terminal_subprocess.py  # Host-shell session + worker
├── static/src/
│   ├── app/                  # OWL app entry point + router + shell
│   ├── components/           # One folder per OWL component
│   └── store/                # Project store (single fetch owner)
└── tests/
    └── test_*.py             # Unit and integration tests
```

---

## Data model

### Key models

| Model | Description |
|---|---|
| `cloud.host` | VPS server — IP, SSH credentials, Traefik config, live metrics, capacity |
| `cloud.project` | Groups instances; holds default repos, dependencies, backup backend, members |
| `cloud.instance` | One Odoo deployment — version, DB config, SMTP, domains, backup backend |
| `cloud.job` | Async SSH execution record; waits in its target's queue (`queued`) until it is handed to a `queue.job` |
| `cloud.job.type` | Job type definition (`code`, `apply_to`, icon, and how it behaves in the queue: `queue_collapse`, `queue_exclusive`, `failure_holds_queue`) |
| `cloud.job.log.chunk` | Individual log line (`stdout`/`stderr`/`system`) |
| `cloud.backup.backend` | S3-compatible backup target — bucket, path, encrypted credentials, quota alerts |
| `cloud.alert` | Infrastructure alert (active/dismissed, warning/critical, dedup by code) |
| `cloud.audit.log` | Who-did-what trail with configurable retention |
| `cloud.settings` | Singleton — global defaults, GitHub PAT, rate-limit caps |
| `cloud.rate.limit` | Tumbling per-minute counters backing all rate limits |
| `cloud.github.app` | Singleton — GitHub App credentials (app_id, private_key, webhook_secret) |
| `cloud.github.event` | Immutable webhook audit log (drives auto-rebuilds) |
| `cloud.terminal.route` | Terminal subprocess routing (encrypted auth token, owner) |
| `cloud.host.terminal.route` | Host-shell subprocess routing (same shape, its own table) |
| `cloud.host.session` | Immutable audit of host-shell sessions: who, when, client IP, user agent — never the content |
| `cloud.connect.token` | One-time session token for connect-as |

Supporting models: `cloud.project.repo`, `cloud.instance.repo`, `cloud.instance.domain`, `cloud.instance.backup`, `cloud.instance.session`, `cloud.tag`, `cloud.host.tag`, `cloud.instance.tag`, `cloud.host.whitelist`, `cloud.instance.whitelist`, `cloud.github.credential.service`, `cloud.security.mixin` (abstract). `res.users` is extended with notification preferences; `queue.job` with the state bridge.

### Entity relationships

```
cloud.host
  ├── cloud.instance (via host_id)
  ├── cloud.host.whitelist
  ├── cloud.alert
  └── cloud.job (via host_id)

cloud.project
  ├── cloud.instance (via project_id)
  ├── cloud.project.repo
  ├── cloud.tag (M2M)
  └── cloud.backup.backend (M2O)

cloud.instance
  ├── cloud.job (via instance_id)
  ├── cloud.instance.repo / .domain / .backup
  └── cloud.backup.backend (M2O, overrides project)

cloud.job
  ├── cloud.job.type
  ├── cloud.job.log.chunk
  └── cloud.job.log.message
```

Backup backend resolution is **instance → project → global default**.

---

## Job execution pattern

All remote operations are modelled as `cloud.job` records and executed asynchronously by an OCA `queue_job` worker.

### Lifecycle

```
1. API call (e.g. deploy_instance)
   → cloud.job.enqueue(host_id, instance_id, 'deploy_instance', payload)
   → Server-side role gate per job type (manager-only types enforced here)
   → The type's queue rules: collapse, exclusive, the cap of five
   → Creates the cloud.job: a free target hands it to queue_job at once
     (a queue.job is created), a busy one keeps it `queued` until the
     dispatcher starts it (see "The job queue" below)

2. queue_job worker picks up queue.job
   → Calls cloud.job.execute()
   → Looks up the executor class in the registry
   → Asks executor.blocking_alert(): an alert returned ends the job at
     once, blocked on it (blocked_alert_id; resolving the alert
     dispatches it again) — the deploy's addon pre-flight. A step of a
     chain cannot wait: it fails instead, and the chain is cancelled
   → A failure before run() (a refused check) calls on_abort()
   → Calls executor.run()

3. AbstractSSHExecutor.run()
   → Opens asyncio event loop, connects via asyncssh
   → Runs commands from get_commands()
   → Streams stdout/stderr into a buffer, periodically flushed to
     cloud.job.log.chunk (fresh cursor)
   → Notifies the frontend via the PostgreSQL bus (cloud_jobs channel)
   → Calls on_success() or on_failure() in a fresh DB cursor; a run
     that ends with neither (a refused check, no connection, a hook
     that raised) calls on_abort() there instead, unless the job will
     be retried or was cancelled

4. Frontend (OWL)
   → Listens on bus channels cloud_jobs / cloud_overview
   → On job events: fetches /cloud/get_job_brief and toasts the outcome
   → Log terminal page: WebSocket stream with HTTP polling fallback
```

### Reliability guarantees

- **Serialization** — an instance runs one visible job at a time, and a host one host-level job at a time; what is asked for meanwhile waits in the target's queue. See [The job queue](#the-job-queue). Monitoring job types (`host_metrics`, `docker_prune`, `instance_health`, `read_last_login`, `capture_secret_key`; `pitr_status` and `pitr_base_backup` with `incubacloud_pitr`) are *hidden*: they never wait, never occupy a target, and stay out of the UI.
- **Transient connection retry** — executors that opt in (the monitoring probes) retry SSH connection failures up to 10 attempts (30 s backoff via `RetryableJobError`, ~10 min in all — sized to outlast a host's own maintenance reboot). Whether an attempt was the last is queue_job's decision, not the executor's: the critical `host_unreachable` alert is raised from the job's **terminal** `failed` state, never mid-flight, because the runner can hand a job one more attempt than its budget.
- **Chains** — `enqueue_chain(steps)` creates every step at once as one entry of the queue of each target it touches, and hands them to queue_job as an OCA DelayableChain when their turn comes. Payloads may reference earlier steps with `__chain_job_N__` placeholders (0-indexed), resolved to real job ids at dispatch. When a step fails or is cancelled, the steps after it in its `queue.job` graph that still wait in `wait_dependencies` end `cancelled` at both the `cloud.job` and `queue.job` level, with a note in their log ("Not run: step #N ended failed."): only the step that failed is a failure, and a step that never ran offers no retry. Only the chain's own graph is ended — a chain can cross instances (a staging built from production takes its dump on production), and a probe failing beside it ends nothing of it.
- **Cancel** — `cancel_job` holds a job, in whatever state, to the role gate `enqueue` holds its type to, and so every job the cancellation ends with it: the rest of a chain waiting in the queue, or the steps waiting on a dispatched or running one. One of them the caller may not ask for refuses the whole cancellation, before anything ends: stopping the step that heads a manager's move would end the move. The caller must also be able to write the job. A job waiting in its target's queue (`queued`) is cancelled on the spot: nothing runs and nothing reaches the host. A dispatched job that has not started is cancelled through queue_job, with `sudo()` (no role of the panel reads `queue.job`); a started job gets a cooperative cancel flag that the executor polls between commands on a separate cursor. The job then ends `cancelled`, also when storing its end collides with the flag's own write and queue_job would otherwise put it back in the queue (`queue_job_ext.write`).
- **Cross-host move** — `cloud.instance.move_to_host()` chains deploy(target) → stop `odoo` only (source) → live dump of the quiet database with its filestore (`backup_download`, `time='live'`, staged on the source) → restore(target, fetched through the panel's disk) → cutover → cleanup(source). It needs no backup container and no bucket. `host_id` flips only on successful cutover; a watchdog cron recovers instances stranded by a chain that died before cutover (restarts the source, raises a `move_stuck` alert), and `rollback_move` does the same on demand. The record stays `deployed` throughout: the deploy on the target does not touch its state during a move. Hooks for an inheriting layer: `_move_deploy_job_type`, `_move_pre_steps`, `_move_post_restore_steps` (between the restore and the cutover), `_on_move_cutover` and `_on_move_rolled_back` (fired once a rollback has cleared the markers).
- **Blocked deploys** — a deploy whose repositories define an addon twice waits on one `addon_conflict` alert instead of failing, and leaves the instance in `draft`; resolving the alert, or a repository save that leaves no conflict (`_check_addon_conflicts`, also run after an import; a repository GitHub would not list closes nothing), puts it back in the instance's queue, in the place its creation gives it, on the host the instance is on by then, and cancels the other jobs of its type waiting on the instance; a deploy or rebuild that goes through cancels those that waited on the alert it closes. Only a job nothing runs after waits: a step of a chain fails. A job whose failure its own critical conflict alert explains raises no `job_failed`, and queue_job's own failure message is left out for platform jobs.

### The job queue

An instance runs one visible job at a time, and a host one host-level job at a time (a job with no instance). The two queues are independent: a deploy does not wait for a host probe, nor the other way round. Asking for a job while another runs on the same target is never refused for that reason: `enqueue` and `enqueue_chain` always create the record, and the job waits.

- **`queued`.** `cloud.job.state` is the job's own stored field, not a mirror of `queue_job_id.state`: a job waiting in its target's queue is `queued` and has no `queue.job` yet. Once dispatched it follows its `queue.job`, which `queue_job_ext` writes back on every transition the runner makes through the ORM (`pending`, `started`, `wait_dependencies`, `done`, `failed`, `cancelled`); `enqueued` is written by SQL and never arrives, so a dispatched job shows `pending` until it starts. `_active_states` includes `queued`; `_running_states` (dispatched and not ended) does not. A job waiting for an alert (`blocked_alert_id`) shows as `blocked` either way, but is stored in one of two ways: blocked when it was asked for (`enqueue` found a dependency or addon conflict) it has no state and no `queue.job`; blocked by its executor (`blocking_alert`, `cloud.job._block_on`) it ran and ended `done`, with `blocked_alert_id` set. Resolving the alert puts it back in the queue (`unblock_and_enqueue`). The 1.0.198 migration ended `cancelled`, with a note, the jobs left with an empty state when their `queue.job` was deleted.
- **Order.** First come, first served, by creation (`_queue_entries`), with no priority between types. What the running job queues for itself — `bypass_running_check`, now meaning "I am part of the job running": an executor's `on_success` chaining a follow-up, a move's rollback jobs, the PITR chain a rebuild queues — goes to the head (`queue_head`), before everything waiting, heads among themselves by creation. Only the platform may ask for it: `enqueue` and `enqueue_chain` are reachable through `call_kw`, so a caller without superuser rights who passes it, or `move_rollback` in a payload, is refused (`AccessError`, `_check_internal_queue_flags`); the platform's own callers ask for it with `sudo()` (a cron runs as its bot, without superuser rights, and an executor's job runs with them only when it was asked for with them). Such a caller is refused, too, a target they cannot read — an instance of a project they are not in — before its queue is looked at (`AccessError`, `_check_target_access`): collapsing into a waiting job, merging a push into a waiting rebuild and the queue's refusals all read the queue with `sudo()`, and would otherwise answer with another project's job, write into it, or tell what waits there. A step of a chain on an instance must name, too, the instance's host or, while it moves, the move's origin or destination (`AccessError`, `_check_step_host`): the dispatcher puts a lone job where its instance is, but a chain's steps keep the hosts they name, and `cloud.host` has no record rule. Nor may such a caller create a job, or write what the dispatcher and the executor read — the state, the head, the chain, the retry, the blocking alert, the queue.job, the type, the payload and its secret, the instance and the host, the rights it runs with and when it ended (`_QUEUE_CONTROL_FIELDS`) — with `create` or `write` (`AccessError`, `_check_queue_control_vals`): a job is queued, retried, unblocked and cancelled only through `enqueue`, `enqueue_chain`, `retry_job`, `unblock_and_enqueue` and `cancel_job`, with their checks. They write those fields with `sudo()` once the caller's own access to the job is checked, record rules included (`_create_queued` for the new ones), so the caller stays the job's creator (`create_uid`, as whom `_dispatch_single` runs it); and only a job waiting for an alert can be unblocked. `execute`, which queue_job calls by name, is private (`@api.private`): through `call_kw` anyone who reads a job would run it again outside its queue. So are `unblock_and_enqueue` (it would queue a job past the alert it waits for, still open), the backup cleanup cron's method and `get_audit_log` (served by its own route). The retry of a failed job goes to the head too.
- **Chains are one entry.** The steps of `enqueue_chain` share a `queue_chain_key`, wait whole and count once towards the cap and towards "N ahead". A chain that touches several targets (a clone to staging: deploy on the staging, download on production, restore on the staging) waits in the queue of each, is dispatched only when every one of them is free and it is first in each, and occupies all of them until its last step ends. What is queued meanwhile waits for that last step. Cancelling a waiting chain cancels every step.
- **What the type says** (`cloud.job.type`, set in the data files of the module that declares it). `queue_collapse`: asked for again while an identical one waits — same type, target, payload and secret (`_queue_identity`; the SaaS manager keeps credentials in `secret_payload`, so two pushes differing only there stay two jobs) — the waiting job is returned instead of a second one; a job already running is never collapsed into. `queue_exclusive`: one at most may wait on a target, whatever its payload ("A Restore Database is already queued for this instance; cancel it first."). `failure_holds_queue`: a failure holds the target's queue (below). The types are judged as the layer finally routes them (the SaaS manager turns a rebuild into its tenant rebuild before the rules apply). A rebuild asked for by a GitHub push merges into the rebuild of a push already waiting instead (`RebuildInstanceExecutor._merge_queued_payload`, see [Webhooks and auto-rebuild](#webhooks-and-auto-rebuild)).
- **Five at most.** A queue holds `QUEUE_MAX_WAITING` = 5 entries; the sixth is refused ("The instance has 5 jobs waiting in its queue."). Heads, retries and merged pushes are not counted and not refused.
- **A failure holds the queue.** While a failed job of a type with `failure_holds_queue` has its alert active — the `job_failed`, or the critical conflict alert (`pip_conflict`, `addon_conflict`) that `_create_job_failed_alert` raises instead — nothing behind it is dispatched (`_queue_held_by`), and the lists show the waiting jobs as "Action required", with the way to the alert (`queue_held`, `queue_held_alert_id` in `_format`). The queue is released when that alert closes: a retry that ends `done` resolves it, or someone dismisses it (`cloud.alert.write` → `_on_holding_alert_closed`). The retry passes the hold and goes to the head; one retry per failed job may wait or run at a time. A delete of the instance (below) and the jobs of a move's rollback — marked `move_rollback`, while the instance's rollback is in progress: the failure is usually the step it rolls back — pass the hold too (`_passes_hold`), and the lists do not show them as held. None of them closes the alert. Cancelling the failed job releases nothing — a failed job cannot be cancelled. A failure of a type without the flag holds nothing: the next job starts. `move_cleanup_source`, the last step of a move, has no flag: it runs after the cutover, when the instance already lives on the destination, and a failure to remove the old copy from the source must not stop the instance's backups and jobs; its alert and its retry stay.
- **A delete empties the queue.** Enqueuing `delete_instance` (and the types a layer adds through `_get_queue_clearing_job_types`, the SaaS manager's tenant delete) first cancels everything waiting on the instance ("Cancelled: the instance was asked to be deleted.") and then waits only for the job running; it passes a held queue too. Whoever asks for it must be able to stop all of that, each job with the rest of its chain, as `cancel_job` would hold them to it (`_check_may_end_waiting`): a consultant may not end that way a developer's restore or a manager's move waiting on a staging, and the delete is refused whole, before anything ends, naming the job. Archiving an instance, whose waiting entries the dispatcher then cancels, and removing its record, which cancels them at once, are held to the same; the delete's own end archives or removes the record as the platform, since what waits behind it was asked for on an instance being removed. An archiving or a removal judges what this transaction's snapshot sees, without the queue's lock that a delete holds: a job asked for at the same moment is not judged, and ends as one asked for on an instance being removed would. `/cloud/delete_instance` no longer refuses while jobs are active.
- **Cancelled before it started.** A job or chain cancelled while it waits — by a person, by a delete, by its instance being gone — is cancelled at once, with a note, and its executor's `on_cancelled_queued` puts back what was written when it was asked for (a deploy's instance back to `draft`, a move's markers cleared), in the canceller's transaction. Then `_after_job_terminal("cancelled")` runs for the layers. A job that left its queue but will never run — a step of a chain that a step before it ended, or a dispatched job cancelled before it started — gets its executor's `on_never_run` instead (`queue.job._end_chain_after`), which keeps what was written when it was asked for and only lets go of what the steps that ran left for it on a host: a `from_host` restore queues the hidden `discard_handoff` on the download's host. Neither hook reaches a host itself: they run in the transaction that records the end, sometimes with the queues' locks held.
- **Checks at dispatch** (`_entry_verdict`). An entry whose instance no longer exists, or is archived when its type is not meant for archived instances (`_runs_on_archived_instance` on the executor, e.g. the purge of an archived instance's backups), is cancelled ("Cancelled: the instance is no longer there.") and the next one is looked at. A lone job goes where its instance is now: a move between queueing and dispatch is followed (a chain's steps keep the hosts they name, which a caller without superuser rights chooses only among the instance's host and its move's: `_check_step_host`). Not a job whose executor runs on the host it was asked for (`_pinned_to_host`: a move's two teardowns and its cutover, an upload grant's jobs), which keeps that host, in its retries too: sent where the instance lived, a rolled-back move's cleanup removed the copy on the source (28 October 2026). The move's teardowns also refuse, before they connect, a host the instance lives on or is moving to. The other queues of a chain are only tried for (`pg_try_advisory_xact_lock`), never waited on, so two dispatchers cannot deadlock on each other.
- **Locks.** Whoever enqueues, dispatches or cancels on a queue takes its PostgreSQL advisory lock first (`_lock_scopes`, one namespace for instances and one for hosts, in a fixed order), so two of them cannot both find the target free.
- **The dispatcher runs after the commit, on a cursor of its own.** The end of a job, a cancellation, a holding alert closed, an unblock and a retry register a dispatch of every queue concerned on `cr.postcommit` (`_schedule_dispatch`, once per queue and transaction); it runs on a fresh READ COMMITTED cursor, as the platform (`_dispatch_scope_fresh`). Odoo's cursors are REPEATABLE READ: the runner that ends a job reads the snapshot it took when the job started, and would not see what was queued during the run. Whatever goes wrong there is logged and never changes how the job that ended is stored; a layer's `_after_job_terminal` runs in a savepoint for the same reason. The jobs it starts run as their creator (`_dispatch_single`), possibly long after they were asked for, with superuser rights only when they were asked for with them (`asked_as_superuser`, set from the caller's environment by `enqueue` and its siblings, by `unblock_and_enqueue` from the one who sets a blocked job going), as until 1.0.197, when `enqueue` handed queue_job the caller's own environment: a consultant's job runs under their record rules — a restore reads the job it takes its data from as its creator may — and what the platform's code asked for with `sudo()` (an executor's follow-ups, the website's flows, a cron that elevates) runs as the superuser. A job waiting for an alert runs as its creator too, with the rights (superuser or not) of whoever sets it going; until 1.0.197 it ran as them. The dispatcher's own writes, the uuid and the state, are the platform's. `enqueue` dispatches in the caller's own transaction only its own new job, and only when the target is free and nobody waits both in its snapshot and in a fresh READ COMMITTED read — so two requests at the same moment do not start two jobs. The exclusive rule, the cap and the one retry per failure count what this transaction's snapshot sees plus what only the fresh read sees (`_fresh_only`: jobs whose ids the snapshot does not know at all), so a delete is not refused by the jobs it has just cancelled, which the fresh read still sees waiting; collapse and merge read the snapshot alone (in the rare race of two identical requests committing at once, the second queues a duplicate).
- **The sweep.** `cron_job_queue_dispatch` (`_cron_dispatch_queued`, every 2 minutes) serves every queue that waits with nothing running and nothing holding it, one fresh cursor per queue: a dispatch lost to a rollback or to a path nobody hooked starts within two minutes.
- **Who reads a busy target.** Everything that asks whether an instance is busy counts a waiting job as busy: the host endpoint guard, the PITR checks, the docker prune's per-instance check, the hardening preflight (`has_active_job`). The move watchdog counts a waiting job only when it is a step of the move or of its rollback: one asked for after the failure waits behind its hold, and would keep the source stopped for as long. The health probe does not — a job that has not started silences nothing — and `/cloud/get_backup_result` treats a queued listing as waiting, not as an error.

### Implementing a new executor

Executors self-register: subclassing `AbstractSSHExecutor` with a `_job_type` adds the class to the registry (`models/registry.py`) automatically. The code must match a `cloud.job.type` record.

```python
from odoo.addons.incubacloud.models.abstract_executor import AbstractSSHExecutor

class MyExecutor(AbstractSSHExecutor):
    _job_type = 'my_job_code'  # must match cloud.job.type.code

    def get_commands(self):
        return [
            ('step_1', 'echo hello'),
            ('step_2', 'uptime'),
        ]

    def parse_results(self, results):
        if results['step_1']['exit_status'] != 0:
            return ['step_1 failed']
        return []

    async def on_success(self, results):
        # runs in a fresh DB cursor after all commands succeed
        instance = self.job.instance_id  # cloud.instance record
        # ``deployed`` is DERIVED from the lifecycle state and cannot be
        # written: cloud.instance.write() rejects both 'deployed' and
        # 'state'. Move the instance with _transition(), which validates
        # the move against the transition map, and write only the plain
        # fields alongside it.
        instance.write({'running': True})
        instance._transition('deployed')

# Commands can include a third element for options:
def get_commands(self):
    return [
        ('build', 'docker compose build odoo', {"stop_on_failure": True}),
        ('test', 'docker compose run --rm odoo odoo --stop-after-init'),
        ('restart', 'docker compose up -d'),
    ]
# If 'build' fails, 'test' and 'restart' are skipped.
```

### Log chunk sources

| Source | Where it appears |
|---|---|
| `system` | Job card in the UI (last message, real-time) |
| `stdout` | Log terminal page `/cloud/log/<id>` |
| `stderr` | Log terminal page `/cloud/log/<id>` |

`stderr` classification is pattern-based (error regexes), not raw-channel-based, so noisy tools that print progress to stderr don't turn the whole log red.

### Transaction model

**The job's transaction is not written.** An executor runs inside the transaction `queue_job` opened for its job (which holds the lock on the `queue.job` row), and nothing written there through `self.env` can be relied on:

- `cloud.job.execute()` ends with `env.clear()`, which in Odoo 19 drops every write not flushed yet. Core 1.0.135 started VictoriaMetrics with a deletion key the database never received that way.
- `queue_job` rolls the whole transaction back when the job fails or is retried, and refuses a commit midway (it would release the job's lock).
- A write that does get flushed holds its row until the job ends; a hook writing the same row on its own cursor then waits for a transaction that is waiting for the hook.
- The transaction is REPEATABLE READ and its snapshot dates from the job's start, so a value committed on another cursor stays invisible to it — invalidating the cache and reading again returns the old value (measured).

Two primitives on `AbstractExecutor` cover every durable write:

| Primitive | Use it for |
|---|---|
| `with self._durable_env() as env:` | Writes that must last. A fresh READ COMMITTED cursor, committed on exit and rolled back if the block raises; the lock wait is capped at 30 s, so a mixed-transaction deadlock becomes an error instead of a stuck worker. Same user, superuser flag and context as the job. |
| `self._persist(record, vals)` | A durable write the rest of the job must also *see*. Writes through `_durable_env()`, then copies the committed values — and the stored computed fields of the same record that depend on them — into the job's cache. |

`on_success` / `on_failure` / `on_abort` already run inside `_durable_env()` (see `_dispatch_outcome`, `_dispatch_abort` and `_as_durable`) and write with `self.env`. `pre_run_checks`, `before_execute`, `get_commands`, `get_extra_vars`, `after_commands` and `parse_results` do not. The one write that waits for the job's own commit is `_unlink_after_job_commit`.

Two exceptions write the job's own transaction, on purpose. `blocking_alert` and `on_blocked` run there when a job ends blocked: it ends without an error, so that transaction commits, and `cloud.job._block_on` flushes it before `execute` clears the environment. For a step of a chain, which cannot wait and fails instead, `execute` runs `blocking_alert` inside `_as_durable()`, so the alert outlives the rollback. `on_unblocked` runs in the transaction of whoever resolved the alert.

```python
# Durable; invisible to the rest of this job (alerts, logs, state for a retry):
with self._durable_env() as env:
    env['cloud.alert'].raise_alert(...)

# Durable AND read again later in this job (the upload step reads it):
self._persist(inst.sudo().with_context(pip_provenance_managed=True), vals)

# WRONG — dropped at the end of the job, or taken back if it fails:
self.env['cloud.instance'].browse(inst_id).write({...})
```

A model helper meant to be called from an executor writes through the environment of the record it is called on, so the executor can bind it to a durable one (`env['cloud.settings'].sudo()._get_system()._ensure_operator_credential()` inside `_durable_env()`); it never opens a cursor of its own.

**Enforced by tests.** `tests/executor_write_rules.py` checks every executor class of core, the SaaS manager and the tenant module by AST: no cursor opened outside `abstract_executor.py`, and no `write` / `create` / `unlink` / `_transition` / `enqueue` / `raise_alert` / … outside a durable block or a hook (a short, verified list of hook-only helpers aside). It cannot see a write made inside a model helper; `tests/test_executor_durable_writes.py` covers those by ending the job the way the runner does (`env.clear()`) and reading the row by SQL — the check the 1.0.135 test was missing. In a `TransactionCase` the durable cursor rides on the test's own transaction (`registry_enter_test_mode()`), so the REPEATABLE READ snapshot cannot be reproduced there; it was measured with two real cursors.

Bus notifications go out from durable cursors too (`_publish_bus`), and only **during** execution. PostgreSQL `NOTIFY` is committed automatically with the transaction.

## Safe rebuild

Rebuilds verify the new Docker image before applying it to the running instance. The flow:

1. `docker compose build --pull --no-cache odoo` — builds the new image (`stop_on_failure`)
2. Clone the production DB via `createdb -T` (instant copy using PG hard-links)
3. Run `click-odoo-update --database __ic_boot_test` against the clone
4. Drop the test DB (`dropdb --if-exists __ic_boot_test`) — always runs, even on failure
5. If the test passed, update the real DB and restart with `docker compose up -d`
6. If the test failed, `stop_on_failure` aborts — the instance keeps the old image

The clone requires terminating active connections first (`pg_terminate_backend`). Odoo reconnects automatically. The boot test runs against the instance's own Postgres image (pgvector-enabled), so extension-dependent modules pass.

## Instance logs: the compose override and the daily archive

Besides the copier-rendered `prod.yaml`/`test.yaml`, every deploy and rebuild writes a `docker-compose.override.yml` next to them (`DeployInstanceExecutor._resource_override_content`, extended by the SaaS executors through `super()`). Compose loads it automatically because every `docker compose` call the executors run uses default file discovery — never `-f prod.yaml`, which would skip it. Per service it carries the resource limits configured on the instance, the `incubacloud.protect=1` label that keeps the daily `docker_prune` away from panel-managed stacks, and a `logging:` block (`json-file`, `max-size`/`max-file` from `cloud.settings`, `10m` × 3 by default).

That cap exists because a container's log otherwise grows until the disk is full: doodba's Odoo logs to stdout, the template sets no logging driver, and only a host that ran `host_hardening` has a `daemon.json` saying otherwise — a Base-mode host set up by `full_setup` alone has none, and daemon defaults only apply to containers created afterwards.

### Odoo's own log is a file on the host

A capped container log is still the wrong place to keep history: it lives inside the container, and a rebuild recreates the container by design, so yesterday's log goes with it. The `odoo` service therefore gets two more keys in the override:

- `command`: the environment's own command plus `--logfile=/var/log/odoo/odoo.log`;
- `volumes`: `./logs:/var/log/odoo`, a bind mount of the instance's own directory.

The flag rides on the command rather than on `odoo.conf` deliberately. The panel runs one-shot containers whose output has to keep reaching the job log — `docker compose run --rm odoo odoo -i <module>` and `click-odoo-update` for the boot test — and `run` replaces the command, so they are unaffected by construction; a `logfile` in the conf would have redirected them into the instance's log file, including tooling with no flag to opt out. The cost is that the override replaces the command outright, so whatever each environment's compose file put there has to be repeated: `prod.yaml` sets none (the image's `CMD`, `/usr/local/bin/odoo`), `test.yaml` pins `--workers=3 --max-cron-threads=1`. Both are asserted by tests and are worth re-checking when the copier template pin moves (RB-15).

`logs/` is created and chowned to uid 1000 before the stack starts (`scripts/instance_logs.sh install`, run by deploy, rebuild and warm claim): Docker creates a missing bind-mount source itself, as root, and Odoo would then be unable to write and fall back to stdout.

### Rotation and retention

The same script writes `/etc/logrotate.d/incubacloud-<compose project>` on the host: `daily`, `dateext`, `compress`, `delaycompress`, `nocreate`, `rotate <cloud.settings.odoo_log_archive_days>` (60 by default). The host's own daily logrotate run does the work — no panel job is involved, so the archive keeps advancing even while the panel is down. `nocreate` matters: a file logrotate created would belong to root and the container could not write to it; Odoo recreates it on its next line through `WatchedFileHandler`, which also means rotation needs no signal, no restart and loses no lines. `delete_instance` drops the config, and the logs go with the instance directory.

The result on the host mirrors what a comparable hosting platform shows: `odoo.log` (today), `odoo.log.<yesterday>` (plain), `odoo.log.<older>.gz`.

### Reading it back

- Live view: `tail -n N logs/odoo.log`, falling back to `docker compose logs` for an instance not rebuilt since this shipped.
- Archive: `/cloud/instance_log_archives` lists what the host keeps, `/cloud/fetch_log_archive` reads one (with an optional fixed-string filter applied on the host), `/cloud/search_log_archives` answers *which* days mention a term, and `/cloud/instance/<id>/log_archive/<name>` downloads a day gzipped. All gated by `can_view_logs`; the file name is validated against the shapes logrotate produces before it reaches a shell.
- The viewer's day picker filters as you type (sixty days do not fit a dropdown) and steps day by day; pressing Enter in the filter box sweeps every archived day on the host and marks the ones that match with their hit counts. The sweep is bounded by a timeout and emits a completion marker, so a run cut short reports itself instead of reading as "no matches".
- Cost and accountability: the read endpoints share one per-user cap (`rate_limit_logs_per_min`, 60/min — the live tail polls every 4 s, so a lower cap breaks a single open viewer), counted in a bucket per endpoint family (`logs_tail_`, `logs_list_`, `logs_day_`, see RB-04 for why one shared bucket was a serialization failure on every viewer open), and the sweep and the download share a tighter cap (`rate_limit_log_search_per_min`, 6/min). What each read may cost the customer's host is configurable rather than baked in: `log_download_max_mb`, `log_search_max_files` and `log_search_timeout_s`, all on Settings → General → Instance Logs. Opening the viewer, searching every day (with its term) and downloading a day write a `cloud.audit.log` row; polling the live tail deliberately does not, or one row every four seconds would bury the ones that answer "who read this instance's logs".
- Security of those reads: the endpoints are gated by `can_view_logs` (Developer+) and read the instance through the caller's own environment, so record rules decide which instances are visible. The file name is matched against the exact shapes logrotate produces before it reaches a shell, the search term is passed as a quoted argument, and **every command refuses anything that is not a regular file**. That last one is not cosmetic: `logs/` belongs to the container's uid, the panel's terminal is deliberately confined to the container, and the reader runs on the host as the SSH user — so following a symlink planted from inside the container would turn the log viewer into a host file reader. The listing only offers regular files, and the read, download and sweep each check `[ -f … ] && [ ! -L … ]`. The sweep's `log_search_max_files` budget is spent on real log files only — candidates come from `find -type f` restricted to logrotate's exact shapes, ordered by mtime — so a planted link or a junk `odoo.log.*` cannot push the real days out of it. A plain day that was compressed after it was listed is read (and downloaded) from its `.gz` twin, so a viewer left open over midnight keeps working. All of these commands are exercised for real by `tests/test_instance_log_commands.py`, which runs them through `sh` against a temporary `logs/` with real files and a planted link.
- The health probe scrapes ERROR lines from the file, filtered by timestamp with an awk state machine that keeps each traceback attached to its header, reading the newest archive alongside the live file so the once-a-day midnight rotation does not open a hole. It reads regular files only too: the newest archive comes from `find -type f`, and `tail`/`stat` on `logs/odoo.log` run only when it is not a link — the probe runs on the host as the SSH user, and a link planted in `logs/` would otherwise carry host-file lines into an alert. It also grades the archive itself and raises `instance_logs_unhealthy` when Odoo is logging to the container instead of the file, or when the live file grew past 512 MB (nothing is rotating it).
- The probe's own job log is held until the check ends. A cycle that passed and reported nothing through an alert, not even a first suspicion, keeps only its verdict line: its readings are on the instance already. Any other cycle keeps every line. Every five minutes for every instance, full logs of checks that found nothing were 83% of the panel's job log rows.

### The proxy's access log

Traefik writes one JSON line per request to `/var/log/traefik/access.log` on the host (mounted into the proxy at the same path). Full Setup installs its rotation (`scripts/proxy_access_log.sh`): daily, or past 100 MB — looked at every ten minutes by a timer of its own (`incubacloud-traefik-logrotate.timer`), because the distro's daily run alone would let a flooded host fill its disk between two runs — compressed, removed after 30 days by age so a busy host rotating several times a day still keeps the full month; the proxy is sent `USR1` to reopen the file, so no line is lost. It used to go to stdout, where Docker kept 30 MB in all and the path changed with every recreate of the container. The panel's "Recent requests" reads the file, together with the end of the previous one so the page is not empty right after the nightly rotation (falling back to `docker logs` on a host not set up since), the log collector ships it, and the host's web-login jail watches it.

## Odoo series: one card each

`models/_odoo_versions.py` is the one place an Odoo series is described, and nothing else in the panel decides on a version by itself. Each series has a card: whether it is `offered` (in the pickers and accepted by the importer), the PostgreSQL range and default (`postgres`), the tool that dumps and loads its database (`db_tool`) and the lowest doodba template able to render it (`template_min`). A series being prepared lands with `offered=False`, so the code that supports it ships before a customer can deploy it; a series that needs something new gets a field on the card, not an `if version` somewhere.

The panel manages Odoo 7.0 to 20.0, all of them offered: 7.0 to 19.0 on PostgreSQL 14 to 18 with `click-odoo`, and 20.0 (offered since 1.0.198) on PostgreSQL 16 to 18 with `odoo-db` and doodba template v10.0.3 or newer.

A panel offers a series only if its pinned template (Settings → Doodba template version, `copier_template_ref`) can render it (`template_ref_allows`; an empty pin is the template's default branch, newer than any tag): the pickers, `may_choose` and the default all read `offered_versions(pin)`. The default of `cloud.project` and `cloud.instance` is the newest of those (`default_version`; 20.0 under production's v10.0.8, 19.0 under a pin below v10.0.3), read when a record is created: the rows that exist keep their series. A new instance runs its project's series unless it names one (`cloud.instance.create`, whoever creates it: the panel's form, an import from a host, code); only an instance without a project takes the default. The SaaS manager names the series of the platform's own panels, which run 19.0. An administrator may still choose a series the pin cannot render, and its deploy refuses with RB-15; anyone else reads that it is not offered on this panel. A save that sends back the series a record holds is no choice: `write` drops it (`without_unchanged_series`), so a pin rolled back below a series' floor does not lock the records of that series. The 1.0.198 migration raises a stored pin that is a version tag below v10.0.3 to v10.0.8, both tags written in the migration itself.

What reads the card:

- `cloud.instance` refuses a PostgreSQL outside the series' range when saved, and `/cloud/get_odoo_versions` hands the SPA the cards so the PostgreSQL picker offers only that range (`static/src/utils/odoo_versions.js`).
- The deploy and the rebuild ask `cloud.instance._check_template_ref()` before uploading anything: a pin older than the series needs fails with the runbook's name (RB-15) instead of inside copier.
- The operation scripts never name a database tool. The executor passes the series' tool in the script's environment (`run_script(..., env=script_env(inst.odoo_version))` → `IC_DB_TOOL`), and `scripts/lib/common.sh` turns an operation into that tool's argv (`ic_db_dump_args`, `ic_db_load_args`, in `IC_DB_ARGS`). `click-odoo` is click-odoo-contrib's `backupdb`/`restoredb`, every series up to 19; `odoo-db` is `odoo db dump`/`odoo db load`, Odoo 18 and later and the only one on 20, where click-odoo-contrib 1.24 refuses its database commands. Every dump and load of an instance — the live backup, the download, the neutralized copy, the restore, the export's instructions, the free hosts' backups in saas — goes through those two functions, so a third tool is one more case there and a value on a card.

## Automatic module updates

On rebuild, `click-odoo-update` (from `click-odoo-contrib`, included in the doodba base image) detects which modules changed by comparing file checksums:

- Checksums are stored in `ir_config_parameter` key `module_auto_update.installed_checksums` (JSON)
- On first deploy: `click-odoo-update --only-compute-hashes` establishes the baseline
- On rebuild: `click-odoo-update` compares checksums and updates only changed modules

## Dependency re-sync and provenance

An unpinned repo line is aggregated at its branch tip, so a rebuild ships whatever upstream published since the last one. Deploy and rebuild therefore re-read the `requirements.txt` of every repo **without** a `commit_sha` just before writing `pip.txt` (`DeployInstanceExecutor._resync_repo_requirements`, inherited by every rebuild variant including the SaaS ones). Pinned repos are skipped: frozen code, frozen dependencies. A fetch that fails is logged and ignored — the stored list is used rather than stopping the fleet on a GitHub blip.

Merging upstream on every rebuild only works because each package remembers who wrote it. `pip_dependency_sources` (a JSON map on `cloud.project` and `cloud.instance`, provided by the `cloud.pip.provenance.mixin`) maps `package → {repo, spec, label}` and holds **only repo-owned packages** — no entry means the operator owns the line. The merge then decides per package:

| Situation | Outcome |
|---|---|
| Package absent | Added, owned by the repo |
| Same spec, unowned | Ownership claimed (lazy backfill — no migration) |
| Different spec, owned by this repo | Applied in place, logged in the job |
| Different spec, owned by operator or another repo | Conflict marker + `pip_conflict` alert, job fails |
| Package dropped upstream | Never removed — noted in the job log |

Ownership is keyed by repo URL, not branch, so switching branches does not hand a package back to the operator. Writing `pip_dependencies` outside the managed paths prunes the entries whose spec no longer appears verbatim (the edit makes those lines the operator's); the managed paths — the requirements merge and `/cloud/resolve_pip_conflict` — pass `pip_provenance_managed` in the context and set the map themselves. Resolving a conflict in favour of the repo's spec hands the line back to it; any other answer makes it operator-owned. Set `incubacloud.requirements_resync_enabled=0` to restore the old frozen behaviour.

## Endpoints that enqueue

No endpoint refuses a job because the instance is busy: the job waits in its queue ([The job queue](#the-job-queue)). The deploy and rebuild endpoints answer through `_job_response` (`controllers/_data_load/_helpers.py`, reusable by an inheriting controller — the SaaS manager's rebuild uses it): `{job_id, state, job_type, queued_behind}`, where `state` is `queued` when the job waits and the state its `queue.job` reached when it started (`pending` at first), `job_type` is the visible name of its type and `queued_behind` the visible name of the type of the job running on its target (`False` when it started, or when nothing visible runs — a queue held by a failure). A job that waits on a conflict alert answers `{job_id, state: 'blocked', blocked: true, alert_id, alert_code, message, conflicts}` instead. What a job's type allows can still refuse it: a second job of an exclusive type, a sixth entry, a role gate — as `{ok: false, error}` or an RPC error, as before.

The other endpoints that enqueue answer the job's id alone (`job_id`, or `job_ids` for a chain, first step first: backups, restores, PITR, the clone and the refresh from production, the move and its rollback, the host's whitelist, hardening and deletion, the metrics central, the conflict resolutions that put a waiting job back in its queue), as do the ORM calls the panel makes (`cloud.job.enqueue` for the instance's and the host's actions, a retry, and in the restore dialog the upload key, its check and the restore itself). Every button behind them reads the same answer from `/cloud/get_job_state` once its request has committed. The panel turns it into one notice — "Started: <type>", "Queued behind <type>", or "Queued" — with the type's name in the reader's language (`cloud.job.type.name` is translated), and opens no tab: a job's log opens from the job lists, where a waiting job shows its place ("Queued · N ahead"). A move rolled back before its chain started answers no job: cancelling it undid it. The SSH restore's upload key is installed by a job too, and the dialog shows the key at once: while that job waits, the dialog says the command works once it has started. Reviving an archived instance and deleting one keep their own words ("Reviving …: deploying, then restoring its copy"): an archived instance has nothing running to wait behind.

---

## Secret management

Sensitive fields use the custom `EncryptedChar` field type (a `fields.Char` subclass) backed by Fernet symmetric encryption. The database column always stores the `enc:<token>` form; Python code always sees plaintext.

- The key comes from the **`INCUBACLOUD_SECRET_KEY`** environment variable, which **must** be set in every environment. There is no fallback and no plaintext mode: a missing or invalid key makes encrypt/decrypt raise (`IncubacloudCryptoError`) instead of silently writing plaintext.
- **Key rotation** is built in: the variable accepts a comma-separated list of Fernet keys (MultiFernet). The first key encrypts new values; every listed key can decrypt. See `docs/runbooks/RB-01-rotate-secret-key.md` for the full rotation procedure.
- Legacy plaintext values (no `enc:` prefix) pass through unchanged on read, which is what migration code relies on.

**Encrypted fields (15 across 7 models, and `pitr_key` with `incubacloud_pitr`):**

| Model | Fields |
|---|---|
| `cloud.host` | `password`, `key_file`, `traefik_panel_password` |
| `cloud.instance` | `odoo_admin_password`, `odoo_admin_user_password`, `postgres_password`, `smtp_relay_password`, `secret_key` |
| `cloud.backup.backend` | `s3_secret_access_key`, `passphrase` |
| `cloud.github.app` | `webhook_secret` |
| `cloud.settings` | `github_pat` |
| `cloud.terminal.route` | `auth_token` |
| `cloud.host.terminal.route` | `auth_token` |
| `res.users` | `cloud_telegram_bot_token`, `cloud_webhook_secret` |

The frontend cannot read encrypted values directly. `/cloud/get_secret` exposes a whitelisted subset (see `docs/api-endpoints.md`) to developers with write access on the record.

### Each instance's own key, kept by the panel

An instance that runs this module encrypts *its* secrets with the `INCUBACLOUD_SECRET_KEY` in its `.docker/incubacloud.env`. That file used to exist on its host only — `deploy.sh ensure-secret-key` generated it and nothing ever read it back — so an instance moved to another host, rebuilt on a new one or cloned to staging got a new key, and its restored database could no longer read what it had encrypted (two panels, 18-oct-2026). **`cloud.instance.secret_key`** (developer-only, never copied, never in the config snapshot, the copier answers or a job payload) is now the panel's copy:

- A new instance is born with a key. One created already on its host (an import) waits for the host's.
- Every deploy, rebuild and warm claim (`SecretKeyEscrowMixin._ship_secret_key`) reads what the host holds, **merges** it with the panel's copy (`merge_secret_key_lines`: the host's keys first — the running Odoo keeps its primary — then any the panel has that the host lacks; no key is ever dropped, whichever side is stale), saves the merge, and writes it to the host before `ensure-secret-key`, which then keeps it. During a move the job runs on the target, where nothing exists yet: the key is read on the origin, where the instance still lives.
- The file is staged in `/tmp` created 0600 (`_upload_private_files`) and removed when the job ends, failed or not; it is moved into place 0600. The value is never logged; who changed it is (INFO).
- A staging keeps a key of its own when its copy of production is neutralized (a clone always is): neutralizing clears every encrypted column, so nothing it holds was encrypted with production's key. A refresh left live (`neutralize=False`) keeps production's secrets, so it gives the staging production's keys too and recreates its Odoo.
- Instances deployed before this existed are read once by the daily `cron_capture_secret_keys` (hidden job `capture_secret_key`, which only reads); one whose host cannot be read raises `secret_key_not_escrowed` on the second day.

What this concentrates: whoever holds the panel's database and its `INCUBACLOUD_SECRET_KEY` can now read every instance's key — but already held every host's SSH key and every instance's admin password, so the threat model does not change. The panel's own key becomes the root of trust of the fleet's: it must have a copy off its host (see RB-01).

**An alert on an unreadable secret closes itself.** `cloud.settings._cron_resolve_readable_secrets` (hourly, and once at startup — the right key usually arrives with a restart) decrypts again, from the raw column, every value an active `encrypted_value_unreadable:*` or `secret_rotation_stranded:*` alert stands for: the record itself for a host/instance/project secret, every row of the model linked to the alert's target otherwise. When all of them open, or the rows are gone, the alert is dismissed as the platform and announced resolved (a provider alert reaches its provider). A read in the hot path cannot do this: the process that failed keeps its cached key chain.

### Neutralized copies

A copy of a panel (development, QA, a neutralized download, a staging of a partner's panel) runs `odoo neutralize`, or the series' load tool with `--neutralize` (`ic_db_load_args`), before it is upgraded: each installed module's `data/neutralize.sql`, one statement per file, so one error aborts all of it, Odoo's own included. A column added after the copy was taken is cleared in a block of its own that skips it when missing. What core's file leaves the copy unable to do:

- **decrypt or use a secret**: every encrypted column is cleared, and the hosts and instances are archived;
- **reach out**: the users' webhooks are cleared, metrics are switched off (their URLs then go unread), and the jobs production had not finished are cancelled with the `cloud.job` mirroring each, so none runs in the copy;
- **switch its crons back on**: Odoo switches every cron off, and none of ours declares `active` in updatable data, which the copy's next module update would write again.

`tests/test_neutralize.py` keeps this true. Encrypted fields are checked against the registry, every installed module at once. `NeutralizeInventory` checks a repository's own modules: every stored field whose name says it points somewhere (`url`, `uri`, `endpoint`, `webhook`) is cleared by a script or exempted with its reason, and no updatable cron declares itself active. A layer above mixes it into a test of its own, with its own exemptions, and sets `allow_inherited_tests_method` (Odoo collects only a class's own test methods otherwise).

---

## Security model

- **5-tier RBAC**: Stakeholder (read-only) → Consultant → Project Manager → Developer → Administrator, implemented as nested groups. `cloud.security.mixin` centralises the gate helpers (`_check_can_deploy`, `_check_can_manage_hosts`, …) used by every endpoint and by server-side `cloud.job` enqueue gating.
- **Job actions are authorised at enqueue, per type**: `cloud.job._get_job_type_min_group()` maps every job type to the minimum role allowed to ask for it (lifecycle/deploy → Consultant; backups/restores/exports → Developer; host-level, observability and host-move → Administrator), enforced in `_check_job_type_allowed` on `enqueue`, `enqueue_chain`, `retry_job`, `unblock_and_enqueue` and `cancel_job` (in every state, on the job and on every step its cancellation ends). The map is fail-closed — an unmapped code requires Administrator — and a structural test (`test_job_type_gate_invariant.py`) fails the build if a declared job type is missing from it, so a new type cannot ship ungated. Extending modules add their own types by overriding `_get_job_type_min_group()`. `delete_instance` additionally escalates to Administrator when the target is a production instance, *before* any remote command runs. Executors are trusted readers of the host connection material (the job runs under the enqueuing user's env, and the SSH endpoint/credential fields are Developer-gated at the ORM): the elevation lives in the executor constructor, `get_transport()` and the Ansible inventory — never in `ssh_connect_kwargs()`, which is public and returns the password.
- **Record rules** scope projects/instances/jobs/alerts to project members; global alerts are hidden from the lower tiers.
- **What a member writes is not all theirs** (`cloud.instance._check_guarded_vals`): a consultant writes their projects' instances, production ones included, but only the platform writes a move's markers (which jobs a rollback and the watchdog cancel, and where the cleanup runs — and a move's chain is only its instance's jobs, `_get_move_chain_jobs`), a pull request preview's identity (what the GitHub webhook rebuilds and deletes by) and the removal and purge stamps; only an administrator sets an instance's directory on its host and the place of its copy (what a teardown removes and a purge empties), changes the host of an instance past draft (its stack is there and its waiting jobs follow it; `move_to_host` moves both), or renames a project's remote folder (which names its instances' directories). Where an instance's or a project's backups are kept is a Developer's choice, as backups are. Creating an instance already past draft is an import, an administrator's, and so is `delete_archived`, which without a copy removes the record as the platform. No project, the platform's included, takes a remote folder the platform keeps on every host (`RESERVED_REMOTE_FOLDERS`: the proxy, the shared whitelist, the warm spares and the observability stacks), by its folder or by the name it is derived from; the SaaS layer refuses those names as tenant slugs. Whether an instance is production is chosen when it is created and no person changes it afterwards, an administrator included (`/cloud/save_instance` refuses a change with its reason): it decides who may delete the instance, whether its egress is filtered and, on a customer's panel, whether it takes a seat of the plan.
- **Audit trail**: `cloud.audit.log` records who did what, filterable per instance/host, with configurable retention and a manager-only purge.
- **Rate limiting**: DB-backed counters protect the public endpoints (webhook, health), the restore upload and terminal opens. Caps are tunable in Settings → Rates.
- **Web terminal**: PTY subprocesses are isolated per session with an encrypted routing token; only the owning user can attach. Idle sessions are reaped (see RB-03).
- **Host shell**: a login shell on the machine itself, for Administrators only (`_check_can_manage_hosts`) — the role that already runs root there through every host job, with the same key. Same subprocess isolation as the web terminal, its own cap (3/min per user and per host), one open session per user and host, and an immutable `cloud.host.session` row per session. What is typed is not recorded: a shell receives passwords on stdin. Only `host_terminal_session.py` may open a command-less PTY; `test_terminal_session.py` fails the build if one appears anywhere else.

### Who may open SSH on a host

The hardening ruleset admits SSH from a named set, `inet filter ic_ssh_allow` (the operator's address, the address the play connected from, and `allowed_ssh_ips`), like the CDN sets: a named set can change without Ansible and without reloading the ruleset. `cloud.host._ssh_allowlist_change(op, ips)` runs `scripts/ssh_allowlist.sh` on the host — the whole set replaced in one nft transaction, never left empty, and the set's line in `/etc/nftables.conf` rewritten after the live change worked (the file parsed with `nft -c` first; the CDN sets' stale lines there are left alone) — and keeps the change in `allowed_ssh_ips`, which the next hardening rebuilds the set from. A host hardened before the set existed has its list inlined in the rule: the first change converts it, live (the set created, the rule replaced by handle) and on disk. `auto-merge` lets a catch-all load beside an address. The SaaS layer uses it to move a panel's own servers with the panel (the old host is the only one they admit until told otherwise).

### Host edge protection (network layers)

How a host is reached decides what can defend it, and that is a **property of the host** (`cloud.host.behind_cdn`), not an assumption. A host reached **directly** — its domains resolve straight to its address — can only be defended on the box itself, and that is how the tenant hosts have always run, because a proxied name cannot answer the TLS challenge a host uses to obtain its own certificate. A host reached **through a CDN** is defended at the edge instead, and its own layers change shape accordingly.

Getting this wrong in either direction is expensive, and both mistakes have been made here. Shipping CDN ranges to a host reached directly turned its per-visitor rate limit into a single bucket shared by every instance on it, because `ipStrategy` reads a forwarded chain that direct traffic does not carry (2026-09-03). Refusing direct traffic on a host whose visitors arrive directly would take it off the internet outright.

The protection is layered, outermost first:

1. **Provider L3/L4 anti-DDoS** — volumetric and protocol floods are absorbed upstream in the provider's network. Hetzner and OVH both include this free and automatically. It is an **assumed dependency, not our code**: a host on a provider without it loses the volumetric layer entirely, and such a provider should not front public instances (or must add one, e.g. Cloudflare Spectrum). See [RB-18](runbooks/RB-18-tune-host-edge-protection.md).
2. **Host connection-rate cap (nftables)** — a per-source new-connection limit on 80/443 in the hardening ruleset (`ansible/playbooks/host_hardening.yml`), on the **forward** hook because tenant traffic is DNAT'd to the Traefik container and never touches the input chain. On by default since 1.0.183 at 50 per second (`cloud.host.http_conn_rate`, passed to every hardening run), after it shipped off from 2026-08-14 — when an unrehearsed drop on that hook took the fleet down — until rehearsed. It weighs only the first packet of a connection redirected from the host's 80/443, with the same guards as the CDN allowlist below; matched on `tcp dport` alone it also counted the containers' own outbound traffic.
3. **Proxy per-IP rate limit (Traefik)** — a `rateLimit` middleware attached as the https entrypoint default (`data/traefik/config.yml`), so it throttles every instance on the host by client IP before a request reaches Odoo's pbkdf2 login. This is the principal control against the asymmetric login-DoS of SEC-008 applied to the tenant sites: one middleware covers N instances because it keys on the source IP, not the instance. Tunable live (the host's `config.yml` is watched); see [RB-18](runbooks/RB-18-tune-host-edge-protection.md).
4. **Login limit (Traefik)** — the general limit is the same for every path, which still lets one address try 300 passwords a minute, and Odoo's own cool-down counts per worker process. Each instance's override (`DeployInstanceExecutor._login_router_labels`) adds a login router beside each of its routers: POSTs to the endpoints that check a password (`/web/login`, `/web/login/totp`, `/web/session/authenticate`, `/web/reset_password`, `/xmlrpc/2/common`), with the host's `loginlimit` middleware first (20 a minute, burst 10) and otherwise the middlewares the template gives the router it shadows, so the login is never less protected than the rest of the site. Priority 15: above the template's routers for those paths (the `/web` one is 10), below any router a deployment layer sets in front of an instance's paths (30 and up). This repeats the template's router scheme on purpose, like the staging middlewares beside it; should a template release rename it, Traefik drops the login router alone and the login falls back to the template's — see [RB-15](runbooks/RB-15-bump-copier-template-pin.md).
5. **Ban on repeated failed logins (fail2ban)** — a jail bans for an hour an address that fails ten web logins in ten minutes, in fail2ban's own nftables table on the **forward** hook and on 80/443 only. It reads the proxy's access log, not Odoo's: Odoo writes the login it was sent as-is, and a login carrying a newline forges whole log lines, so the jail could be made to ban anybody; Traefik writes one JSON object per request and escapes what the client sent. A form login that fails is answered 200 (the form drawn again) and one that succeeds is redirected; the RPC logins answer 200 either way and are left to layer 4. Off behind a CDN, where the connecting address is the CDN's; and whatever the proxy trusts as a proxy is exempt, since a failed login arriving through one is the visitor's. The jail has a chain of its own (`f2b-weblogin`) in fail2ban's table: the action's default chain is the one the sshd jail creates on the input hook wherever the distro bans with nftables (Ubuntu 24.04), and a chain cannot sit on two hooks — with the default name the web-login rule landed in the input chain, where DNAT'd instance traffic never passes, and banned nothing (measured on 24.04).
6. **Application counters (`cloud.rate.limit`)** — DB-backed per-user/per-IP caps on the panel's own public endpoints (webhook, health, terminal, logs). See [RB-04](runbooks/RB-04-tune-rate-limits.md).

Layers 1–5 protect the **instances**; layer 6 protects the **panel**. All of this is **core**: a partner hardening their own VPS inherits the same defaults, chosen to be safe for any host rather than tuned to our pool.

**Behind a CDN, layers 2 and 3 change rather than stack.** Layer 2 becomes an explicit allowlist of the CDN's ranges on 80/443 instead of a per-source cap — behind a proxy every visitor arrives from a handful of edge addresses, so a per-source cap would throttle the CDN and protect nobody, and a host behind a CDN gets no cap at all, closed or still open at the origin — a pool host between moving its names and closing its firewall is open, and its visitors already arrive from the edges. The list sits on the same **forward** hook as the cap it replaces, because that is the hook DNAT'd instance traffic crosses (the same list in the input chain alone lets everything through — measured), and it judges only the first packet of a connection redirected from the host's 80/443 (`meta l4proto tcp ct state new ct status dnat ct original proto-dst`; the protocol first, because nftables 1.0.2 cannot type `ct original proto-dst` without it), so replies and the containers' own outbound traffic are never weighed against it. A name that resolves straight to such a host, bypassing the CDN, stops answering — ACME challenges included. Layer 3 keeps the same middleware but keys it on the forwarded chain (`sourceCriterion.ipStrategy.depth: 1`), which is only correct *because* the chain is now always present and always written by a proxy Traefik has been told to believe (`forwardedHeaders.trustedIPs`). Neither switch is inferred: they turn on `behind_cdn`, and the firewall half additionally waits for `block_direct_access`, so the two cannot be enabled in an order that leaves the host answering nobody.

**The CDN's addresses are not ours alone.** They are shared by every customer of the CDN, so an allowlist of them admits anybody's zone pointed at our origin, past every rule of ours. A host behind the CDN with direct access refused can therefore be given the authority its client certificate is signed by (`cloud.host.cdn_client_ca`, or a deployment layer's `_effective_cdn_client_ca`): the proxy then requires that certificate on every TLS connection (`tls.options.default.clientAuth`, `RequireAndVerifyClientCert`, in `dynamic/tls-client-auth.yml`, shipped by Full Setup and the proxy-settings push like the default certificate). Cloudflare calls this Authenticated Origin Pulls; with the zone's own certificate rather than Cloudflare's shared one, only that zone gets through.

**Certificates follow the same fork.** A host reached directly obtains its own through ACME. A host behind a CDN is *given* one — `tls_default_cert` / `tls_default_key`, served as Traefik's default certificate — because the challenge cannot reach it. In this platform that is one origin certificate for the whole zone, held in settings and handed to every host marked as being behind the CDN; in core it is simply a pair of fields, so a partner can supply a certificate from any source — pasted on the host's Traefik tab, where the pair is refused unless it matches (half a pair, an unreadable PEM or a key from another certificate would make Traefik answer every handshake with a throwaway), and shipped by *Push Trusted Proxy Settings* or *Full Setup*.

Each domain then says how its router gets a certificate (`cloud.instance.domain.cert_resolver`), and only the default defers to the host. **`auto`** (the default) asks the host per name: its held certificate when a CDN answers for the name *and* the certificate covers it, ACME otherwise. **`letsencrypt`**, **`custom`** (the held certificate, whatever the host) and **`none`** are explicit choices a host never overrides. Copier only ever receives the resolved value — `letsencrypt`, `true` or `false` — never `auto`. Every deploy, rebuild and warm claim writes one line per domain saying what its choice resolved to, and refuses `custom` on a host that holds no certificate — neither a default one nor one declared by hand in its `config.yml` (RB-12) — before anything reaches the host. The endpoint that saves an instance reads the options from the field itself: a copy of the list kept there once lacked `auto` and quietly rewrote it to `letsencrypt` on every save.

### Host maintenance window (04:00 UTC)

Hardening enables `unattended-upgrades` on every host it touches, and when the host has `auto_security_updates` set it also allows an automatic reboot, pinned to **`Automatic-Reboot-Time "04:00"`** (`ansible/playbooks/host_hardening.yml`). So a host that needed a kernel or `openssh-server` update disappears for a minute or two just before 04:00 UTC — sshd restarts mid-upgrade, then the box reboots — and comes back on its own. Tenants1 did this on 23 Jul, 30 Jul, 6 Aug, 9 Aug and 29 Aug 2026.

This is normal maintenance, not an incident, and the monitoring is sized for it: the SSH probes retry a transient connection failure ten times about thirty seconds apart (`AbstractExecutor._connection_retry_attempts`), so ~10 minutes of unreachability pass without alerting anyone. `host_unreachable` is raised only once a probe has actually exhausted that budget and reached a terminal state — never from a mid-flight guess about which attempt was the last.

When planning around it: the window is per host and only fires when an update demands a reboot, so it is not a schedule you can count on, only one you must not be surprised by. To move it, change the pin in the playbook and re-run hardening; to disable the reboot entirely, clear `auto_security_updates` on the host and patch it on your own schedule.

---

## GitHub integration

IncubaCloud supports GitHub App authentication for private repository access and receives webhooks for installation, push and pull request events. A stored PAT (in `cloud.settings`) is the fallback when no App is configured.

### Authentication flow

```
1. User opens Settings → GitHub App
2. GET /cloud/github/setup
   → Generates App manifest (branded, fleet-unique app name)
   → Redirects to github.com/apps/manifest-code

3. User installs app on GitHub org
   → GitHub redirects to GET /cloud/github/callback?code=...
   → Exchanges code for (app_id, private_key, webhook_secret)
   → Stores in cloud.github.app (singleton)

4. On each API request needing auth:
   → Generate short-lived JWT (10 min) from private_key
   → Exchange JWT for installation token (1h) via GitHub API
   → Cache installation token in memory (per worker process)
   → Use Bearer token in API requests
```

### Webhooks and auto-rebuild

All webhook events are stored in `cloud.github.event` (immutable audit log). The receiver validates the HMAC-SHA256 signature, rate-limits per IP and ignores replayed delivery ids.

`push` events drive **auto-rebuilds**: an instance rebuilds when it is active, deployed, has `auto_rebuild` enabled and its host is active. A push always queues its rebuild (`last_auto_rebuild` is written then): on a free instance it starts at once, behind a running job it waits in the instance's queue, and while the rebuild of an earlier push waits it merges into that one instead — the waiting job's `coalesced_pushes` grows with it and its head stays the first push (`RebuildInstanceExecutor._merge_queued_payload`), so no commit is built twice and none is dropped. A rebuild someone asked for by hand, or one already dispatched, is never merged into. Per-repo **freeze** flags are honoured. Archived instances and archived hosts never auto-rebuild.

### Pull request previews

`pull_request` events arrive through the same receiver and are handled by `cloud.github.event._process_pull_request_event`. The head ref is checked with `is_safe_git_ref` first: it ends up in `cloud.instance.repo.branch`, so a hostile ref name is dropped here rather than at the constraint.

- **`opened` / `reopened`** — every `cloud.instance.repo` matching the repository with an **empty `commit_sha`** (a pinned repository never takes part), on a `production`, deployed instance whose project has `pr_reviews_enabled`, yields a candidate. Each candidate is put to `_pr_preview_allowed(inst)` — yes by default; layers on top override it to keep instances they manage themselves out, the same pattern as `_rebuild_job_type`. Deduplicated on `(pr_number, pr_repo, project)`, the instance is then cloned with `clone_to_staging('pr-<n>', …)` inside a savepoint, so a clone that fails half-way leaves no row behind to be mistaken for an existing preview.
- The clone lands on the **production's host**, follows the pull request's branch on the matching repository, gets its domain from the host's wildcard like any other instance (`create()`), and runs the chain `deploy_instance` → `backup_download` (on production; always a live dump — `latest` failed on a production with no backup yet) → `restore_instance` with `neutralize` and `reset_base_url`. The restore's success posts the "ready" comment (`cloud.instance._pr_preview_ready_body`) through the GitHub App.
- **`synchronize`** re-points the branch and enqueues a rebuild, skipping archived instances and inactive hosts. **`closed`** deletes the comment and enqueues `delete_instance`, or unlinks a preview that never deployed.
- **Failure is reported where people look.** A `UserError`/`ValidationError` from the clone is commented on the pull request verbatim and raised as a `pr_preview_failed` warning on the production instance; anything else is logged with a reference and published only as a generic line, because the repository may be public. The alert is resolved by the next preview created for that production. Commenting is best effort and never fails the event.

`pr_reviews_enabled` defaults to **off** and travels through `/cloud/get_project`, `/cloud/create_project` and `/cloud/save_project`. Everything the webhook enqueues is signed by the platform bot (`as_platform`), not by whoever opened the pull request.

---

## Staging autopurge

Once a staging stops counting against the plan allowance, nothing pushes anyone to delete one, and they are not free: disk on the customer's host, probes and series in the central stack, and a copy of production data nobody remembers is there. A staging unused for `cloud.settings.staging_autopurge_days` (default 90, `0` disables, floor of 28 so the warnings still land in time) is deleted. Production is never a candidate, nor is an archived instance, nor one flagged `autopurge_exempt`.

**The clock is two fields and the later one wins** (`cloud.instance._autopurge_clock`). Both are fields of their own and never `write_date`, which the daily crons would push forward forever — the same trap `archived_at` documents.

- `last_touched_at` — somebody acted from the panel. Sealed on `create()` (so no row is ever clockless, including copies, since `copy=False` drops it), by `queue_job_ext.QueueJob._seal_autopurge_clock` when a **visible** job authored by a **human internal user** reaches `done` (the seal runs after the job commits, on a cursor of its own: the job's hooks commit writes on the same row, so an UPDATE inside the job's transaction loses the serialization race and queue_job re-runs the whole job), and by the connect-as and terminal-open controllers, neither of which leaves a job behind. Hidden types (the probes) and anything the bot starts on its own — a rebuild off a push above all — are excluded by design: either counting would keep every staging alive forever and leave the feature dead code that looks alive. `create_uid` is trustworthy here precisely because everything the platform starts goes through `as_platform`.
- `last_login_seen_at` — the last login inside the staging's own database, read daily by the hidden `read_last_login` job (`cron_read_staging_logins`, running and non-exempt stagings only). The script picks `res_users_log` or `res_users.login_date` by asking the database with `to_regclass`, not by trusting the panel's `odoo_version`. **A failed reading writes nothing**: a stopped container, an unreachable database or a garbled answer must never be recorded as "nobody logged in". The value is also monotonic — an older answer (a restore from an old backup) and a future one (a skewed host clock) are both refused.

**The ladder** (`_cron_autopurge_stagings` → `_autopurge_step`, daily) climbs **one rung per tick**, and each rung requires the one below it: first warning at `AUTOPURGE_WARN_DAYS` (14) out, final warning at `AUTOPURGE_FINAL_DAYS` (3), deletion at the deadline **and** at least 24 h after the final warning was stamped. That ordering is the whole design: the naive version — evaluate every condition, act on all that match — warns twice and deletes in one sub-second pass when the cron comes back from an outage, which is notice in form and nothing in substance. Failures are reported and skipped per instance, so one unreachable host does not keep the rest alive.

Two alert codes (`staging_expiring` warning, `staging_expiring_final` critical) rather than one that escalates, because `cloud.alert` only dispatches notifications on **create**: re-raising the same row at a higher level reaches nobody, and `critical` is the only level the default `failures` preference delivers. Both are resolved by `_touch_autopurge_clock()`, which also clears the two warning stamps — an instance that comes back to life gets its whole notice period again, never "three days left and no warning showing".

Deletion goes through the ordinary path (`delete_instance` enqueued `as_platform` when deployed, `unlink()` when not) — the same one a closing pull request uses; a second way to delete an instance is a second way to get deletion wrong. An audit row carrying the instance **name** is written first, because `cloud.audit.log.instance_id` is `ondelete='set null'`. A busy instance no longer refuses it: the delete cancels what waits in its queue and waits for the job running. An enqueue refused anyway (a delete already waiting, a failure) raises `staging_autopurge_stuck` and retries tomorrow.

`_autopurge_days_left()` answers only once the first warning is out — a countdown on every staging from birth is something people learn to scroll past — and travels through `/cloud/get_instance` and the project instance list. `/cloud/keep_instance` (consultant-gated, the same role that can deploy) is the one-click reset, audited.

## Egress on staging (the "Whitelist" tab)

A staging is a copy of production that people poke at, so what it can reach matters. Until template v9.6.0 the answer was decided per *host*: `cloud.host.whitelist` builds one `docker-whitelist` proxy per hostname in `~/globalwhitelist`, and every test instance on the machine joins the same network by alias. That answers "what may stagings on this box reach", which is not the question a customer asks — theirs is "this staging has to reach *their* API".

`cloud.instance.whitelist` is the per-instance answer. It travels as the `whitelisted_hosts_test` copier answer, so it is part of the config snapshot: saving lights the drift pill and it applies on the next rebuild. A new staging is seeded from its host's list, and production is never filtered by us — its answer is always `[]`. What the template renders from that answer depends on its version, and **a stack keeps the model of the template that rendered it until it is rebuilt**: the panel records that template on the instance (`stack_template_ref`, written by deploy and rebuild; empty for a stack rendered before it existed, which is the old model) and reads the stack from it — `expected_services()`, the health probe, where the deploy puts its Traefik labels.

*Template v9.6 to v9.x:* a non-empty list renders a NAT gateway (`proxy_general`) plus a sidecar (`odoo_net_setup`) that shares the odoo container's network namespace and replaces its default route; an empty list falls back to the host's shared proxies, which the template joins either way.

*Template v10 (the pin since core 1.0.195):* one `gatekeeper` service owns the network namespace `odoo` runs in (`network_mode: service:gatekeeper`), filters with nftables (reject by default; the allowed set is resolved from `ALLOWED_HOSTS` every `REFRESH_INTERVAL`; private ranges and the two upstream resolvers always pass) and carries the staging's Traefik labels, the login routers among them. It fails closed: without the gatekeeper odoo has no network at all, and `odoo` waits for its healthcheck (`condition: service_healthy`) to start. The shared whitelist network is gone, so an empty list of the staging's own would mean no egress at all; the panel keeps the old meaning by rendering the host's list instead (`_egress_whitelist`). The override pins the image by digest (`GATEKEEPER_IMAGE`: upstream publishes `latest` and pull-request tags only) and gives it the restart policy the template does not, so a host reboot brings the staging back (`_gatekeeper_override`). The gatekeeper gets a new namespace every time it starts, and a running odoo stays in the old one, which keeps no route out (Traefik, which reaches odoo through the gatekeeper, answers 502): after an `up`, `start` or `restart`, `compose_op.sh` reads odoo's routes and restarts `odoo` when it has none, so odoo joins the namespace the gatekeeper has now; a restart of the whole stack is a `stop` and a `start`, in dependency order, waiting for what odoo depends on to be healthy. When the script cannot read what a step after the operation needs — the compose, what runs, odoo's routes (30 seconds at most) — the operation has still run: it names the step it could not check and what may be wrong, and exits 24 (`COMPOSE_OP_EXIT_UNCHECKED`) instead of 0. The start, restart, restore and backup restore jobs take 24 for their step done: they succeed, with a warning in the job log, and everything a success brings runs as usual. What may be left wrong is the health probe's to see: for a stack with a gatekeeper it reads the same routes, and odoo without one, or with its gatekeeper not running, counts as Odoo not answering (`instance_unresponsive`, critical, on the second probe in a row, with the cause and "Restart the instance"); routes it could not read change nothing. It also grades an unhealthy gatekeeper as `instance_egress_filter_lost`: nothing opens up, but the staging reaches nothing it is allowed to.

**Two of the old template's choices are not ours to ship, and the deploy override undoes both** (`_net_setup_override`, for stacks still on that model).

*The host's docker socket.* `test.yaml.jinja` mounts `/var/run/docker.sock` into the sidecar unconditionally — no copier question turns it off. It is read by exactly one function of the image's entrypoint, guarded by `DNS_INTERNAL_FROM_DOCKER`, whose default *in the script* is `0`; the template is what turns it on. The work that matters — resolve the gateway, pick the interface, `ip route replace default` — never touches Docker, and the internal DNS that function builds is already served by Docker's own resolver at `127.0.0.11`, which the sidecar's dnsmasq forwards to and which answers service names and network aliases alike. Measured on this panel's own devel, which has run with the lookup off and no socket for months: `/run/dnsmasq-internal.hosts` is empty, `db`/`smtp`/`wdb` and the network aliases all resolve, the default route points at the gateway, and a host outside the list still gets nothing.

So the mount buys nothing and costs everything. `:ro` marks the *mount*, not the protocol: talking to a unix socket is `connect()` plus `send()`, and the Docker API has no read-only mode — the same channel accepts `POST /containers/create` with a bind of `/` and `privileged`. On a host shared between customers that is not "can see other containers", it is root over the machine and everyone on it. The override re-points the target at `/dev/null`, because **an empty `volumes:` list does not remove a mount**: compose merges volumes by target path, so the base file's entry survives it.

*A disabled healthcheck.* The template ships `healthcheck: disable: true`, while the route injection happens once, at start. Restart the odoo container and Docker hands it a fresh netns with the default NAT route: the injection is gone, the staging has free egress, and the sidecar stays `Up` because its last line is `tail -f /dev/null`. Every light green. That is the 2026-08-06 incident on this panel — three and a half hours sending Telegram to the live channel — which is why `devel.yaml` has carried a route check ever since. The override ships the same check (pure shell: the image has `/sbin/ip` and little else, and a lost injection is recognised by the default gateway's last octet being Docker's own `.1`), and `disable: false` is said out loud because compose merges the healthcheck mapping key by key.

`expected_services()` names both sidecars whenever the list is non-empty, which is the same condition the template renders them on. That is what gets them probed, labelled against the daily `docker system prune`, and log-rotated; they get no resource limits, because two shell scripts and a dnsmasq are not a number this module should invent.

**Which containers a user may sit inside is a server-side list.** `terminal_open` used to validate the *shape* of a service name and open whatever matched, so the browser's selector was the gate. `cloud.instance._shell_services()` now answers it — `odoo`, `db`, `smtp`, `backup`, intersected with what the last deploy actually found — and the route refuses everything else. An allow-list, not a deny-list: a service the template grows next year is excluded until somebody says otherwise.

---

## Captured mail on staging (the "Mails" tab)

Every staging renders a MailHog container and the doodba template wires Odoo to it (`SMTP_SERVER: smtplocal`), so a copy of production never mails real customers. That has always been true and was never visible. `controllers/_data_load/_mailbox.py` builds the three commands that make it readable; `_routes_ops` exposes them at `/cloud/instance_mailbox`, `/cloud/instance_mail` and `/cloud/instance_mailbox_clear`.

**The catcher is only reachable from inside the stack**, which is the point: core 1.0.128 took away its Traefik router, because the template publishes the inbox at `/<domain>/smtpfake/` with no authentication and `cidr_whitelist` is `null` on our deploys. So the request is issued with `curl` from the instance's own `odoo` container, which is on the compose network and already carries `curl` (the health probe uses it). Taking the container off Traefik also hides the middlewares the template declares on it — the ones the staging's Odoo routes use — so the override declares the same five (`addSTS`, `buffering`, `compress`, `forbid-crawlers`, `forceSecure`) on `odoo`; without them every staging answered `404 page not found` (fixed in 1.0.151).

**The projection runs on the host, not in the container.** A staging can be any Odoo from 7.0 up, so its container's Python is whatever that era shipped — the trap `read_last_login.sh` had to dodge. The host is ours and has python3, so `docker compose exec … curl | python3 -c "$PROG"` puts the parser on the right of the pipe, in the SSH shell. Two consequences: nothing is assumed about the tenant image, and only the projected JSON crosses SSH — a mailbox of fifty messages with attachments is megabytes, none of which the panel would show.

Bodies are rebuilt from MailHog's `Raw.Data` with Python's `email` package rather than from its own `MIME.Parts`, because the raw message is the only representation carrying each part's charset and transfer encoding — which is what turns a quoted-printable `=C3=B1` back into an `ñ`, and an RFC 2047 `=?utf-8?q?…?=` subject into something a person can read. Attachments are listed (name, type, size) and never carried. The listing, the message and the attachment list are all bounded (100 messages, 256 KB of HTML, 64 KB of text, 50 attachments), and a truncated body says so.

Gated on `can_view_logs` (developer+): the same data class as the logs, read the same way, and capped per user by its own `rate_limit_mail_reads_per_min` because a mail read costs a `docker compose exec` plus an HTTP call inside the stack where a log read is a `tail`. Nothing is stored in the panel. Production has no catcher, so the tab is absent rather than empty, and the four ways to have no mailbox — production, not deployed, stopped, gone — each answer in their own words: an operator who cannot tell them apart concludes the feature is broken. Clearing is audited; reading is not, matching the proxy access log.

In the browser the HTML body renders in an `iframe` with an empty `sandbox`, so no scripts and no same-origin: it is mail generated by somebody else's database.

---

---

## Alerts and notifications

### Alerts

`cloud.alert` rows target a host, instance or project (or none = global) with a machine `code`, a `warning`/`critical` level and an `active`/`dismissed` state. **Every producer goes through `raise_alert`/`resolve_alert`**; none creates a row by hand. One active alert per identity: (code, instance, key) for an alert about an instance — the host given with it only says where it runs, so the alert stays the same when the instance moves —, (code, host, key) for one about a host, and the project too for one about a whole project. The `dedup_key` tells apart what the target alone does not: `backup_backend:<id>` for a backend's alerts, `job_type:<code>` for `job_failed`, `project:<id>` for a project's; a module built on top may add its own. Creators re-use the existing row, and a partial unique index (`cloud_alert_active_identity_uidx`) enforces it against two producers racing under snapshot isolation — the loser's insert is refused and treated as success. `job_failed` is exempt from the index; a repeat failure of the same type and target refreshes the open alert and bumps its `occurrences` instead of stacking a row per attempt. Health and metrics executors **auto-resolve** their alerts when the condition clears, and a job success dismisses stale `job_failed` alerts for the same type and target. Dismissed alerts are garbage-collected after 60 days (cron); active ones never expire.

**A measurement confirms itself first.** `cloud.alert.policy` holds, per alert code (or per family, `instance_service_*_down`), how many sightings in a row make the condition an alert and how fresh each must be; `raise_alert` reads it for every caller that brings no count of its own, keeps the first sightings as a `cloud.alert.suspicion` (one row per code and target, its own unique index, never listed, counted or notified) and only raises on the N-th sighting in a row, each within the window of the last; a clear reading or `resolve_alert` forgets the suspicion, and the alert GC drops suspicions nobody refreshed for a day. The shipped rows (`data/alert_policy_data.xml`, not `noupdate`) are the metrics backend, `instance_down`, the companion containers and the SSH disk probe, all at two sightings within three ticks; the SaaS manager adds its own rows (`catchall_unreachable`, two within three hours). The producers that measure raise their code and nothing else. A code with no row is an event (`job_failed`, `host_key_changed`, …) and raises on the spot. Metric rules are the one caller that passes its own count: `confirm_cycles` per rule (default 2; 1 for expressions that already measure over a window), because their codes are the operator's. A document read once a day (the edge-range crons in the SaaS manager) has no next tick to confirm on and reads twice within the pass instead. The older per-instance `*_streak` fields of the SSH health probe predate all this and still do their own counting for CPU, memory and HTTP.

**Audience.** `cloud.alert.audience` is `fleet` (default: whoever runs this panel's hosts, instances and repositories) or `provider` (whoever runs a service the panel depends on but does not own: the metrics central when `metrics_central_host_id` is empty, an undecryptable secret, an instance record left behind, a prune that swept managed containers). `_dispatch_notifications` and `_notify_alert_resolved` ask `_route_provider_alert(resolved=)` before paging anyone about a provider alert; core's default returns False, so a self-hosted operator — who is the provider — is notified as usual. The bus broadcast is never skipped: the panel shows the alert either way. The SaaS tenant module overrides the hook to relay the alert to IncubaCloud (`/saas/alert`), where it is raised against the tenant's instance on the master.

**Provider alerts are inventoried.** `cloud_alert.PROVIDER_ALERT_CODES` lists every code core raises with `audience='provider'`; `test_provider_alert_inventory` reads the source (every call or dict that can set the audience to `'provider'`, conditionally or not) and fails on one missing from the list or listed but no longer raised. `encrypted_value_unreadable` reached customers' panels before anybody knew it was one (18-oct-2026).

Alert codes in use: `secret_key_not_escrowed`, `job_failed`, `disk_critical`, `host_unreachable`, `instance_down`, `instance_unresponsive`, `instance_high_cpu`, `instance_high_memory`, per-service container-down codes, `instance_error_logs`, `pip_conflict`, `addon_conflict`, `move_stuck`, `secret_rotation_stranded:<field>`. `pip_conflict`/`addon_conflict` are *actionable*: they block re-enqueue of the affected operation and are excluded from bulk dismiss.

### Notification channels

All channels share one recipient rule: **active internal users filtered by record-rule visibility** and their own preferences (level `all`/`failures`/`none`, muted projects). Two paths feed them:

- **Alert pipeline (unified)** — every `cloud.alert` creation calls `_dispatch_notifications()`, which fans out to email, Telegram and webhook. Job **failures** create a `job_failed` alert and are notified through this path — there is a single code path for infrastructure alerts and failed jobs.
- **Job path** — successful job completion (`done`) is notified directly from the `queue.job` state bridge (no alert is created for successes).

| Channel | Enabled by | Notes |
|---|---|---|
| Bus (real-time) | always | `cloud_jobs` (job events → toasts) and `cloud_overview` (alert badge + critical alert toasts) |
| Email | per-user toggle | Immediate or **daily digest** (07:00 cron, QWeb template, watermark per user). Critical alerts and severe failures bypass digest mode. |
| Telegram | bot token + chat id set | Markdown message via the Bot API, every name and message in it escaped as text (`telegram_markdown`); chat-id auto-detect and test-message endpoints in the UI |
| Webhook (HMAC) | HTTPS URL set | JSON POST per event, optionally signed |

For users at level `failures`, the alert pipeline only delivers `job_failed` alerts and critical alerts.

Webhook payloads — two event types:

```json
{"event": "job_state_change", "job_id": 123, "job_name": "Deploy Instance",
 "state": "done", "severe": false, "host": "host-1", "instance": "prod",
 "log_url": "https://…/cloud/log/123", "timestamp": "2026-07-12 10:00:00"}
```

```json
{"event": "alert", "alert_id": 45, "code": "job_failed", "level": "critical",
 "message": "Deploy Instance on prod failed", "host": "host-1",
 "instance": "prod", "job_id": 123, "log_url": "https://…/cloud/log/123",
 "timestamp": "2026-07-12 10:00:00"}
```

When a secret is configured the request carries `X-IncubaCloud-Signature: sha256=<hex>` — HMAC-SHA256 of the raw body — on both event types. Verify with a constant-time compare. No secret → no header.

A separate email alert covers **backup bucket usage** (quota threshold per backend, 7-day debounce), outside the alert panel.

---

## Extending an instance's stack (addon hooks)

Deploy, rebuild and warm claim render the whole stack in `DeployInstanceExecutor` and its subclasses. An addon adds to it through `cloud.instance` hooks instead of patching executors; each answers "nothing" in core, so an instance without such an addon is rendered exactly as before:

- `_extra_services()` — compose services the addon adds. Listed in `expected_services()`: they get the platform's protect label and log rotation, start with the stack and are watched by the health probe. Each must be defined by a fragment (with its `image`).
- `_compose_override_fragments()` — `{service: {key: value}}` merged into `docker-compose.override.yml` and never over it: mappings gain keys the platform did not set, `volumes`/`env_file`/`depends_on` gain entries, any other key is set only when absent (a fragment cannot replace Odoo's `command` or a limit).
- `_extra_env_files()` — files shipped into the instance directory on every build, staged 0600 and moved into place 0600 before the stack starts ("Write extra env files"). A file an addon stops listing stays until the addon removes it.
- `_teardown_pre_steps(dir, keep_in_panel)` — steps run before a delete tears the stack down; never on a move's cleanup of the copy it left (the instance lives on).
- `_after_stack_rendered()` — called by deploy and rebuild success: what the hooks answered is now on the host and running.
- `_after_restore_hook(kind)` — called once the database was replaced: `restore` (a copy restored onto it), `backup_restore` (one of its own backups, a revival included), `move` (its cutover, after the restore on the target), `pitr` (its own database brought back to a moment by `incubacloud_pitr`, also when its files could not be).

`cloud.job._get_health_blocking_job_types()` lets an addon whose jobs restart containers on purpose keep the health probe from reporting them, without overriding the probe. `cloud.job._get_health_blocking_host_job_types()` does the same for host jobs that stop the instances of their host: while one runs, those instances' probes skip their pass, and leave their streaks and alerts as they were — the job changes nothing in them, where a job of the instance's own resets both. After either kind has ended, the probe's error window opens one minute after its `date_done`, so the connections it cut are not read as errors.

## Continuous recovery (`incubacloud_pitr`)

A separate module, off until switched on per production instance (managers; a staging never archives). Once on, the instance's database hands its write-ahead log to its own backup bucket and its filestore is copied as it changes, so it can be restored to **any minute** of its backup window (its backend's retention; a SaaS plan's retention for a tenant's panel). The recovery objective — the most it may lose — is 1, 5, 15 or 60 minutes.

- **The credentials never reach the database's container.** Odoo connects as a superuser, and a superuser's SQL can read that container's environment (`COPY … FROM PROGRAM`): an addon of the instance's would hold the bucket's key, which often opens every instance's backups. They live in one container, the **`pitr` agent** (rclone image, by digest), with `.docker/pitr.env` (0600). `db` only mounts `.walg/` read-only, and its `archive_command` is `/opt/walg/archive %f`, a script that holds nothing secret: it waits, up to a minute, for the mark `spool/done/<segment>` the agent leaves once it uploaded that segment, and fails otherwise (PostgreSQL keeps the segment, retries and counts the failure).
- **The agent** sees the data directory read-only. Every second it uploads, in order, the segments PostgreSQL marked ready (`pg_wal/archive_status/*.ready`), each under **its cluster's** prefix (`<prefix>/<system identifier>`, read from `pg_control`): a move or a new deploy initialises a new cluster, whose segment names would otherwise land on the old one's, and the prefix follows a change of backend with the next rebuild. Every objective it copies the filestore's files changed since the last copy that worked (`--max-age`, no listing of the bucket; `spool/fs-ok` records when it started); once a day an exact copy, whose deletions are moved to `deleted/<day>` so a restore to before them brings them back. Encrypted (rclone crypt, a password derived from `pitr_key`). It runs the base copies too, connecting to `db` with Odoo's password.
- **wal-g**, pinned (`_constants.WALG_VERSION`, sha256 checked on the host, a download file of its own per install), compresses (brotli) and encrypts (libsodium, with `cloud.instance.pitr_key`, born with the instance; a neutralized copy gets a new one when switched on) before uploading. Its builds are for glibc and the images are Alpine: `pitr.sh install` copies the host's glibc loader and the four libraries wal-g needs into `.walg/` and runs it through them (`--argv0`, so the copies of itself wal-g starts go through the same wrapper).
- **Archiving**: a stack rendered with the module starts `db` with `-c archive_mode=on` (the one setting only a restart changes; `CONF_EXTRA` would be replaced by the override), so switching on is a reload and no database restarts under a customer opening an instance just announced ready. Until then it has no archive command and keeps its segments, which `pitr_enable`, queued as the stack is rendered, follows within minutes. Everything is also written with `ALTER SYSTEM` (`postgresql.auto.conf` lives in the data volume and survives recreations): `archive_command`, `archive_timeout` = the objective, and `archive_mode` itself, since a restore's recovery starts the data directory without the stack's command line and must archive the timeline it begins; a database not archiving yet (a stack rendered before) is restarted once, Odoo stopped around it and started again however it ends. Switching on checks one segment end to end. Switching off starts a stopped database to drop the settings (left there, its next start would archive to a command the rebuild removes and keep every segment); while the flag still holds archiving on, until the rebuild that follows drops it, the archive command becomes `/bin/true`, letting the segments go, with no restart.
- **Base copies**: the first of a cluster is full; then daily (an hour per instance, 01–05 UTC), delta, and full again whenever the newest copy is of another timeline — wal-g would otherwise build a delta on a copy taken before a restore to the past, which holds changes the restored database never had. Then housekeeping, each part on its own: what leaves the window (`delete before FIND_FULL`, so a kept delta keeps its full copy), old filestore deletions, a previous cluster's archive once out of the window (never the current one). A base copy whose housekeeping failed is still recorded.
- **Watch**: every ten minutes, for running instances not moving, one policy (`_pitr_reconcile`, also called after a rebuild) switches on what is wanted and off what is not, never again within a day of a switch that failed; and `pitr_status` reads `pg_stat_archiver`, the segments waiting in `pg_wal/archive_status` (none waiting means nothing is owed, however long ago the last left: an idle database is not behind), whether the agent runs, and `fs-ok`. Archiving off or a new cluster → switched on again; failures or the agent down → `pitr_failing` (critical); the log or the files behind by more than three objectives → `pitr_archive_lagging`; no base copy, or none for 36 h → `pitr_base_missing`.
- **Restore to a moment** (`pitr_restore`, after a duplicity snapshot of the present when the instance has one). The base copy is chosen by the panel (`choose_base`): each restore starts a new timeline at its promotion, branching off the moment it went back to (`pitr_timelines`); the copy must have been taken before the moment on the timeline live at that moment, or serve the moment that timeline branched off — a copy taken after the branch on the old timeline holds a history that was abandoned. The restore itself runs on the host, **detached** (`pitr.sh restore-start` hands it to a process of its own, holding `.walg/restore/lock`): Odoo stops, what the database still holds is archived, the copy is fetched **beside** the current data (wal-g only fetches into an empty directory) with room checked against the data as it is, swapped in, and recovered in a database container of its own that has the credentials `wal-fetch` needs — the `db` service never does, and nothing of the instance's runs meanwhile. If it does not come up, or the process is interrupted before it did, the previous data is put back; however it ends, the instance is started again and `.walg/restore/result` says how. The job follows it for 50 minutes and hands it to a job of the same type (`follow`) while it runs; the same job run again follows its restore instead of starting a second one. Then the filestore, a record of the new timeline and a full base copy of it.
- **Its pieces reach the stack through the hooks above**: the mount on `db`, the `pitr` service, `.docker/pitr.env` (kept while the instance has an archive, so a delete purges it with fresh credentials), and the README files that create `.walg/` and its spool as the host user. Switching on is a rebuild then `pitr_enable`; a plan applied by a rebuild switches it with `_after_stack_rendered`, without a second one. Deleting the instance purges its prefix (`<folder>/<name>-pitr`, beside the duplicity prefix, never under it), once with the stack up and once more after the agent and the database stopped.
- `pitr_*` fields are written by managers or by the panel itself only (`write` refuses others): switching the archive off, or pointing it at the wrong cluster to purge, loses a customer's history. `pitr_stack_external`: a stack whose compose is not the panel's (the platform's own manager): the panel only watches it and takes its base copies; switching and restoring go through its runbook (`invoke pitr-manager`). Uninstalling the module is refused while an instance archives.

## Frontend SPA

The UI is an **OWL single-page application** served from `/cloud`. It uses a custom router with hash-free URLs.

### Routes

| Route | Path | Component |
|---|---|---|
| `projects` (home) | `/cloud`, `/cloud/projects` | Project dashboard |
| `new_project` | `/cloud/projects/new` | Project form |
| `project_detail` / `project_settings` | `/cloud/projects/:id[/settings]` | Project detail |
| `create_instance` | `/cloud/projects/:id/instances/new` | Instance form |
| `instance_detail` | `/cloud/projects/:id/instances/:iid` | Instance detail |
| `hosts` / `new_host` / `host_detail` | `/cloud/hosts[...]` | Hosts (manager-gated) |
| `settings` | `/cloud/settings?tab=…` | Settings (manager-gated) |
| `backup_backends` / detail / new | `/cloud/backup-backends[...]` | Backup backends |

Jobs and alerts are **slide-over panels** (not routes) opened from the header; notification preferences are a header modal. The router is extensible: private modules register additional routes with `registerRoute(name, parseFn, buildFn)`.

### Theme

Light and dark themes ship with the Relay design system (`scss/relay.scss`). The choice persists in `localStorage["ic-theme"]` and applies as `data-ic-theme` on `<html>` before mount (no flash). Default is light; the toggle lives in the user menu.

### Real-time updates

Two bus channels: `cloud_jobs` (job state changes; components re-fetch via `/cloud/get_job_brief`, project store refreshes debounced) and `cloud_overview` (alert badge refresh + per-user critical alert toasts). A visibility hook re-fetches on tab focus as a safety net.

### Pagination

Long lists use server-side `offset`/`limit` pagination (page size 20) with the `IcPager` component: alert history, instance backups, and audit logs. Dashboard-style lists are truncation-capped (limit 200 + banner) instead.

### Toast notifications

A global service injected through `useSubEnv` in the app shell:

```javascript
this.env.toast?.success("Instance deployed");   // auto-dismiss 4 s
this.env.toast?.error("Deploy failed: " + e);   // persistent until dismissed
this.env.toast?.warning("Unsaved changes");     // 6 s
this.env.toast?.info("Refreshing backups…");    // 4 s
```

The OWL `env` is frozen — never mutate it directly.

A button that enqueues says what became of its job, from the endpoint's answer or from `/cloud/get_job_state` (`utils/job_queue.js`, `announceJob`): "Started: <type>", "Queued behind <type>" or "Queued", never a "queued" that only meant "asked for". It opens no tab: the job's log opens from the job lists.

### Unsaved changes

Forms track dirtiness by comparing a JSON snapshot of the form state against the last saved snapshot. A shared nav-guard hook wires that into both the browser `beforeunload` event and the SPA's internal navigation (confirm dialog before route changes).

### Log pages

The job log terminal (`/cloud/log/<id>`) and the container log viewer (`/cloud/instance/<id>/logs`) are server-rendered pages opened in a new tab, not SPA routes. The job terminal streams over WebSocket with a 10 s polling fallback; the container viewer polls every 4 s.
