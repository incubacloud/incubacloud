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
- View live logs (Instance detail → Logs)
- Open a shell on the container (Instance detail → Shell)
- [Restore from a backup](../backups/restore.md)
- Add a custom domain (Instance detail → Networking → Domain)

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
