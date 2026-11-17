## Deploying

1. **Settings → Monitoring** — pick the host that should run the central
   and press **Enable observability**. That is the whole procedure.
2. The job deploys VictoriaMetrics, Loki and vmauth (the account
   boundary) and, when they answer, writes the endpoints back, generates
   this panel's account credential and switches observability on.
3. **Hosts enrol themselves.** A reconciliation cron (every 15 min)
   installs the agents on any host that should be reporting and is not,
   with a growing back-off per host and an alert once failures persist.
   Host setup also chains an install, but only as an accelerator — if it
   fails, the cron picks the host up.
4. **Hosts follow the playbook.** Each host records the revision of the
   agents' install it last completed (`metrics_agents_revision`, from
   `AGENTS_REVISION` in `observability_agents_executor.py`). The same
   cron re-applies the installed hosts on an older revision, oldest first,
   **five per pass** (`_REAPPLY_PER_PASS`): twenty an hour, so an upgrade
   spreads the installs (image pulls, a package) over the fleet instead of
   queuing them at once, and a host installed by a panel still on older
   code converges once its panel is updated. No migration queues installs.
   A change to the playbook that installed hosts need raises
   `AGENTS_REVISION` (12-nov-2026: revision 1, the database collector;
   revision 2, Fluent Bit and each server's security logs).
5. **The central follows its playbook too.** The panel records the
   revision of the central's last good deployment
   (`metrics_central_revision`, from `CENTRAL_REVISION` in
   `observability_central_executor.py`). While it is behind, the same cron
   deploys the central again: not while a deployment waits or runs on its
   host, and after a failure with the agents' growing wait. An update
   that changes the central converges this way even when its first
   deployment ran on older code, failed, or never ran. Not with
   observability off: the deployment switches it on, so it waits for the
   operator (12-nov-2026: revision 1, the central without Grafana;
   14-nov-2026: revision 2, Loki's series per query and its restart when
   its configuration changes; 15-nov-2026: revision 3, Loki splitting a
   query by day; 17-nov-2026: revision 4, the operator's `/admin-r/`
   closed in vmauth and in the proxy's routers). A change of Loki's configuration restarts Loki alone:
   about 20 s in which a log read says the central does not answer and
   the agents keep their lines and send them again (Loki 2.9.8, its
   write-ahead log on the volume).

There is no per-host button, and no step to remember. There never should
have been: there is no host to which observability does not apply, so
enrolment is not a decision.

**What the button cannot derive.** Agents on hosts *other* than the
central's need a public HTTPS endpoint, which requires DNS and a
certificate. That field is left empty under *Advanced* and the job log
says so, because inventing a value would leave a fleet quietly failing to
push.

# Observability — operations guide

How the metrics stack is put together, what it costs, and how to move or
resize it. User-facing documentation lives in
`docs/user/docs/reference/monitoring.md`; this file is for whoever runs
the platform.

## Shape of the system

```
 每 host                                   central (one, shared)
┌──────────────────────────────┐   ┌──────────────────────────────────┐
│ node_exporter  (system)      │   │  vmauth  ← the account boundary  │
│ cAdvisor       (containers)  │   │    /w/  /r/  /lw/  /lr/           │
│ Traefik        (HTTP, :8082) │   │  /admin-d/  (operator: delete)   │
│ vmagent   ── push ───────────┼──▶│         │                        │
│ Fluent Bit ── push ──────────┼──▶│         ├→ VictoriaMetrics       │
│  (security logs, if on)      │   │         └→ Loki (security logs)  │
└──────────────────────────────┘   └──────────────────────────────────┘
                                              ▲
                                     PromQL   │
                    panel (charts, rule cron) ┘   → cloud.alert
```

**Push, not scrape.** Hosts open no inbound port; the agents dial out.
That is what makes bring-your-own-host and NAT'd boxes work without
firewall exceptions, and it is why the write endpoint must be reachable
from every host.

**vmauth is the account boundary for data** (metrics and logs), because
only it can force the account into a query arg / header that a client
cannot override — Traefik cannot touch a query string. The panel draws
every chart itself, reading through it; the central ran Grafana until
12-nov-2026.

**vmauth is published; VictoriaMetrics and Loki are not.** The two
backends have no host port at all — anything able to reach them directly
would bypass the boundary, including another panel's container on a
shared host. They authenticate nothing.

### The account boundary

The central can be shared: every panel given an account writes to and
reads from it. Neither VictoriaMetrics nor Loki has a notion of accounts, so
the boundary is imposed in front by **vmauth** — VictoriaMetrics' own
auth proxy, which forces the account per route from the authenticated
user:

| Path | Rule |
| --- | --- |
| `/w/api/v1/write` metrics write | the route forces `extra_label=ic_account=<user>`; a client's own copy is **dropped** for colliding |
| `/r/api/v1/{query,query_range}` metrics read | the route forces `extra_filters[]={ic_account="<user>"}` the same way, so a client cannot widen the result |
| `/lw/loki/api/v1/push`, `/lr/loki/api/v1/{query,query_range}` logs | the `X-Scope-OrgID` header is **set** from the user, replacing whatever the sender supplied |

None of that is caution. Measured against real VictoriaMetrics + Loki
(2026-08-10): vmauth drops a client's query arg that collides with the
one the route forces, so the account filter cannot be widened — and it
needs no hand-written reject rules that would rot when VictoriaMetrics
changes a query arg's meaning. A credential-less request is 401 because
the config declares no `unauthorized_user`.

**Each route names its endpoints; none is a catch-all.** They used to be
`/w/.*` and friends, and VictoriaMetrics serves writes, deletions and its
admin endpoints on the same port as reads. Measured on 2026-09-27 against
the production configuration: through its *read* route an account posted
to `/api/v1/import/prometheus` with another account's `ic_account` — a
read route forces a read filter, not a write label — and the other
account saw the series as its own; the same route created snapshots and
forced merges. The read list is a dashboard's and a metrics browser's
query API; the panel calls `query` and `query_range`. A reader that starts
calling another endpoint gets a 400 from vmauth: add the endpoint to
`_ACCOUNT_READ_PATHS`, never a wildcard.

An agent runs on a machine its owner has root on. Its claim about who it
is can never be trusted, which is why the label comes from the
credential and not from the payload.

**Three credentials, and they are not interchangeable.** Each account has
its own, scoped to itself, which the panel reads with, and an *agents'
user*, `<account>-agent`, which only writes (`/w/` and `/lw/`), forced to
the same account's label and Loki `X-Scope-OrgID`: the agents write with it, so
root on one server can no longer read the logs and metrics of the
account's other servers. The *operator* credential only deletes series
(`/admin-d/`, exactly `delete_series`) and is never written to any host;
vmauth carries it only while the central has a deletion key, since a user
without a route is a configuration vmauth refuses. No credential reads
across accounts: the operator's read route, `/admin-r/`, closed on
17-nov-2026, in vmauth and in the proxy's routers alike, and a request
there now meets no route (the host's catch-all, or vmauth's 400 on the
bridge). It served the accounts' `query` and `query_range` and `/health`
from 12-nov-2026, and the whole backend (`/admin-r/.*`) before. The
deployment and the account sync prove the gateway with the panel's own
account instead: `/r/api/v1/query?query=vector(1)` must answer 200
(`tasks/vmauth_acl_apply.yml`). `/r/` and `/lr/` served Grafana's
endpoints too (series, labels, label values, metadata, the log index)
until 1.0.230.

**The agents' user is handed out by state, never by version** (1.0.230,
plan #84 A2):

- The agents write with it only while the central's list carries it
  with the token the panel holds (its digest in
  `metrics_credentials_deployed`, not the user's name alone: a token
  minted again is not in force until a sync puts it there), or where a
  module that extends the panel hands it over only once its central knows
  it (`cloud.settings._metrics_credentials_injected`); otherwise with the
  account's credential, as before. A credential vmauth does not know is
  a 401 loop on every server.
- The moment it comes into force, every server's agents are applied
  again (`_reapply_observability_everywhere`), so none stays on the
  account's credential unseen. Each server records which one its agents
  hold (`cloud.host.metrics_write_user`: `agent`, `account`, or empty
  for an install before 1.0.230).
- **The read credential is rotated once no server holds it:** every
  active server whose agents were installed, or failed to be, writes with
  the agents' user. The new one goes on the central beside the old one
  (`metrics_read_token_next`; vmauth admits two passwords for one user,
  measured on v1.102.0, 14-nov-2026), the panel moves to it once the list
  carries it (`metrics_credentials_deployed` keeps a digest of each
  credential in force) and the next sync drops the old one. From then on
  the account's user only reads (`metrics_read_rotated`) and has a cap on
  requests in flight, `READ_MAX_CONCURRENT` (8, provisional until step M
  of plan #84 measures the board's and the cron's LogQL). The agents'
  reconciliation cron drives it (`_reconcile_metrics_credentials`), and a
  central deployed before 1.0.230 gets the agents' user with its next
  sync or deployment.
- While the agents' user is in force, the reconciliation re-applies, five
  a pass, every installed server whose agents still write with the
  account's credential, whatever its revision: an install that ran on the
  old credential as the new one came, a job resumed by another worker, or
  a server brought back from the archive.
- **After the rotation:**
  - An archived server whose agents still run is not waited for, and
    gets a 401 for everything it sends once the old credential is gone.
    Unarchived, the reconciliation applies it again with the agents' user.
  - Going back to a core before 1.0.230 leaves without writes every server
    that core applies again: it hands them the account's credential,
    which no longer writes. It is fixed forward, by updating again.
  - A second panel or tool pointed by hand at the central with the
    account's credential loses it: give it the new read credential, which
    only reads.

vmauth keeps its credentials in clear (it has no hash support). That is
mitigated the same way the agents' copy of the token is: the config lives
in the central's `0700` directory and every task that writes it is
`no_log`. The token already exists in clear on every agent, so this adds
no new exposure.

**Alerts do not live in the metrics stack.** There is no Alertmanager and
no vmalert. A panel cron (`cloud.metric.rule._cron_evaluate`, every 5
min) runs each rule's PromQL, compares against its threshold and
raises/resolves `cloud.alert` — the same multichannel pipeline every
other alert uses. One place to look, one notion of "alert".

Rules are host- or instance-scoped. An instance-scoped rule resolves
through the instance label and raises against the instance, so it lands
on its page. Instances a plan puts to sleep are suppressed: sleeping
stops their containers on purpose, and a nightly critical alert per Free
instance would train everyone to ignore the alert that matters.

## Costs and sizing

| Piece | Rough cost |
|---|---|
| Agents per host | ~50 MB RAM total, and Fluent Bit ~61 MB idle, 81–87 MB under 600 failed SSH attempts a minute, where the server sends its security logs (F0, 9-nov-2026); its container is capped at 128 MB |
| VictoriaMetrics | ~0.5–1 GB RAM; disk grows with series × retention |
| Loki | the hosts' security logs: tens of lines a minute under SSH attack, units at rest; a host's failed web logins up to 600 lines a minute (`ic_weblogin_per_minute`) |
| Reading them back | the security events' cron: sign-ins, sudo (one read per user the panel signs in as) and auditd, 10 pages of 500 at most each, plus one instant query, every 5 minutes per panel; a Security view: 10 PromQL queries and, for a manager, six log reads, kept 60 s; the web-login alert: one read per firing instance, 10 a pass |

The panel's own charts cost the central one PromQL query per panel each
time a board is read: an instance's Metrics tab is ten (`metrics_charts.
PANELS["instance"]`), at most four in flight from one worker, each given
8 s and asked once (no retry: a retry would double the wait on a central
that is already slow), again every 45 s while the `1h` or `6h` range is
open and the tab is visible; a click on a range or a zoom replaces the
pending refresh instead of starting another. One user may read 20 boards
a minute (`_BOARD_READS_PER_MIN` in `controllers/_data_load/_routes_ops.py`).
A reading on demand (*Queries now*, *Slow routes*) is one SSH command on
the instance's host, only when pressed, and holds a worker of the panel
for at most 20 s, the SSH connection and login included (`_READ_TIMEOUT`;
jobs keep the host's 30 s for each); the board lets one run at a time, and
each reading counts against the user's log reads per minute
(`rate_limit_logs_per_min`).

**Only users that never write have a cap on requests in flight**: the
operator, 16 (`_OPERATOR_MAX_CONCURRENT`, vmauth's
`max_concurrent_requests`, 12-nov-2026), and an account's user once its
read credential is rotated (above). vmauth counts per user, not per route,
and answers the next request 429 at once, with no queue. An account's user
that still writes is shared by agents' writes, logs and its panel: a cap
there answered a write 429 while two reads ran (measured on v1.102.0 with
a cap of 2), and after an outage every host empties its queue at once. So
it has none, nor has the agents' user. The
operator never writes and, since 17-nov-2026, only deletes series; 16
covered four fleet boards at four queries each while the fleet's board
read with it (measured: 16
reads in flight pass, the 17th to 20th get 429, an account's 30 reads and
its write meanwhile all pass). VictoriaMetrics queues the reads
themselves: `-search.maxConcurrentRequests`, twice the cores (8 on a
4-core central), each waiting up to 10 s. A share per account belongs
with a read-only user per account, later.

**A 429 is "busy", not "down".** `promql_query` asks once more after a
second (`_BUSY_PAUSE`); the rule cron, if it is still busy, evaluates
nothing that run, raising nothing (no `metrics_backend_unreachable`) and
resolving nothing; a board says *The metrics central is busy* (`reason:
"busy"`, no since) and asks again on its own.

### On each server: the collectors

What the agents' install puts on a server besides the three containers,
measured on F0 of the observability plan (9-nov-2026, a `cpx22`, ten
`db` services of 800 tables each) and chosen on 10 and 12-nov-2026:

| Collector | Runs | Cost | Why so |
| --- | --- | --- | --- |
| `collect_pg.sh` (`ansible/playbooks/files/`), root's | every minute, `/etc/cron.d/incubacloud-collect-pg` | 83 ms of CPU per running instance: 1.7 s a minute with 20, 2.8 % of a core | The host's psql through the socket of the instance's `db` container as its filesystem shows it (`/proc/<pid>/root/run/postgresql`): a `docker exec` of the container's psql cost 169 ms, twice the plan's estimate, and the owner chose every minute over every two |
| Database size (`pg_database_size`) in `collect_disk.sh` | every 10 minutes | one `docker exec` of the `db` container's psql, ~170 ms per instance | It cost the per-minute collector half its CPU (46 of 124 ms) on 6 700 files |
| Filestore size (`du` in `collect_disk.sh`) | every 10 minutes on a server of 6 GB or more, once an hour below (`ic_filestore_hourly_below_mb`) | the I/O of a `du` over the filestore | Under `nice -n 19 ionice -c 3` and `timeout 120` **inside** the `odoo` container: run on the host they lowered the docker client, not `du`, which containerd starts. Hourly on the 2–4 GB servers, where that I/O is felt |

`collect_pg.sh` reads every running instance in one pass: one query per
database in a read-only session (`PGOPTIONS` with `statement_timeout=3000`
and `default_transaction_read_only=on`, `PGCONNECT_TIMEOUT=3`), at most
6 s each and **45 s for the whole pass**, so twenty databases that hang end
within the minute; a pass that finds the previous one still running exits
(`flock -n` on `/run/incubacloud-collect-pg.lock`). A stopped `db` (an
instance asleep) or a paused one is left out and is no error; one that runs
and does not answer counts in `ic_pg_scrape_errors`, one left unasked when
the 45 s ran out in `ic_pg_scrape_skipped`. It writes `ic_pg.prom` in one
rename, ten series per instance plus three of its own. Its script and
its list of databases (`collect_pg.instances`) are in
`/usr/local/lib/incubacloud/`, root's: root is no more than the connecting
user already holds through the docker group and sudo, but what root runs
is not that user's to change.

`postgresql-client` and `cron` come with Full Setup, and the playbook
installs each only when missing (apt's lock waited 60 s at most; Debian 13
no longer installs cron, and the project's own Debian 13 image had none,
found on 12-nov-2026). Every step that needs root is in a block whose
rescue lets the playbook go on: the agents still come up, the server's
board shows "Database collector not installed" (`ic_pg_collector 0`), and
the playbook exports what failed and why. The executor then **fails the
job** with what to do: install cron (`apt install cron`, or use the
project's image), give the connecting user passwordless sudo, or free apt;
the reconciliation cron retries it with the back-off of any failed
install, and the alert follows after three. The owner chose a failed job
over a green one with a collector missing (12-nov-2026), so that a server
without figures is fixed when it happens, not found a month later. Without
cron the teardown removes no schedule and says so in its log; a database
collector it cannot remove (sudo no longer works) is said there too, and
the server is still deleted.
*Collect database figures*
(`metrics_collect_pg`, under *Advanced*) switches it off: the next install
removes its schedule, its files and its last readings, and switching it
either way has the reconciliation cron re-apply every installed host. The
teardown of a host removes it too, before the agents' directory.

Retention is set from Settings and applied on the next central deploy.
Shortening it does not delete existing data immediately — VictoriaMetrics
drops it as it ages out.

Loki keeps the hosts' **security logs** (`log_type="security"`) **90
days**, because an incident is often found weeks after it began, and
anything else 30 days: nothing else arrives since the agents stopped
sending the proxies' access log (12-nov-2026), and 30 lets the access
lines already stored, which carry the instances' visitors' addresses,
expire as before. Both apply on the next central deploy, which restarts
Loki when its configuration changed (until 1.0.230 nothing did, and a new
value never applied). Loki returns up to `ic_loki_max_query_series`
series a query (5000, provisional until step M of plan #84 measures the
top source addresses under a distributed attack; Loki's default, 500,
breaks them). It splits a query by day and queues 2 048 subqueries per
account (`ic_loki_split_queries_by_interval`,
`ic_loki_max_outstanding_per_account`, 15-nov-2026): as it comes (2.9) it
splits every 30 minutes and queues 100, so a week of a server's sign-ins
was 336 subqueries and answered 429 even alone, and the Security view's
7-day tables said the central was busy. A host's credential reaches only
the push routes above —
Loki's delete API is not routed — so a host that is compromised cannot
erase what it already sent. The proxy's access log stays on each host, in
`/var/log/traefik/access.log` (rotated daily or past 100 MB, 30 days),
where the panel reads it when asked; of it, only the instances' failed
web logins leave the server, as below.

### Security logs

Where a server's **Send security logs** (`cloud.host.security_logs_enabled`,
its **Connection** tab) is on and the central has a log store, its agents
run **Fluent Bit 5.1.5** beside the other three (F0 of the observability
plan, 9-nov-2026: 61 MB idle against Vector's 94; it reads the journal
itself and its counters never expire).

- **Sources.** The journal's `ssh.service`, `sshd.service` and the
  identifiers `sshd`, `sshd-session`, `sshd-auth` (OpenSSH 9.8 and later)
  and `sudo`; `/var/log/audit/audit.log`, `/var/log/fail2ban.log` and,
  since 1.0.230, the proxy's `/var/log/traefik/access.log`. Each from
  where it stopped, and from the end the first time.
- **What leaves the server.** The journal's lines; of auditd, only the
  `SYSCALL` records of its four keys (`passwd_changes`, `shadow_changes`,
  `sudoers_changes`, `ssh_config_changes`) and the `PATH` records of the
  files they watch (auditd's other records were 68 % of what it sent
  under attack and repeat sshd and fail2ban); of fail2ban, the jail
  `sshd` whole, only the bans of `incubacloud-weblogin` (its `Found`
  lines repeat the `weblogin` lines below without the instance, which
  fail2ban's line does not carry), the daemon's own lines, and no other
  jail. Labels: `log_type`, `host_id` (since 1.0.230: the name is not
  unique, and a rename does not re-apply the agents; lines from before
  have none), `host`, `source` (`sshd`, `sudo`, `fail2ban`, `auditd`,
  `weblogin`).
- **The instances' failed web logins** (1.0.230, plan #84 C1.9). A native
  `grep` keeps the access log's `POST`s to `/web/login` before anything
  else sees them; the Lua keeps what the web-login jail counts as failed
  (`POST`, `/web/login` or `/web/login/totp` with or without a query,
  `200`) and drops the rest uncounted. Fields are found by their exact
  quoted names, which a client cannot forge (Traefik escapes the quotes
  it sends). The instance is the one whose Traefik prefix (`traefik_prefix`,
  as vmagent's relabelling) a router, with `https-` or not, or a service
  starts with, the longest winning, or whose exact service name a module
  put in front of it (`traefik_extra`); none is `0` (a proxy kept by hand,
  or a module's router answering for an instance asleep). What leaves is a
  line of its own, `weblogin failed ip=<address> instance=<id> t=<the
  proxy's time>`, never the request, its path, the host asked for or the
  user, up to `ic_weblogin_per_minute` a minute (600, provisional until
  step M measures it); above it they are counted and not sent, so an
  attack on the open web port cannot fill the buffer SSH and auditd
  share. The jail does not count `ignoreip` addresses (the panel, trusted
  proxies); the counter does. A line longer than 32 KB (a `POST` to
  `/web/login` with a long query) is skipped whole (`skip_long_lines`):
  the jail counts it and the collector does not, so an attack spread over
  many addresses with such requests would raise no alert. A known limit,
  until step M of plan #84 measures Fluent Bit's memory with a buffer
  above Traefik's largest header (1 MB). bats checks the Lua against the
  jail's own filter on the same lines (`fluent_bit_lines.bats`, with
  `IC_FLUENT_BIT_TEST=1`: it needs Docker, and the CI skips it); a test of
  the module pins the filter, the grep and the Lua's condition together,
  so none moves without running it.
- **Counters**, scraped by vmagent from Fluent Bit (`job="security"`), with
  a closed set of labels: `ic_ssh_auth_failed_total` (one per connection
  that never authenticated), `ic_ssh_accepted_total{kind=panel|other,
  method}` (`panel` when the key's fingerprint is the panel's),
  `ic_sudo_total{kind=ansible|panel_user|other}` (a refused sudo is not
  counted here), `ic_sudo_failed_total`, `ic_f2b_total{jail, action}`,
  `ic_audit_events_total{key, who=system|root|panel|user}`,
  `ic_weblogin_failed_total{instance_id}` (the ids the panel wrote, and
  `0`) and `ic_security_lines_total{source}`, the heartbeat of each
  source; plus
  `fluentbit_output_{proc_records,dropped_records,retries_failed}_total`
  (`job="security-agent"`). A counter appears with its first event, so
  `increase()` does not see that one.
- **The central down.** Lines wait on disk, up to 64 MB (about seven
  hours of 600 lines a minute), retried for ever at most five minutes
  apart; full, the oldest go.
- **The switch.** Off, the next install removes the container, its
  cursors and its buffer: on again, nothing of the time it was off is
  sent. A change applies at once, to that server, also after a failed
  install: turning it off is how a Fluent Bit that misbehaves is removed.
  It can always be turned off. With observability off nothing is
  installed, and the agents already on a server keep sending: the change
  sets the server's agents' revision to 0, so the reconciliation applies
  it once observability is on again.
  An agent's `manage_host` that turns it off asks for approval unless an
  auto-approval rule covers it; the change is in the audit log either
  way. The update to 1.0.227 replaced the panel-wide *Send hosts'
  security logs* with it and turned it on on every server, unless a
  module that extends the panel decides otherwise
  (`cloud.host._security_logs_after_upgrade`,
  `_security_logs_available`, `_security_logs_default`).
- **Reading them.** Through `/lr/` with the account's credential, which no
  server holds once it is rotated (`models/logs.py`): the transport of the
  charts (8 s, one retry, a 429 asked again after a pause and never
  counted as the central down), 500 lines a page, pages taken by time from
  the last instant of the one before, included, and a line read twice
  counted once. The last lines of a long window (the Security view's
  7-day tables, "see lines") are read in tranches of 12 hours, newest
  first (`logs.TRANCHE_SECONDS`), its pages and lines shared by the whole
  window: 24 subqueries each on a Loki with its own limits, so a board's
  four reads at once fit its queue of 100 before the central is deployed
  again with its own, and one or two after. Its parsers are anchored at the start of the line; the
  user names and commands an attacker writes are cut to 32 and 64
  characters of a closed set before they can reach a message, a sudo
  record keeps the program without its arguments, and no record carries
  the line itself. An auditd change is its `SYSCALL` joined to its `PATH`
  records by server and `audit(seconds:serial)`, once every page is read;
  a `SYSCALL` whose `PATH` has not arrived in the last 60 s of the window
  waits for the next pass. The addresses with the most failed web logins
  on an instance are counted on the panel over the last 1 000 `weblogin`
  lines of its server (`logs.weblogin_top_sources`, IPv6 by its
  /64), never by an aggregation per address, so Loki's
  `max_query_series` cannot refuse them; a central that is busy or does
  not answer gives no addresses, never an error.
- **The panel's addresses and uid.** The agents' install reads
  `SSH_CONNECTION` without `become` (sudo clears it) and the panel keeps,
  per server, the last five addresses it came from, each forgotten 30
  days after it was last seen, IPv6 by its /64
  (`cloud.host.security_panel_sources`, for whoever manages servers, and
  out of every MCP read). The address a change of the panel's address
  left is forgotten everywhere when its alert closes. Nothing learns one
  from Odoo.sh's notice, which carries no credential, nor from a log
  line. The install also records the uid the panel signs in as
  (`security_panel_uid`), which tells its changes in auditd's records.
- **What it costs.** See *Costs and sizing*. Fluent Bit runs as root with
  every capability dropped but reading files, a read-only filesystem and
  no port published; its credential is in a 0600 file.
- **The country of an address** (`models/geoip.py`, not shown yet).
  Looked up on the panel, and only the country: no address leaves it.
  From Odoo's `geoip_country_db` when that file is there (Odoo.sh ships
  one; a Doodba image does not); otherwise from DB-IP's IP to Country
  Lite (CC BY 4.0), which the daily *Refresh the GeoIP database* cron
  downloads once a month from `download.db-ip.com` and keeps as an
  attachment of the settings. While a month's file is not published, the
  month before is taken. The download is HTTPS to that host alone (a
  redirect elsewhere is refused), at most 32 MB, 64 MB uncompressed, in
  60 s; a file that does not open as a country database never replaces
  the one kept. Private, loopback and link-local addresses are *local*.
  The setting *Show the country of addresses* (`security_geoip`, on by
  default) turns both the lookup and the download off. A country from
  DB-IP is shown with its attribution, `geoip.ATTRIBUTION`.
- **The alerts of the security events**
  (`models/cloud_security_events.py`, the *Read the servers' security
  events* cron, every 5 minutes). Over the servers sending their security
  logs whose agents were installed with `host_id` (`metrics_write_user`
  set), three reads, each with its cursor in `ir.config_parameter`
  (`incubacloud.security_events_cursor.<accepted|sudo|auditd>`): sign-ins,
  sudo with the panel's own user left out in LogQL (one read per user the
  panel signs in as), and auditd's changes. Each reads from 15 minutes
  before its cursor to 30 seconds ago, at most 10 pages of 500 lines; the
  cursor starts at "now" (nothing earlier is alerted on), stays where it
  was when the read fails, and moves only to the last line read when the
  pages ran out, two passes in a row of which raise
  `security_events_backlog` (a warning per read, closed by the next pass
  that reads to the end, or by a pass with no server or no central to
  read). After an outage the window reaches 24 hours
  back. One more instant query a pass counts each server's sshd, sudo,
  fail2ban and auditd lines of the last 15 minutes: a server silent
  longer than that (`cloud.host.security_last_line_at`) is read again
  alone from where it went quiet, five a pass, since Fluent Bit sends what
  it kept with the time it happened. That field and the panel's addresses
  a connection learns are written on a READ COMMITTED cursor of their
  own, as the metrics cron writes its hosts: the cron's transaction never
  writes a server's row, so a job's hook or a save of the server during
  the pass does not roll the pass back.
  - *Sign-ins.* With the panel's key from an address it connects from
    (`security_panel_sources`, or `_hardening_panel_ip` where a layer
    knows it; IPv6 by /64), never an alert. Another key, or a password on
    a server the panel signs in to with a key, is
    `security_ssh_login_foreign` (critical); so is, on a server the panel
    signs in to with a password or a key it cannot read, another user.
    The key a change of the server's key replaced counts as the panel's
    for a sign-in up to a minute after the change
    (`security_previous_key_fingerprint`, `security_key_changed_at`). The
    panel's key (or user) from an unknown address is confirmed first: the
    cron opens one connection of its own to that server, at most 10 a
    pass, and reads `SSH_CONNECTION`. The address it reads is learned as
    the panel's there and counts as the panel's on every server for the
    rest of the pass; the same address as the sign-in's, no alert; another,
    `security_ssh_login_panel_key_elsewhere` (critical); no connection,
    `security_ssh_login_unconfirmed` (a warning without the address). A
    sign-in left without a connection stops its cursor for the next pass.
  - *Known keys* (`cloud.host.known.key`, the design note of 17-nov-2026).
    A key that is not the panel's but one of the server's known keys,
    within its window (`added_at` to `removed_at`, against the line's
    instant, the server's clock), raises nothing, is not kept in
    `cloud.security.event` and writes the key's `last_used_at` (once a
    pass, on a cursor of its own); from outside its `allowed_networks`
    (IPv6 by /64) it is `security_ssh_login_foreign` with reason
    `known_key_address`. The panel's key is looked at first. A key is added
    and removed only by `cloud.host._security_known_key_add` and
    `_security_known_key_remove` (Administrators, write access to the
    server; the model is read-only to every role), never edited but for its
    name, and archived rather than deleted. Each addition writes an audit
    row, raises `security_known_key_added` (critical, one per key, not one
    of the events' alerts, so the hourly cap does not hold it) and emails
    every Administrator and the account's holder with no daily quota; the
    alert says whether the key is limited to some addresses, never which
    (`allowed_networks` is a manager's field). A server keeps 20 active
    keys at most. Reading the keys has a savepoint of its own and fails
    closed; the last use is written apart and never undoes a pass. The
    sudo and auditd reads do not look at known keys.
  - *sudo.* Another user's command or refusal is `security_sudo_foreign`
    (a warning), with the program and never its arguments.
  - *Changes.* By login uid, against the uid the agents' install read for
    the panel's user: another user is `security_sensitive_change`
    (critical); the panel's user outside every job of the server or of its
    instances, console and terminal of the panel (a minute's margin around
    each), critical; root there, a warning (`ROOT_CHANGE_LEVEL`,
    provisional until root's changes on the fleet are measured: root's
    crontab writes with login uid 0 too), also where the panel signs in as
    root; the system's own, nothing. Each event is judged in a savepoint
    of its own: one that fails is logged and skipped, and the pass goes
    on. A change's time is auditd's stamp, or Loki's instant when the
    stamp is more than 24 hours away from it.
  - *Once.* `cloud.security.event` keeps each event that raised or
    refreshed an alert, by server and line digest (or auditd's stamp),
    never the line, for 7 days: the overlap never counts one twice and a
    dismissed alert does not come back for the same event. The alerts do
    not resolve themselves; a repeat while one is open refreshes its text
    with the count since the first, and the payload holds the count and
    the last event's sanitized fields. Only the two sign-in alerts carry
    an address; none carries a country. The texts are translated in the
    language of the cron's context, a broken translation giving the
    English one.
- **For whoever reads the alerts and runs the servers.** What leaves a
  server, who sees what and what the logs cannot prove, in the user guide's
  *Security logs* (`docs/user/docs/hosts/security.md`); each alert and what
  to do, in *Security alerts* (`reference/security-alerts.md`); the
  operator's triage in [RB-43](runbooks/RB-43-security-alert-on-a-host.md).
  The thresholds and what step M measures to fix them are in
  `architecture.md`, *Security alerts and their thresholds*.

## The panel's own charts

Since 1.0.224 an instance's Metrics tab is drawn by the panel, not
embedded: `/cloud/instance/resource_series` answers every panel of
`models/metrics_charts.py`'s `PANELS["instance"]` and a diagnosis in one
call, and the browser draws it with the Chart.js Odoo ships
(`static/src/components/metrics/`). The browser only names a range or a
zoom window; the server fills the PromQL from the record's ids
(`instance_id`, and the instance's `host_id` for the server's pressure and
its neighbours) and asks the central with the panel's own account
credential, like the liveness cron. A panel's roles are joined in one
query (`label_replace(…, "ic_role", …) or …`), and gauges are wrapped in
`max_over_time` past a two-scrape step so a short peak still shows on the
7-day range. A panel that fails is answered with its own `error` and the
board is drawn without it.

The diagnosis (`models/metrics_diagnosis.py`) is a list of fixed rules
over the last 15 minutes of the board, each threshold with its reason and
date in the module; the user guide lists them
(`docs/user/docs/reference/monitoring-panels.md`). Change both together.

An agent reads the same board by the hour (the MCP's `read_metrics`):
`/cloud/instance/metrics_summary` runs `PANELS["instance_summary"]`, the
tab's latency, requests, CPU and memory panels as they are plus the
database's connections (`ic_pg_connections` and `ic_pg_max_connections`
of the instance while the collector's file is fresh), over the last 1 to
168 hours, at a step that divides an hour (`summary_step`: 300 s for 24
hours, 1800 s for 168, so every hour has the same number of points), and
`summarize_hours` gives each hour's mean and highest point and each metric's
peak (its share of the limit is the largest of the services, each against
its own limit). Same gate as the tab, and its reads count in the tab's
twenty boards a minute per user, one for each page of the hours.

Two readings are taken from the host only when somebody presses their
button, with the same gate as *Recent requests* (the `developer` role and
the record rules on the instance):

- `/cloud/instance/db_activity` runs `scripts/read_pg_activity.sh`:
  psycopg2 inside the instance's odoo container, connecting with the
  container's own libpq environment, so no credential crosses SSH; it
  prints one JSON line with the sessions, the totals and the tables with
  most dead rows. Query texts can carry personal data: never stored.
- `/cloud/instance/slow_routes` runs `scripts/read_odoo_log.sh` over the
  last 15 or 60 minutes of the instance's Odoo log (live file and newest
  archive, regular files only, as the health probe reads them) and prints
  only a projection of each line; the panel adds it up
  (`models/cloud_instance_slow_routes.py`). A request stopped by a time
  limit carries a trace only without workers: Odoo then logs "Dumping
  stacktrace of limit exceeding threads before reloading" and, on its next
  line (`odoo.tools.misc`), each thread's stack, of which the script keeps
  the `# Thread:` header (database, user, URL, timings) and the last nine
  lines, the innermost frames. With workers, the master logs
  `WorkerHTTP (<pid>) timeout after <n>s` and kills the worker with
  SIGKILL: there is no stack to read, and the board says so.

A reading is no job, so nothing uploads its script beforehand: the
controller sends the script's text on stdin into a private `mktemp` file
that the remote shell removes when it ends, and runs it with its own
standard input closed (`OpsMixin._read_with_script`). The scripts source
nothing for that reason.

### A server's board and the fleet's

A server's **Metrics** tab and Monitoring's **Hosts** view draw
`PANELS["host"]` from `/cloud/host/resource_series`, the same way and
with the same gate as an instance's board (the `developer` role and the
record rules on the host), with the panel's account credential. Every
expression selects by the host's `host_id`; the stacked panels (CPU,
memory and directory per instance) keep the nine largest instances and
add the rest up as `others`, so the stack still reaches the server's
total. The "who uses it" table (`kind: "table"`, instant queries over the
last five minutes) is matched to the instances the caller can read, which
are the ones it links to. Its database and filestore sizes are the last of
the past 30 minutes and 2 hours; its connections and lock waits come from
`ic_pg.prom` only while node_exporter's copy of it is under three minutes
old (`node_textfile_mtime_seconds`), since node_exporter keeps serving the
last file it found after the collector stops. Its diagnosis (`diagnose_host`) reads the same
board: memory over 92 % and the root disk over 90 % (the thresholds of
`metrics_memory_high` and `metrics_disk_critical`), load per core over 2,
and the kernel's waits for memory over 10 % and for the disk over 20 % of
the time.

Monitoring's **Fleet** view draws `PANELS["fleet"]` from
`/cloud/fleet/resource_series`. Its expressions select no record: read
with the account credential they are that account's servers, and that is
the only credential the board reads with, on every panel. Until
17-nov-2026 the panel that holds the operator credential
(`metrics_operator_token`, the one that deployed the central) read the
central's operator route, `/admin-r`, and drew every account's servers;
it no longer does: a panel's Monitoring is its own fleet, and no panel
lists or charts the servers of another account. The aggregations keep
`ic_account` beside `host_id`, and `cloud.host._fleet_own_servers` drops
any series or row of another account should a gateway that does not
filter answer one, before taking the account out of the answer. Each
server is named as the panel names it when the caller can read it
(`own`, the only ones that link to a board), by the name its agents
report otherwise, or *Server <id>* without one, so no line of a legend
is unnamed; load per core divides with `group_left`, without which the
division kept only the labels it matched on and lost the name. The
ingestion line (`kind: "state"`) is
`count_over_time(up{job="node"}[window])` per server: a step with a
sample is reporting, one without is silent; servers with a silence come
first, twenty at most. It is a card of the charts' grid, on the row the
four charts leave unfinished.

The three routes share one budget per user (20 boards a minute,
`_BOARD_READS_PER_MIN`).

The Fleet view's **Security** block (plan #84 C7) is figures only, for
the same `developer` role and above, with no address, user or alert text
for anyone:

- **From the central** (`PANELS["fleet"]` with `needs: "fleet_security"`,
  this panel's account, as the rest of the board): failed SSH logins and bans of
  the last hour and per hour over the window, the servers under SSH brute
  force and the instances under web-login brute force now (over the rules'
  10-minute windows and with the current thresholds of
  `security_ssh_bruteforce` and `security_weblogin_bruteforce`, so the
  board and the alerts agree; instance `0` left out), and the servers
  whose security logs do not arrive, as `security_logs_silent` reads it.
- **From the panel's database**: the servers with *Send security logs* off
  and those not hardened, and the servers with active security alerts
  the caller can read, by count and worst level.
- **The servers to look at** are this panel's own the caller can read,
  twenty at most, the worst first. The per-server rows of the central
  (`kind: "hosts"`) are matched by account and `host_id`, so a server of
  another account is never named even when its id is one of this
  panel's; a server whose switch is off is not counted as silent.
- **Not asked at all** where `_security_logs_available()` is false: the
  block says security logs are not available on this panel yet.

### A server's Security view

The server's **Metrics** tab has two views, *Performance* (the board
above) and *Security* (`models/metrics_security.py`, plan #84 C6),
answered by `/cloud/host/security_board` within the same budget.

- **Without reading the central:** *not available on this panel* while
  `_security_logs_available()` is false (a managed panel until its
  manager allows security logs), observability off or without a central
  (the boards' own states), and *off* for a server whose *Send security
  logs* is off.
- **For the `developer` role and above:** `PANELS["host_security"]`,
  Fluent Bit's counters only, none with an address or a user (failed SSH
  connections and bans by jail a minute; sign-ins, sudo, auditd and lines
  an hour by kind; the collector's deliveries and drops; failed web logins
  per instance), the HTTP of the instances as means over the range with
  their failed web logins, the web-login jail's bans and lifts, how many
  addresses the last web logins came from, the defence from the panel's
  own fields (how many addresses may reach SSH, never which; hardening and
  whether its alert is open; CDN and closed origin; the switch and the last
  line the cron read; the credential the agents write with; whether the
  panel can tell its own key) and a diagnosis that names how many security
  alerts are open, never their text.
- **For `group_cloud_manager` alone, decided by the route:** the answer
  carries `tables` (addresses with the most failed SSH connections and
  the users tried, with country and whether the jail holds them now;
  sign-ins of 7 days attributed as the security events' cron does, without
  its confirming connection; sudo with the program alone; the watched
  files' changes; the addresses with the most failed web logins with the
  instances each went to; the web-login jail's bans of 7 days with the
  instances tried before), and `/cloud/host/security_lines` gives the last
  200 raw lines of one closed kind. The sign-ins name a known key in its
  window (`known_key`, `known_key_elsewhere` from outside its addresses),
  removed keys included.
- **The known keys' card** (`known_keys`, read on every call and never
  cached; the cache key carries the keys' stamp, so a table is read again
  after an addition or a removal): the keys and the removed ones, who
  added and removed them and when, their last use; their addresses and
  `can_edit` for `group_cloud_manager` alone. `/cloud/host/known_key_add`
  and `/cloud/host/known_key_remove` take a session only (`auth="user"`):
  no API key reaches them and no MCP tool calls them. Anyone else gets no `tables` key and
  is refused the lines: hiding them on the screen is not the control.
- **How the tables count:** on the panel, over the last 5 000 lines of a
  window (10 pages of 500, newest first), by address with IPv6 by its /64,
  the way the web-login alert counts (`logs.count_weblogin_sources`). No
  aggregation by address runs in Loki, so `max_query_series` never refuses
  a table during an attack from thousands of addresses (review M4); a
  window that held more says *counted over the last 5 000 lines*. The
  aggregations reach 24 hours whatever the range; the filtered reads
  (sign-ins, sudo, changes, bans, one instance's web logins) reach 7 days.
  An address is banned now when its last `Ban` of the jail has no `Unban`
  after it. Countries are looked up when the answer is built and never
  kept; the view names DB-IP's attribution when a country came from it.
- **Ids, never LogQL:** the browser sends a host id, a range and at most
  an instance id, which must be of an instance of that server the caller
  can read. An instance in a table is named from the panel's database by
  its id and only when it is of this server; any other id is
  *unidentified*. An archived server, one the caller cannot read or an id
  that is not an integer is *not found*, before anything is asked of the
  central.
- **Cost:** one board is 10 PromQL queries and, for a manager, six log
  reads in parallel (four at a time), one for anyone else; an answer is
  kept 60 s per person, server, window and filter.

## Where the central runs

The central is always installed on a server the panel manages: added to
the panel and through Full Setup *before* *Enable observability*. **A
server of its own is recommended**: it can be hardened and does not
compete with Odoo for memory. A server shared with Odoo instances the
panel manages also works. The panel's own server is not supported: the
panel does not manage it, so the job cannot assume Docker, its proxy or
its configuration.

The central installs no Traefik: with `metrics_central_public_host` set
it joins the one Full Setup leaves on every managed server (network
`inverseproxy_shared`). A server that has not been through Full Setup
refuses the job before it starts, with "Full Setup has not been run on
this server: run it and enable observability again."
`cloud.host.traefik_deployed`, set when Full Setup ends, is what the
check reads. The central needs about 1–2 GB depending on the number of
servers (see Costs and sizing).

**Whether it fits.** Picking a host under *Where the central runs* shows
a note, never a refusal (`/cloud/monitoring/central_sizing`,
`models/_central_sizing.py`), from the host's last measures (its metrics
agents) and the servers that would send to the central, the host
included. The figures, of 13-nov-2026, take the upper end of a running
fleet's (≈ 1.7 GB of VictoriaMetrics and Loki for two servers after three
months): disk ≈ 0.8 GB per sending server per 90 days of retention plus
2 GB; memory 4 GB on a host of its own, 8 GB on one with instances, with
at least 1.5 GB not assigned to them; 2 cores. Past 20 sending servers
nothing is measured, and the note says so; a retention of 0 keeps
everything, and the note says the disk grows without end. A host never
measured reads *Not measured yet*. Change the figures there and here
together.

