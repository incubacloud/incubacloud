# RB-19: Get a large backup onto a host and restore it

**Severity:** planned
**Typical trigger:** a customer sends a 20 GB dump; a restore has to be
done on a host nobody holds credentials for; the browser upload is not
an option.
**Who runs it:** ops, or a developer with backup rights.

Four routes exist, and the one to pick depends on where the archive is,
not on how large it is.

| Route | Archive is | Needs | Ceiling |
|---|---|---|---|
| Browser | on the operator's machine | nothing | the panel's staging disk (`incubacloud.restore_upload_max_bytes`, default 2 GiB) |
| Upload key | on the operator's machine | `rsync` and a terminal | none |
| Link | already on a server | a reachable URL | none |
| Platform backup | taken by us | nothing | none |

A backup the platform took (scheduled, pre-restore, clone-to-staging,
host move) never needs any of this: it is restored host-to-host and
never crosses HTTP.

## Why the browser route has a ceiling at all

Not the network. The archive is streamed in 32 MiB pieces, so no proxy
in front of the panel caps it — that was the old failure, a 413 produced
by Cloudflare's 100 MB request limit before Odoo ever saw the body. What
remains is disk: the rebuilt file waits under the panel's data directory
until the executor sends it to the host, and a 2 GiB archive pins 2 GiB
there. Raise deliberately:

```
db$ INSERT INTO ir_config_parameter (key, value)
    VALUES ('incubacloud.restore_upload_max_bytes', '5368709120')
    ON CONFLICT (key) DO UPDATE SET value = excluded.value;
```

Abandoned uploads are swept after 24 hours by
`cron_cloud_restore_staging_gc`.

## Upload key: what actually gets installed

`Restore Database → Through SSH → Give me an upload key` generates a key
pair, installs the public half on the host and shows the private half
once. The line added to the host's `authorized_keys` is:

```
command="exec /bin/bash -c 'ulimit -f <KiB> && exec /bin/bash /var/tmp/incubacloud-restore/.door.sh receive /usr/bin/rrsync -wo -munge -no-del /var/tmp/incubacloud-restore/<token>'",restrict,expiry-time="<stamp>" ssh-ed25519 AAAA… ic-restore-<token>
```

The directory is in `/var/tmp`, not `/tmp`: Debian 13 and Ubuntu 26.04
(Ubuntu since 24.10) mount `/tmp` as a tmpfs of half the memory, 1.9 GiB
on a server of 4 GB, and before 1.0.239 no archive larger than about half
of that could be sent, whatever the instances' disk had free. `/var/tmp`
is on disk, and on the usual server the same disk Docker keeps the
instances on. Every restore lands its archive there: the doors, the path
`restore_from_host_file` reads, and the restore's own working directory
(`restore_space.STAGING_ROOT`). So do the downloads that become a
restore (clone to staging, refresh from production, move: the dump, the
archive and its handoff), the other backup downloads and the export,
each the size of the instance's data. A download measures what it is
about to write first, and refuses, writing nothing, when that and its
copies would leave less than the 5 GiB reserve free
(`backup_download.sh`, `IC_RESERVE_BYTES`).

A door takes **one file**, `restore.zip`, sent as the dialog says:

```
rsync -P --inplace -e "ssh -i ic-upload-key" <your-backup.zip> <account>@<server>:restore.zip
```

`/var/tmp/incubacloud-restore/.door.sh` (`scripts/restore_door.sh`, put
there by the job that installs a key) reads the command the client sent
and passes on to `rrsync` only `rsync --server`, options that send one
file and write nothing beside it, `--inplace` (or `--append`), and
`. restore.zip`; it creates `restore.zip` empty first, so rsync itself
refuses a second file. `-a`, `-r`, `-d`, `-R`, `-b`, `-s`, another name,
or no `--inplace` are refused before anything is written, with the
command to use, and so is every short option but `-v`, `-t` and `-z`:
`-X` and `-A` would store metadata neither `ulimit -f` nor `du` counts,
`-D` a device or a pipe, and `-o`, `-g` and `-p` an owner, a group and a
mode that hold where the panel connects as root. The name is never free
while a key works: the restore takes the archive with a hard link and
renames an empty file over `restore.zip`, so a session held open since
before still finds a file there. So `ulimit -f` bounds all the door holds, not each file
of it: a key that took any number of files, each up to the limit, could
fill the disk in the five minutes between two watches (18-nov-2026).

The job that installs the key measures that disk and Docker's, and sets
`<KiB>` to the door's share of them: the largest archive they can
restore with the copy the restore makes of it
(`restore_space.largest_archive`), less what the server's other open
doors may still receive (their limit, less what they already hold).
Two doors of one server opening at once are measured one after the
other, under a lock of the server's doors, so they never count the same
free disk twice. The Import dialog shows the limit once that job has
run, and so does `check_restore_upload`. A larger file stops at that
size (rsync says *File too large*), and *Check what arrived* says the
archive is larger than the door takes instead of offering what arrived.
Where Docker keeps the instances on a disk of its own, each disk keeps
the 5 GiB reserve: the one the archive lands on is then the server's
root disk.

Every part is load-bearing:

- `.door.sh receive` — one file, `restore.zip`, in place, nothing
  beside it (above).
- `rrsync -wo -munge -no-del <dir>` — rsync's own restricted wrapper,
  **write only**, confined to that directory. No shell, no reads,
  nothing else runs. `-munge` stores a link it receives so that it
  leads nowhere: without it, one session uploaded a link to any
  directory of the account and the next wrote through it (its
  `authorized_keys` included). `-no-del` refuses `--delete`.
