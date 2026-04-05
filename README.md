# IncubaCloud Core

[![License: ELv2](https://img.shields.io/badge/License-Elastic%20v2-blue.svg)](LICENSE)
[![Odoo](https://img.shields.io/badge/Odoo-19.0-purple.svg)](https://www.odoo.com)
[![CI](https://github.com/incubacloud/core/actions/workflows/ci.yml/badge.svg)](https://github.com/incubacloud/core/actions/workflows/ci.yml)

**IncubaCloud** is an Odoo-native platform for deploying, managing and monitoring remote Odoo instances — host management, instance lifecycle, GitHub integration, backups, terminals and audit trails, all from inside your ERP.

---

## What IncubaCloud does

- Deploy [doodba](https://github.com/Tecnativa/doodba)-based Odoo instances on your own servers with a single click
- Full instance lifecycle: safe rebuilds with boot tests, start/stop/restart, clone to staging, delete
- GitHub App integration with webhook-triggered auto-rebuilds
- S3 backups via duplicity: list, create, download, restore
- Interactive terminals (xterm.js) and container log viewer
- Connect-as: passwordless login to managed instances
- 5-tier role-based access control with full audit trail

---

## Features

### Infrastructure

- Register and manage VPS servers with SSH credentials (password or key)
- Automated host setup: Docker, Traefik reverse proxy, deployment tools
- Live host metrics (CPU, RAM, disk) collected every 5 minutes
- Health checks and Docker prune (automatic daily cleanup)
- Transport abstraction layer — SSH today, extensible to other backends
- Multi-domain support per instance via Traefik with optional redirects

### Instance lifecycle

- **Deploy** instances via [copier](https://copier.readthedocs.io/) with automatic module initialization
- **Safe rebuild**: new Docker images are tested with a boot test against a cloned database before applying — if it fails, the instance keeps the old image
- **Auto-update modules** via `click-odoo-update` checksums (only changed modules are updated)
- Start, stop, restart, pause and delete operations
- Clone production to staging with database and filestore copy
- Rebuild fingerprinting for smart cache invalidation
- Pluggable action system for custom job types
- Data race prevention: blocks concurrent operations on the same instance

### GitHub integration

- GitHub App authentication for private repository access
- Webhook-triggered auto-rebuilds on push events
- Freeze functionality to prevent auto-rebuilds on specific repos
- PR preview environments
- Webhook event audit log

### Backups and data

- S3-compatible backup backends with encrypted credentials
- Full backup lifecycle via duplicity: list, create, download, restore
- Export for development: sanitized tarballs stripped of tokens and passwords
- Scheduled backup refresh and retention policies

### Developer tools

- Interactive terminal (xterm.js + PTY) with session management and idle timeout
- Container log viewer with real-time streaming per service
- Job log terminal with stdout/stderr separation
- Connect-as: passwordless login to any user on managed instances

### Security and governance

- **5-tier RBAC**: Stakeholder (read-only) → Consultant → Project Manager → Developer → Administrator
- Audit trail with filtering, configurable retention and automated purge
- Multi-user attribution and job notifications
- Encrypted secrets (Fernet) for passwords, SSH keys and S3 credentials
- HMAC-SHA256 validated GitHub webhooks

---

## Modules

| Module | Description |
|---|---|
| `incubacloud` | Core platform — hosts, projects, instances, jobs, GitHub, backups, terminals and UI |
| `incubacloud_connect` | Companion module: magic-link passwordless login installed on managed instances |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Odoo (IncubaCloud)                    │
│                                                         │
│  ┌─────────────────┐    ┌──────────────────────────┐   │
│  │   OWL SPA       │    │   JSONRPC Controllers    │   │
│  │  /cloud/        │◄──►│   /cloud/*               │   │
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

IncubaCloud acts as the **control plane**, not the runtime. All heavy lifting happens on the remote server; Odoo is the orchestrator.

### Key models

| Model | Description |
|---|---|
| `cloud.host` | VPS server — IP, SSH credentials, Traefik config, live metrics |
| `cloud.project` | Groups instances; holds default repos and team members |
| `cloud.instance` | One Odoo deployment — version, DB config, SMTP, domains, backup backend |
| `cloud.job` | Async remote execution record linked to `queue.job` |
| `cloud.job.type` | Job type definition with pluggable action support |
| `cloud.job.log.chunk` | Individual log line (stdout / stderr / system) |
| `cloud.backup.backend` | S3-compatible backup target — encrypted credentials |
| `cloud.github.app` | GitHub App configuration (singleton) |
| `cloud.github.event` | Immutable webhook audit log |
| `cloud.alert` | Infrastructure alerts and notifications |

See [docs/architecture.md](docs/architecture.md) for the full module structure and [docs/api-endpoints.md](docs/api-endpoints.md) for the REST API reference.

---

## Installation

Standard Odoo 19.0 module. Add this repository to your `addons_path`, install the Python dependencies and the OCA `queue_job` module.

**Python dependencies:** `asyncssh`, `cryptography`, `boto3`, `PyYAML`

**Odoo dependencies:** `queue_job` ([OCA/queue](https://github.com/OCA/queue) 19.0)

---

## Running tests

```bash
odoo --test-enable --stop-after-init --workers=0 \
     -u incubacloud \
     --test-tags /incubacloud
```

---

## Use cases

- Odoo partners managing multiple customer environments
- Technical teams standardizing deployment workflows
- Foundation for building managed Odoo services

---

## Roadmap

### Near term

- Additional transport backends (the `BaseTransport` ABC is in place)
- Internationalisation — additional language packs (only Spanish shipped today)
- Extension point documentation for third-party modules
- `EncryptedChar` key rotation tooling
- Structured log export (JSON)
- Job duration tracking and per-executor statistics on the dashboard

### Medium term

- GitLab and Gitea support via the existing `cloud.project.repo` model
- Provider abstraction (`cloud.provider`) for API-based VPS providers alongside SSH
- Notification integrations (Slack, Telegram, email) for job state changes and alerts

### Long term

- Host groups spanning multiple datacentres
- Instance replication and failover
- Centralised metrics aggregation

Open a GitHub issue with the `enhancement` label to influence priorities.

---

## Status

**Production-ready** — IncubaCloud Core is actively used in production environments. The platform provides a stable feature set with comprehensive test coverage, role-based access control and audit logging.

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

If you are an Odoo partner or developer:
- Bug reports and security issues → see [SECURITY.md](SECURITY.md)
- Architectural discussions → open an issue first
- New providers or integrations → discuss before submitting

---

## Licensing

IncubaCloud Core is licensed under the **[Elastic License 2.0 (ELv2)](LICENSE)**.

You are free to:
- Use the software for your own Odoo installations
- Study and modify the source code
- Distribute modified versions

You may **not**:
- Offer the software (or a product substantially based on it) as a hosted or managed service to third parties

---

## Contact

Project maintained by **IncubaCloud**

Enterprise features, support and partnerships:
contact@incubacloud.io
