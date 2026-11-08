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

!!! note "Odoo Enterprise"
    A project can deploy Odoo Community or Odoo Enterprise. The panel itself
    runs on Community only, and IncubaCloud does not provide Enterprise: for
    an Enterprise instance, you bring its code in your repositories and need
    your own Odoo Enterprise subscription ("On-premise") from Odoo S.A. See
    [Third-party software](../reference/third-party.md#odoo-enterprise).

## Repositories

A project's repositories are **GitHub** repositories, written
`https://github.com/<owner>/<repo>`; other spellings of a GitHub address are
rewritten to that form, and any other host is refused. The project's list is
copied to each instance created in it; an instance's own list is the one its
deploys use.

### Private repositories

The panel reads a private repository with credentials from **Settings →
GitHub**: the **GitHub App**, or a **Personal Access Token**, which also reaches
repositories the App cannot (`odoo/enterprise`, for instance). A token written
inside the repository's URL is refused.

Only the App receives GitHub's push and pull request notifications, which
**Auto-rebuild** and [pull request previews](../instances/index.md#pull-request-previews)
need, and only on a panel GitHub can reach from the internet. A token gives
access, with manual deploys and rebuilds.

### Which modules a repository brings

Each repository row has **Modules** and **Exclude**:

- **Modules** empty — shown as `ALL` — makes every module of the repository
  available to the instance.
- **Modules** with names makes **only those** available: the list replaces
  `ALL`, it does not add to it.
- **Exclude** takes modules out of `ALL`. It only has a meaning while
  **Modules** is empty.

*Available* is not *installed*. The first deploy installs `base` and the
panel's sign-in module only; you install the rest from Odoo's **Apps**, or
they come installed with a database you restore.

!!! note "`auto_install` modules and `ALL`"
    Odoo installs a module marked `auto_install` by itself once every module
    it depends on is installed — at the first deploy, or later when you
    install something from **Apps**. With `ALL`, every such module of the
    repository is available, so it is installed as soon as its dependencies
    are, whether you wanted it or not. To keep one out, list the modules you
    want instead, or put it under **Exclude**.

Two repositories that bring a module of the same name stop the deploy with a
conflict alert; **Exclude** in one of them is the usual way out.

### Freezing a repository

**Freeze** (the lock on a repository row, or **Freeze all**) pins the
repository to the commit its branch points to at that moment:

- every deploy and rebuild builds that commit, not the branch's newest one;
- a push to the branch does not rebuild the instance;
- the repository's `requirements.txt` is not read again.

Unfreeze to follow the branch again.

### Python and system packages

- A repository's own **`requirements.txt`** is read on every deploy and rebuild
  (frozen repositories excepted), and what it lists is installed.
- The **Dependencies** tab — of the project, as the default for new instances,
  or of an instance — has **pip.txt — Python** and **apt.txt — System**: one
  package per line, installed in the instance's image at the next deploy or
  rebuild. **Sync with project**, on an instance, brings the project's lists
  over.

A package listed in a `requirements.txt` and in the Dependencies tab with
different versions stops the build with a dependency conflict alert: open it
and pick which one wins.

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
