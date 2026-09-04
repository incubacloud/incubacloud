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
command="/usr/bin/rrsync -wo /tmp/incubacloud-restore/<token>/",restrict,expiry-time="<stamp>" ssh-ed25519 AAAA… ic-restore-<token>
```

Every part is load-bearing:

- `rrsync -wo <dir>` — rsync's own restricted wrapper, **write only**,
  confined to that directory. No shell, no reads, nothing else runs.
- `restrict` — no pty, no forwarding of ports or agent, no user rc.
- `expiry-time` — OpenSSH stops accepting the key on its own, whether or
  not anything remembers to remove it (needs OpenSSH ≥ 8.9; the fleet is
  well past that).
- The comment is the marker removal keys on, so an operator's own keys
  in the same file are never touched.

If the host has no `rrsync`, the job installs it from the `rsync`
package's scripts. **If it cannot, the grant is closed rather than
installed unrestricted** — that trade is not made silently.

### Checking what arrived

`Check what arrived` runs `stat` and `sha256sum` on the host and shows
the result. The restore refuses to start until that has been done. This
is the control that matters: if the key leaked inside its window,
someone could have written *a* file into the directory, and the digest
is what tells you it is not yours.

### Revoking

Three things revoke a grant, and all are idempotent:

- the restore that consumes the archive, when it starts;
- `cron_cloud_restore_upload_grant_gc`, daily, for anything expired;
- the operator, from the panel.

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
- redirects are **not** followed, since a redirect is precisely how the
  validation would be escaped;
- credentials are split off the URL, stored encrypted apart from the job
  payload, and delivered to the host as a `netrc` file created and
  removed inside the same step — never on a command line, where `ps`
  would show them.

The download resumes (`curl -C -`), so a dropped connection on a large
archive costs the remainder, not the whole.

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
