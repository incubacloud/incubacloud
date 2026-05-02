# Migrations

Move an existing Odoo into IncubaCloud without rebuilding from scratch.
Three paths, depending on where your data lives today:

## Pick your starting point

- :material-source-branch:{ .lg .middle } **Coming from Odoo.sh** — your project lives in a Git repo on Odoo.sh
  and you can download backups from the dashboard.
  [:octicons-arrow-right-24: Walkthrough](from-odoosh.md)

- :material-server:{ .lg .middle } **Importing an instance already running on a VPS** — doodba already deployed
  on a host you can SSH into, and you want to bring it under IncubaCloud's
  management without redeploying.
  Use **Host detail → Import Instance** in the SPA. We read `.copier-answers.yml`,
  `repos.yaml`, `addons.yaml`, `.env` and the docker-compose file directly.

- :material-database-arrow-up:{ .lg .middle } **Importing a project from Git** — your custom code is in a Git repo
  (doodba layout via `.copier-answers.yml` or Odoo.sh layout via `.gitmodules`)
  and you'll restore data later via the Backups → Restore flow.
  Use **Dashboard → New Project** and paste the repo URL.

## What we cannot do (yet)

- **Live sync** — we don't replicate continuously from a running instance.
- **Detection from a `*.odoo.sh` URL** — you must paste the Git URL of the repo,
  not the Odoo.sh dashboard URL.
- **Cutover with zero downtime** — the instance is briefly unavailable during
  the database restore.
- **Automatic neutralization of restored DBs** — restored databases keep their
  original cron jobs, mail servers, and admin users. To neutralize, download
  the [neutralized backup](../backups/restore.md) instead and import that.

## See also

- [Backups → Restore](../backups/restore.md) — the second half of any data migration.
- [Hosts → Connect](../hosts/connect.md) — needed if your data lives on a VPS we
  don't manage yet.
