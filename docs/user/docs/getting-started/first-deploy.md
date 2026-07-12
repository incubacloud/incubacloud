# Deploy your first instance

This guide walks you through deploying your first Odoo instance:
picking a host, an Odoo version, the modules, and watching the deployment
log in real time.

!!! info "Prerequisites"
    - A project ([create one](first-project.md)).
    - A host: either [your own VPS](../hosts/connect.md) or
      [one ordered through the platform](../hosts/hire.md).

## 1. Open the project and click `Deploy instance`

A side panel opens with the deployment form. The same form is reused later for
re-deploys, so it's worth getting comfortable with each field.

## 2. Pick the environment

- **Production** — the customer-facing instance. Re-deploys go through the safe-rebuild
  flow: if the new image fails its boot test, the previous version keeps serving.
- **Staging** — test copy. You can have many. Re-deploys are direct.

## 3. Pick the host

Select one of your registered hosts. The platform checks capacity automatically
and prevents over-provisioning. If your hosts are all full, [order a new one](../hosts/hire.md)
or [connect another](../hosts/connect.md).

## 4. Pick an Odoo version

Versions 7.0 to 19.0 are supported. The version influences which modules are available
and which Postgres major version we use.

If your project has a connected repo with `.copier-answers.yml`, we pre-fill the version
from there. You can still override.

## 5. Pick modules

Either:

- **Pick from the catalog** — the official Odoo modules for the chosen version,
  plus the OCA modules in our default repo set.
- **Use your own** — modules from your project's repo. The repo URL was set when you
  created the project; you can override per-instance under
  `Settings → Repository`.

Both modes can be mixed. Our deployer composes a Docker image with everything
you selected.

## 6. Optional: custom domain

Add `app.example.com` (or any domain you control). The deployer requests a
Let's Encrypt certificate automatically and wires up HTTPS.

!!! warning "DNS must be in place"
    The domain's A or AAAA record must already point to the host's IP **at the moment
    of deployment**, otherwise Let's Encrypt will fail to validate. Set the DNS first.

If you skip the domain, the platform generates a subdomain under its base domain
for testing (on the hosted SaaS: `<name>.<your-subdomain>.incubacloud.io`).

## 7. Hit Deploy

A job starts. The job log opens automatically. You'll see the steps in order:

1. Build image (1–3 min depending on dependencies).
2. **Boot test** (production only) — clone the database to `__ic_boot_test`,
   run `click-odoo-update`, drop. If this fails, the deploy aborts before
   touching the live instance.
3. Update modules in the live database.
4. Bring up the new container.
5. Cleanup.

When the job ends in green, your instance is up.

## Verify it worked

- [x] The job shows `Done` in green.
- [x] The instance card shows `Running`.
- [x] Clicking `Open` takes you to the Odoo login.
- [x] If you set a custom domain, browsing to it shows the same Odoo with HTTPS.

## What happens next

- Set up a [backup backend](../backups/index.md) so your data is safe.
- Add the rest of your team via `Settings → Users`.
- Read about [environments](first-project.md#environments) if you'll use stagings.

## Troubleshooting

??? note "Build failed: pip install error"
    Usually a Python dependency missing for the Odoo version you picked.
    Check the build log; the failing line tells you which package. Add it
    to your repo's `requirements.txt` and redeploy.

??? note "Boot test failed"
    A migration script in your custom modules raised an error. Check the
    boot test log section. Fix the migration, push, redeploy.
    Production stays untouched.

??? note "HTTPS certificate not issued"
    Re-check DNS resolution: `dig +short app.example.com` should return the
    host's IP. Let's Encrypt has rate limits — retry after 1 hour if you
    hit them.

??? note "Instance running but Odoo errors on login"
    Open `Logs` from the instance detail. Most first-time issues are missing
    Postgres extensions or wrong Odoo version vs database version.
