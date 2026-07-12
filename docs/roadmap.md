# Roadmap

This document describes the planned direction for IncubaCloud Core. It is a living document — priorities may shift based on community feedback and real-world usage.

---

## Recently shipped

- Notification channels for job state changes: email (immediate or daily digest), Telegram, and signed HMAC webhooks — plus the alerts overhaul (dedup, auto-resolve, retention)
- Light theme and the Relay UI redesign; server-side pagination for long lists
- Cross-host instance move with automatic recovery of interrupted moves
- Transient host-connection retry before alerting; host-scoped job serialization
- PR preview environments and coalesced webhook auto-rebuilds

---

## Near term

- **Agentless monitoring** — lightweight metric history (host load, memory, disk per mount point, per-container stats, database size, HTTP latency, job queue depth), an external HTTP health check with TLS-expiry verification as a second independent signal, configurable alert thresholds, and sparklines in the dashboard. No agent installed on the servers.
- **Instance log rotation and log access** — guaranteed log rotation for every deployed instance (including retroactive application to existing ones) and a better container log viewer: incremental follow, log download, and rate-limited access.
- **User SSH keys and direct shell access** — register per-user public SSH keys (added manually or imported from a GitHub account), grant them per instance, and open a shell directly over SSH with your own identity. Restricted server-side authorization, immediate revocation and full audit trail.

## Medium term

- **Login with GitHub** — sign in to the platform with a GitHub account. Pairs with SSH key import and the existing GitHub App integration.
- **Managed version upgrades** — migrate instances between Odoo major versions with a guided pipeline: snapshot, upgrade on a staging copy, automated smoke tests, report, manual approval, and a cutover window with the previous instance kept as rollback. OpenUpgrade for Community; the official upgrade service for Enterprise databases.
- **Data migrations (ETL)** — assisted data onboarding into freshly deployed instances: partners, products, pricing, opening balances, open invoices, initial stock and CRM from spreadsheets, another Odoo, or other ERPs — validated, repeatable, with per-row error reports.

## Long term

- Host groups spanning multiple datacentres
- Instance replication and failover
- Additional transport backends beyond SSH

---

## How to influence the roadmap

Open a GitHub issue with the `enhancement` label.
