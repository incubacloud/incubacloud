# Roadmap

This document describes where IncubaCloud Core is going: what has shipped, what is in progress, what is planned and what is left for later. It is a living document — priorities may shift based on community feedback and real-world usage.

---

## Recently shipped

- Notification channels for job state changes: email (immediate or daily digest), Telegram, and signed HMAC webhooks — plus the alerts overhaul (dedup, auto-resolve, retention)
- Light theme and the Relay UI redesign; server-side pagination for long lists
- Cross-host instance move with automatic recovery of interrupted moves
- Transient host-connection retry before alerting; host-scoped job serialization
- **A job queue per instance and per host** — an action asked for while another runs waits its turn instead of being refused, shows its place in the job list and can be cancelled while it waits; a failed deploy, rebuild, restore, delete or move step holds the queue until it is retried or its alert dismissed (not the removal of the old copy once a move has cut over)
- PR preview environments and coalesced webhook auto-rebuilds
- Login with GitHub — sign in to the platform with a GitHub account, alongside the existing GitHub App integration
- Staging autopurge — a staging nobody has used for 90 days is deleted, after two warnings and with a one-click Keep
- GitLab merge request previews, and GitLab repositories alongside GitHub ones in a project. See [Repositories on GitLab](user/docs/projects/gitlab.md)
- Mail relay sign-in with OAuth2 for Microsoft 365, next to the password one. See the [mail relay](user/docs/instances/mail-relay.md)
- Auto-approval for an agent's credential, per person. See [auto-approval](user/docs/agents/auto-approval.md)
- Odoo versions 8.0 to 20.0 on the panel
- **Host and instance monitoring** — automatically enrolled host agents, host and container metrics, central metrics and log storage, dashboards, and configurable alert thresholds. See the [operations guide](observability-operations.md) and [user reference](user/docs/reference/monitoring.md).
- **Instance log rotation and log access** — daily Odoo log archives retained on the host across rebuilds, with live viewing, archive search and download. See the [logs guide](user/docs/instances/logs.md).

---

## In progress

- **Server security monitoring** — failed logins and where they come from, alert rules for them, a **Security** view on each server's Metrics tab and a fleet summary.
- **One standard for the panel's forms** — readable help text, switches and tab bars announced correctly to a screen reader, and creating an instance on a single page.
- **The 18.0 branch catches up with 19.0** — the changes of 19.0 carried over in batches.

## Planned

- **SFTP backup destinations** — back up to a server of your own over SFTP, with the server's fingerprint pinned and the connection tested, and continuous recovery (restore to any minute) over it too.
- **Database size** — a daily measurement with a Storage card, and a check that stops a repository list from being saved when it would leave an installed module out, with an action to fit the list to what is installed.
- **Assistant and agents** — Stop that takes effect at once in the assistant, and a `read_metrics` tool in the [MCP server](mcp.md).
- **Accessibility** — a show-password control on the login page and an accessibility audit of the portal's pages.

## Later

Not scheduled; they come up when the need does.

- **Monitoring follow-up** — per-instance database metrics (connections, cache hit ratio and locks) and synthetic probes from outside. See [what is not here](user/docs/reference/monitoring.md#what-is-not-here).
- **Managed version upgrades** — migrate instances between Odoo major versions with a guided pipeline: snapshot, upgrade on a staging copy, automated smoke tests, report, manual approval, and a cutover window with the previous instance kept as rollback. OpenUpgrade for Community; the official upgrade service for Enterprise databases.
- **Data migrations (ETL)** — assisted data onboarding into freshly deployed instances: partners, products, pricing, opening balances, open invoices, initial stock and CRM from spreadsheets, another Odoo, or other ERPs — validated, repeatable, with per-row error reports.
- Host groups spanning multiple datacentres
- Instance replication and failover
- Additional transport backends beyond SSH

---

## How to influence the roadmap

Open a GitHub issue with the `enhancement` label.