**The central's server cannot leave the fleet.** Since 1.0.228 the host
in `metrics_central_host_id` cannot be deleted or archived: the model
refuses (`cloud.host._check_not_metrics_central`, on `unlink` and on
`write({'active': False})`), `/cloud/delete_host` refuses before queuing
the teardown (so the MCP's `delete_host` reads the same refusal), a
`delete_host` job queued before refuses to run (`JobRefused`), and the
foreign key is `ondelete='restrict'`. Every other change to that host,
and every job on it, runs as before. A central recorded on a host that
was archived before 1.0.228 protects nothing.

**Not on another host while it runs.** *Enable observability* on a host
other than the central's used to start an empty central there and move
the panel to it, leaving the history behind, unread. The route and the
job (`ObservabilityCentralExecutor.pre_run_checks`, inherited by the
account sync) now refuse it: "The central runs on X…". On the central's
own host, and with no central at all, it deploys as before, and so does
the reconciliation cron.

Two ways out, both in Settings › Monitoring, both with a confirmation:

- **Start an empty central.** Picking another host turns the button into
  *Start an empty central* (`/cloud/monitoring/empty_central`). It
  queues the same deployment with `discard_history` in its payload, the
  only thing that lets the job run on another host. `enqueue` refuses
  that key to any caller without superuser rights
  (`cloud.job._check_internal_queue_flags`), so neither `call_kw` nor the
  MCP can set it. When it ends, the panel reads and writes the new,
  empty central; the old one is left as it was on its server, its
  history with it, and the job's log says so. The old server can then
  be deleted, or the stack removed by hand there with
  `docker compose -p incubacloud-central down -v`. Under a public name
  (`metrics_central_public_host`) the panel and the agents keep using
  the name, so point its DNS record at the new server first: the
  confirmation and the job's log say so. The panel does not check the
  record (13-nov-2026): behind a CDN the name answers with the CDN's
  addresses, and a host's own address may be one only the panel reaches,
  so a comparison would refuse good setups.
- **Retire the central (the history is lost).** For a central whose
  server is lost, or a panel whose only server is going
  (`/cloud/monitoring/retire_central`, `cloud.settings.retire_metrics_central`):
  the panel forgets the host, empties the query and remote-write URLs,
  the public name and the list of accounts on the central, closes the
  central's open alerts and switches observability off. The direct name
  stays: it is probed before it is used. The remote-write URL goes even
  when it was written by hand. Nothing runs on the server. Refused while a
  deployment or an account sync of the central waits or runs, which would
  record the host again. Agents already installed keep sending to the
  old address until observability is enabled on another host.
  A module above core may refuse it, when its central serves more than
  this panel (`cloud.settings._central_retire_refusal`); the button is
  then not offered.

**To keep the history**, by hand: start the empty central on the new
host, then stop VictoriaMetrics and Loki on both servers
(`docker compose -p incubacloud-central stop victoriametrics loki`; vmauth
answers 502 and the agents keep what they send), copy the contents of the
two volumes, `incubacloud-central_vmdata` and
`incubacloud-central_lokidata`, from the old server to the new one, and
start them again on the new one (`docker compose -p incubacloud-central
up -d`). Delete the old server only once the new central answers with the
old series.

**Odoo.sh** does not let the central run, so a panel there needs a
metrics server of its own, prepared as above.

## Moving the central to its own VPS

The destination is a parameter, not a hard-coded host. A central on a
host of its own is published under a name of its own, which the
deployment wires into that host's Traefik; the hand-written gateway
below is only for a central next to the panel.

1. Add the new server as a host, **not** behind the CDN (the central
   needs a certificate of its own and Cloudflare's bot challenge drops
   `vmagent`), and prepare it with Full Setup, with a **direct port** on it if
   the central has a direct name.
2. Point a DNS-only A record, `metrics.<domain>` for example, at it.
3. Point the direct name's A record at it too (DNS-only, no AAAA). If the DNS
   token is restricted to client addresses, add the new host's.
4. In Settings → Monitoring → *Advanced…*, fill in **Central's public
   name** with that name and **Who may delete series on the central**
   with the panel's public address (`/32`): it opens the series deletion
   to that address, under the name.
5. Pick the new host under *Where the central runs* and press *Start an
   empty central* (*Enable observability* is refused there while the
   central runs on the old host). When the job answers, the panel's query
   URL and the agents' remote-write URL point at the new name.
6. Hosts enrolled before the move are re-applied on their own once the new
   central's host has applied the list and its direct name answers.
7. Remove `metrics-gateway.yml` from the old host's Traefik and
   decommission the old stack.

Historical series are not migrated by this procedure. If you need them,
copy the central's two volumes across before step 5 ("To keep the
history" above) — otherwise expect the charts to start from the moment of
the switch. The old host can be deleted once the new central runs.

What the deployment publishes under the name is what the hand-written
gateway publishes (next section), with one difference: the operator's
`/admin-d/` has a router of its own (`ic-central-delete`, priority 30)
that admits the addresses in **Who may delete series on the central**
plus the host itself, the forwarded address when the name is behind a
CDN (`ipstrategy.depth: 1`). With the setting empty, it answers the host
only. The operator's read had a router there too, `ic-central-read` on
`/admin-r/`, until 17-nov-2026; the deployment of central revision 4
removes it. No operator route is published on the direct entrypoint.
Removing a router label recreates vmauth, a few seconds the agents'
queues cover.

## Undo

- **What Grafana left.** The first deployment with 1.0.227, which the
  update queues on its own, removes the volume `incubacloud-central_grafana-data`,
  the central's `provisioning/` and `dashboards/` directories and the
  image `grafana/grafana:11.2.0`, after the boundary answered. Nothing in
  them was the only copy: Grafana's dashboards and organisations were
  written from the code. To keep them, create the system parameter
  `incubacloud.metrics_keep_grafana_data` (any value) **before** updating;
  remove it, or run `docker volume rm incubacloud-central_grafana-data` on
  the central's server, when they are no longer wanted. A removal that
  fails is said in the job's log and does not fail the deployment.
- **Fluent Bit misbehaves on a server** (memory, CPU): turn off **Send
  security logs** on that server; its container goes at once. For the
  whole fleet, the same server by server (an agent's `manage_host` asks
  for approval for each).
- **Back to the release before.** The columns 1.0.227 stopped using stay
  in the database, emptied: the release before starts. Its agents'
  revision is lower, so hosts already on revision 2 keep Fluent Bit until
  their next install gives them promtail back; both send to `/lw/`. That
  install removes Fluent Bit's container but not its configuration,
  `~/observability/fluent-bit/` (the credential is in it, 0600), nor its
  volume: on each server, `rm -rf ~/observability/fluent-bit` and
  `docker volume rm incubacloud-observability_fluent-bit-data` as the
  panel's user. A
  deployment of the central by the release before brings Grafana back
  with an empty volume and nothing pointing at it.

## Labels

Every sample carries `host`, `host_id`, and — for container metrics —
`instance`, `instance_id`, `project` (plus the account label a module
that extends the panel may fill).
This is not cosmetic: labels cannot be added to series that were already
written, so anything not labelled at ingest is unattributable forever.
The panel generates the label map (only it knows which compose project
belongs to which instance) and passes it to the agent playbook.

**The map is a snapshot, not a subscription.** It is rendered when the
`install_observability` job runs and written into the host's vmagent
config; nothing re-reads it afterwards. An instance that appears on the
host later — or one whose *names* change later — keeps producing series
that no rule matches, and those series are unattributable for as long as
that lasts. `cloud.host.refresh_observability_labels()` is the call that
re-renders it; every operation that changes what a host holds is expected
to end with it, and one that forgets is invisible until somebody asks a
question the missing label was the answer to.

Measured on 2026-09-22: three instances deployed ahead of being handed
to their owners had no `instance_id` on their sleep-proxy series,
because they were deployed before they had a name and the hand-over that
gave them one did not re-render the map. The panel's activity cron filters `instance_id!=""`, so all
three were invisible to it — one with 195 requests over seven days and
two days left before an inactivity auto-suspend would have taken it
down. The fix is in the hand-over; the lesson is that a stale label map fails
silently and looks exactly like disuse.

To check a host by hand: `grep -c svc@file ~/observability/scrape.yml`
on the host should match the number of non-draft instances it holds, and
`sum by (router, instance_id) (…)` over any per-instance metric should
show no empty `instance_id`.

## Deleting series

Series of what no longer exists are deleted; before 1.0.135 they stayed
until the retention dropped them.

| What goes | When | Selector |
| --- | --- | --- |
| an instance | its record is unlinked (every removal path ends there) | `{ic_account, instance_id}` |
| a host | it is retired (archived — hosts are never unlinked by the teardown) | `{ic_account, host_id, instance_id=""}` |
| a whole account | the account is revoked, through a module that extends the panel | `{ic_account}` |

**The selector always names the account.** `instance_id` and `host_id`
are row ids of the panel that owns them, so `instance_id="5"` exists in
many accounts at once; `series_selector()` refuses to build a selector
without one. A host's selector keeps series that carry an `instance_id`,
so an instance that moved to another host keeps its history.

**It waits 30 minutes.** The per-instance disk collector rewrites its file
every ten minutes and node_exporter serves the old one meanwhile —
measured, a removed instance kept sending samples for three minutes —
and a sample that arrives after the deletion recreates the series. The
job is queued inside the removal's own transaction, on `root.bg`, so a
rolled-back removal deletes nothing.

**Only the panel that owns the central deletes.** It holds the operator
credential; the gateway adds VictoriaMetrics' `-deleteAuthKey` on the
operator's `/admin-d/` route, so the panel never sends the key and no account can delete at all.

**The key is a file, not a flag value.** Until 12-nov-2026 it was in the
VictoriaMetrics container's command, which `docker inspect` shows to
anyone in the host's docker group. The deployment now writes it to
`victoriametrics/delete_auth_key` in the central's directory (`0600`,
`no_log`), mounts that directory read-only and passes
`-deleteAuthKey=file:///etc/victoriametrics/delete_auth_key`.
VictoriaMetrics reads such a file again every few seconds, so a new key
takes effect without a restart (measured on v1.102.0). The first
deployment with this release replaces a key recorded before it, once
(`incubacloud.metrics_delete_key_in_file` marks a key that only ever
travelled in the file), and recreates the VictoriaMetrics container
because its command changes: a few seconds without ingestion, which the
agents' queues absorb. To rotate it again: delete the system parameter
`incubacloud.metrics_delete_key_in_file` and press **Enable
observability**; the deployment mints a new key, writes it to the file
and to vmauth's route in the same run. The account sync never rotates
it: it does not write the file. Another
panel with an account names what it removed through a route a module
that extends this panel provides, and core deletes it under the account
core holds for that panel — never one the request names. Until the central is redeployed with 1.0.135 there is no key and
every deletion is skipped.

