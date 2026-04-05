# API Endpoints

All endpoints are defined in `incubacloud/controllers/`. Unless noted, endpoints use JSON-RPC 2.0 (`type="json"`) and require `auth="user"` (logged-in Odoo session).

---

## Health

| Method | Path | Description |
|---|---|---|
| JSONRPC | `/cloud/ping` | Returns `{"ok": true}`. Use to verify session is alive. |

---

## Configuration

| Method | Path | Description |
|---|---|---|
| JSONRPC | `/cloud/get_config` | Feature flags and global settings for the SPA. |
| JSONRPC | `/cloud/get_dashboard` | Counts (hosts, projects, instances), active alerts, recent jobs. |
| JSONRPC | `/cloud/get_langs` | Available Odoo language codes and names. |

---

## Tags

| Method | Path | Body | Description |
|---|---|---|---|
| JSONRPC | `/cloud/get_tags` | — | All tags with id, name, color. |
| JSONRPC | `/cloud/create_tag` | `{name}` | Create tag. Color is auto-assigned. |

---

## Hosts

| Method | Path | Body | Description |
|---|---|---|---|
| JSONRPC | `/cloud/get_hosts` | — | List all hosts with metrics and instance count. |
| JSONRPC | `/cloud/get_host` | `{id}` | Full host detail including Traefik config and alerts. |
| JSONRPC | `/cloud/create_host` | `{name, ip_address, port, user, login_type, ...}` | Create host. |
| JSONRPC | `/cloud/save_host` | `{id, ...fields}` | Update host. Also syncs `whitelist_ids`. |
| JSONRPC | `/cloud/delete_host` | `{id}` | Archive or delete host. |
| JSONRPC | `/cloud/setup_whitelist` | `{id}` | Enqueue whitelist setup SSH job. |
| JSONRPC | `/cloud/get_host_instances` | `{host_id}` | Instances deployed on a host. |
| JSONRPC | `/cloud/dismiss_alert` | `{alert_id}` | Mark alert as dismissed. |

---

## Projects

| Method | Path | Body | Description |
|---|---|---|---|
| JSONRPC | `/cloud/get_projects` | — | List all projects with instance count and status. |
| JSONRPC | `/cloud/get_project` | `{id}` | Full project detail (repos, dependencies, backup backend). |
| JSONRPC | `/cloud/save_project` | `{id, ...fields}` | Update project. Repos are replaced by the `repos` array. |
| JSONRPC | `/cloud/delete_project` | `{id}` | Delete project (fails if instances exist). |
| JSONRPC | `/cloud/get_project_instances` | `{project_id}` | Instances in a project. |

---

## Instances

| Method | Path | Body | Description |
|---|---|---|---|
| JSONRPC | `/cloud/get_instance` | `{id}` | Full instance detail (Odoo config, DB, SMTP, repos). |
| JSONRPC | `/cloud/create_instance` | `{project_id, name, environment, host_id, ...}` | Create instance with repos. |
| JSONRPC | `/cloud/save_instance` | `{id, ...fields}` | Update instance. |
| JSONRPC | `/cloud/delete_instance` | `{id}` | Delete instance record. |
| JSONRPC | `/cloud/deploy_instance` | `{id}` | Enqueue `deploy_instance` SSH job. |
| JSONRPC | `/cloud/rebuild_instance` | `{instance_id}` | Enqueue `rebuild_instance` SSH job. Blocked if a job is already running. |
| JSONRPC | `/cloud/clone_to_staging` | `{instance_id, staging_name}` | Clone production instance to staging (DB + config). |
| JSONRPC | `/cloud/list_backups` | `{instance_id, refresh?}` | List backups for instance. If `refresh=true`, enqueues a refresh job. |
| JSONRPC | `/cloud/create_backup` | `{instance_id}` | Enqueue backup creation job. |
| JSONRPC | `/cloud/download_backup` | `{instance_id, backup_time}` | Enqueue backup download job. Returns attachment URL when ready. |
| JSONRPC | `/cloud/restore_backup` | `{instance_id, backup_time}` | Enqueue backup restore job (production only). |

---

## Backup Backends

| Method | Path | Body | Description |
|---|---|---|---|
| JSONRPC | `/cloud/get_backup_backends` | — | List all S3 backends (no secrets). |
| JSONRPC | `/cloud/get_backup_backend` | `{id}` | Backend detail (no `s3_secret_access_key` or `passphrase`). |
| JSONRPC | `/cloud/create_backup_backend` | `{name, s3_bucket, s3_path, s3_access_key_id, ...}` | Create backend. Passphrase and secret key auto-generated if omitted. |
| JSONRPC | `/cloud/save_backup_backend` | `{id, ...fields}` | Update backend. Empty secret fields preserve the existing value. |
| JSONRPC | `/cloud/delete_backup_backend` | `{id}` | Delete backend. |
| JSONRPC | `/cloud/test_backup_backend` | `{id}` | Test S3 connectivity via boto3. Returns `{ok, error}`. |

