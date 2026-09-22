# Instances

An **instance** is a running Odoo deployment — one container, one database,
one filestore. It belongs to a project and runs on a host.

## Concepts

- **Production instance** — customer-facing. Re-deploys go through the safe-rebuild
  flow (boot test before swap). One per project.
- **Staging instance** — test copy. Re-deploys are direct. As many as you like:
  stagings run on servers you already pay for, so they never count against the
  instance allowance of a SaaS plan.
- **Sleeping instance** (SaaS Free plan) — automatically suspended after ~30 minutes
  without traffic to save resources. Wakes up on the next request (the first request
  after a nap takes a few seconds).

!!! info "IncubaCloud SaaS"
    Instances on the hosted service are served through a CDN, which caps a single
    request at **100 MB** and at **100 seconds** to first byte. See
    [Service limits](../reference/limits.md) for what to use instead when you hit
    either one.

## Lifecycle

```
deploy → running → rebuild → running → ... → archive or delete
```

A rebuild updates the image and modules without downtime (production) or
restarts the container (staging).

## Ending an instance

Both endings remove the containers, the database and the files from the host.
They differ in what happens to the **backups**.

!!! warning "Deleting destroys the backups too"
    An instance's backups belong to that instance. **Delete completely** empties
    them before the containers come down — there is no option to keep them, and
    nothing to restore from afterwards. If you might want the data later,
    archive instead, or [download a backup](../backups/restore.md) first.

    Because it cannot be undone, the panel asks you to type the instance name.

**Archive** keeps one restorable copy. A fresh full backup is taken at that
moment — not the last nightly one, so nothing since it is lost — and everything
older is pruned, leaving exactly one copy. The instance moves to the
**Archived** tab of its project, where you can see the size of that copy and
when it was last verified.

From there you can:

- **Revive** it — deploy it again, on the original host or any other, and
  restore the copy into it.
- **Delete** it — which destroys the copy as well, and again asks for the name.

An archived instance keeps its name reserved inside its project: creating a new
instance with that name is refused, because it would compute the same backup
path and write into the copy being kept.

!!! note "Archiving without a backup destination"
    An instance with no backup backend has no copy to keep, so archiving it
    keeps only the record. The panel says so and asks you to confirm in
    writing.

## Common tasks

- [Deploy your first instance](../getting-started/first-deploy.md)
- Rebuild after pushing changes to your repo (Instance detail → Rebuild)
- [View logs](logs.md) (Instance detail → Logs). Odoo's log is kept one file per day and survives rebuilds: pick a day, search every day at once, or download one
- Open a shell on the container (Instance detail → Shell)
- [Restore from a backup](../backups/restore.md)
- [Refresh a staging with production data](#refresh-from-production)
- [Get a preview instance for every pull request](#pull-request-previews)
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

## Pull request previews

A project can create a throwaway staging for every pull request, so a change can
be looked at running — on real data — before it is merged. It is **off by
default**; turn it on under **Project → Settings → Preview instances for pull
requests**.

With it on, opening (or reopening) a pull request on a repository that one of
the project's **production** instances follows does this, with nobody pressing
anything:

1. A staging named `pr-<number>` is created on the **same host** as that
   production, following the pull request's branch.
2. It receives a copy of the production's data — the latest backup, or a live
   dump when the production has no backup destination — **neutralized** exactly
   like [Refresh from production](#refresh-from-production): scheduled actions
   and outgoing mail servers disabled, test banner showing.
3. It gets an address of its own under the host's wildcard domain
   (`<project>-pr-<number>.<host domain>`), and the panel comments that link on
   the pull request once the preview is up.
4. Every new push to the pull request rebuilds it.
5. Closing or merging the pull request deletes it, comment included.

Things worth knowing before switching it on:

- A preview is a full copy of production data sitting on your host for as long
  as the pull request stays open. Anyone who can reach its address and log in
  sees that data.
- It needs the [GitHub App](../getting-started/first-project.md) connected to the
  repository: the webhook that announces the pull request, and the comment, both
  go through it.
- A production instance that pins the repository to a fixed commit does not get
  previews for it — a pin says "this instance does not move with the branch".
- If a preview cannot be created, the pull request gets a comment saying why and
  the production instance an alert. Fix the cause, then close and reopen the
  pull request to try again.

## See also

- [Hosts](../hosts/index.md) — where instances run.
- [Backups](../backups/index.md) — how to keep their data safe.

!!! note "Reference page is in progress"
    A full per-screen reference for the Instance detail UI is coming soon.
