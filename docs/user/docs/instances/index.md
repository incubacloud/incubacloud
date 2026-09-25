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
- [Keep a staging that is about to expire](#stagings-expire-when-nobody-uses-them)
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

## Stagings expire when nobody uses them

A staging that nobody has touched for **90 days** is deleted automatically.
Production instances are never affected.

The clock measures *use*, not age: a staging somebody opens every week never
expires, however old it is. Two things reset it to zero:

- **You did something with it from the panel** — deployed, rebuilt, restarted,
  restored, refreshed it, connected as a user, or opened a shell on it.
- **Somebody logged into its Odoo.** The panel reads the last login from the
  staging's own database once a day.

Two things deliberately do **not** count. Rebuilds triggered by a push are code
arriving, not a person looking at it. And the health probes that run every few
minutes are the platform watching, not you.

!!! danger "A staging has no backup to go back to"
    Deletion removes the containers, the database and the files. Unlike
    [archiving](#ending-an-instance), it keeps no copy, so whatever was only in
    that staging is gone. If it holds something you want, download a backup
    before the deadline.

### What you see before it happens

You get two warnings, and the second one reaches you whatever your notification
preferences are:

| When | What happens |
|---|---|
| 14 days before | A warning, and an amber **Expires in N days** badge on the instance |
| 3 days before | A second, critical warning; the badge turns red |
| On the day | Deleted |

The badge appears next to the instance name in the project sidebar and in the
instance's own header, from the first warning on.

### How to stop it

- **Use it.** Anything in the list above starts the 90 days over.
- **Press Keep** on the badge in the instance header. One click, same effect.
- **Mark it as permanent** — *Instance detail → Settings → Never purge
  automatically*. For the ones that should stay regardless: a QA environment
  nobody logs into for months is still not disposable.

!!! note "Nothing is ever deleted the day the feature notices it"
    Each step waits for the one before it, and deletion waits at least a day
    after the final warning. A platform that had been down for a month comes
    back and *warns*; it cannot warn and delete in the same pass.

[Pull request previews](#pull-request-previews) follow the same rule, on top of
being deleted when their pull request closes.

Administrators can change the 90 days, or switch the whole thing off, under
**Settings → General → Staging autopurge**.

## Email on staging

A staging never emails anybody. Whatever it sends — password resets, order
confirmations, invitations — is caught by a mail server that runs alongside it
and goes no further. That is what makes it safe to restore a copy of production
and click around in it: the customers in that copy are real, and none of them
will hear from you.

Open **Instance detail → Mails** to read what was caught. The list shows when
each message arrived, who it was for and what it said; open one to see the HTML
as the recipient would have seen it, the plain-text alternative, and what was
attached.

!!! warning "The mailbox is temporary"
    The catcher keeps everything in memory. Restarting the instance — a rebuild,
    an update, a stop and start — empties it. If a message matters, read it
    before you restart.

**Clear mailbox** empties it on purpose, which is the way to make "now watch
what this button sends" readable.

Production is the opposite: its email is real and leaves the server. There is no
Mails tab there, and the SMTP relay you configure under *Networking* is the one
that will actually deliver.

## See also

- [Hosts](../hosts/index.md) — where instances run.
- [Backups](../backups/index.md) — how to keep their data safe.

!!! note "Reference page is in progress"
    A full per-screen reference for the Instance detail UI is coming soon.
