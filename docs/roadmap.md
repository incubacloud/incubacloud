# Roadmap

This document describes the planned direction for IncubaCloud Core. It is a living document — priorities may shift based on community feedback and real-world usage.

---

## Recently shipped

- Notification channels for job state changes: email (immediate or daily digest), Telegram, and signed HMAC webhooks — plus the alerts overhaul (dedup, auto-resolve, retention)
- Light theme and the Relay UI redesign; server-side pagination for long lists
- Cross-host instance move with automatic recovery of interrupted moves
- Transient host-connection retry before alerting; host-scoped job serialization
- PR preview environments and coalesced webhook auto-rebuilds
- Login with GitHub — sign in to the platform with a GitHub account, alongside the existing GitHub App integration
- Staging autopurge — a staging nobody has used for 90 days is deleted, after two warnings and with a one-click Keep
- **Host and instance monitoring** — automatically enrolled host agents, host and container metrics, central metrics and log storage, dashboards, and configurable alert thresholds. See the [operations guide](observability-operations.md) and [user reference](user/docs/reference/monitoring.md).
- **Instance log rotation and log access** — daily Odoo log archives retained on the host across rebuilds, with live viewing, archive search and download. See the [logs guide](user/docs/instances/logs.md).

---

## Near term

- **Monitoring follow-up** — per-instance database metrics (connections, cache hit ratio and locks) and synthetic probes from outside remain designed but deferred; they are not part of the monitoring already shipped. See [what is not here yet](user/docs/reference/monitoring.md#what-is-not-here-yet).
- **User SSH keys and direct shell access** — register per-user public SSH keys (added manually or imported from a GitHub account), grant them per instance, and open a shell directly over SSH with your own identity. Restricted server-side authorization, immediate revocation and full audit trail.

## Medium term

- **Managed version upgrades** — migrate instances between Odoo major versions with a guided pipeline: snapshot, upgrade on a staging copy, automated smoke tests, report, manual approval, and a cutover window with the previous instance kept as rollback. OpenUpgrade for Community; the official upgrade service for Enterprise databases.
- **Data migrations (ETL)** — assisted data onboarding into freshly deployed instances: partners, products, pricing, opening balances, open invoices, initial stock and CRM from spreadsheets, another Odoo, or other ERPs — validated, repeatable, with per-row error reports.

## Long term

- Host groups spanning multiple datacentres
- Instance replication and failover
- Additional transport backends beyond SSH

---

## How to influence the roadmap

Open a GitHub issue with the `enhancement` label.
