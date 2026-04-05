# Roadmap

This document describes the planned direction for IncubaCloud Core. It is a living document — priorities may shift based on community feedback and real-world usage.

---

## Near term

- Internationalisation (i18n) — all strings are translatable (`.pot` generated); additional language `.po` files needed (currently only Spanish)
- Define and document extension points for third-party modules
- Harden `EncryptedChar` migration path (re-encryption on key rotation)
- Structured log export (JSON) from `/cloud/log/<id>`
- Job duration tracking and per-executor statistics on the dashboard

---

## Medium term

- GitLab and Gitea support via the same `cloud.project.repo` model
- Provider abstraction (`cloud.provider`) to support API-based VPS providers and local Docker alongside SSH
- Notification integrations (Slack, Telegram, email) for job state changes and alerts

---

## Long term

- Host groups spanning multiple datacentres
- Instance replication and failover
- Centralised metrics aggregation

---

## How to influence the roadmap

Open a GitHub issue with the `enhancement` label.