- `restrict` — no pty, no forwarding of ports or agent, no user rc.
- `expiry-time` — OpenSSH stops accepting the key on its own, whether or
  not anything remembers to remove it (needs OpenSSH ≥ 8.9; the fleet is
  well past that).
- The comment is the marker removal keys on, so an operator's own keys
  in the same file are never touched.

If the host has no `rrsync`, the job installs it from the `rsync`
package's scripts — only the one of rsync 3.2.4 and later, which knows
`-munge` and `-no-del`; the Perl one before it does not, and is never
installed nor used. **If there is none, or only an older one, the grant
is closed rather than installed** — that trade is not made silently.
Update rsync on the host, or restore from a link. It is installed as
root: directly when the panel connects as root, with `sudo -n`
otherwise. An account without passwordless sudo gets `NOSUDO` and is
told so: install the script as root at `/usr/bin/rrsync` (mode 755).

### Checking what arrived

`Check what arrived` runs `stat` and `sha256sum` on the host and shows
the result. The restore refuses to start until that has been done. This
is the control that matters: if the key leaked inside its window,
someone could have written *a* file into the directory, and the digest
is what tells you it is not yours.

The restore names the digest the dialog showed: the panel refuses it
if another check has recorded a different file since, and the restore
job checks it again before taking the archive. It copies the archive
into its own directory and checks the copy's digest against that one,
so anything still written through the key afterwards cannot change what
is restored. Until the original is
removed the host needs room for both copies, unless its filesystem can
share their blocks (`cp --reflink=auto`).

Where the panel connects as root (the platform's hosts), what the key
receives may keep device-node or setuid bits. Two things keep that
harmless: the grants' root, `/var/tmp/incubacloud-restore`, is 0700 and the
panel's account's own (checked each time a key is installed), so no
other account reaches inside it; and the restore takes only a regular
file, with a plain `cp`, which keeps no setuid bit.

### Revoking

Removing a key ends the sessions it still has open: the panel account's
`rsync` processes working in the door's directory. Until the job that
removes it has done so, a door closed in the panel still counts against
the server's free disk when another door opens, and the watch still
measures it (`key_removed`), up to its deadline.

Three things revoke a grant, and all are idempotent:

- the restore that consumes the archive, when it starts — the key
  goes, the directory stays, so a failed restore retried within three
  days needs no second upload;
- `cron_cloud_restore_upload_grant_gc`, daily, for anything expired,
  and for consumed grants three days after their restore: that removes
  the directory and whatever is left in it;
- the operator, from the panel.

On a host where that removal never ran, the daily sweep of `/tmp` and
`/var/tmp` (`docker_prune`) removes the grant directories three days old,
and a restore's working directory its worker left: only the panel's
account's own, and only under a root that is its own. `/var/tmp` survives
a reboot, where a tmpfs `/tmp` did not: the sweep is what empties it. It
also sweeps `/tmp/incubacloud-restore`, where releases before 1.0.239 put
the doors, and the handoffs they left in `/tmp`. The update to 1.0.239
closes every door open then, whose key took any number of files and wrote
to `/tmp`; the Import dialog says to ask for another key.

To check what is outstanding:

```
db$ SELECT id, instance_id, state, expires_at, fingerprint
    FROM cloud_restore_upload_grant
    WHERE state = 'granted';
```

To confirm a host is clean:

```
$ ssh <host> "grep -c ic-restore- \$HOME/.ssh/authorized_keys"
```

## Link route: what is checked before the host dials

The panel validates, not the host:

- scheme is `https`, `sftp` or `ftp`;
- the name resolves, and **every** address it resolves to is public — a
  name answering with one public and one private address is refused
  outright;
- the address is pinned into the download with `curl --resolve`, so the
  name cannot answer differently by the time the host dials it;
- the host name must be plain ASCII: an internationalised name is
  written in its punycode form (`xn--…`), since Python and the host's
  curl may turn it into different ASCII names. curl gets the link
  rebuilt from the name checked, so the pin always matches it;
- curl reads nothing the host configured (`-q`, `--noproxy '*'`): no
  `~/.curlrc`, no proxy from the environment;
- redirects are **not** followed, since a redirect is precisely how the
  validation would be escaped;
- credentials are split off the URL, stored encrypted apart from the job
  payload, and delivered to the host as a curl config file, owner-only
  in the job's directory and removed when the download ends — never on
  a command line, where `ps` would show them. (A `netrc` file was used
  before: curl before 7.84, Ubuntu 22.04's included, does not read the
  quoting a password with a space, a quote or a `#` needs there.)

A download that fails is not resumed: each attempt starts over in a
directory of its own.

## Rollback

- **An upload key you no longer want:** revoke it from the panel, or
  `UPDATE cloud_restore_upload_grant SET state = 'revoked'` and run the
  sweep. The key stops working at its expiry regardless.
- **A staged browser upload that will not be used:** delete the file
  under `<data_dir>/incubacloud-restore/`; the sweep does it within 24h.
- **A restore already running:** it cannot be undone. On production a
  safety backup is taken first, always, and restoring that is the way
  back.

## See also

- [RB-05: triage a failed job](RB-05-triage-failed-job.md)
- [Backups: restore](../user/docs/backups/restore.md) — the same routes,
  written for the person using them.
