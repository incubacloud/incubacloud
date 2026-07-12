# Projects

A **project** groups everything that belongs to one customer or one product:
its instances, its repositories, its environments.

## Concepts

- **Project** — top-level container. Owned by your account.
- **Environment** — production or staging. Each project has one production and
  any number of stagings.
- **Repository** — Git URL where your custom modules live. Detected as doodba
  (via `.copier-answers.yml`) or Odoo.sh (via `.gitmodules`).
- **Backup backend** — the S3 destination for backups. Inherited from project
  unless overridden per instance.

## Common tasks

- [Create your first project](../getting-started/first-project.md)
- Add a repository to an existing project (Project detail → Settings → Repository)
- Promote a staging to production (use the rebuild flow with the same repo URL)
- Transfer a project between team members (Project detail → Settings → Owner)

## See also

- [Instances](../instances/index.md) — what lives inside a project.
- [Backups](../backups/index.md) — per-project or per-instance configuration.

!!! note "Reference page is in progress"
    A full per-screen reference for the Project detail UI is coming soon.
    Meanwhile, the [marketing site quickstart](https://www.incubacloud.io/docs/first-project)
    covers the most common flow.
