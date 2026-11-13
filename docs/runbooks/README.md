# IncubaCloud Operational Runbooks

Concrete steps for recurring operational scenarios. Each runbook is
self-contained: symptoms → diagnosis → resolution → rollback, with
exact commands an on-call operator can paste.

All commands assume you run them from the doodba checkout root
(`/home/<user>/.../incubacloud-doodba/`) unless stated otherwise.

## Index

A number missing from the list is a procedure of one particular
deployment, kept with that deployment.

| # | Runbook | Severity | When to use |
|---|---------|----------|-------------|
| [RB-01](RB-01-rotate-secret-key.md) | Rotate `INCUBACLOUD_SECRET_KEY` (MultiFernet) | planned | Scheduled rotation, suspected leak |
| [RB-02](RB-02-restore-master-db.md) | Restore the panel's database from off-site backup | critical | Control-plane DB lost or corrupted |
| [RB-03](RB-03-orphan-terminal-subprocesses.md) | Clean up orphan terminal subprocesses | warning | High memory, stray `python -m incubacloud.terminal_subprocess` |
| [RB-04](RB-04-tune-rate-limits.md) | Tune rate limits | warning | Legitimate 429s, false-positive throttling |
| [RB-05](RB-05-triage-failed-job.md) | Triage a failed `queue.job` / `cloud.alert` | critical | `job_failed` alert in the Alerts panel |
| [RB-06](RB-06-multi-worker-checklist.md) | Multi-worker deployment checklist | planned | Before enabling `workers>1` |
| [RB-07](RB-07-webhook-replay-investigation.md) | Investigate a webhook replay | info | `GitHub webhook replay ignored` log line |
| [RB-08](RB-08-rotate-cron-bot.md) | The cron bot user | planned | A cron is not owned by the bot, or a credential was attached to it |
| [RB-09](RB-09-docker-prune-failed.md) | `docker_prune` failed for a host | warning | Disk alert on host, failing prune cron |
| [RB-10](RB-10-github-app-rate-limited.md) | GitHub App / PAT rate-limited or revoked | critical | Webhooks silent, `403`/`401` in logs |
| [RB-11](RB-11-metrics-backend-unreachable.md) | Metrics backend unreachable — alerting is blind | critical | `metrics_backend_unreachable` alert, empty dashboards |
| [RB-12](RB-12-custom-domain-certificate.md) | Serve a domain with an existing certificate | planned | Wildcard or customer-supplied cert instead of Let's Encrypt |
| [RB-15](RB-15-bump-copier-template-pin.md) | Bump the doodba template pin | planned | Adopting a newer doodba-copier-template release |
| [RB-17](RB-17-instance-log-archive.md) | An instance's log archive is broken | warning | `instance_logs_unhealthy` alert, no days in the log viewer |
| [RB-18](RB-18-tune-host-edge-protection.md) | Tune host edge protection | planned | Proxy 429s on an instance, enabling the host conn-rate cap, provider without anti-DDoS |
| [RB-19](RB-19-restore-large-backup.md) | Get a large backup onto a host and restore it | planned | A dump too large for the browser, or a host nobody holds credentials for |
| [RB-20](RB-20-neutralize-restored-copy.md) | Restore a copy of production without arming it | planned | Developing against real data, reproducing a bug, rehearsing a migration |
| [RB-23](RB-23-continuous-recovery.md) | Continuous recovery: alerts, a stuck restore, a moved instance | critical | `pitr_failing`, `pitr_archive_lagging`, `pitr_base_missing`; a restore that never ended |
| [RB-24](RB-24-direct-path.md) | Direct path: down, a host not admitted, rotate the DNS token, roll back | high | `direct_path_down`, `direct_allowlist_stale`, `direct_certificate_expiring`, `direct_path_unreachable_from_host` |
| [RB-25](RB-25-visitor-address-behind-a-cdn.md) | The visitor's address behind a CDN | planned | Putting a panel behind a CDN, sessions showing the CDN's addresses, Odoo sharing its network |
| [RB-31](RB-31-bump-relay-oauth-image.md) | Bump the relay-oauth image | planned | A `relay-oauth:` issue from the weekly versions check, an advisory for a library in the image, a new docker-mailserver for the relays |
| [RB-32](RB-32-git-repositories-on-the-host.md) | The repositories downloaded on the host | warning | «Fetch the repositories» fails or runs out of time, a series goes back to cloning in the image, the PAT is rotated, instances still to rebuild |
| [RB-33](RB-33-session-guard.md) | The session guard | planned | Switching it on, users signed out more than once, a `session_guard_antiloop` alert, setting a signal to log only |
| [RB-35](RB-35-central-name.md) | The central's single name: pending, down, its certificate, a server left out | high | `metrics_central_dns_pending`, `metrics_central_name_down`, `metrics_central_cert_expiring`, `metrics_central_writer_outside`, `metrics_central_acme_same_name` |
| [RB-36](RB-36-move-the-central-by-hand.md) | Move the central to another server by hand, with its history | planned | The central's server is to be freed, or the central needs a bigger one |
| [RB-38](RB-38-scheduled-actions-heartbeat.md) | The heartbeat of the scheduled actions | planned · critical | Setting up a panel for daily use, a heartbeat service saying the panel's pings stopped |
| [RB-39](RB-39-proxy-network-guard.md) | The guard on the proxy network | planned · warning | A host's first Full Setup with 1.0.228, a `shared_guard_open` alert, an instance that cannot reach another one, turning the guard off |
| [RB-41](RB-41-panel-address-change.md) | The panel's outgoing address changed | critical | A `panel_address_changed` alert, Odoo.sh moving the project's production, a hardened host's jobs failing to connect after the panel moved |
| [RB-42](RB-42-rebuild-left-unapplied.md) | A rebuild left its work unapplied on the server | warning | Start bringing back the previous build, a Start that started nothing, a restore refused, a move postponed `unapplied` |

## Conventions

- Commands prefixed with `$` run on the operator workstation.
- Commands prefixed with `odoo$` run inside the Odoo container
  (`docker compose exec odoo bash`, then the command).
- Commands prefixed with `db$` run inside a `psql` session on the
  panel's database.
- SQL assumes the database is named `prod`; adjust if you run a
  different layout.
- Every runbook has a **Rollback** section — read it before
  starting, not when things are on fire.
