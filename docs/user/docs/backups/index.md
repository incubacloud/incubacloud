# Backups

Backups go to **your own S3 bucket** — AWS, MinIO, Backblaze B2, Wasabi, or any
S3-compatible service. You hold the keys. We run the schedule and the encryption.

## How it works

1. You register a **backup backend** (your S3 endpoint + credentials + a passphrase).
2. You attach the backend to a project (or set it as the global default).
3. The platform runs a nightly backup on every production instance and
   uploads the encrypted bundle to your bucket.
4. Manual backups are available any time from the instance detail.

The backup format is the standard Odoo dump (`dump.sql + filestore/`) wrapped
in an encrypted archive.

## Retention by plan

| Plan | Automatic backup retention |
| --- | --- |
| Free | None (manual backups only) |
| Starter | 7 days |
| Professional | 14 days |
| Business | 30 days |

Manual backups never expire. They count against your bucket storage,
not against the retention window.

## Encryption

Every backup is encrypted with the **passphrase** you set on the backend before
it leaves the host. We don't store the passphrase in clear and we can't recover it.
Treat it like a master key — store a copy in your password manager.

## What lives where

- **Bucket** — your S3, encrypted bundles. We never see the raw data.
- **IncubaCloud database** — backend metadata (endpoint, region, bucket name).
  Credentials are encrypted at rest with our own platform key.

## Common tasks

- Set up your first backup backend (use the [marketing site quickstart](https://www.incubacloud.io/docs/backups))
- [Restore a backup](restore.md)
- Receive an email when a backup completes (Backend detail → Notifications)
- Override the project-level backend on a single instance
  (Instance detail → Settings → Backup backend)

!!! note "Reference page is in progress"
    A full per-screen reference for the Backup backend UI is coming soon.
