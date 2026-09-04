# Migrate from Odoo.sh

Two steps: import the project from your Git repo, then restore the database
from the backup ZIP you download from Odoo.sh. Each step is straightforward.

!!! info "Honest expectation"
    A full Odoo.sh migration is two steps, not one click.
    We don't do live sync. We don't promise zero downtime during cutover.
    Restore time depends on dump size — budget 10–30 minutes for a typical
    customer database.

## Prerequisites

- Your project's Git URL (find it on the Odoo.sh dashboard).
- A backup ZIP from Odoo.sh.
- An IncubaCloud account — for production use a paid plan (Free-plan instances
  sleep when idle and get no automatic backups).
- A host (yours or hired).

## Step 1 — import the project from Git

1. On the Odoo.sh dashboard, copy your project's Git URL (HTTPS with a token,
   or SSH).
2. In IncubaCloud, click **New Project**. Paste the URL.
3. We auto-detect the Odoo.sh layout via `.gitmodules` and reconstruct the
   project structure: submodules, branches, Odoo version (from the project's
   `requirements.txt` or pinned image).
4. Save. The project shows up with the detected branches and version pre-filled.

!!! warning "Private repos need credentials"
    HTTPS with a personal access token works (`https://<token>@github.com/...`).
    SSH needs the platform's deploy key added to your repo.

## Step 2 — restore the database

1. Download a backup from Odoo.sh. From the project's **Backups** tab, pick the
   most recent ZIP. The format is the standard Odoo dump (`dump.sql + filestore/`).
2. In IncubaCloud, [deploy a fresh instance](../getting-started/first-deploy.md) under your new
   project, with the same Odoo version. Don't worry about the data — you're
   about to overwrite it.
3. Open the instance → **Restore Database**.
4. Send the ZIP through your browser, and for a large database use the
   temporary upload key or paste a link to it instead
   (see the [restore docs](../backups/restore.md#restore-from-an-external-zip)).
5. The restore job runs and replaces the empty database with your real one.

## Step 3 — cut over

Point your DNS to the IncubaCloud instance:

```text
A    app.example.com    →    <new host IP>
```

DNS propagation usually takes a few minutes. Once it's through, your customers
land on the new instance. Old Odoo.sh instance can stay running until you're
confident, then cancel.

!!! warning "Restored DB is not neutralized"
    Your restored database keeps the original cron jobs, mail servers, and admin
    users. If you restore to a staging instance for testing, **disable mail and
    crons** before running scenarios that send emails or trigger automations.
    Or: use the [neutralized backup download](../backups/restore.md) on the
    Odoo.sh side first and import that.

## Verify it worked

- [x] You can log in to the restored instance with the original admin credentials.
- [x] The data matches what you had on Odoo.sh at the time of the dump.
- [x] Your custom modules from the imported repo are installed and visible.
- [x] DNS resolves to the IncubaCloud instance with HTTPS.

## Troubleshooting

??? note "Project import failed: \"layout not detected\""
    We expect either `.copier-answers.yml` (doodba) or `.gitmodules` (Odoo.sh)
    in the repo root. Older Odoo.sh projects sometimes lack `.gitmodules` in
    the default branch. Check the branch you're importing has it.

??? note "Restore is slow"
    Most of the time is spent unzipping the filestore. Large filestores
    (10+ GB) can take an hour. Do the migration during a maintenance window.

??? note "Custom modules from Odoo.sh not loading"
    Odoo.sh sometimes pulls modules via submodules from private repos.
    Check `Project → Settings → Repository` and ensure the deploy key has
    access to all submodule repos, not just the main one.

??? note "I want to keep both running for a few days"
    Don't change DNS yet. Test the IncubaCloud instance via its
    auto-generated subdomain. When confident, flip DNS in your registrar.

## See also

- [Migrations overview](index.md)
- [Backups → Restore](../backups/restore.md)
- [Deploy your first instance](../getting-started/first-deploy.md)
