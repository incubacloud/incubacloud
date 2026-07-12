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
| `cloud.job` | Async SSH execution record linked to `queue.job` |
| `cloud.job.type` | Job type definition (`code`, `apply_to`, icon) |
| `cloud.job.log.chunk` | Individual log line (`stdout`/`stderr`/`system`) |
| `cloud.backup.backend` | S3-compatible backup target — bucket, path, encrypted credentials, quota alerts |
| `cloud.alert` | Infrastructure alert (active/dismissed, warning/critical, dedup by code) |
| `cloud.audit.log` | Who-did-what trail with configurable retention |
| `cloud.settings` | Singleton — global defaults, GitHub PAT, rate-limit caps |
| `cloud.rate.limit` | Tumbling per-minute counters backing all rate limits |
| `cloud.github.app` | Singleton — GitHub App credentials (app_id, private_key, webhook_secret) |
| `cloud.github.event` | Immutable webhook audit log (drives auto-rebuilds) |
| `cloud.instance.pending.push` | Pushes awaiting a coalesced auto-rebuild |
| `cloud.terminal.route` | Terminal subprocess routing (encrypted auth token, owner) |
| `cloud.connect.token` | One-time session token for connect-as |

Supporting models: `cloud.project.repo`, `cloud.instance.repo`, `cloud.instance.domain`, `cloud.instance.backup`, `cloud.instance.session`, `cloud.tag`, `cloud.host.tag`, `cloud.instance.tag`, `cloud.host.whitelist`, `cloud.github.credential.service`, `cloud.security.mixin` (abstract). `res.users` is extended with notification preferences; `queue.job` with the state bridge.

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
   → Running-job guard (per instance / per host)
   → Creates cloud.job + queue.job records

2. queue_job worker picks up queue.job
   → Calls cloud.job.execute()
   → Takes a PostgreSQL advisory lock (per instance, or per host for
     host-level jobs) so concurrent workers cannot double-run
   → Looks up the executor class in the registry
   → Calls executor.run()

3. AbstractSSHExecutor.run()
   → Opens asyncio event loop, connects via asyncssh
   → Runs commands from get_commands()
   → Streams stdout/stderr into a buffer, periodically flushed to
     cloud.job.log.chunk (fresh cursor)
   → Notifies the frontend via the PostgreSQL bus (cloud_jobs channel)
   → Calls on_success() or on_failure() in a fresh DB cursor

4. Frontend (OWL)
   → Listens on bus channels cloud_jobs / cloud_overview
   → On job events: fetches /cloud/get_job_brief and toasts the outcome
   → Log terminal page: WebSocket stream with HTTP polling fallback
```

### Reliability guarantees

- **Serialization** — two advisory-lock namespaces (instance-scoped and host-scoped) plus a running-job guard at enqueue time prevent concurrent jobs on the same target. Monitoring job types (`host_metrics`, `docker_prune`, `instance_health`) are *hidden*: they skip the guard and the UI.
- **Transient connection retry** — executors that opt in retry SSH connection failures up to 3 attempts (30 s backoff via `RetryableJobError`); only the final failure raises a critical `host_unreachable` alert.
- **Chains** — `enqueue_chain(steps)` builds an OCA DelayableChain. Payloads may reference earlier steps with `__chain_job_N__` placeholders (0-indexed), resolved to real job ids at enqueue. When a step fails, dependent jobs stuck in `wait_dependencies` are cancelled at both the `cloud.job` and `queue.job` level.
- **Cancel** — not-yet-started jobs are cancelled through the queue; started jobs get a cooperative cancel flag that the executor polls between commands on a separate cursor.
- **Cross-host move** — `cloud.instance.move_to_host()` chains deploy(target) → stop `odoo` only (source) → fresh backup → download → restore(target) → cutover → cleanup(source). `host_id` flips only on successful cutover; a watchdog cron recovers instances stranded by a chain that died before cutover (restarts the source, raises a `move_stuck` alert), and `rollback_move` does the same on demand.

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
        instance = self.instance  # cloud.instance record
        instance.write({'deployed': True})

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

The `queue_job` worker holds a cursor with a lock on the `queue.job` row for the duration of execution. All DB operations inside an executor must use **fresh cursors** to avoid deadlocks:

```python
# Correct pattern inside an executor:
with self.job.env.registry.cursor() as cr:
    env = self.job.env(cr=cr)
    env['cloud.instance'].browse(instance_id).write({...})
    cr.commit()

