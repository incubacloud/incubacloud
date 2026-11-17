# Security alerts

The alerts the panel raises from your servers' [security logs](../hosts/security.md),
what each one means and what to do. They reach you through your
[notification channels](notifications.md), like every other alert, and exist
only for servers that send their security logs.

There are two kinds:

- **Events**, read from the logs every 5 minutes: a sign-in, a `sudo` command
  or a change that the panel did not make. They do not close by themselves:
  dismiss one once you know what it was. If the same thing happens again
  while it is open, the alert says how many times and since when, and you
  are notified again of each new one, at most once an hour per server: the
  ones within an hour of that server's last notice only update the alert
  on the panel.
- **Rules**, measured on the servers' counters every 5 minutes, like the
  [other metric rules](monitoring.md#alerts). They close by themselves when
  the figure goes back under the threshold.

Each text ends by pointing at the server's **Metrics → Security** view,
where an Administrator sees the addresses, users and lines behind it.

## Sign-ins over SSH

**`security_ssh_login_foreign`** · critical. Someone signed in to the server
over SSH in a way the panel never does: with another key, with a password
where the panel uses a key, or as another user. The text names the user, the
address and the time. A known key used from outside the addresses it may
sign in from raises it too, and the text names the key.

- If it was you or a colleague (a key of your own, your provider's console),
  dismiss it. To stop the alerts of a key of your own, add it to the
  server's [known keys](../hosts/security.md#known-keys).
- If nobody you know signed in, treat the server as taken over: limit who may
  reach SSH (**Apply Hardening** with the server's allowed SSH IPs), remove
  the keys you do not know from `~/.ssh/authorized_keys` of every user, and
  look at **Changes to watched files** and **sudo** in the Security view for
  what was done.

**`security_ssh_login_panel_key_elsewhere`** · critical. The panel's own key
was used from an address the panel does not connect from. Before raising it,
the panel opened a connection of its own to the server to read the address
it connects from, and it was another one. The key may have been copied.

- Replace the panel's key for the server on its **Connection** tab, put the
  new public key on the server and remove the old one from
  `authorized_keys`.
- Then look, as above, at what was done from that address.

**`security_known_key_added`** · critical. An Administrator added a key to
the server's [known keys](../hosts/security.md#known-keys): its sign-ins no
longer raise an alert. The text names who added it and the key's name and
fingerprint, and says whether the key may sign in from some addresses only,
never which. Removing the key does not close it.

- If you expected it, dismiss it.
- If not, remove the key in **Metrics → Security**, talk to whoever added it,
  or take away their access to the panel, and look at **Sign-ins over SSH**
  for what the key was used for.

**`security_ssh_login_unconfirmed`** · warning. A sign-in with the panel's key
or user came from an address the panel was not seen using, and the panel
could not connect to the server to check whether the address is its own. It
carries no address. It usually means the server was unreachable at that
moment; if the server is reachable and it repeats, look at the sign-ins in
the Security view.

## sudo

**`security_sudo_foreign`** · warning. A user the panel does not sign in as
ran a command with `sudo`, or was refused one. The text names the user and
the program, never its arguments. If that user is yours, dismiss it. A
`sudo` line alone proves nothing (see
[What the logs cannot prove](../hosts/security.md#what-the-logs-cannot-prove)):
read it with the sign-ins around it.

## Changes to the watched files

**`security_sensitive_change`** · critical. `passwd`, `shadow`, `sudoers` or the
SSH configuration was changed by another user, or by the panel's own user
outside every job, console and terminal of the panel. A change by `root`
outside the panel's work is a warning, because the server's own scheduled
tasks also run as `root`. The text names the file, the program and who.

- If you changed it (a user added by hand, an SSH setting), dismiss it.
- If not, open **Changes to watched files** and **Sign-ins over SSH** to see
  who was signed in at that time.

## Brute force

**`security_ssh_bruteforce`** · warning. More than 100 failed SSH logins in
10 minutes, two checks in a row. Bots try every server on the internet; the
alert says this one is a target now. Limit the addresses that may reach SSH
with the server's allowed SSH IPs and **Apply Hardening**.

**`security_f2b_ineffective`** · critical. More than 20 attempts in 30 minutes
from addresses fail2ban had already banned, two checks in a row: the bans are
not stopping them. **Apply Hardening** again; if it goes on, the server's
firewall (nftables) is not loading fail2ban's rules.

**`security_weblogin_bruteforce`** · warning. More than 50 failed sign-ins to
one instance's Odoo login form in 10 minutes, two checks in a row. The alert
is the instance's and names the three addresses with the most attempts, with
their count; an address of a user who forgot their password can be among
them. fail2ban already bans an address after ten failures in ten minutes, so
an alert means many addresses, or one the server does not ban. Check that the
instance's users have strong passwords or two-factor authentication. On a
server behind a CDN the alert names no address, since the server only sees
the CDN's.

The thresholds of these three are first values, to be adjusted once real
traffic is measured; an Administrator can tune them like any metric rule.

## When the logs do not arrive

**`security_logs_silent`** · warning. The server is set to send its security
logs, but they are not reaching the central: its collector stopped
answering, dropped lines, or reads lines and delivers none. Until it is
fixed, nothing that happens on the server raises an alert. Run **Install
Observability** again from the server's page.

**`security_events_backlog`** · warning. The servers send more sign-ins,
`sudo` commands or changes than the panel reads in one pass, two passes in a
row. Nothing is skipped: they are read in order, so their alerts come late.
It closes once a pass reads to the end, or when no server sends its
security logs any more.
