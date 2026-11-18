# RB-44 — The server's restart for its updates

**Severity:** critical · warning · **When:** a `host_reboot_failed`,
`host_reboot_incomplete` or `host_reboot_pending` alert, a job that says «Waiting for the restart of the
server (job #N)», a Restart Server job that ended «Not restarted».

## How it works

A hardened server installs its security patches by itself and never
restarts by itself. When an update needs a restart (`/run/reboot-required`),
the panel restarts the server inside the host's restart window (from 04:00
for 3 hours, UTC, by default), once nothing of the panel runs there, and
checks that it comes back. While it does, the jobs of the server and of its
instances wait. The panel restarts a server once per opening of its window:
one whose update still asks for a restart once it is back waits for the next
night. A server the panel has not checked for a day restarts by itself
(«A panel away», below). The details: `docs/architecture.md`, «Host
maintenance window».

| Field of the host | Default | What |
|---|---|---|
| `reboot_window_start` | 4.0 | Hour the window opens at, in its time zone |
| `reboot_window_hours` | 3 | Hours it stays open; the panel tries every 30 min |
| `reboot_tz` | UTC | Time zone of the window |
| `auto_security_updates` | on | Off: the panel never restarts the server by itself |
| `reboot_required_since`, `reboot_required_pkgs` | — | Since when a restart is pending, and for which packages |

The defaults are constants at the top of
`incubacloud/models/cloud_host_reboot.py`, one line each.

## `host_reboot_failed` (critical)

The server did not answer 15 minutes after the order to restart, or
answered with the boot it had before. The Restart Server job failed; its
hold ended with it, so the jobs that waited go on (and fail to connect while
the server is down).

1. Open the server's console at its provider: is it booting, stuck in a
   disk check, asking for something?
2. Once it answers SSH, run Check on the host. Rebuild nothing yet: the
   instances come back by themselves with the server (`unless-stopped`).
3. Look at the instances' status; one that stays down raises its own
   `instance_down`.
4. When it answered with the same boot: someone cancelled the restart on
   the server (`shutdown -c`), or the order did not take. The next window
   tries again.

The alert closes by itself once the server answers the panel's check with
another boot than the one it had before the restart: restarted by the
panel, by a person or by itself. The check runs every 6 hours; Check on
the host does not run it.

## `host_reboot_incomplete` (warning)

The server came back, but Docker is not running, or some containers that
ran before the restart are not running again after 5 minutes; the alert
names them. The panel starts nothing by hand. Only the containers whose
restart policy brings them back after a boot (`always`, `unless-stopped`)
are waited for; the job's log names the others it left out (a container
started without a policy, or `on-failure`, which Docker does not start
again after a clean stop).

1. On the server: `systemctl status docker`, then `docker ps -a` for the
   containers named.
2. Start each instance named from its page (Start), or Full Setup for the
   proxy. An instance that stays down also raises its own `instance_down`.

The alert closes by itself once the panel's check finds Docker and every
container it named running, however they came back.

## `host_reboot_pending` (warning)

A restart has been pending for 7 days.

- **The panel restarts this server** (hardened, Auto Security Updates on):
  each pass of the window found a job running, or the server said no. The
  check's lines say why («Not restarted in this pass: …»), and so does a
  Restart Server job that ended «Not restarted: …»; look also at long jobs
  running at night. Widen the window, move it, or restart the
  server by hand at a quiet moment.
- **It does not** (Auto Security Updates off, or not hardened): restart it
  yourself when nothing runs there:

  ```bash
  sudo shutdown -r +1
  ```

The alert closes once the check finds nothing pending.

## The server says no: `reboot-guard.d`

`host_reboot.sh guard` vetoes the restart while a step of a job runs apart,
a copy or a load runs (`duplicity`, `pg_dump`, `pg_restore`,
`pg_basebackup`), or an image builds. The owner of a server adds a veto of
their own with an executable in `/etc/incubacloud/reboot-guard.d/`: it runs
with a 60 s limit, and any exit other than 0 vetoes the restart for this
pass.

```bash
sudo install -d -m 755 /etc/incubacloud/reboot-guard.d
sudo tee /etc/incubacloud/reboot-guard.d/10-my-batch >/dev/null <<'EOF'
#!/bin/sh
# No restart while the nightly export runs.
! pgrep -f my-nightly-export
EOF
sudo chmod 755 /etc/incubacloud/reboot-guard.d/10-my-batch
```

## A panel away

Each check leaves on the server the panel's mark
(`/var/lib/incubacloud/reboot/panel-seen`: when it checked, whether it
restarts the server by itself, its window) and the instances to count
inside (`/var/lib/incubacloud/reboot/inside/`); on a server the panel
restarts by itself it also installs the timer
`incubacloud-reboot-when-away` (every 30 minutes) and its copy of the script
in `/usr/local/lib/incubacloud/`. When the panel has not checked the server
for 24 hours (`REBOOT_PANEL_AWAY_H`: a panel that sleeps through the window,
or one that is gone), the timer restarts it by itself, with the same rules:
a restart pending, inside the window, once per opening, the same veto
(`reboot-guard.d` included), no SSH session open, nothing running inside its
instances. Once the panel checks it again, the panel decides. The check
after such a restart says it in its log.

```bash
journalctl -u incubacloud-reboot-when-away --since today
cat /var/lib/incubacloud/reboot/panel-seen
```

A server whose Auto Security Updates is off is marked so: the timer does
nothing there.

## A server hardened before 1.0.232

Its policy still says `Automatic-Reboot "true"` until its first check, in
the 30 minutes after the update: the check turns it off, with no new
hardening run. To see it:

```bash
grep Automatic-Reboot /etc/apt/apt.conf.d/52incubacloud-unattended
```

## Rollback

A server no longer restarts by itself: the check turns the server's own
restart off on every host it reaches, whatever the host's settings. To keep
the panel from restarting a server, switch Auto Security Updates off on the
host; the pending restart is still recorded, and alerted after 7 days.
