# Migrate from another host

Your company's Odoo runs somewhere else today: on a server of your own, at a
hosting provider, in Docker or installed as a package. This page moves it to
IncubaCloud with what the panel already does: import the project, bring the
data, try it, and switch over. For Odoo.sh, see
[Migrate from Odoo.sh](from-odoosh.md), which follows the same plan.

## Before you start

Write down four things about the Odoo you have:

| What | Where to find it | Why |
|---|---|---|
| **Odoo version** | In Odoo, *Settings*, at the bottom (for example *Odoo 18.0*) | The new instance runs the same one: a restore does not change versions. |
| **Edition** | *Settings*: Community or Enterprise | Enterprise needs its code and your own Odoo Enterprise subscription ([Projects](../projects/index.md)). |
| **Your own modules** | In Odoo, *Apps*, filter *Installed*: those that are neither Odoo's nor in its app store, or ask whoever wrote them | Their code has to be in a Git repository the project reads. |
| **Size of the data** | The size of a backup ZIP | It decides how the file reaches the panel. |

!!! warning "Same version, or upgrade first"
    The panel does not upgrade a database to another Odoo version. To move to a
    newer version, upgrade the database first, with Odoo's upgrade service for
    Enterprise or with OpenUpgrade for Community, and restore the upgraded one.

!!! note "Odoo Online"
    A database on Odoo Online runs Odoo Enterprise, sometimes on an
    intermediate version only Odoo hosts. Before you download its backup, have
    it on a major version (such as 18.0 or 19.0), and plan for the Enterprise
    subscription, or for leaving the Enterprise apps behind.

## 1. Get a host and backups

If you have not yet: a [host](../hosts/index.md) of the right size
([which one](../getting-started/zero-to-odoo.md#2-choose-the-server)) and a
[backup destination](../backups/index.md#backup-destinations). A production
instance is not deployed without the second.

## 2. Bring the project

Pick the case that matches where your Odoo runs.

=== "A doodba project on a server"

    Your Odoo runs from a doodba project folder (with `.copier-answers.yml`) on
    a server you reach by SSH.

    1. [Connect that server](../hosts/connect.md) to the panel, as a host.
    2. Open it in **Hosts**, then **More → Import Instance**.
    3. Browse to the project's folder and import it.

    The panel reads the project's files (`.copier-answers.yml`, `repos.yaml`,
    `addons.yaml`, the `.env` files and the Compose override) and creates the
    project and its instance as they are, without deploying again. You can
    skip steps 3 and 4: the data is already there.

=== "Code in a Git repository"

    Your own modules are in a Git repository: a doodba project, an Odoo.sh
    one, or one with modules only.

    1. On **Projects**, press **Import from GitHub** and paste the repository's
       URL (for GitLab, see [Repositories on GitLab](../projects/gitlab.md)).
    2. Pick the branch. The panel reads the Odoo version and the repositories
       from it, and creates the project with a production instance ready to
       deploy.

    A private repository needs the panel's GitHub App or a token first:
    [Create your first project](../getting-started/first-project.md#3-connect-a-git-repository-optional-recommended).

=== "No code of your own"

    Your Odoo uses only Odoo's modules, and perhaps some from the app store.

    1. On **Projects**, press **New Project**, and choose the **Odoo Version**
       of your database.
    2. Add the production instance with the same modules, as in
       [Deploy your first instance](../getting-started/first-deploy.md).

    Modules from the app store go in a Git repository of yours, for the
    project to read them.

## 3. Deploy the production instance

Deploy it once, empty: it is what the data is restored into. Choose on its
**Database** tab a PostgreSQL of the same major version as today's, or a newer
one.

## 4. Bring the data

Make a ZIP of the database with its filestore on the old host, and restore it
on the new instance with **Restore Database**.

!!! warning "Two Odoos sending the same mail"
    Nothing is switched off by the restore: the new production comes back with
    the scheduled actions and the mail servers of the old one. While the old
    Odoo runs, both would send the same mail to your customers and fetch the
    same mailboxes. Switch them off on the new one right after restoring
    (point 3).

1. **Make the ZIP**: from Odoo's database manager (`/web/database/manager`,
   **Backup**, format **zip**), or by hand on the server. The
   [steps](../backups/restore.md#bring-an-odoo-you-run-elsewhere) for both.
2. **Send it** the way its size allows: through your browser (up to 2 GB by
   default), through SSH with a temporary upload key (no limit), or as a link
   the host downloads itself (no limit):
   [Restore from an external ZIP](../backups/restore.md#restore-from-an-external-zip).
3. **Right after it, switch off** in the new Odoo, with the developer mode on,
   under **Settings → Technical**: the **Automation → Scheduled Actions**, and
   the **Email → Outgoing Mail Servers** and **Email → Incoming Mail
   Servers**. Select them all and archive them (**Actions → Archive**). The
   final restore of step 6 brings them back.
4. **Sign in** with the users and passwords of the old database.

## 5. Try it before you switch

The old Odoo keeps working while you try the new one: nobody has moved yet.

- **Try it in a staging, not in production.** **Clone to Staging** makes a
  copy where mail goes nowhere ([why](../instances/index.md#email-on-staging)),
  so you can open documents and run your usual tasks without writing to your
  customers.
- **Check what matters to you**: the last invoices, stock, reports, your own
  modules' screens, an import you do every week.
- **Keep the scheduled actions and mail servers off** on the new production
  until the switch (step 4,
  [details](../backups/restore.md#bring-an-odoo-you-run-elsewhere)).

## 6. Switch over

Pick a quiet moment, outside working hours, and tell your team.

1. **The day before**, lower the TTL of your domain's DNS record, if your DNS
   lets you, to a few minutes: the change will then be seen sooner.
2. **Stop work on the old Odoo**: stop it, or tell everyone not to use it.
3. **Make a fresh ZIP and restore it** on the new production (step 4), so
   nothing done since the test is lost. This time leave the scheduled actions
   and the mail servers on: the old Odoo no longer runs.
4. **Point your domain at the new host**: change the A record to the new
   host's IP address, and add the name to the instance's **Networking →
   Domains** ([how](../getting-started/zero-to-odoo.md#6-your-own-domain)).
5. **Deploy** the instance for its certificate, sign in, and check that mail
   leaves ([outgoing mail](../getting-started/zero-to-odoo.md#7-mail-that-leaves-odoo)).

Keep the old host as it is for a few days, stopped, in case you need to look
something up. Cancel it when you are sure.

## Verify it worked

- [x] Your domain opens the new instance, with HTTPS.
- [x] The last documents from before the switch are there.
- [x] A test mail arrived, and not in spam.
- [x] The morning after, the instance's **Backups** tab lists a backup.

## If you need help

Write to us through [the contact form](https://www.incubacloud.io/contactus)
with where your Odoo runs today, its version and edition, the size of a backup,
and whether it has modules of your own. We reply with the steps for your case.

## See also

- [Migrate from Odoo.sh](from-odoosh.md)
- [Restore a backup](../backups/restore.md)
- [From zero to Odoo in production](../getting-started/zero-to-odoo.md)
