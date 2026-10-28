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
command="/usr/bin/rrsync -wo -munge -no-del /tmp/incubacloud-restore/<token>",restrict,expiry-time="<stamp>" ssh-ed25519 AAAA… ic-restore-<token>
```

Every part is load-bearing:

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
harmless: the grants' root, `/tmp/incubacloud-restore`, is 0700 and the
panel's account's own (checked each time a key is installed), so no
other account reaches inside it; and the restore takes only a regular
file, with a plain `cp`, which keeps no setuid bit.

### Revoking

Three things revoke a grant, and all are idempotent:

- the restore that consumes the archive, when it starts — the key
  goes, the directory stays, so a failed restore retried within three
  days needs no second upload;
- `cron_cloud_restore_upload_grant_gc`, daily, for anything expired,
  and for consumed grants three days after their restore: that removes
  the directory and whatever is left in it;
- the operator, from the panel.

On a host where that removal never ran, the daily sweep of `/tmp`
(`docker_prune`) removes the grant directories three days old: only the
panel's account's own, and only under a root that is its own.

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
