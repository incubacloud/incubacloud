# Instances

An **instance** is a running Odoo deployment — one container, one database,
one filestore. It belongs to a project and runs on a host.

## Concepts

- **Production instance** — customer-facing. Re-deploys go through the safe-rebuild
  flow (boot test before swap). One per project.
- **Staging instance** — test copy. Re-deploys are direct. As many as your plan allows.
- **Sleeping instance** (SaaS Free plan) — automatically suspended after ~30 minutes
  without traffic to save resources. Wakes up on the next request (the first request
  after a nap takes a few seconds).

## Lifecycle

```
deploy → running → rebuild → running → ... → archive
```

A rebuild updates the image and modules without downtime (production) or
restarts the container (staging). An archive deletes the container but keeps
the configuration so you can re-deploy later.

## Common tasks

- [Deploy your first instance](../getting-started/first-deploy.md)
- Rebuild after pushing changes to your repo (Instance detail → Rebuild)
- [View logs](logs.md) (Instance detail → Logs). Odoo's log is kept one file per day and survives rebuilds: pick a day, search every day at once, or download one
- Open a shell on the container (Instance detail → Shell)
- [Restore from a backup](../backups/restore.md)
- [Refresh a staging with production data](#refresh-from-production)
- Add a custom domain (Instance detail → Networking → Domain)

## Refresh from production

**Instance detail → Refresh from production** replaces a staging instance's
database and filestore with a copy of its project's production, keeping the
staging's own code (repositories and branches) untouched. It is the button you
press when your test copy has drifted away from real data.

Two data sources:

- **Latest backup** (recommended) — restores the most recent snapshot from the
  production's backup destination. No extra load on production.
- **Live dump from production** — dumps the production database on the spot, so
  you get the data as of right now. It costs a `pg_dump` on a live database, and
  it is the only option when the production has no backup destination configured
  (the dialog then picks it for you).

**Neutralize the database** is on by default: the restored copy comes up with
scheduled actions and outgoing mail servers disabled and Odoo's test banner
showing. Leave it on unless you are deliberately testing a cron or a mail flow —
a staging carrying production's data will otherwise invoice, dun and email your
real customers.

The refresh runs as two jobs (a download on the production, a restore on the
staging) that you can follow in the activity timeline. If the download fails the
restore never starts and the staging is left untouched. Refreshing does **not**
run a module update: if the staging's branch lacks a module the restored database
has installed, use a rebuild afterwards.

Cloning a production to a new staging (**Project → Clone to staging**) applies
the same neutralization.

## Auto-update

If you push commits to a connected repo, two settings control what happens:

- **Auto-rebuild** — trigger a rebuild automatically when commits land on the
  connected branch. Off by default.
- **Auto-update modules** — during rebuild, run `click-odoo-update` to update
  only the modules whose checksum changed. On by default.

Both live under `Instance detail → Networking`.

## See also

- [Hosts](../hosts/index.md) — where instances run.
- [Backups](../backups/index.md) — how to keep their data safe.

!!! note "Reference page is in progress"
    A full per-screen reference for the Instance detail UI is coming soon.
