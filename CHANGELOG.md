# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.0.33] — 2026-08-06

### Fixed

- **A new template question no longer writes a placeholder secret into `backup.env`.** The doodba template v9.7.0 adds `backup_backend_password`, whose default is the literal string `example-backup-backend-password`; since deploys run copier with `--defaults`, any question we leave unanswered takes its default. The answers file now sets it empty, which keeps the line out of the rendered file entirely. Only password-capable duplicity backends read it — ours authenticate with AWS keys — so nothing was broken, but a fake credential in a live env file is not something to ship

- **The host list no longer flickers while a job runs.** Every background refresh replaced the rendered fleet with the loading skeleton for about 120 ms, so a running job — which notifies watchers twice a second — turned the page into a strobe. The skeleton now belongs to the first paint and to the Retry button; bus events and tab refocus take a silent path that leaves the cards in place. A failed background refresh no longer replaces good data with the error stub either

### Changed

- **Executors notify on output, not on the clock.** The run loop published a bus event every half-second for the whole life of a job, whether or not anything had been written. A job sitting in a silent multi-minute `docker compose build` therefore cost every open panel a full refetch twice a second for nothing. The tick now publishes only when it actually persisted log rows; the final drain still publishes unconditionally so watchers settle on the last state, and the standalone log page keeps its own fallback poll
- **Job events reach only the users allowed to read the job**, resolved through the same visibility helper the email and external notifiers use. A stakeholder no longer receives traffic for the projects they are not a member of
- **Job events now name their target** (`host_id`, `instance_id`, `project_id`), so a panel can ignore an event that does not concern it without asking the server what the event was about. The instance page used to spend one `load_jobs` call per event — for every job in the fleet, not just its own — purely to learn that. Naming the target is safe because the audience is ACL-filtered first; the host page and the tenant page regain filters that had been dropped for costing an extra round-trip
- Refreshed collections are merged by id instead of replaced wholesale, so a refresh only re-renders the rows that changed. This also fixes an active host search being cleared by a background refresh whenever every host happened to match the query

## [1.0.32] — 2026-08-06

### Added

- Revoking a host's trusted SSH key on an endpoint change now raises a critical alert (`host_key_revoked`) through every notification channel, and re-running "Trust SSH Key" resolves it. The revocation used to leave only an audit row, so a host could sit unreachable — excluded from every cron, refusing to build SSH connections — with nothing telling the operator to re-verify the key
- **Machine-identity check on re-trust.** A revocation now records the fingerprint of the key it dropped, and the next capture is compared against it: an identical fingerprint proves the machine did not change and the endpoint edit was administrative, while a different one raises a critical `host_key_changed` alert naming both fingerprints. The verdict reaches the operator who clicked (toast), the panel (alert) and the audit log. This is the verification actually available to us — our provider exposes no console output or metadata carrying host keys — so the alert text no longer asks for an out-of-band check the platform cannot support. Unlike the revocation alert, a change alert is not auto-resolved: it records a past event and waits for a human to dismiss it
- Host key fingerprints are visible in the panel (host overview, `SHA256:…` in the same form `ssh-keygen -lf` prints), alongside the fingerprint of a pending revocation

### Fixed

- **The SSH host key fingerprint was never logged.** The capture read the key *type* field instead of the key blob, so the base64 decode always failed and the log line fell back to a message with no fingerprint in it — the mechanism meant to let an operator verify a TOFU capture had never produced a single fingerprint since it was written

- **Hosts hardened by the panel became unreachable for Ansible-backed jobs.** Hardening rotates the SSH port without revoking the trusted key (same machine), but the stored `known_hosts` line kept the label captured on port 22. OpenSSH files a non-default port under `[ip]:port` and reads the bare form as port 22 only, so it treated those hosts as unknown and failed host-key verification; asyncssh accepts either form, which is why the SSH executors and the terminal never noticed. Hardening now re-files the verified key under the rotated endpoint, and a migration repairs existing hosts. Recurring Docker Prune was the only job affected in practice — it had been failing on every hardened host since the Ansible executors landed in 1.0.20

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
