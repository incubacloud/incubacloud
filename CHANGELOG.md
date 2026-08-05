# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.0.31] — 2026-08-04

### Added

- **Refresh from production**: replace a staging instance's database and filestore with a copy of its production's, keeping the staging's own code. Pick the latest backup snapshot or an on-demand live dump; the dialog falls back to the live dump when the production has no backup destination. Runs as the existing `backup_download` → `restore_instance` pair, so both instances keep their running-job protection and their timeline entries
- `backup_download` accepts `time='live'` on production for an on-demand dump, alongside the duplicity snapshot path
- `restore_instance` accepts `neutralize` and `reset_base_url` payload flags

### Changed

- **Chained restores no longer round-trip through the core.** Clone to staging, refresh from production and host moves used to relay the full backup ZIP (database + filestore) as a base64 `ir.attachment` — the whole archive in the core's RAM twice and in its database for up to 2 hours. The download step now stages the archive on the host (`handoff='host'`) and the restore consumes it in place when both run on the same machine, or streams it source host → core disk → target host (`mode='from_host'`) when they don't. Memory stays flat regardless of archive size, and a failed restore stays retryable because the staged copy is only removed on success. Operator-facing downloads (backup, neutralized dump, export) keep the attachment but now write it via `raw`, halving their peak memory
- **Staging clones are now neutralized.** `Clone to staging` (including GitHub PR previews) restores with `--neutralize`, so the copy comes up with scheduled actions and outgoing mail servers disabled and Odoo's test banner on. Previously a clone inherited production's live crons and mail servers, which could invoice, dun and email real customers from a test copy
- A restored copy's `web.base.url` is rewritten to the target instance's own domain, and any inherited `web.base.url.freeze` is dropped, instead of keeping the source's URL until the next admin login

### Fixed

- Cloning a production with no backup destination no longer fails mid-chain on the duplicity path (leaving the fresh staging deployed but empty): the clone falls back to an on-demand live dump, same as the refresh dialog
- The Developer requirement for cloning and refreshing is now enforced on the model methods themselves, closing the direct-RPC path that only the panel routes used to gate

## [1.0.9] — 2026-07-12

### Added

- Telegram and HMAC-signed webhook push notifications for job state changes, with per-channel configuration, chat-id auto-detect and test-message actions
- Unified alert notification pipeline: every alert (including job failures, routed through their `job_failed` alert) dispatches to email, Telegram and webhook from a single code path
- Instance activity timeline split into active and recent jobs with an overflow indicator
- Watchdog cron that recovers instances stranded by a cross-host move that failed before cutover

### Fixed

- Cross-host move pipeline: fresh backup before transfer, only the `odoo` service stopped on the source, chained `queue.job` cleanup on failure
- Traefik proxy restarted after Full Setup so it re-reads its static configuration
- Webhook payload timestamp serialized as a string for JSON compatibility

## [1.0.8] — 2026-07-07

### Added

- Server-side role gate on job enqueue: host-level job types require the Administrator role

### Fixed

- Backup/restore hardening and Traefik ACME storage persistence

## [1.0.7] — 2026-07-04

### Added

- Alerts & notifications overhaul: alert dedup and auto-resolve, dismissed-alert garbage collection, per-user notification preferences (level, muted projects), email channel with immediate or daily-digest mode

## [1.0.6] — 2026-06-28

### Added

- Server-side pagination for backups, audit logs and alert history
- Light theme and Relay UI redesign
- Host pools-aware placement hook and cross-host instance move
- Webhook auto-rebuild can be declined per instance; auto-rebuild guards for archived instances/hosts

### Fixed

- Backup container hostname capped at 64 bytes; S3 301 redirects reported as backend connection errors

## [1.0.5] — 2026-06-10

### Added

- Transient host-connection failures retried automatically before alerting
- Host-scoped job serialization (running-job guard per host)

### Fixed

- Cron-bot server actions authorized for the Odoo 19 cron pre-check

## [1.0.1 – 1.0.4] — April–May 2026

### Added

- Multi-VPS DNS support, executor consolidation and terminal UX improvements
- Instance filter and searchable host/instance selectors in the SPA
- User documentation site (mkdocs-material) at docs.incubacloud.io

## [1.0.0] — 2026-03-29

### Added

- Initial public release of IncubaCloud Core for Odoo 19.

<!-- Rebuild routing check: two pushes in a row (1/2). -->

<!-- Rebuild routing check: two pushes in a row (2/2). -->
