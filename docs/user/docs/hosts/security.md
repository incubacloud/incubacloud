# Security logs

Each server your panel manages can send its security logs to the metrics
central: who signed in over SSH, who used `sudo`, what changed in the files
that decide who may get in, what fail2ban banned, and which addresses failed
to sign in to your Odoo instances. The panel reads them back to show them in
the server's **Metrics → Security** view and to raise
[security alerts](../reference/security-alerts.md).

## Turning them on and off

**Send security logs**, on the server's **Connection** tab, is the switch,
one per server. It is on by default and needs observability on, with a
central that keeps logs ([Monitoring](../reference/monitoring.md#what-you-need-for-it-to-work)).
An Administrator can turn it off at any time:

- Off, the server stops sending at once and the panel removes the collector
  from it. Nothing from the time it was off is sent later.
- What was already sent stays until it expires (below).
- On again, the server sends from that moment on.

A panel may say *Security logs are not available on this panel yet*: the
provider that runs the panel has not allowed them, and until it does its
servers send none. On the hosted service they start when the version of the
Terms of Service that describes them takes effect.

## What leaves the server

| Source | What is sent | What is not |
| --- | --- | --- |
| SSH (the system journal) | Each sign-in and failed attempt: the address it came from, the user, the method and, for a key, its fingerprint | Passwords |
| `sudo` (the system journal) | Who ran what as root, and the refusals | |
| auditd | Changes to `passwd`, `shadow`, `sudoers` and the SSH configuration: the file, the program and the user that changed it | auditd's other records |
| fail2ban | The SSH jail's lines, and the bans and lifts of the web-login jail, with the address | Other jails |
| The proxy's access log | **Only the failed sign-ins to Odoo's login form**, and of each only the address it came from, the instance (a number) and the time | The user name, the password, the page asked for, the domain, the headers and every other request |

The rest of the proxy's access log stays on the server, where it is kept 30
days. Besides the lines, the server sends counters (failed connections,
sign-ins, bans, failed web logins per instance), figures with no address in
them, which the charts and the alerts read.

A few limits:

- **Up to 600 failed web logins a minute per server** are sent; above that
  they are counted and not sent, so an attack on the web port cannot crowd
  out the SSH and `sudo` lines.
- **Behind a CDN** the proxy sees the CDN's addresses, not the visitors', so
  the web-login lines carry the CDN's. The figures per instance still count.
- **A server that keeps its journal in memory only** (no `/var/log/journal`)
  sends no SSH or `sudo` lines; the agents' install says so.

## Where they are kept, and for how long

On the metrics central, in its log store, under your panel's account. Each
line is **deleted automatically 90 days after it was recorded**; nothing
deletes one earlier, also when a server is deleted. A self-hosted panel keeps
them on its own central. On the hosted service the central is in the EU.

The server writes with a credential that may only write: it can add lines,
not read them or delete them, and it cannot read the logs or metrics of your
other servers.

## Who sees what

| Who | What |
| --- | --- |
| Developer and above | The Security view's figures and charts, the state of the defence and the diagnosis: no address and no user |
| Administrator | Also the tables with addresses, their country, the users tried, the commands and the changes, and **See lines** |
| Whoever sees an alert | Its text, wherever it is read |

The panel decides it when it answers, not the screen: a Developer's browser
never receives an address.

**An alert's text can carry addresses** that its readers would not see in
the tables. The alerts of a sign-in over SSH name the address and the user;
a server's alerts are seen by Project Managers and above. The web-login
alert of an instance names the three addresses with the most attempts and
their count; it is the instance's, so the members of its project see it. The
text reaches every channel the readers set up (email, Telegram, a webhook)
as it is. An address in that list can be a user of yours who forgot their
password.

## The country of an address

The panel works out the country itself, from a database on the panel:
Odoo's own GeoIP database when the server it runs on has one, otherwise
DB-IP's free country database (CC BY 4.0), which the panel downloads once a
month. No address leaves the panel for this. The country is looked up when a
table is shown and never stored, and no alert carries it. Where it comes from
DB-IP, the view says so. The panel's setting `security_geoip` (*Show the
country of addresses*) turns off both the lookup and the download.

## The Security view

Open the server, then **Metrics → Security**.

- **What is happening?** A verdict on the last minutes and how many security
  alerts are open.
- **Is the defence working?** How many addresses may reach SSH (never which),
  whether the server is hardened and up to date, whether it is behind a CDN
  and its origin closed, whether its security logs arrive, and whether its
  collector writes with a credential that may only write.
- **Charts:** failed SSH connections and bans, sign-ins, `sudo`, changes to
  the watched files, the lines sent, and the requests and failed web logins
  of each instance.
- **Tables, for Administrators:**
    - *Addresses with the most failed connections* (24 hours), with the users
      tried, the country and whether fail2ban holds the address now;
    - *Sign-ins over SSH*, *sudo* and *Changes to watched files* (7 days),
      each with whose it looks: the panel, the system, a
      [known key](#known-keys), another key or user, or the panel's own user
      outside every job and console of the panel;
      `sudo` shows the program, never its arguments;
    - for the web login, the addresses with the most attempts with the
      instances each went to, and the bans of the last 7 days with the
      instances tried before; filter by instance to reach 7 days;
    - **See lines**: the last 200 lines of one kind, as the server sent them.

The tables count the last 5 000 lines of their window and say so when there
were more. The view says *This server does not send its security logs* when
its switch is off.

## Known keys

If you sign in to a server over SSH with a key of your own, each sign-in
raises the critical alert `security_ssh_login_foreign`. Add that key to the
server's **known keys**: its sign-ins are still listed in *Sign-ins over
SSH*, as *known key: <its name>*, and raise no alert.

In **Metrics → Security**, under *Sign-ins, sudo and changes*, an
Administrator clicks **Add a key**, names it and pastes the line of the key's
`.pub` file, or its fingerprint as `ssh-keygen -lf` prints it. Only the
fingerprint is kept, and a private key is refused. Each server has its own
list.

- **Only from** limits the key to some addresses or networks: a sign-in with
  it from anywhere else raises a critical alert that names the key. Leave it
  empty for a key that signs in from changing places, such as a laptop. An
  IPv6 address counts with its whole /64; a network wider than a /8 (IPv4)
  or a /32 (IPv6) is refused.
- A key counts from the moment it is added: earlier sign-ins keep their
  alerts. **Remove** ends it at once, and the key stays listed under
  *Removed keys* with who added it, who removed it and when. A key is never
  edited: to change its addresses, remove it and add it again. A server
  keeps 20 known keys at most.
- Adding a key raises the critical alert `security_known_key_added`, is
  written in the audit log and, on a panel that sends access notices, is
  emailed to every Administrator. Nobody can quiet the alerts of a key of
  their own without the others knowing.
- A known key covers the sign-in only. The `sudo` commands and the changes to
  watched files of that session raise their alerts as anyone's, because the
  logs do not say which sign-in they came from.
- The *Sign-ins over SSH* chart still counts a known key as *another key*;
  the table names it.
- A Developer sees the list without its addresses, and cannot change it.
  Agents and API keys cannot add or remove keys.

## What the logs cannot prove

- **A server you lose root on cannot be trusted, nor what it sends.** Its
  credential writes for your whole account, so whoever is root there can
  also write lines that claim to come from another of your servers. The same
  held before, with the server's name; there is no cheap way to stop it. An
  alert on one server raised while another was taken over deserves a second
  look.
- **A `sudo` line alone proves nothing.** Any user of the server can write a
  journal line that says it came from `sudo`. Read it with the sign-ins and
  the changes around it.
- **Alerts start from the moment the panel first reads**: nothing earlier is
  alerted on.
- **An open event alert notifies again at most once an hour.** While a
  sign-in, `sudo` or change alert is open on a server, each new one adds to
  its count, updates its text and reaches your notification channels again,
  but not within an hour of that server's last notice: those stay on the
  panel. Your own sign-ins over SSH count too, unless their key is a
  [known key](#known-keys); dismiss each alert once you know what it was.
- **A failed web login with a request longer than 32 KB** is banned by
  fail2ban as any other, but is not counted or sent.
- **The addresses fail2ban never bans** (the panel's own and the proxies the
  server trusts) are still counted.

## See also

- [Security alerts](../reference/security-alerts.md): what each one means and
  what to do.
- [Hardening](connect.md#hardening-optional): the firewall and the bans these
  logs show at work.
