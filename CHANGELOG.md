# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.0.9] — 2026-07-12

### Added

- Telegram and HMAC-signed webhook push notifications for job state changes, with per-channel configuration, chat-id auto-detect and test-message actions
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
