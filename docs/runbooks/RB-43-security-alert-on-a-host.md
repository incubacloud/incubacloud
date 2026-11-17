# RB-43 — A security alert on a server

**Severity:** critical (`security_ssh_login_foreign`,
`security_ssh_login_panel_key_elsewhere`, `security_sensitive_change`,
`security_f2b_ineffective`, `security_known_key_added`) · warning (`security_ssh_login_unconfirmed`,
`security_sudo_foreign`, `security_ssh_bruteforce`,
`security_weblogin_bruteforce`, `security_logs_silent`,
`security_events_backlog`) · **When:** one of them is raised, or someone
reports a server behaving as if somebody else were on it.

What each alert means for whoever reads it is in the user guide,
`docs/user/docs/reference/security-alerts.md`; how they are raised, in
`docs/observability-operations.md`, *Security logs* and *Tuning alerts*.
This runbook is the operator's triage.

## 1. Was it us?

Most alerts are someone of the team working by hand. Before anything
else, ask, and compare with the alert's time:

- a sign-in with a personal key, from the provider's console or from a
  laptop (`security_ssh_login_foreign`): a key the team signs in with
  every day belongs in the server's known keys (section 6);
- a user added or an SSH setting changed by hand (`security_sensitive_change`,
  a warning when it was `root`);
- a `sudo` by a user created for an incident (`security_sudo_foreign`);
- the panel moved and the server refuses its new address, so the panel
  could not confirm a sign-in of its own (`security_ssh_login_unconfirmed`:
  see RB-41).

If it was, dismiss the alert. The same event never raises it again
(`cloud.security.event` keeps its digest for 7 days); a new one does.

## 2. Read what the server sent

In the panel, as an Administrator: the server, **Metrics → Security**.

- **Sign-ins over SSH** (7 days): who, from where, with what, and whose it
  looks. A sign-in that is not the panel's is the start of the timeline.
- **sudo** and **Changes to watched files** around that time: what was
  done with it.
- **See lines** for the raw lines of one kind (the last 200).