# WRONG — never use self.job.env.cr directly for writes
```

Bus notifications must also be sent from fresh cursors, but only **during** execution. PostgreSQL `NOTIFY` is committed automatically with the transaction.

## Safe rebuild

Rebuilds verify the new Docker image before applying it to the running instance. The flow:

1. `docker compose build --pull --no-cache odoo` — builds the new image (`stop_on_failure`)
2. Clone the production DB via `createdb -T` (instant copy using PG hard-links)
3. Run `click-odoo-update --database __ic_boot_test` against the clone
4. Drop the test DB (`dropdb --if-exists __ic_boot_test`) — always runs, even on failure
5. If the test passed, update the real DB and restart with `docker compose up -d`
6. If the test failed, `stop_on_failure` aborts — the instance keeps the old image

The clone requires terminating active connections first (`pg_terminate_backend`). Odoo reconnects automatically. The boot test runs against the instance's own Postgres image (pgvector-enabled), so extension-dependent modules pass.

## Automatic module updates

On rebuild, `click-odoo-update` (from `click-odoo-contrib`, included in the doodba base image) detects which modules changed by comparing file checksums:

- Checksums are stored in `ir_config_parameter` key `module_auto_update.installed_checksums` (JSON)
- On first deploy: `click-odoo-update --only-compute-hashes` establishes the baseline
- On rebuild: `click-odoo-update` compares checksums and updates only changed modules

## Data race prevention

Deploy and rebuild endpoints check for running jobs before enqueuing. If a job is active, the endpoint returns `{ok: false, error: "A job is already running: ..."}` (or a `{blocked: true, ...}` envelope when a blocking conflict alert exists).

---

## Secret management

Sensitive fields use the custom `EncryptedChar` field type (a `fields.Char` subclass) backed by Fernet symmetric encryption. The database column always stores the `enc:<token>` form; Python code always sees plaintext.

- The key comes from the **`INCUBACLOUD_SECRET_KEY`** environment variable, which **must** be set in every environment. There is no fallback and no plaintext mode: a missing or invalid key makes encrypt/decrypt raise (`IncubacloudCryptoError`) instead of silently writing plaintext.
- **Key rotation** is built in: the variable accepts a comma-separated list of Fernet keys (MultiFernet). The first key encrypts new values; every listed key can decrypt. See `docs/runbooks/RB-01-rotate-secret-key.md` for the full rotation procedure.
- Legacy plaintext values (no `enc:` prefix) pass through unchanged on read, which is what migration code relies on.

**Encrypted fields (14 across 7 models):**

| Model | Fields |
|---|---|
| `cloud.host` | `password`, `key_file`, `traefik_panel_password` |
| `cloud.instance` | `odoo_admin_password`, `odoo_admin_user_password`, `postgres_password`, `smtp_relay_password` |
| `cloud.backup.backend` | `s3_secret_access_key`, `passphrase` |
| `cloud.github.app` | `webhook_secret` |
| `cloud.settings` | `github_pat` |
| `cloud.terminal.route` | `auth_token` |
| `res.users` | `cloud_telegram_bot_token`, `cloud_webhook_secret` |

The frontend cannot read encrypted values directly. `/cloud/get_secret` exposes a whitelisted subset (see `docs/api-endpoints.md`) to developers with write access on the record.

---

## Security model

- **5-tier RBAC**: Stakeholder (read-only) → Consultant → Project Manager → Developer → Administrator, implemented as nested groups. `cloud.security.mixin` centralises the gate helpers (`_check_can_deploy`, `_check_can_manage_hosts`, …) used by every endpoint and by server-side `cloud.job` enqueue gating.
- **Record rules** scope projects/instances/jobs/alerts to project members; global alerts are hidden from the lower tiers.
- **Audit trail**: `cloud.audit.log` records who did what, filterable per instance/host, with configurable retention and a manager-only purge.
- **Rate limiting**: DB-backed counters protect the public endpoints (webhook, health), the restore upload and terminal opens. Caps are tunable in Settings → Rates.
- **Web terminal**: PTY subprocesses are isolated per session with an encrypted routing token; only the owning user can attach. Idle sessions are reaped (see RB-03).

---

## GitHub integration

IncubaCloud supports GitHub App authentication for private repository access and receives webhooks for installation and push events. A stored PAT (in `cloud.settings`) is the fallback when no App is configured.

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

`push` events drive **auto-rebuilds**: an instance rebuilds when it is active, deployed, has `auto_rebuild` enabled and its host is active. Rebuilds respect a cooldown, defer while a blocking job is running (the push is recorded in `cloud.instance.pending.push` and coalesced), and honour per-repo **freeze** flags. Archived instances and archived hosts never auto-rebuild.

---

## Alerts and notifications

### Alerts

`cloud.alert` rows target a host, instance or project (or none = global) with a machine `code`, a `warning`/`critical` level and an `active`/`dismissed` state. One active alert per (target, code): creators re-use the existing row. Health and metrics executors **auto-resolve** their alerts when the condition clears, and a job success dismisses stale `job_failed` alerts for the same type and target. Dismissed alerts are garbage-collected after 60 days (cron); active ones never expire.

Alert codes in use: `job_failed`, `disk_critical`, `host_unreachable`, `instance_down`, `instance_unresponsive`, `instance_high_cpu`, `instance_high_memory`, per-service container-down codes, `instance_error_logs`, `pip_conflict`, `addon_conflict`, `move_stuck`, `secret_rotation_stranded:<field>`. `pip_conflict`/`addon_conflict` are *actionable*: they block re-enqueue of the affected operation and are excluded from bulk dismiss.

### Notification channels

Job terminal states (`done`/`failed`) fan out from the `queue.job` state bridge to four channels. Recipients are always **active internal users filtered by record-rule visibility** and their own preferences (level `all`/`failures`/`none`, muted projects):

| Channel | Enabled by | Notes |
|---|---|---|
| Bus (real-time) | always | `cloud_jobs` (job events → toasts) and `cloud_overview` (alert badge + critical alert toasts) |
| Email | per-user toggle | Immediate or **daily digest** (07:00 cron, QWeb template, watermark per user). Severe failures (deploy/rebuild) bypass digest mode. |
| Telegram | bot token + chat id set | Markdown message via the Bot API; chat-id auto-detect and test-message endpoints in the UI |
| Webhook (HMAC) | HTTPS URL set | JSON POST per state change, optionally signed |

Webhook payload:

```json
{"event": "job_state_change", "job_id": 123, "job_name": "Deploy Instance",
 "state": "failed", "severe": true, "host": "host-1", "instance": "prod",
 "log_url": "https://…/cloud/log/123", "timestamp": "2026-07-12 10:00:00"}
```

When a secret is configured the request carries `X-IncubaCloud-Signature: sha256=<hex>` — HMAC-SHA256 of the raw body. Verify with a constant-time compare. No secret → no header.

A separate email alert covers **backup bucket usage** (quota threshold per backend, 7-day debounce), outside the alert panel.

---

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

### Unsaved changes

Forms track dirtiness by comparing a JSON snapshot of the form state against the last saved snapshot. A shared nav-guard hook wires that into both the browser `beforeunload` event and the SPA's internal navigation (confirm dialog before route changes).

### Log pages

The job log terminal (`/cloud/log/<id>`) and the container log viewer (`/cloud/instance/<id>/logs`) are server-rendered pages opened in a new tab, not SPA routes. The job terminal streams over WebSocket with a 10 s polling fallback; the container viewer polls every 4 s.
