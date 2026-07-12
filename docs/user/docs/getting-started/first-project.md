# Create your first project

A project groups everything that belongs to one customer or one product:
its instances, its repositories, its environments. Every Odoo deployment in
IncubaCloud lives under a project.

!!! info "Why projects?"
    Projects let you reason about ownership, billing splits per customer, and
    environment promotion (staging → production). They also scope which team members
    can see what.

## 1. Open the dashboard

From `/cloud`, click **New Project** at the top right. A side panel slides in
with the project form.

## 2. Fill in the basics

- **Name** — your customer's name or the product name. Shows up in lists and breadcrumbs.
- **Description** (optional) — a short note for your team.
- **Default environment** — leave on `production` unless you know you want a staging-first project.

## 3. Connect a Git repository (optional, recommended)

Paste an HTTPS or SSH URL. We auto-detect the project layout:

- `.copier-answers.yml` → **doodba** layout. We pick up the answers (Odoo version, Postgres version, repos, addons) and fill defaults for you.
- `.gitmodules` → **Odoo.sh** layout. We register the modules so your custom code is available at deploy time.

If you don't have a repo yet, skip this step. You can add it later from
`Project detail → Settings → Repository`.

!!! warning "Private repositories"
    For private GitHub repos, install the platform's **GitHub App**
    (`Settings → GitHub App`) — it grants repository access without sharing
    credentials — or store a personal access token in `Settings → GitHub`.
    Plain HTTPS URLs with an embedded token
    (`https://<token>@github.com/your-org/your-repo.git`) also work.

## 4. Pick a backup backend (optional, recommended)

Select an existing backup backend or skip and configure one later from
`Settings → Backups`. See [Set up backups](../backups/index.md) for the full walkthrough.

The backend you select here applies to every instance created under this project,
unless an instance overrides it.

## 5. Save

You land on the project detail page. The sidebar lists environments (none yet)
and a button invites you to deploy the first instance.

[:octicons-arrow-right-24: Next: deploy your first instance](first-deploy.md)

## Environments

Each project supports:

- **One production instance.** The customer-facing one. Deploys here go through
  the safe-rebuild flow (boot test before swap).
- **Many staging instances.** For test copies. They share configuration with production
  but use independent databases.

You can clone production data to a staging instance at any time
(see [Backups → Restore](../backups/restore.md)).

## Verify it worked

- [x] The new project shows up in your `/cloud` dashboard list.
- [x] Opening it shows the sidebar and a `Deploy instance` CTA.
- [x] If you connected a repo, the project detail shows the detected layout.

## Troubleshooting

??? note "Repo connection says \"unable to authenticate\""
    Verify the URL works from your terminal first:
    `git clone <your-url> /tmp/test-clone`. If that succeeds, double-check the
    GitHub App installation covers this repository (or that the token is active).

??? note "Layout not detected"
    We look for `.copier-answers.yml` and `.gitmodules` in the repo root.
    If neither is present, we treat the repo as a generic codebase — you can
    still deploy, you just lose the auto-fill of versions and module list.