---

## Secrets

| Method | Path | Body | Description |
|---|---|---|---|
| JSONRPC | `/cloud/get_secret` | `{model, field, record_id}` | Returns the plaintext value of an encrypted field. |

Allowed `(model, field)` pairs:

| Model | Field |
|---|---|
| `cloud.host` | `password` |
| `cloud.host` | `traefik_panel_password` |
| `cloud.instance` | `odoo_admin_password` |
| `cloud.instance` | `postgres_password` |
| `cloud.instance` | `smtp_relay_password` |
| `cloud.backup.backend` | `s3_secret_access_key` |
| `cloud.backup.backend` | `passphrase` |

The caller must have **write access** to the record. Requests for non-whitelisted fields return an error.

---

## Jobs

| Method | Path | Body | Description |
|---|---|---|---|
| JSONRPC | `/cloud/get_jobs` | `{limit?, offset?}` | Paginated job history. |
| JSONRPC | `/cloud/get_job` | `{id}` | Job detail with state, host, instance, timestamps. |
| JSONRPC | `/cloud/load_job_chunks` | `{id, after_id?}` | Log chunks for job (excludes `execution_guard`). Use `after_id` for incremental polling. |
| JSONRPC | `/cloud/cancel_job` | `{id}` | Cancel a pending or started job. |
| JSONRPC | `/cloud/retry_job` | `{id}` | Re-enqueue a failed or cancelled job. |

---

## GitHub Integration

| Method | Path | Body | Description |
|---|---|---|---|
| JSONRPC | `/cloud/get_github_app` | — | GitHub App status. Returns `{configured, has_installation, app_id}`. Private key is never returned. |
| JSONRPC | `/cloud/save_github_app` | `{app_id, private_key?, webhook_secret?, github_pat?}` | Update GitHub App credentials. |
| JSONRPC | `/cloud/test_github_connection` | — | Call GitHub API to validate current credentials. |
| JSONRPC | `/cloud/get_repo_branches` | `{repo_url}` | Fetch branch list from GitHub (uses App token or PAT). |
| JSONRPC | `/cloud/get_repo_modules` | `{repo_url, branch}` | Fetch Odoo addon module names from repository. |
| JSONRPC | `/cloud/get_repo_requirements` | `{repo_url, branch}` | Fetch `requirements.txt` content. |
| JSONRPC | `/cloud/fetch_odoojs_submodules` | `{repo_url, branch}` | Fetch `.gitmodules` for odoo.sh import wizard. |

---

## GitHub Webhooks

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/cloud/github/webhook` | public (HMAC-SHA256) | Receive GitHub App webhooks. Validates `X-Hub-Signature-256` header. Returns 200 or 401. |
| GET | `/cloud/github/setup` | user | Start GitHub App creation via manifest flow. |
| GET | `/cloud/github/callback` | user | Handle GitHub callback after app creation (exchanges code for credentials). |

---

## Instance Connect

| Method | Path | Body | Description |
|---|---|---|---|
| JSONRPC | `/cloud/get_instance_users` | `{instance_id}` | List active Odoo users in a running instance (via SSH + psycopg2). |
| JSONRPC | `/cloud/prepare_instance_connect` | `{instance_id, user_id}` | Inject a pre-auth session token into the instance and return a redirect URL. |

---

## Static pages

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/cloud/ui` | user | Main SPA (OWL app). |
| GET | `/cloud/ui/<path>` | user | Deep-link entry point for any SPA route. |
| GET | `/cloud/log/<int:job_id>` | user | Log terminal page (polls chunks via JSONRPC every 600ms). |
| GET | `/cloud/instance/<int:instance_id>/logs` | user | Container log viewer for a running instance. |
| POST | `/cloud/instance/<int:instance_id>/restore` | user | Upload and restore a backup ZIP to an instance. |

---

## Error handling

All application-level endpoints return a consistent JSON envelope:

**Success:**
```json
{"ok": true, "data": {...}}
```

**Error:**
```json
{"ok": false, "error": "Human-readable error message"}
```

**Blocked (conflict):**
```json
{"blocked": true, "reason": "pip_conflict", "message": "..."}
```

Deploy and rebuild endpoints include data race prevention — if a job is already running for the instance, the endpoint returns:

```json
{"ok": false, "error": "A job is already running: Deploy Instance. Wait for it to complete or cancel it first."}
```
