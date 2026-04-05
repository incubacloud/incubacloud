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
│  │  /cloud/ui      │◄──►│   /cloud/*               │   │
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
│   ├── data_load.py       # ~45 JSONRPC endpoints (CRUD, GitHub, backups)
│   ├── main.py            # HTTP routes (SPA, log terminal, restore upload)
│   ├── connect.py         # Instance user list + session injection
│   ├── github_webhook.py  # Webhook receiver (HMAC-SHA256 validated)
│   └── github_setup.py    # GitHub App manifest flow
├── github/
│   ├── client.py          # GitHub API client (App token + PAT)
│   ├── credentials.py     # GitHubAppCredentials dataclass
│   ├── jwt_utils.py       # RS256 JWT generation (no PyJWT dependency)
│   ├── token_cache.py     # In-memory installation token cache
│   └── webhook_utils.py   # HMAC-SHA256 signature validation
├── models/
│   ├── abstract_executor.py          # SSH executor base class
│   ├── cloud_host.py
│   ├── cloud_project.py
│   ├── cloud_instance.py
│   ├── cloud_job.py
│   ├── cloud_backup_backend.py
│   ├── cloud_alert.py
│   ├── cloud_github_app.py
│   ├── cloud_github_event.py
│   ├── *_executor.py                 # One file per job type
│   └── executor_registry.py          # Maps job type codes → executor classes
├── static/src/
│   ├── app/
│   │   ├── main.js                   # OWL app entry point
│   │   └── app.js                    # Router
│   └── components/
│       ├── toast/                   # Global toast notification service
│       └── <component>/              # One folder per OWL component
└── tests/
    └── test_*.py                     # Unit and integration tests
```

---

## Data model

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
  ├── cloud.instance.repo
  └── cloud.backup.backend (M2O, overrides project)

cloud.job
  ├── cloud.job.type
  ├── cloud.job.log.chunk
  └── cloud.job.log.message

cloud.backup.backend  (standalone, referenced by project and instance)
cloud.github.app      (singleton)
cloud.github.event    (webhook audit log)
```

### Key models

| Model | Description |
|---|---|
| `cloud.host` | VPS server — IP, SSH credentials, Traefik config, live metrics |
| `cloud.project` | Groups instances; holds default repos, dependencies, backup backend |
| `cloud.instance` | One Odoo deployment — version, DB config, SMTP, domain, backup backend |
| `cloud.job` | Async SSH execution record linked to `queue.job` |
| `cloud.job.type` | Job type definition (`code`, `apply_to`, icon) |
| `cloud.job.log.chunk` | Individual log line (`stdout`/`stderr`/`system`/`execution_guard`) |
| `cloud.backup.backend` | S3-compatible backup target — bucket, path, encrypted credentials |
| `cloud.alert` | Infrastructure alert on a host (active/dismissed) |
| `cloud.github.app` | Singleton — GitHub App credentials (app_id, private_key, webhook_secret) |
| `cloud.github.event` | Immutable webhook audit log |

---

## Job execution pattern

All remote operations are modelled as `cloud.job` records and executed asynchronously by an OCA `queue_job` worker.

### Lifecycle

```
1. API call (e.g. deploy_instance)
   → cloud.job.enqueue(host_id, instance_id, 'deploy_instance', payload)
   → Creates cloud.job record
   → Creates queue.job record (queue_job)

2. queue_job worker picks up queue.job
   → Calls cloud.job.execute()
   → Writes execution_guard log chunk (re-execution protection)
   → Looks up executor class in executor_registry
   → Instantiates AbstractSSHExecutor subclass
   → Calls executor.run()

3. AbstractSSHExecutor.run()
   → Opens asyncio event loop
   → Connects via asyncssh
   → Runs commands from get_commands()
   → Streams stdout/stderr into log buffer
   → Periodically flushes buffer to cloud.job.log.chunk
   → Notifies frontend via PostgreSQL bus (cloud_jobs channel)
   → Calls on_success() or on_failure() in fresh DB cursor

4. Frontend (OWL)
   → Listens on bus channel cloud_jobs
   → On notification: fetches job state + last system message
   → Log terminal: polls /cloud/log/<job_id> for stdout/stderr
```

### Implementing a new executor

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
| `execution_guard` | Internal — prevents zombie re-execution |

### Transaction model

The `queue_job` worker holds a **serializable cursor** with `FOR NO KEY UPDATE` on the `queue.job` row for the duration of execution. All DB operations inside an executor must use **fresh cursors** (read-committed) to avoid deadlocks and prevent the lock from being released prematurely:

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

The clone requires terminating active connections first (`pg_terminate_backend`). Odoo reconnects automatically.

## Automatic module updates

On rebuild, `click-odoo-update` (from `click-odoo-contrib`, included in the doodba base image) detects which modules changed by comparing file checksums:

- Checksums are stored in `ir_config_parameter` key `module_auto_update.installed_checksums` (JSON)
- On first deploy: `click-odoo-update --only-compute-hashes` establishes the baseline
- On rebuild: `click-odoo-update` compares checksums and updates only changed modules
- This replaces the old `-i incubacloud_connect` / `-u all` approach

## Data race prevention

Deploy and rebuild endpoints check for running jobs before enqueuing:

```python
running_job = env['cloud.job'].search([
    ('instance_id', '=', instance.id),
    ('state', 'in', ('started', 'pending', 'enqueued')),
], limit=1)
```

If a job is active, the endpoint returns `{ok: false, error: "A job is already running: ..."}`.

---

## Secret management

Sensitive fields use a custom `EncryptedChar` field type backed by Fernet symmetric encryption. The encryption key is stored as an environment variable (`INCUBACLOUD_ENCRYPTION_KEY`). If no key is set, values are stored in plaintext (development only).

**Encrypted fields:**

| Model | Field |
|---|---|
| `cloud.host` | `password`, `traefik_panel_password` |
| `cloud.instance` | `odoo_admin_password`, `postgres_password`, `smtp_relay_password` |
| `cloud.backup.backend` | `s3_secret_access_key`, `passphrase` |

The frontend cannot read encrypted values directly. The endpoint `/cloud/get_secret` exposes them only for whitelisted `(model, field)` pairs after verifying write access.

---

## GitHub integration

IncubaCloud supports GitHub App authentication for private repository access and receives webhooks for installation events.

### Authentication flow

```
1. User opens Settings → GitHub App
2. GET /cloud/github/setup
   → Generates App manifest
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

### Webhook handling

All webhook events are stored in `cloud.github.event` (immutable audit log) regardless of whether they are acted upon. Only `installation.created` triggers active logic (auto-populates `installation_id`).

---

## Frontend SPA

The UI is an **OWL single-page application** served from `/cloud/ui`. It uses a custom router (`app.js`) with hash-free URL routing.

### Key routes

| Route | Path | Description |
|---|---|---|
| `dashboard` | `/cloud/ui` | Counts, alerts, recent jobs |
| `projects` | `/cloud/ui/projects` | Project list |
| `project_detail` | `/cloud/ui/projects/:id` | Edit project |
| `hosts` | `/cloud/ui/hosts` | Host list |
| `host_detail` | `/cloud/ui/hosts/:id` | Edit host |
| `instance_detail` | `/cloud/ui/projects/:id/instances/:instance_id` | Edit instance |
| `jobs_history` | `/cloud/ui/jobs` | Job history |
| `settings` | `/cloud/ui/settings` | GitHub App + global defaults |
| `backup_backends` | `/cloud/ui/backup-backends` | S3 backend list |
| `backup_backend_detail` | `/cloud/ui/backup-backends/:id` | Edit backend |

The router is extensible: private modules can register additional routes with `registerRoute(name, parseFn, buildFn)`.

### Real-time updates

Job state changes are pushed to the frontend via the Odoo bus (`cloud_jobs` channel). The executor sends a notification after every flush of log chunks. The job drawer and job history components subscribe to this channel and re-fetch state on each message.

### Toast notifications

A global notification service available to all components via `this.env.toast`:

```javascript
this.env.toast?.success("Instance deployed");
this.env.toast?.error("Deploy failed: " + error);
this.env.toast?.warning("Unsaved changes");
this.env.toast?.info("Refreshing backups...");
```

The service is implemented as a singleton (`ToastContainer`) wired through `useSubEnv` in `app.js`. The OWL `env` is frozen — never mutate it directly.

### Unsaved changes

Instance and project forms track modifications via a JSON snapshot comparison. The browser's `beforeunload` event warns users before navigating away with unsaved changes.
