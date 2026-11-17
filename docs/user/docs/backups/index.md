# Backups

Backups go to **your own storage**: an S3 bucket (AWS, MinIO, Backblaze B2,
Wasabi or any S3-compatible service) or an SFTP server. You hold the keys. We
run the schedule and the encryption.

## How it works

1. You register a **backup backend** (your S3 bucket or SFTP server, its access, and a passphrase).
2. You attach the backend to a project (or set it as the global default).
3. The platform runs a nightly backup on every production instance and
   uploads the encrypted bundle to your bucket.
4. Manual backups are available any time from the instance detail.

The backup format is the standard Odoo dump (`dump.sql + filestore/`) wrapped
in an encrypted archive.

## Backup destinations

A destination (a *backup backend*) is a bucket or an SFTP server, and the way
in. A production instance cannot be deployed without one.

### S3-compatible storage or SFTP

A destination is **S3 compatible** (AWS S3 or any service that speaks its API)
or **SFTP** ([below](#sftp)). The type is chosen when the destination is
created and does not change. **Azure Blob Storage, a local disk and FTP are not
destinations.**

Each instance's backups are made on its host, by a backup container that runs
[duplicity](https://duplicity.us/). The panel also reads the bucket, to list
the backups and measure their size, so the endpoint must be reachable **from
the hosts and from the panel**.

### Create one

1. Create the bucket at your provider: the panel does not create it.
2. Create a key that can **list the bucket, and read, write and delete
   objects** in it: backups write, the panel lists, the retention and the end
   of an instance delete.
3. In the panel, **Backup Destinations → New Backend**: a **Name** on the
   **General** tab, then the **Storage** tab:

    | Field | What to put |
    |---|---|
    | **Bucket** | The bucket's name, nothing else. |
    | **Path** | A prefix inside it, `backups` by default. Each instance writes under `<path>/<project folder>/<instance>/`. |
    | **Endpoint URL** | Empty for AWS S3; the provider's S3 endpoint otherwise (below). |
    | **Access key ID**, **Secret access key** | The key of step 2. |
    | **Passphrase** | Read [the passphrase](#encryption) first. |

4. **Test Connection**. It names what is wrong: the key (*403*), a bucket that
   is not there (*404*), or an endpoint other than the bucket's (*301*).

There is no region field: the panel and the backup container use the endpoint
you give.

### Examples

| Provider | Endpoint URL | The key |
|---|---|---|
| **Cloudflare R2** | `https://<account id>.r2.cloudflarestorage.com` | An R2 API token with **Object Read & Write**, limited to the bucket. Its *Access Key ID* and *Secret Access Key* go in the two fields. |
| **Backblaze B2** | `https://s3.<region>.backblazeb2.com`, as the bucket's page shows it (for instance `https://s3.us-west-004.backblazeb2.com`) | An application key with **Read and Write** access, limited to the bucket: *keyID* is the Access key ID, *applicationKey* the secret. |
| **MinIO** | The address of its S3 API, for instance `https://minio.example.com:9000` — not its web console's | A user or access key with the policy below. |

A MinIO policy that gives a key exactly what backups need, on the bucket
`odoo-backups`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::odoo-backups"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::odoo-backups/*"]
    }
  ]
}
```

### Use it

Choose the destination on an instance, on its project, or for every production
instance in **Settings → General → Default backup destination**. An instance
uses its own if it has one, else its project's, else the default.

## SFTP

An SFTP destination is a folder on a server you reach over SSH: a storage
service, a NAS or a server you run. The panel logs in with
a key it generates for the destination (no password), and checks the
server's own key against the one you confirmed: a server presenting another
key is refused.

### Create one

1. **Backup Destinations → New Backend**: a **Name** and the type **SFTP** on
   the **General** tab, then the **Storage** tab:

    | Field | What to put |
    |---|---|
    | **Server** | Its host name or IPv4 address. |
    | **Port** | 22 unless your provider says otherwise ([a storage service](#a-storage-service)). |
    | **User** | The SFTP user, for instance `backups`. |
    | **Folder** | Under the user's home, `backups` by default; start with `/` for an absolute path. Each instance writes under `<folder>/<project folder>/<instance>/`. |

2. **Create Destination**. The **Keys** block now shows the public key, as a
   line for `authorized_keys`. Add it to the user's `~/.ssh/authorized_keys`
   on the server (**Copy** puts it on the clipboard). The line starts with
   `restrict`, which keeps the key to file transfers; leave it out if your
   provider does not accept options.
3. **Read the server key**. Compare the fingerprint shown with the one your
   provider publishes, or with `ssh-keygen -lf` on the server, then **Trust
   this key**. Until a key is trusted no instance can be deployed to back up
   there.
4. **Test connection**: the panel logs in, creates the folder, writes and
   removes a small file, and reports the free space when the server tells it.

The test runs from the panel; the backups run from each server that hosts an
instance. A server that accepts only some addresses must accept both.

#### A storage service

Turn on SSH or SFTP access for the service, or for the sub-account you give the
backups. Use the user and the port the provider shows: some use a port other
than 22. Add the public key as the provider's documentation says. If the
service does not accept options in `authorized_keys`, remove `restrict` from
the line.

### Changing it later

- **Another server or port** drops the trusted key: read and trust the new
  one, then rebuild the instances on the destination. The server, port and
  user cannot change while an archived instance keeps its copy there.
- **Regenerate key** replaces the key pair. Add the new public key to the
  server and keep the old line until every instance on the destination has
  been rebuilt: until then they log in with the old key.
- **The server's key changed**: backups fail, and the daily usage measure
  and backup listing raise a critical alert that names the key the server
  presents and the one trusted. **Test** on the destination says the same.
  Nothing is trusted on its own: check the new key with your provider and
  trust it again.

Retention works as for S3, except that a server has no bucket for a lifecycle
rule: under **Who deletes old backups**, choose **The backup service
(Duplicity)** or **Nobody: keep everything**. The backups are encrypted with
the destination's passphrase before they leave the host, so the SFTP server
only holds encrypted files.

## How long backups are kept

Every backup backend has a **retention period** (under **Advanced** on its
**General** tab) — 3 months unless you change it (`30D`, `3M`, `1Y`…) — and a
**Who deletes old backups** setting, which says who removes the backups older
than that:

- **The backup service (Duplicity)** (the default) — the instance's backup
  service deletes them.
- **A lifecycle rule on the bucket** — a lifecycle rule you set up on the
  bucket, outside IncubaCloud, deletes them.
- **Nobody: keep everything** — nothing is deleted, and the archive keeps
  growing.

A manual backup of a production instance runs the same job as the nightly one,
into the same bucket, and expires under the same rules.

!!! info "IncubaCloud SaaS: the backup days in your plan"
    The backup days in your [plan](../billing/index.md#plans) — 7 on Starter,
    30 on Professional, Business and Agency, none on Free — are for the backups
    we take of your **control plane**, the panel at
    `<subdomain>.incubacloud.io`. They do not change how long your instances'
    backups are kept: that is always the retention of their backend.

## What happens to backups when an instance ends

Backups belong to the instance that made them, and they never outlive it.

| You choose | The instance | Its backups |
| --- | --- | --- |
| **Delete completely** | removed from the host and from the panel | **all destroyed**, before the containers come down |
| **Archive** | removed from the host, kept in the panel | **exactly one copy kept**, taken at that moment |

There is no third option, and no "delete the instance but leave the backups":
a chain nobody owns is one nobody can find, restore or prune, while it keeps
consuming your bucket.

Archiving takes a **fresh full backup first** and prunes everything older, so
the copy is from the moment you archived rather than from the last nightly run.
That copy still counts against your bucket storage — you can see its size in the
project's Archived tab — and it stays until you revive the instance or delete it.

!!! warning "Deleting cannot be undone"
    Both deleting an instance and deleting an archived one destroy the objects
    in your bucket. Download a backup first if you might need the data.

## Managed Backup Storage

!!! info "IncubaCloud SaaS — paid add-on"

If you'd rather not bring your own bucket, the platform can provision one for
you: a dedicated bucket with its own scoped credentials, handed to you as a
ready-to-use backup backend. Three tiers:

| Tier | Included storage | Monthly | Overage |
| --- | --- | --- | --- |
| Starter | 20 GB | 2.99 € | 0.05 €/GB |
| Standard | 100 GB | 8.99 € | 0.04 €/GB |
| Pro | 500 GB | 29.99 € | 0.03 €/GB |

The bucket lives on **Cloudflare R2 in the European Union**, and the backups in
it are encrypted with your passphrase before they leave the host — see
[Service limits](../reference/limits.md#where-managed-backups-are-stored).

Usage is measured periodically; storage beyond the included amount is billed as
overage on your subscription invoice. Ordering a tier, or moving to a bigger
one, charges your saved card right away for the days left in your current
billing period, and it renews with your plan; an amount under 0.50 € is not
charged at once, but added to your next renewal invoice. Cancelling the add-on
or moving to a smaller tier takes effect at your next renewal; until then the
storage keeps working as it is, and nothing is refunded. After a cancellation, backups
are kept for a 30-day retention window before the bucket is purged. A smaller
tier applies at the renewal only if your usage fits in it by then; otherwise
you stay on your current tier and we tell you.

## Encryption

Every backup is encrypted with the **passphrase** you set on the backend before
it leaves the host. We don't store the passphrase in clear and we can't recover it.
Treat it like a master key — store a copy in your password manager.

- **Type your own** when you create the destination, and copy it to your
  password manager before saving. Left empty, the panel generates one and, like
  every secret it stores, never shows it again: the field only says that a
  value is set.
- **Why a copy outside the panel.** Without the passphrase nobody can read the
  backups: not you, not the panel, not your storage provider. The day you need
  them most — the panel or the host is gone — the copies the panel and the
  host keep are gone with them.
- **Changing it later** re-encrypts nothing: backups made before still need the
  passphrase they were made with (see
  [Restore failed: passphrase mismatch](restore.md#troubleshooting)).

## What lives where

- **Bucket** — your S3, encrypted bundles. We never see the raw data.
- **IncubaCloud database** — backend metadata (endpoint, bucket, path).
  Credentials are encrypted at rest with the panel's key
  (`INCUBACLOUD_SECRET_KEY` on a self-hosted panel).

## Common tasks

- [Set up a backup destination](#backup-destinations)
- [Restore a backup](restore.md)
- [Restore to any minute](any-minute.md) with continuous recovery
- Receive an email when a backup completes (Backend detail → Notifications)
- Override the project-level backend on a single instance
  (Instance detail → Backups → Destination)
