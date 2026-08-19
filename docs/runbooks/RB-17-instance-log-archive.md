# RB-17 — An instance's log archive is broken

**Severity:** warning · **When:** an `instance_logs_unhealthy` alert, or
"the log viewer has no days to pick".

Odoo writes its log to `logs/odoo.log` inside the instance's directory
on the host — a bind mount, so the file outlives the container a
rebuild replaces — and the host's logrotate turns it into one dated
file per day. Two things can break silently; the health probe raises
`instance_logs_unhealthy` with a `reason` in the alert payload.

## 1. Read the reason

Alerts panel → the alert's payload, or:

```sql
db$ SELECT i.name, a.message, a.payload
    FROM cloud_alert a JOIN cloud_instance i ON i.id = a.instance_id
    WHERE a.code = 'instance_logs_unhealthy' AND a.state = 'active';
```

- `fallback` — the mount is there but Odoo is still logging to the
  container, so nothing is being archived and the next rebuild will
  discard the history.
- `rotation_stalled` — the live file grew past 512 MB, so nothing is
  rotating it and the disk is filling up.

## 2. `fallback` — Odoo cannot write to the file

Almost always ownership: Docker creates a missing bind-mount source
itself, as root, and the container runs as uid 1000.

```bash
$ ssh <host> 'ls -ld ~/<project>/<instance>/logs'
```

Expected: `drwxr-xr-x … 1000 1000`. If it is `root root`, fix it and
recreate the container:

```bash
$ ssh <host> 'sudo chown 1000:1000 ~/<project>/<instance>/logs'
$ ssh <host> 'cd ~/<project>/<instance> && docker compose up -d odoo'
```

Then confirm Odoo is writing to it (the file's mtime should move):

```bash
$ ssh <host> 'stat -c "%y %s" ~/<project>/<instance>/logs/odoo.log'
```

If the directory does not exist at all, the instance has not been
rebuilt since file logging shipped — that is not an alert, just an
instance still on the old behaviour. A rebuild from the panel adopts
it (the step is part of every deploy and rebuild flavour). To apply it
without a full rebuild, run the deploy's own step by hand. The panel
uploads its scripts to a per-job directory it removes when the job
ends, so copy the script and its library over first:

```bash
$ ssh <host> 'mkdir -p /tmp/ic-logs/lib'
$ scp odoo/custom/src/core/incubacloud/scripts/instance_logs.sh <host>:/tmp/ic-logs/
$ scp odoo/custom/src/core/incubacloud/scripts/lib/common.sh <host>:/tmp/ic-logs/lib/
$ ssh <host> 'bash /tmp/ic-logs/instance_logs.sh install \
    ~/<project>/<instance> <project>-<instance> 60 && rm -rf /tmp/ic-logs'
```

`<project>-<instance>` is the instance's compose project name (the
`doodba_project_name` field); `60` is the retention in days
(`cloud.settings.odoo_log_archive_days`).

## 3. `rotation_stalled` — nothing rotates the file

```bash
$ ssh <host> 'cat /etc/logrotate.d/incubacloud-<project>-<instance>'
$ ssh <host> 'sudo logrotate -d /etc/logrotate.d/incubacloud-<project>-<instance>'
```

If the config is missing, the deploy could not write it (no sudo on
that host — the job log says so as a warning). Fix sudo access and
rebuild, or install the config by hand with the command above.

If the config is fine, the host is not running logrotate at all:

```bash
$ ssh <host> 'systemctl is-enabled logrotate.timer; ls -l /etc/cron.daily/logrotate'
$ ssh <host> 'sudo systemctl enable --now logrotate.timer'
```

Force one rotation to reclaim the disk immediately:

```bash
$ ssh <host> 'sudo logrotate -f /etc/logrotate.d/incubacloud-<project>-<instance>'
```

## 4. Finding the right day

The viewer's day picker filters as you type (`08-17`) and steps with
◀ ▶. When you do not know the date, type the error in the filter box
and press **Enter**: the host greps every archived day and the picker
marks the ones that match with their hit counts. The sweep is bounded
— newest 60 files and 30 s by default, both on Settings → General →
Instance Logs (`log_search_max_files`, `log_search_timeout_s`); if it
is cut short the viewer says so rather than reporting "no match".

## 5. Verify

The alert clears on the next health probe (a few minutes). The viewer
(Instance detail → Logs) should offer today plus the archived days.

## 6. If the viewer shows a day as missing that `ls` shows on the host

The panel reads regular files with logrotate's own names only. A
`logs/odoo.log.<date>` that is a symlink is deliberately ignored — the
reader and the health probe run on the host as the SSH user, and
following a link planted from inside the container would let them read
host files — and it does not count against the sweep's file budget
either. Check with `ls -l ~/<project>/<instance>/logs`; if a day is a
link, something created it that should not have, and that is worth
investigating rather than working around.

A day shown as plain (`odoo.log.<date>`) that the host has meanwhile
compressed into `.gz` still opens and downloads: the plain name falls
back to its compressed twin, so a viewer left open over midnight keeps
working.

## Rollback

There is nothing to roll back: the archive is additive. If a host must
stop archiving, remove its config
(`sudo rm /etc/logrotate.d/incubacloud-*`) — but note the next deploy
or rebuild writes it again, by design.
