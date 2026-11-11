# Restore a backup

Three flavours:

- **Download exact** — full database with original users, crons, mail servers, credentials.
  For forensic recovery.
- **Download neutralized** — same data, but crons turned off, mail servers archived,
  credentials scrubbed, and a red ribbon on top. Safe to load on a dev laptop.
- **Restore in place** — replaces the live database of an instance with the chosen backup.
  Used to roll back a bad update.

## Pick the right mode

If you're moving data to development or staging, **download neutralized**.
If you're rolling back production after a broken release, **restore in place**.
If you need a forensic copy with original credentials and crons, **download exact**.

## Download a backup

1. Open the instance and go to **Backups**. You see a chronological list of backups
   available in your bucket.
2. Pick a backup, click **Download**.
3. Choose **Exact** or **Neutralized**.
4. The job starts and shows a progress bar.
5. When ready, click the download link. You get a standard ZIP
   (`dump.sql + filestore/`), the same on every Odoo version from 8.0 to 20.0 —
   restorable on any Odoo of the same version.

## Restore in place

1. Open the instance and go to **Backups**.
2. Pick the backup you want to restore.
3. Click **Restore**.
4. On production, you'll be asked to confirm twice. We want to be very sure.
5. Wait for the restore to finish. The instance is briefly unavailable while
   the database is replaced. When the job ends in green, you're back online with the older data.

Before Odoo starts, the restored database has its modules updated to the code
the instance runs now, as a rebuild does (unless **Auto-update modules on
rebuild** is off); the job's log names the branches and pinned commits. A
restore does not change the code: to undo a deployment, revert its code first,
or its migration runs again on the restored database. If the update fails, Odoo
stays stopped and the job says which module failed: fix it, dismiss the job's
alert and rebuild the instance.

!!! warning "In-place restore is destructive"
    The current database is replaced by the backup. Anything created or modified after
    that backup's timestamp is lost. Take a fresh manual backup first if in doubt.

## Restore from an external ZIP

A backup that did not come from here — an export from your previous provider, a
dump a colleague sent you — can be restored the same way. Three routes, and the
right one depends on where the file is and how big it is.

### From your browser

Open the instance → **Restore Database** → **Through your browser**, and pick the
ZIP. Nothing to install: the file is sent in pieces, so it is not limited by the
size a single request may carry, and a piece that fails is re-sent on its own. A
progress figure tells you where it is.

The ceiling here is disk, not the network: the file waits on the panel's own
storage until it is sent to the host. 2 GB by default; your administrator can
raise it.

### From your computer, without a size limit

For a 20 GB database, or when you would rather not send it through the panel at
all, use **Through SSH**. You do not need an account on the machine and you do
not need to have a key of your own:

1. Click **Give me an upload key**. We generate a key pair, install the public
   half on the host, and show you the private half **once**.
2. Copy the command shown, put your file's name in place of
   `<your-backup.zip>` and run it. It writes the key with private
   permissions, sends your file as `restore.zip`, and removes the key
   afterwards.
3. Click **Check what arrived**. The host reports the name, the size and the
   SHA-256 of the file it actually received — check it is yours.
4. Click **Import**.

The key can only write into one directory on the host, cannot open a shell, and
stops being accepted after twelve hours whether or not anyone remembers to
remove it. The restore removes it as soon as it starts.

!!! note "Windows"
    The command uses `rsync`, which Windows does not ship. On Windows use the
    browser route, or **From a link** below.

### From a link

If the backup already lives somewhere reachable — your previous provider's
download URL, an FTP or SFTP server, a pre-signed link from object storage —
use **From a link** and paste the address. The host downloads it itself, so
nothing passes through your connection or through the panel, and there is no
size limit. `https://`, `sftp://` and `ftp://` are accepted, and a link carrying
a user and password works too; the password is stored encrypted and never shown
again.

### Bring an Odoo you run elsewhere

To move the data of an Odoo installed somewhere else — a package install, a
container, another provider — into a **new production instance**:

1. **Check the three things a restore needs.**
    - **The same Odoo series** as the database: a database of another version
      has to be upgraded first.
    - **Every module the database has installed**, available to the instance:
      put the repositories that carry them in the project
      ([which modules a repository brings](../projects/index.md#which-modules-a-repository-brings)).
    - **PostgreSQL** of the source's major version or a newer one, chosen on the
      instance form's **Database** tab.
2. **Make the ZIP** on the source.

    === "With Odoo's database manager"

        Open `/web/database/manager`, **Backup** the database with the format
        **zip** (the other one leaves the filestore out). For small databases:
        the download goes through Odoo's web server.

    === "By hand, on the source server"

        ```bash
        mkdir odoo-export && cd odoo-export
        pg_dump --no-owner --format=plain --dbname=<database> > dump.sql
        cp -r <data_dir>/filestore/<database> filestore
        zip -r ../odoo-export.zip dump.sql filestore
        ```

        `<data_dir>` is the `data_dir` of the source's Odoo configuration
        (`~/.local/share/Odoo` of the user Odoo runs as, when unset). Run
        `pg_dump` with the client of the source server's PostgreSQL.

    Either way, `unzip -l` shows `dump.sql` and `filestore/…` at the top of
    the archive, not inside another folder.

3. **Create the instance**: a production instance in a project with those
   repositories and a [backup destination](index.md#backup-destinations).
   **Deploy** it: it comes up with an empty database, which the restore
   replaces.
4. **Restore Database**, by one of the three routes above. On a panel or a host
   only your network reaches, use the browser or SSH: a link is fetched from
   the internet only.

The job stops Odoo, replaces the database and the filestore, installs the
panel's sign-in module if it is missing, and starts Odoo again. The archive is
loaded as a **copy**: the database gets a new unique id and a new secret, so it
does not pass for the source database.

!!! warning "Nothing is switched off"
    The data comes back as it was: scheduled actions, outgoing mail servers,
    users. If the source installation still runs, both now send mail and run
    the same scheduled actions. Stop the source first, or switch them off on
    one side.

Before Odoo starts, the modules whose code differs from what the database last
ran are updated to the instance's code, as a rebuild does, unless **Auto-update
modules on rebuild** is off. If that update fails, Odoo stays stopped and the
job names the module: fix it, dismiss the job's alert and rebuild the instance.
Sign in with the users and passwords of the source database.

## Verify it worked

For downloads:

- [x] The ZIP opens and contains `dump.sql` + `filestore/`.

For an Odoo brought from elsewhere:

- [x] The instance opens on your data, attachments and images included.
- [x] **Settings → Technical → Automation → Scheduled Actions** shows the ones
  you expect active, and only those.

For in-place restore:

- [x] The instance is back up.
- [x] The data matches the backup's timestamp.
- [x] The job log ends in green.

## Troubleshooting

??? note "Restore failed: passphrase mismatch"
    The backup was encrypted with a different passphrase than the one currently
    configured on the backend. Check `Backend detail → Passphrase`. If you've
    rotated passphrases, you need the original to read older backups.

??? note "The restore of an archive from elsewhere fails while loading it"
    Check the archive's layout first: `dump.sql`, in plain SQL, at the top of
    the ZIP, with `filestore/` beside it (`unzip -l`).

??? note "The instance starts but Odoo fails on a module"
    A module installed in the database is not available to the instance: its
    repository is missing, or left out by the repository's **Modules** list.
    Add it and rebuild.

??? note "Restore failed: schema version mismatch"
    The backup was taken from a different Odoo version than the instance you're
    restoring to. Deploy a fresh instance with the matching Odoo version, then
    restore there.

??? note "The upload key expired before I finished"
    Ask for another one — they are single-use and cost nothing. Each key writes
    into a directory of its own, so the new command starts the upload from the
    beginning: what the previous key got across is not reused.

??? note "The link was refused"
    We only fetch from `https`, `sftp` and `ftp`, and only from an address that
    is reachable from the public internet. A link pointing at a private or
    internal address is refused on purpose: otherwise "restore from a link"
    would be a way to make our hosts read things they should not.

??? note "I want to restore to a different instance, not the source"
    Deploy a new instance, configure its backup backend to point to the same
    bucket, and the backup list will include the source's backups. Pick the one
    you want and Restore.

## See also

- [Backups overview](index.md)
- [Migrate from Odoo.sh](../migrations/from-odoosh.md) — uses the same restore flow.