Older or wider than that, read Loki directly with the panel's account and
read credential (`metrics_account` and its token in the panel's settings)
through `/lr/`, filtered by `host_id`, which is the server's id in the
panel:

```bash
$ curl -sG -u "<account>:<read credential>" \
    "https://<central>/lr/loki/api/v1/query_range" \
    --data-urlencode 'query={log_type="security", host_id="<id>", source="sshd"}' \
    --data-urlencode "start=$(date -d '-2 days' +%s)000000000" \
    --data-urlencode 'limit=500'
```

`source` is `sshd`, `sudo`, `fail2ban`, `auditd` or `weblogin`. Lines are
kept 90 days. Save what matters now (the output above, to a file outside
the server): if this becomes an incident, it is the evidence.

Read the lines knowing their limits:

- **A server whose root was taken writes what it wants**, with any
  `host_id` of its account: its own lines after the takeover, and lines it
  claims for the account's other servers. An alert on another server that
  starts after the first one was taken is suspect until checked on that
  server itself.
- **A `sudo` line alone proves nothing**: any local user can write a
  journal line with the identifier `sudo`. auditd's records of the watched
  files (login uid, program) and the sign-ins are firmer.

## 3. Contain

When nobody of the team did it, and before the cleanup:

1. **Close SSH to everyone but the panel and the team.** Set the server's
   *allowed SSH IPs* and run **Apply Hardening**; the dialog says who will
   be able to connect. Live, without waiting:
   `sudo nft add element inet filter ic_ssh_allow '{ <address> }'` for each
   address that must keep access, as RB-41 does.
2. **Remove the keys nobody knows** from every user's
   `~/.ssh/authorized_keys` (`root` included), and lock any user nobody
   created: `sudo usermod -L <user>`.
3. **The panel's key** (`security_ssh_login_panel_key_elsewhere`): give the
   server a new key on its **Connection** tab, put its public half in the
   panel's user's `authorized_keys` and remove the old one. The old key is
   anyone's from then on.
4. **The instances on the server**: their database passwords, their
   configuration's secrets, the mail relay's credentials and anything in
   their environment were readable by whoever was root. Rotate the ones
   that matter, from the panel where it holds them.

A server that was taken over is not cleaned in place. Order a new one,
move its instances there (each instance's *Move to*) or restore their
latest backups, then delete the old server. Keep a snapshot of it at the
provider until the investigation is over.

## 4. Personal data

The server holds the instances' databases, and its security logs hold
addresses of people. A confirmed access by someone else may be a personal
data breach: whoever is the controller of that data has 72 hours from
becoming aware of it to tell the supervisory authority (GDPR, article 33).
Follow your deployment's own breach procedure from here, and record what
you found and when, even if you conclude that nothing was exposed.

## 5. The other alerts

- **`security_ssh_bruteforce`**: the server is a target, which is normal
  on the internet. Limit the SSH addresses (step 3.1); nothing else to do
  if the bans hold.
- **`security_f2b_ineffective`**: bans are not taking effect. Run **Apply
  Hardening** again; then, on the server, `sudo fail2ban-client status
  sshd` and `sudo nft list table inet f2b-table` should list the banned
  addresses. If the table is missing, `sudo systemctl restart fail2ban`
  creates it again.
- **`security_weblogin_bruteforce`**: one instance's login form. The alert
  names the three addresses with the most attempts; the Security view,
  filtered by that instance, the rest. An address the jail does not ban is
  the panel's own, a trusted proxy's, or a server behind a CDN (the jail is
  off there: put the rate limit on the CDN). Check that the instance's users
  have two-factor authentication.
- **`security_logs_silent`**: run **Install Observability** on the server.
  If it persists, read the collector on the server:
  `docker logs --tail 100 incubacloud-observability-fluent-bit-1`. A 401
  there is a credential the central does not know: see
  `docs/observability-operations.md`, *The agents' user is handed out by
  state*.
- **`security_events_backlog`**: the servers write more than 5 000 lines of
  one kind in five minutes. Nothing is lost, the alerts come late. Look
  for the server that writes them (often a script calling `sudo` in a loop)
  in the *Security lines* chart of each server's Security view.

## 6. Known keys

A server's known keys (**Metrics → Security**, *Known keys*) are the keys
the team signs in with by hand: a sign-in with one, from the addresses it
may sign in from, is listed as *known key: <name>* and raises nothing. Only
an Administrator adds or removes one, from that card; nobody edits one.

- **`security_known_key_added`** is raised by every addition, critical, one
  per key, and stays open when the key is removed. Whoever added it is in
  its text and in the audit log (*Known SSH key added*, with the server).
  Not expected: remove the key, then read the sign-ins with it as in
  section 2.
- **A key counts from its addition.** A sign-in from before it keeps its
  alert, also one the cron reads late. Each pass reads the known keys when
  it starts: a sign-in in the minutes the key is being added can still
  raise one `security_ssh_login_foreign`, on the safe side. Add the key
  before signing in with it, and dismiss that one alert if it comes.
- **Two clocks.** A key's addition and removal are the panel's clock; a
  sign-in's instant is the server's, as its journal wrote it. Keep both on
  NTP: a server whose clock is N seconds off moves the key's window by N
  for its lines, at its addition as at its removal.
- **From outside *Only from*** the sign-in is `security_ssh_login_foreign`
  and the text names the key: treat it as a copied key (section 3).
- **It covers the sign-in, nothing else.** `sudo` and the changes to the
  watched files in that session alert as anyone's: the lines carry no
  session that ties them to the sign-in. A change made as `root` is a
  warning outside the panel's jobs, consoles and terminals. auditd only
  watches, on a server the panel hardened, `passwd`, `shadow`, `sudoers`
  (with `sudoers.d`) and `/etc/ssh`: other changes made with a known key
  raise nothing.
- **Fails closed.** A list of keys that cannot be read, a database error
  included, judges the sign-in as if no key were known: it alerts, and the
  error is in the log (`[security] the known keys of cloud.host(id=…) not
  read`). A last use that cannot be written is logged and the pass goes on.
- A sign-in with a known key is not kept in `cloud.security.event`: each
  pass that reads it again judges it again, so removing the key never
  turns an old sign-in into an alert, and a new one after the removal
  alerts.

## Rollback

Nothing here changes the panel's code. To stop a server's security logs
while an alert is studied, turn **Send security logs** off on its
**Connection** tab: what was sent stays 90 days, and nothing more arrives,
so do it only for a server that floods the central, never for one under
investigation.