**Failure is a warning, not an error.** `metrics_purge_failed` means some
series were left to expire with the retention; it clears on the next
deletion that works.

**Disk comes back later.** VictoriaMetrics hides deleted series at once
and reclaims their space in background merges.

**A retired host stops shipping.** The teardown takes the agents down and
removes their directory, which holds the account credential. Left
running, a server that outlives its record would recreate every series
deleted for it.

## Tuning alerts

Rules are data (`cloud.metric.rule`), not code: threshold, comparator,
severity, message and the PromQL itself are all editable. The seeded
rules are shipped updatable on purpose, so an improved threshold reaches
existing installs on upgrade.

A rule whose expression uses `absent(...)` is a presence check — that is
how "this host stopped reporting" is expressed.

Rules added with the server's board (12-nov-2026), whose thresholds are
to be tuned in QA with real data:

| Code | Reads | Threshold | Confirm | Why |
| --- | --- | --- | --- | --- |
| `metrics_host_io_pressure` | PSI I/O "some", % of the last 5 min | > 20 | 3 cycles (15 min) | A fifth of the time is where every instance of the server feels the disk; a backup saturates it for minutes, not for fifteen |
| `metrics_host_memory_pressure` | PSI memory "full", % of the last 5 min | > 10, critical | 2 cycles | A tenth of the time with nothing running for lack of memory: the kernel is evicting every instance's pages |
| `metrics_instance_cpu_throttled` | CFS periods the `odoo` container was throttled, % of the last 5 min, one-off runs left out | > 25 | 3 cycles (15 min) | A quarter: requests queue behind the quota most of the time (the instance board's threshold, 10-nov-2026) |
| `metrics_instance_db_lock_wait` | `min_over_time(ic_pg_waiting_lock[10m])`: the fewest sessions waiting for a lock in any minute of the last ten | > 0 | 1 cycle | Somebody waited the whole ten minutes; two readings five minutes apart could each catch a short wait (review of 12-nov-2026) |
| `metrics_instance_db_connections` | `ic_pg_connections / ic_pg_max_connections`, % | > 85 | 2 cycles | The next workers or a burst of crons start being refused a connection |

Both database rules read only a fresh `ic_pg.prom`
(`time() - node_textfile_mtime_seconds{file=~".*ic_pg.prom"} < 180`): with
no collector, or one that stopped, there is no sample, so neither an alert
nor a failure.

The servers' security rules (#84, 14-nov-2026) read Fluent Bit's
counters, which exist only where a server sends its security logs. Their
thresholds are **provisional**: the measurements that fix them (the
noise of normal use, the fleet's `already_banned` and failed web logins)
are still to be taken, and each value is replaced with its measurement
and date when they are.

| Code | Reads | Threshold | Confirm | Why this value for now |
| --- | --- | --- | --- | --- |
| `security_ssh_bruteforce` | failed SSH connections in 10 min, per server | > 100 | 2 cycles | Ten a minute: a person who mistypes makes a few, a scanner of the internet hundreds (the F0 burst: 600 a minute) |
| `security_f2b_ineffective` | fail2ban's `already banned` in 30 min, per server | > 20, critical | 2 cycles | An address banned that keeps arriving means the ban is not applied; under a heavy attack a few repeats while a ban is set may be normal, so a second reading before paging |
| `security_weblogin_bruteforce` | failed web logins in 10 min, **per instance** (`ic_weblogin_failed_total`, `instance_id="0"` left out) | > 50 | 2 cycles | Five a minute against one instance: more than a forgotten password, well under what the web-login jail bans an address for |
| `security_logs_silent` | 1 when Fluent Bit's exporter has not answered in 15 min, its Loki output dropped lines in 30 min, or it read lines of sshd, sudo or weblogin in 30 min and delivered none | > 0 | 2 cycles | "up" alone says Fluent Bit lives, not that Loki takes what it sends (a 401 is retried for hours without dropping anything); a second reading, as the other silence rules, so a line counted and not yet delivered at the scrape, or Loki down for a minute, does not page every server |

Every one of them ends in `or (… * 0)`, a 0 for each target seen in the
last hour (the reporting servers, for the silence rule): the evaluator
resolves only a target the answer names, and Fluent Bit's counters vanish
when it restarts, until the next line of their kind. Each target is
judged once a pass, on its worst sample.

**The web-login alert names the addresses with the most attempts.** Only
when the rule fires, the panel reads that instance's `weblogin` lines of
the last 10 minutes from the central's Loki and adds the three addresses
with the most attempts and their count (IPv6 by /64) to the text; the
alert's payload keeps the same list, never a line. At most 10 reads a
pass, the instances with the most attempts first; the others, a central
that is busy or fails, and a template without `%(sources)s` go out with
the figure alone. A server reached through a CDN is not read: its proxy
sees the CDN's addresses, and the text says so.

**Names and messages are translatable.** The text is written in the
language of the context the rules are evaluated in (the cron runs
without one, so English), as every other alert producer does. A
translation whose markers are broken, any `%` that is not `%(host)s`,
`%(value)s`, `%(sources)s` or `%%`, is not used: the English text is.

## URLs: which one must resolve from where

The two URLs in Settings are consumed by **different containers**, and
this is the single easiest thing to get wrong (it was got wrong during
the first live deploy):

| Setting | Used by | Value shape | Must resolve from |
|---|---|---|---|
| **Metrics backend URL** | the panel (PromQL queries) | `…/r` (the panel appends `/api/v1/query`) | inside the **panel** container |
| **Remote-write URL** | `vmagent` on every host | `…/w/api/v1/write` | inside the **agent** container, on each host |

The remote-write URL is the fallback: each host's agents use the central's direct
name when it applies to them (see _The direct path_), and the host page shows the
URL its agents were installed with.

The endpoint travels *in* the URL, after the account prefix (`/w/`,
`/r/`), because vmauth strips the prefix and appends the rest — a bare
`/w/` would not reach `/api/v1/write`. The log endpoint is derived from
the write one (`/w/api/v1/write` → `/lw/loki/api/v1/push`), so there is
one fewer field to get wrong.

`127.0.0.1` is almost never correct for either: a container's loopback is
its own, not the host's. The central therefore binds vmauth to the
**Docker bridge gateway** (`172.17.0.1` by default, discovered from
facts), reachable by containers on that host and not routable from
outside it.

- Agents **on the central's own host**: the bridge address, filled in
  automatically by the deploy.
- Agents **on any other host**: the central's public HTTPS URL, published
  through Traefik (see below).

Symptom of getting it wrong: the backend is healthy and `curl` works from
the host shell, while `docker logs …vmagent` repeats `connection
refused`. Buffered samples are not lost — vmagent retries and backfills
once the URL is right.

## Publishing the central through Traefik (agents on other hosts)

Agents and panels that are not on the central's host reach it over
public HTTPS, terminated by the panel's existing Traefik. This is a
one-time edit to Traefik's dynamic config on the central's host — a
**separate file**, so a bad edit is rejected without taking the rest of
the proxy down. Its shape:

- `Host(metrics.<domain>)` and `/admin-d/` → **denied** from the public
  (an `ipWhitelist`). Every operator route vmauth serves must be on this
  list; a test pins it to what the code renders. vmauth serves no
  `/admin-r/` since 17-nov-2026, so a file that still denies it denies
  nothing more. Their
  only real caller, the panel, reaches vmauth over the bridge, never
  through Traefik.
- `Host(metrics.<domain>)` and `/w/ /r/ /lw/ /lr/` → vmauth's bridge
  port, which returns 401 without a credential. **Only those four
  prefixes**: vmauth answers its own `/flags`, `/metrics`,
  `/debug/pprof/` and `/-/reload` to anyone, and until 2026-09-27 a POST
  to them from outside got 200. Any other path of the name, `/grafana/`
  among them, falls to the host's catch-all.

Then set the URLs the agents on other hosts use in Settings to
`https://metrics.<domain>/w/api/v1/write` and
`https://metrics.<domain>/r`.

Behind a CDN, a bot challenge on the ingest path can silently drop `vmagent`.
Machines that are yours should not go through it: give the central a direct name
(next section). The CDN-fronted name stays for browsers and for hosts you do not
run.

## The direct path (machines only)

A host can open a **direct entrypoint** (`cloud.host.direct_port`, applied by Full
Setup): a port of its own, never 443, with a certificate from Let's Encrypt obtained
by DNS-01 (`acme_dns_cloudflare_token`, written to the host as a root-only file), no
trust in forwarded headers, a per-source rate limit and an allowlist enforced twice
— in nftables (`ic_direct_v4`) and in Traefik (`direct-peers`). The list is the
panel's own hosts plus what an upper layer adds, pushed by `sync_direct_path`
whenever a host comes or goes and at least hourly, without restarting Traefik. The
port also caps new connections per source and has its own timeouts: one Traefik
serves it and 443. A catch-all router at priority 10000 answers 403 to anything
without a router of its own above it, so routers that listen on every entrypoint
stay invisible there.

With **Central's direct name** set, the central's deployment publishes
`/w/ /r/ /lw/ /lr/` on that entrypoint. A host's agents are offered the direct name
when the name answers (the job probes it from the central's host), the host's own
address is on the list the central's host has applied, and the host is not marked
_Use the public path_. The agents' install then tries it from the host itself and
falls back to the remote-write URL in Settings if it gets no answer; a host that
fell back is not offered the direct name again for six hours. A change in any of
these re-applies the agents of the hosts it affects, at once and again every 15
minutes.

Two failed probes in a row (one, if the name resolves to another address or has an
IPv6 record) put every host back on the public path and raise `direct_path_down`;
two good ones, and no sooner than 30 minutes, bring them back. The job probes at
least hourly; an upper layer may probe more often. A host whose metrics stop
arriving while the path is up is re-applied after 20 minutes, which tests again from
the host. Changing the URL leaves whatever vmagent still had queued for the old one
behind. A certificate with less than 21 days left raises
`direct_certificate_expiring`. When it goes wrong, see
[RB-24](runbooks/RB-24-direct-path.md).

## The single path: one name, the platform port, the panel's authority

Settings → Monitoring → *Advanced…* → **One path, without a CDN**. With it on and
**Central's public name** set, the central is published under that one name only,
outside every CDN, on the **platform port** of its server: the direct entrypoint
above (`direct_port`, 9443 for example), with its two barriers, nftables and Traefik.
The URL carries the port: `https://metrics.example.com:9443/r` for the panel,
`…:9443/w/api/v1/write` for the agents. Nothing is published on 443 for the name.
Without a public name the switch changes nothing: the central stays on its server's
Docker bridge, as on a single server.

The switch is on in a new database. An update leaves it **off** where a central was
already recorded: that panel keeps every path it had (the public name on 443, the
direct name, a hand-written gateway) until its operator turns it on. Off, nothing of
this section runs.

**What the name needs.** An A record pointing at the central's server, **without a
CDN proxy and without an AAAA record**, and a direct port applied on that server (Full
Setup). The deployment refuses a server without the port. The DNS record is created
by hand; the panel checks it instead of trusting it.

**The certificate.** The panel holds a certificate authority of its own
(`metrics_ca_cert`, and its key `metrics_ca_key`, encrypted and readable only by
system administrators): ECDSA P-256, ten years, minted at the first deployment that
needs it. Each deployment of the central signs a certificate for the name, valid one
year, and writes it on the central's server with its TLS option, `ic-central`
(TLS 1.2 at least): `~/traefik/certs/ic-central.{crt,key}` (the key 0600) and
`~/traefik/dynamic/ic-central-tls.yml`, which Traefik reloads without a restart. The
job writes these itself, not Full Setup, so it works on a proxy kept by hand as long
as it mounts `~/traefik/certs` at `/etc/certs` and watches `dynamic/`. The
reconciliation cron deploys the central again when its certificate has less than 60
days left; at 21 days `metrics_central_cert_expiring` (critical) says renewing has
been failing. The agents trust the authority for the central only, from a file of
their own (`~/observability/central-ca.pem`, given to vmagent as
`-remoteWrite.tlsCAFile` and to Fluent Bit as `tls.ca_file`), never through the
system's store; the panel verifies its reads with the usual roots plus the authority.

**When the URLs move.** The deployment leaves the panel's URLs as they were. The
direct path's job (`sync_direct_path`) then checks the name from the central's server
with the authority: DNS resolves to the server and nothing else, the TLS handshake
validates, `POST /w/api/v1/write` answers 401 and `/flags` is refused. Until it
passes, `metrics_central_dns_pending` (warning; critical after 12 hours when a central
was already serving) names the record to create, and the check runs again every 15
minutes and with **Check now** under the name. When it passes, the panel writes
`https://<name>:<port>/r` and `…/w/api/v1/write` as its URLs, keeps the earlier two in
the system parameter `incubacloud.metrics_central_legacy_urls`, and the
reconciliation re-applies every server's agents with the new URL and the authority.
Each server records the fingerprint of the authority it was installed with
(`metrics_agents_ca_fingerprint`), and the reconciliation also re-applies one whose
fingerprint is not the authority in force: one installed before the authority reached
its panel, or with an authority since rotated (RB-35).
One check that passes is enough, even after failed ones: the two good checks 30
minutes apart that bring a direct name out of *Failing* protect a name in use from
flapping, and a name not in use yet has nobody on it. Only this complete check, which
reads DNS from the central's server, moves the URLs; the light probe never does.

From then on, two failed checks in a row raise `metrics_central_name_down` (critical,
with the layer: `connect`, `tls` or `route`); a DNS answer that is not the central's
server (a CDN's proxy turned back on, the record deleted, or left behind after a move
to another server) raises `metrics_central_dns_pending` again, critical at once, with
the record to fix: every agent writes to the name. A resolver of the central's server
failing for a moment gives the same answer, so while the alert is open the
observability cron checks the name again every 15 minutes, and the alert does not
claim that no agent writes. One complete check that passes (**Check now**) closes it;
the name's state leaves *Failing* after two good checks 30 minutes apart, as a direct
name's. There is no path to fall back to; the agents' queues (512 MB per URL for
vmagent, in its volume so a re-apply keeps it; 64 MB for Fluent Bit) hold what they
cannot send. See [RB-35](runbooks/RB-35-central-name.md).

A central that was published on 443 under the same name stops answering there at
the deployment that turns the switch on: nothing is published on 443 with it. Where
the name's record already points at the central's server, with no CDN proxy, the
check that deployment queues moves the URLs within the minute, and until then the
agents queue. Where the name goes through a CDN, the agents keep writing through it
to 443, which no longer answers: the name's certificate is in the server's default TLS
store, which every entrypoint shares, and the CDN refuses it. Change the record (the
server's address, no proxy, no AAAA) first, wait until the central's server resolves
it, and then turn the switch on; RB-35, "Before cutting the earlier path", gives the
order and the times measured.

A certificate ACME obtained for the name while it was published on 443 stays in the
proxy's store, and Traefik then serves it or the panel's after each reload: the
agents refuse the first. Each deployment under the single name looks for it in the
store and, when it is there, says so in its log and raises
`metrics_central_acme_same_name`; it does not edit the store, which RB-35 says how
to prune.

**The way back.** Turning the switch off puts back the two URLs kept, the
reconciliation re-applies the agents to them, and a deployment of the central is
queued at once: until it removes the name's certificate, the proxy answers the name
with it on 443 too. A central's direct name, while set, is
published beside the single name on the same port, so both work during a transition.

**Who can write to the central.** *Only this panel's servers, by their address* (the
default): the platform port of the central's server admits the panel's hosts, the
private ranges (what is born on that server) and, with the single path on, the
addresses in **Who may delete series on the central** (the panel
itself, when it runs elsewhere). *Any address, with each account's credential*: that
one port admits every address, and vmauth's credential is the only barrier; for
servers behind NAT or with an address that changes. The other servers' ports keep the
list. A module that serves several panels may fix the choice, and the setting is then
not shown.

With the servers only, a server whose traffic leaves with an address the port does not
admit cannot write. Its agents' install checks the central from the server (401 is the
central reached) and asks the panel which address it leaves with
(`GET /cloud/egress_ip`, over IPv4 with `curl -4`, as the name has an A record only;
answered without a session and without writing anything). The panel keeps the answer
only when it is an IPv4 address: a page a CDN answered instead (a challenge) is
dropped, and the alert then says the panel could not learn it. A 403 or no answer,
or 20 minutes without its metrics while other servers write, raises
`metrics_central_writer_outside` for that server, with both addresses and the two ways
out: correct the server's address in the panel, or let any address write. A server
whose check refused the certificate it was answered with is named that way instead,
without addresses: the port let it in. The
silence alone raises nothing while the panel cannot reach that server either
(`host_unreachable` open: switched off, or its network down), or while its own last
check got 401 leaving with the address the panel has for it.

**The operator's route** (`/admin-d/`, series deletion; `/admin-r/` until
17-nov-2026) answers under the name on the platform port to the server itself, the
private ranges (a panel beside the central reaches the port through Docker's DNAT,
with a container's address) and the addresses in **Who may delete series on the
central**; the operator's credential and the deletion key still guard it.

To move a central with its history to another server by hand, see
[RB-36](runbooks/RB-36-move-the-central-by-hand.md).

## cAdvisor version requirement (do not pin it back)

**cAdvisor must be v0.54.0 or newer.** Docker 28/29 default to the
containerd image store, and older cAdvisor cannot resolve container
metadata under it: it logs `failed to identify the read-write layer ID …`
for every container and registers **none**, so only the root cgroup is
exported. The failure is silent in the worst way — host metrics keep
flowing, so the stack looks healthy while no instance can ever be
identified.

Measured on Docker 29.6 / Ubuntu 24.04, with a two-container compose
project:

| cAdvisor | containerd image store (default) | legacy overlay2 store |
|---|---|---|
| v0.49.1 | 0 labelled series | 59 |
| v0.52.1 | **0** | — |
| v0.54.1 | 54 | — |
| v0.55.1 | **54** | 69 |

So no Docker downgrade and no daemon flag is needed: just a current
cAdvisor. The playbook pins v0.55.1.

If per-instance panels are empty, check the version first:

```bash
docker inspect incubacloud-observability-cadvisor-1 --format '{{.Config.Image}}'
docker logs incubacloud-observability-cadvisor-1 | grep 'read-write layer'
```

## Retiring the SSH telemetry

The old per-target SSH jobs are being retired *conditionally*, never
deleted outright, so no window exists where nobody collects.

**Host specs** — `host_metrics_executor` becomes a no-op while metrics
are actually arriving, and resumes on its own if they stop. It decides
that from `cloud.host.metrics_last_seen`, which **only the metrics reader
writes**. That distinction is load-bearing: the field it used to read,
`last_probed`, is also written by the SSH job itself, so the fallback
stood down on the strength of its own footprint and degraded from every
five minutes to roughly every fifteen — precisely when it was the only
thing collecting.

**Instance liveness** — both the metrics cron and `instance_health` can
decide whether an instance is up. Metrics own the flag while their
readings are fresh (`cloud.instance.metrics_last_seen`); the SSH probe
keeps doing what metrics cannot — HTTP probing, error-log scraping — and
takes the flag back by itself the moment the readings go stale. One
exception: when the probe finds `odoo` running and answering its HTTP
check while the flag reads False, it writes True even though metrics own
it — direct evidence, fresher than any scrape. It does not hold the flag
there: the cron's next reading writes whatever that reading says, so a
lasting blind spot in the metrics (a host clock minutes off, say) shows
as the flag flipping between the two. A stopped or not-yet-answering
odoo is still the metrics' to report while they are fresh. Without
that arbitration the two wrote on their own schedules and the state
flapped whenever they briefly disagreed.

A job that brings odoo up (start, restart, deploy, rebuild, the
restores, a move's cutover) confirms `running` and stamps
`running_confirmed_at` when it ends. Switching continuous recovery on or
off, when it restarts the database with odoo stopped around it, does so
mid-job instead, the moment `pitr.sh` prints `IC_PITR_ODOO_RESTARTED`:
the first base copy that follows a switch takes longer than the grace.
For 120 s after (`_CONFIRMED_GRACE_SECONDS`) the
cron does not write the flag False while the recreated container's
series is missing — a 30 s scrape plus VictoriaMetrics' 30 s latency
offset, doubled. A rise the cron reads itself opens no grace, and any
write of `running` False — a Stop, the probe finding the container
down, a stack handed back stopped — closes it.

`instance_health` cannot retire until synthetic probes cover its HTTP
check. Its error-log scraping now has a better source in the access logs.

## Gotchas

- **Traefik metrics need the config, and reach vmagent over docker.**
  Traefik exposes them on its `metrics` entrypoint (`:8082`). vmagent
  scrapes it **by service name over Traefik's `public` network**
  (`inverseproxy_public`), not through a published host port: publishing
  on `127.0.0.1` is unreachable from a container, and publishing on
  `0.0.0.0` would expose instance names and domains on any host that is
  not hardened. Not over `inverseproxy_shared`: every instance on the
  host joins that one, so the entrypoint is bound to the proxy's address
  on `public` (`proxy.inverseproxy_public:8082`) and vmagent listens on
  its own loopback (`-httpListenAddr=127.0.0.1:8429`) — on `shared`, any
  instance could read the other instances' traffic, and push series
  vmagent would forward under the host's credential. The scrape job is
  only generated when that network exists, so a host without Traefik does
  not sit at `up=0` forever.
  **That name has to resolve, or the proxy does not start at all**:
  Traefik looks an entrypoint's address up once, at start-up, and exits
  when the lookup fails (measured on v2.11). Docker answers it from the
  compose project `inverseproxy`, its service `proxy` and its network
  `public`, which is how Full Setup starts the proxy. A self-hosted
  `inverseproxy.yaml` edited by hand must keep all three; one that renames
  or drops any of them stops every site on the host on the next restart.
  Existing hosts get the Traefik config retrofitted into their stored
  template on upgrade (a minimal merge that leaves customised templates
  alone), but it only reaches the host on the next *Setup Host* run.
- **The remote-write token is a fleet-wide credential, enforced as HTTP
  basic auth.** Single-node VictoriaMetrics has no bearer-token check, so
  the first version — a token written to every host, sent on every push,
  and read by nobody — authorised nothing: any client that could reach
  the endpoint could write series, and the alert rules read those series.
  The central now starts with `-httpAuth.username/-httpAuth.password`,
  vmagent pushes with `-remoteWrite.basicAuth.*`, and the panel's own
  PromQL queries carry the same credential.

  Both sides must agree. Rotating the token means **redeploy the central
  first, then let the reconciliation cron re-apply the agents everywhere**; in between,
  pushes are refused with 401 and the staleness rule will start firing —
  which is the intended behaviour, not a fault.

- **The staleness watchdog's PromQL is not obvious, and two plausible
  forms are silently wrong.** `absent(up{job="node"})` carries no
  `host_id`, so the evaluator cannot attribute it and drops it; and
  `timestamp(last_over_time(up[1h]))` returns an *empty* result on
  VictoriaMetrics v1.102 even while the host is reporting. Both look
  right and neither ever fires. The form that works is the subquery
  `time() - max by (host_id, host) (last_over_time(timestamp(up{job="node"})[1h:1m]))`,
  measured against a host silenced for six minutes: 341 s, labels
  intact. Verify any change to this rule against a live backend — a
  broken watchdog is indistinguishable from a healthy fleet.

- **Per-instance HTTP is not available.** Traefik reports per
  service, and the service name is a copier-time literal from each
  customer's own `prod.yaml`; across eight real doodba projects it bore
  no relation to `COMPOSE_PROJECT_NAME`, the key every other rule joins
  on. Guessing a mapping would attribute one customer's traffic to another,
  so the Traefik panels live on the **host** dashboard, grouped by
  service. Per-instance HTTP needs probes from outside, which the monitoring does not include.
- **`running` comes from cAdvisor**, not from HTTP traffic. An idle but
  healthy instance must keep reading as running; sourcing it from traffic
  would eventually feed the 14-day auto-suspend with false idleness. It
  is read from the freshest series of the `odoo` service (`min by`): a
  recreated container leaves the old one's series reported for minutes.
  The one-offs of compose (`<project>-odoo-run-<hash>`) and the rebuild's
  boot test (`ic_boot_odoo_<id>`) carry the service's label too, and are
  left out by name, as the instance dashboard leaves them out.

## What each panel is allowed to show, and why

Observability is not one switch: it is three independent facts, and the
panel asks `cloud.settings._observability_capabilities()` for all three
rather than reading fields. They are:

| Axis | True when | Owned by |
|---|---|---|
| `collect` | `metrics_enabled` — agents enrol, rules evaluate, PromQL is queried | the panel's own settings |
| `dashboards` | `collect` **and** a non-blank `metrics_central_url` (the panel draws its own charts) | the panel's own settings |
| `configure` | this panel's operator edits those settings | core says `True`; a layer above says otherwise |

Core publishes `configure: True` because it has no notion of a panel
whose settings arrive from elsewhere. A module that extends the panel
and pushes those settings from another panel sets it to `False`, and
everything else such a managed panel shows follows from the settings it
was pushed.

**The rule every surface follows:** never offer something whose only
possible message is an instruction the viewer cannot act on. Concretely:

- The **Monitoring nav entry** appears when `dashboards` is true, or when
  it is false and `configure` is true (so the empty state is a prompt the
  operator can act on). A panel with neither gets no entry.
- The **Monitoring page** draws the fleet's board and one server's. Its
  boards state what is missing when `configure` is true (observability
  off, or on without a central), and state plainly that charts are
  unavailable when it is false. That branch is reachable only by typing
  the URL, and exists so that doing so is honest rather than misleading.
- A **server's Metrics tab** follows the instance's: its board whenever
  `collect` is true, the same prompts otherwise.
- The **instance Metrics tab** draws its own board (no embed) whenever
  `collect` is true: it reads the central with the panel's own account
  credential. With `collect` false it shows the
  "enable observability" prompt only when `configure` is true, and no
  board otherwise. The readings on demand (*Queries now*, *Slow routes*)
  and *Recent requests* stay in every case: they go to the host over SSH.

### Convivencia matrix — check this before releasing an observability change

Seven deployments, five surfaces. Run this by hand when the descriptor or
any of its consumers change; the axes themselves are pinned by
`test_observability_wiring.TestObservabilityCapabilities` and the nav
gate by `TestNavGateConsultsEveryAxis`, but "does the whole thing still
make sense together" is a reading, not an assertion.

| Deployment | C | D | F | Nav entry | Monitoring page | Instance Metrics | Settings→Monitoring |
|---|---|---|---|---|---|---|---|
| Partner, observability off | ✗ | ✗ | ✓ | hidden | (typed URL) "Observability is not enabled" | prompt + readings + recent requests | shown |
| Partner, on without a central | ✓ | ✗ | ✓ | shown | "there is no metrics central" | "there is no metrics central" + readings + recent requests | shown |
| Partner, with its central | ✓ | ✓ | ✓ | shown | fleet and hosts boards | board + readings + recent requests | shown |
| Panel that pushes settings to others | ✓ | ✓ | ✓ | shown | fleet (its own servers) and hosts boards | board + readings + recent requests | shown |
| Managed panel, granted | ✓ | ✓ | ✗ | shown | fleet and hosts boards of its account | board + readings + recent requests | hidden |
| Managed panel, granted, no central pushed yet | ✓ | ✗ | ✗ | **hidden** | (typed URL) "Charts are not available on this panel" | "not available" + readings + recent requests | hidden |
| Managed panel, not granted yet | ✗ | ✗ | ✗ | hidden | (typed URL) "Charts are not available on this panel" | no board; readings + recent requests | hidden |
