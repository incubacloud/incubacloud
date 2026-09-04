# API Endpoints

All endpoints are defined in `incubacloud/controllers/`. Unless noted, endpoints are JSON-RPC (`type="jsonrpc"`, the Odoo 19 dispatcher), require `auth="user"` (logged-in internal Odoo session) and are called via POST. The SPA additionally calls a few `cloud.job` model methods through the standard Odoo ORM RPC (`/web/dataset/call_kw`) — see [Jobs](#jobs-and-audit-log).

Access control is layered:

1. **Record rules** scope what each user can see (project membership).
2. **Role gates** inside each endpoint enforce the 5-tier RBAC before acting.
3. A few sensitive reads have an extra **field whitelist** (see [Secrets](#secrets)).

## Role gates

Groups form a hierarchy (each implies the previous): `user` < `consultant` < `project_manager` < `developer` < `manager`. Gates used in the tables below:

| Gate | Required group |
|---|---|
| manage hosts / manage settings | `group_cloud_manager` |
| deploy / create instance | `group_cloud_consultant` |
| terminal / view logs / manage backups / clone to staging / export | `group_cloud_developer` |
| create project | `group_cloud_project_manager` |
| delete project | `group_cloud_manager` |
| delete instance | manager if production, consultant otherwise |
| connect as user | `group_cloud_user` |

`sudo()`/superuser bypasses all gates. "record rules" in the tables means no explicit gate — visibility is delegated to record rules.

---

## Health and configuration

| Path | Gate | Description |
|---|---|---|
| `/cloud/ping` | none | Liveness check, returns `{response: "pong"}`. |
| `/cloud/get_config` | none | Feature flags, permissions and global settings for the SPA. |
| `/cloud/get_dashboard` | record rules | Counts (hosts, projects, instances), active alerts, recent jobs. |
| `/cloud/get_langs` | none | Available Odoo language codes and names. |
| `/cloud/get_odoo_versions` | none | Supported Odoo versions (7.0 – 19.0). |
| `/cloud/global_search` | record rules | Cross-entity search (projects, instances, hosts). |
| `/cloud/get_users` | project manager | Internal users (for member pickers). |
| `/cloud/get_tags` | none | All tags with id, name, color. |
| `/cloud/create_tag` | consultant | Create tag; color auto-assigned. |

`GET /cloud/health` is a separate **public** HTTP endpoint (DB liveness probe returning 200/503, rate-limited per IP) intended for external uptime monitors.

## Hosts

| Path | Gate | Description |
|---|---|---|
| `/cloud/get_hosts` | none¹ | List hosts with metrics and instance count. |
| `/cloud/get_host` | manage hosts | Full host detail including Traefik config and alerts. |
| `/cloud/host_defaults` | manage hosts | Defaults for the new-host form. |
| `/cloud/create_host` | manage hosts | Create host (field whitelist enforced). |
| `/cloud/save_host` | manage hosts | Update host; also syncs whitelist entries. |
| `/cloud/delete_host` | manage hosts | Archive or delete host. |
| `/cloud/trust_host_key` | manage hosts | Pin the host's SSH key (TOFU confirmation). |
| `/cloud/setup_whitelist` | manage hosts | Enqueue whitelist setup SSH job. |
| `/cloud/get_host_instances` | record rules | Instances deployed on a host. |
| `/cloud/browse_host_dir` | manage hosts | Browse a remote directory (path-safety validated). |
| `/cloud/import_host_instance` | manage hosts | Import an instance already running on the host (reads `.copier-answers.yml`, `repos.yaml`, compose files). |

¹ SSH-related fields are redacted from the response unless the caller is a manager.

## Projects

| Path | Gate | Description |
|---|---|---|
| `/cloud/get_projects` | record rules | Paginated project list (`{items, total, truncated, limit}`). |
| `/cloud/get_project` | record rules | Project detail (repos, dependencies, backup backend). |
| `/cloud/get_project_full` | record rules | Project + instances + jobs in one payload (SPA store). |
| `/cloud/create_project` | create project | Create project (field whitelist enforced). |
| `/cloud/save_project` | consultant | Update project; repos replaced by the `repos` array. |
| `/cloud/delete_project` | delete project | Delete project (fails if instances exist). |
| `/cloud/get_project_instances` | record rules | Instances in a project. |
| `/cloud/import_project` | create instance | Import a project from a Git URL (doodba or Odoo.sh layout; SSRF host allowlist). |

## Instances

| Path | Gate | Description |
|---|---|---|
| `/cloud/get_instance` | record rules | Full instance detail (Odoo config, DB, SMTP, repos, domains). |
| `/cloud/create_instance` | create instance | Create instance with repos. May return `{blocked: true, reason}` (`pip_conflict`, `host_required`, `no_host`). |
| `/cloud/save_instance` | consultant | Update instance (field whitelist enforced). |
| `/cloud/delete_instance` | delete instance | Tear down and delete/archive the instance. |
| `/cloud/deploy_instance` | deploy | Enqueue `deploy_instance` job. |
| `/cloud/rebuild_instance` | deploy | Enqueue `rebuild_instance` job (safe rebuild with boot test). |
| `/cloud/clone_to_staging` | clone to staging | Clone production instance to a staging (DB + config). |
| `/cloud/move_instance` | manage hosts | Move an instance to another host (chained jobs: deploy → quiesce → fresh backup → restore → cutover → cleanup). |
| `/cloud/rollback_move` | manage hosts | Recover a move that failed before cutover (restarts the source). |
| `/cloud/resolve_pip_conflict` | consultant | Apply the chosen resolution for a Python dependency conflict. |
| `/cloud/resolve_addon_conflict` | consultant | Apply the chosen resolution for an addon conflict. |
| `/cloud/compare_sync` | consultant | Diff instance config vs. what is deployed on the host. |
| `/cloud/apply_sync` | consultant | Apply the config diff to the host. |
| `/cloud/fetch_container_logs` | view logs | Tail container logs for the log viewer page. |
| `/cloud/instance_log_archives` | view logs | List the daily Odoo log files kept on the host (name, size, mtime). |
| `/cloud/fetch_log_archive` | view logs | Read one archived day, with an optional fixed-string filter applied on the host. |
| `/cloud/search_log_archives` | view logs | Which archived days mention a term, with hit counts (bounded sweep on the host). Rate-limited; audited with the term. |
| `/cloud/instance/<id>/log_archive/<name>` | view logs | Download one archived day, gzipped (HTTP, not JSON-RPC). Rate-limited; audited. |

Deploy/rebuild endpoints include data-race prevention: if a job is already running for the instance, the response is an error (or a `{blocked: true, alert_id, alert_code, conflicts}` envelope when a blocking pip-conflict alert exists).

## Backups

| Path | Gate | Description |
|---|---|---|
| `/cloud/list_backups` | manage backups | Paginated backup list for an instance (`offset`/`limit`). |
| `/cloud/get_backup_result` | manage backups | Poll the outcome of a backup-related job. |
| `/cloud/create_backup` | manage backups | Enqueue backup creation job. |
| `/cloud/download_backup` | manage backups | Enqueue backup download job; returns attachment URL when ready. |
| `/cloud/download_backup_neutralized` | manage backups | Same, but neutralized (crons off, mail servers archived, credentials scrubbed). |
| `/cloud/restore_backup` | manage backups | Enqueue in-place restore job. |

Uploading an external ZIP goes through three plain-HTTP endpoints, all multipart, CSRF-protected and gated on *manage backups*:

| Path | Description |
|---|---|
| `POST /cloud/instance/<id>/restore/begin` | Open a staged upload; returns `{upload_id, max_bytes, chunk_bytes}`. Rate-limited per user (2/min). |
| `POST /cloud/instance/<id>/restore/part` | Append one piece (`upload_id`, `offset`, `chunk`). `offset` must equal the current size: a piece behind it is acknowledged without rewriting (safe retry), one past it is refused 409 (a hole no reader could detect). Capped at 40 MiB per request. |
| `POST /cloud/instance/<id>/restore/finish` | Close the upload and enqueue the restore. |

Pieces rather than one request because a CDN or reverse proxy in front of the panel caps a single body long before Odoo sees it — Cloudflare at 100 MB — so the one-shot route (`POST /cloud/instance/<id>/restore`, still accepted) answered 413 to anything larger. The total is capped by `incubacloud.restore_upload_max_bytes` (default 2 GiB), which is a disk budget: the rebuilt file waits under the data directory until the executor sends it.

Two JSON-RPC routes cover the archives that never pass through the panel: `cloud.instance.grant_restore_upload` (installs a one-use, directory-confined, expiring SSH key and returns the private half once) and `cloud.instance.restore_from_url` (has the host fetch it over `https`/`sftp`/`ftp`, with the address validated and pinned here). See [RB-19](runbooks/RB-19-restore-large-backup.md).

## Backup backends

| Path | Gate | Description |
|---|---|---|
| `/cloud/get_backup_backends` | record rules | List S3 backends (no secrets, truncation-capped). |
| `/cloud/get_backup_backend` | manage settings | Backend detail (never returns `s3_secret_access_key` / `passphrase`). |
| `/cloud/create_backup_backend` | manage settings | Create backend; passphrase and secret auto-generated if omitted. |
| `/cloud/save_backup_backend` | manage settings | Update backend; empty secret fields preserve the stored value. |
| `/cloud/delete_backup_backend` | manage settings | Delete backend. |
| `/cloud/test_backup_backend` | manage settings | Test S3 connectivity via boto3. |
| `/cloud/measure_backup_usage` | manage settings | Measure bucket usage (feeds quota alerts). |

## Secrets

| Path | Gate | Description |
|---|---|---|
| `/cloud/get_secret` | developer + whitelist + write access | Return the plaintext of one encrypted field. |

Allowed `(model, field)` pairs (`_SECRET_FIELDS`):

| Model | Fields |
|---|---|
| `cloud.host` | `password`, `traefik_panel_password` |
| `cloud.instance` | `odoo_admin_password`, `postgres_password`, `smtp_relay_password` |
| `cloud.backup.backend` | `s3_secret_access_key`, `passphrase` |
| `res.users` | `cloud_telegram_bot_token`, `cloud_webhook_secret` |

The caller must be in `group_cloud_developer`, the pair must be whitelisted, **and** the caller needs write access to the record. Anything else returns an error. Secrets are stored with `EncryptedChar` (Fernet) — see `docs/architecture.md`.

## Alerts

| Path | Gate | Description |
|---|---|---|
| `/cloud/get_alert_count` | record rules | Active alert count for the header badge. |
| `/cloud/get_alert_history` | record rules | Paginated alert history (`state_filter`, `level_filter`, `offset`, `limit`). |
| `/cloud/dismiss_alert` | consultant + write access | Dismiss one alert. |
| `/cloud/dismiss_all_alerts` | consultant | Dismiss all non-blocking visible alerts (`pip_conflict`/`addon_conflict` excluded). |

## Jobs and audit log

| Path | Gate | Description |
|---|---|---|
| `/cloud/get_job_brief` | read access | Minimal job state + last system message (used after bus events). |
| `/cloud/get_audit_log` | record rules | Paginated, filterable audit log (`instance_id` or `host_id`, `q`, `action_filter`, `date_from/to`, `offset`, `limit`). |
| `/cloud/purge_audit_logs` | manager | Purge audit rows older than the retention window. |

There are **no** dedicated `/cloud/*` routes for job history, log chunks or cancel/retry. The SPA calls `cloud.job` model methods through the standard ORM RPC instead: `load_history` (paginated history), `get_host_jobs`, `enqueue(host_id, instance_id, job_type_code)` (host-level actions such as `host_probe` and `full_setup`; role-gated server-side) and `cancel_job`. The job log terminal page consumes chunks over WebSocket with an HTTP polling fallback (see [Static pages](#static-pages-and-public-http-endpoints)).

## Notification preferences

| Path | Gate | Description |
|---|---|---|
| `/cloud/get_user_preferences` | own user | Notification prefs; secrets returned only as booleans (`cloud_telegram_configured`, `cloud_webhook_configured`). |
| `/cloud/save_user_preferences` | own user | Save prefs (level, mode, email toggle, muted projects, Telegram, webhook; webhook URL must be `https://`). |
| `/cloud/telegram_detect_chat_id` | own user | Poll the bot's `getUpdates` to auto-detect the chat id. |
| `/cloud/telegram_send_test` | own user | Send a test message to the configured chat. |

The outbound job-state webhook (payload, `X-IncubaCloud-Signature` HMAC header) is documented in `docs/architecture.md`.

## Platform settings

| Path | Gate | Description |
|---|---|---|
| `/cloud/get_general_settings` | manage hosts | Global defaults (singleton `cloud.settings`). |
| `/cloud/save_general_settings` | manage hosts | Update global defaults. |
| `/cloud/get_core_rate_limits` | manage hosts | Current admin-tunable rate limits. |
| `/cloud/save_core_rate_limits` | manage hosts | Update rate limits. |

## GitHub integration

App management (all gated on **manage settings**):

| Path | Description |
|---|---|
| `/cloud/get_github_app` | App status `{configured, has_installation, app_id}`. Private key never returned. |
| `/cloud/save_github_app` | Update App credentials (force-confirm on destructive overwrite). |
| `/cloud/save_github_pat` | Store a personal access token (fallback auth). |
| `/cloud/test_github_connection` | Validate current credentials against the GitHub API. |
| `/cloud/reset_github_app` | Clear the stored App. |
| `/cloud/detect_github_installation` | Detect/refresh the installation id. |

Repository operations (all gated on **consultant**):

| Path | Description |
|---|---|
| `/cloud/get_repo_branches` | Branch list (App token or PAT). |
| `/cloud/get_branch_head` | Head commit of a branch. |
| `/cloud/get_repo_modules` | Odoo addon names in a repository. |
| `/cloud/get_repo_requirements` | `requirements.txt` content. |
| `/cloud/fetch_odoojs_submodules` | `.gitmodules` for the Odoo.sh import wizard (SSRF host allowlist). |
| `/cloud/freeze_repo` / `/cloud/unfreeze_repo` | Toggle auto-rebuild freeze on one repo. |
| `/cloud/freeze_all_repos` / `/cloud/unfreeze_all_repos` | Bulk freeze/unfreeze (record-rule scoped). |

## Terminal

| Path | Gate | Description |
|---|---|---|
| `/cloud/terminal/open` | terminal + rate limits | Spawn a PTY subprocess for an instance shell; returns session id. |
| `/cloud/terminal/<sid>/output` | session owner | Poll output chunks. |
| `/cloud/terminal/<sid>/input` | session owner | Send base64 keystrokes. |
| `/cloud/terminal/<sid>/resize` | session owner | Resize the PTY. |
| `/cloud/terminal/<sid>/close` | session owner | Close the session. |

`GET /cloud/terminal/<sid>` (HTTP) renders the xterm.js page; only the session's owner may load it.

## Instance connect

| Path | Gate | Description |
|---|---|---|
| `/cloud/get_instance_users` | connect as user + read access | List the instance's internal Odoo users (via docker exec). |
| `/cloud/prepare_instance_connect` | connect as user + read access | Inject a one-time session token, return the `/ic/login` redirect URL. |

## Static pages and public HTTP endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/cloud`, `/cloud/<path>` | user | SPA shell (OWL app); deep links land on the same handler. |
| GET | `/cloud/log/<job_id>` | user | Job log terminal page. Live chunks over **WebSocket**, with a 10 s HTTP polling fallback. |
| GET | `/cloud/log/<job_id>/download` | user | Download the job log as a `.log` file. |
| GET | `/cloud/instance/<id>/logs` | user (view logs) | Container log viewer (polls `/cloud/fetch_container_logs` every 4 s). |
| POST | `/cloud/instance/<id>/restore` | user (manage backups) | Upload a backup ZIP (≤ 2 GiB) and enqueue restore. Rate-limited. |
| GET | `/cloud/terminal/<sid>` | user (owner) | xterm.js terminal page. |
| GET | `/cloud/health` | **public** | DB liveness probe → 200/503 (429 when rate-limited). |
| POST | `/cloud/github/webhook` | **public** | GitHub App webhook receiver. HMAC-SHA256 signature required, per-IP rate limit, replay protection by delivery id. |
| GET | `/cloud/github/setup` | user (manage settings) | Start GitHub App creation via manifest flow. |
| GET | `/cloud/github/callback` | user (manage settings) | Exchange the manifest code for credentials (state token, 10 min TTL). |

All SPA/log/terminal pages additionally require an **internal** user (portal/public users get 404).

## Rate limiting

DB-backed tumbling counters (`cloud.rate.limit`), 60-second windows, atomic upserts. The admin-tunable caps live in Settings → Rates:

| Endpoint | Scope | Default | Tunable |
|---|---|---|---|
| `POST /cloud/instance/<id>/restore` | per user | 2/min | no |
| `GET /cloud/health` | per IP | 60/min | no |
| `POST /cloud/github/webhook` | per IP | 300/min | yes |
| `/cloud/terminal/open` | per user | 10/min | yes |
| `/cloud/terminal/open` | per instance | 30/min | yes |

Exceeding a limit returns HTTP 429 (HTTP endpoints, webhook adds `Retry-After: 60`) or `{ok: false, error}` (JSON-RPC).

## Error handling

Application errors use a consistent envelope:

```json
{"ok": false, "error": "Human-readable error message"}
```

Unexpected exceptions are logged with a correlation id and returned as `"<message> (ref: <id>)"`; `UserError`/`ValidationError`/`AccessError` messages pass through verbatim.

Gated create/deploy flows can return a **blocked** envelope instead:

```json
{"blocked": true, "reason": "pip_conflict", "message": "..."}
```

Success responses are **not** uniformly wrapped: mutating endpoints generally return `{"ok": true, ...}`, while read endpoints return bare data objects or lists (e.g. `get_projects` → `{items, total, truncated, limit}`, `get_secret` → `{value}`).

Plain-HTTP endpoints use real status codes instead of the JSON-RPC envelope: the restore upload returns `{"error": ...}` with 400/403/404/429/500 or `{"job_id": ...}` on success; `/cloud/health` returns `{"status": "ok"|"down"|"rate_limited"}`; the GitHub webhook responds with plain text.
