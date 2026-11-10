## Deploying

1. **Settings → Monitoring** — pick the host that should run the central
   and press **Enable observability**. That is the whole procedure.
2. The job deploys VictoriaMetrics, Loki, Grafana and vmauth (the account
   boundary) and, when they answer, writes the endpoints back, generates
   this panel's account credential and switches observability on.
3. **Hosts enrol themselves.** A reconciliation cron (every 15 min)
   installs the agents on any host that should be reporting and is not,
   with a growing back-off per host and an alert once failures persist.
   Host setup also chains an install, but only as an accelerator — if it
   fails, the cron picks the host up.

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
│ Traefik        (HTTP, :8082) │   │  /admin-r/ /admin-d/ /gadmin/    │
│ vmagent   ── push ───────────┼──▶│         │                        │
│ promtail  ── push ───────────┼──▶│         ├→ VictoriaMetrics       │
└──────────────────────────────┘   │         └→ Loki                  │
                            HTTPS  │                                   │
   browser ─ HTTPS ─▶ Traefik ─────┼──▶ Grafana (org per account)     │
                                   └──────────────────────────────────┘
                                              ▲
                                     PromQL   │
                                 panel cron ──┘   → cloud.alert
```

**Push, not scrape.** Hosts open no inbound port; the agents dial out.
That is what makes bring-your-own-host and NAT'd boxes work without
firewall exceptions, and it is why the write endpoint must be reachable
from every host.

**Two proxies, by capability, not preference.** vmauth is the account
boundary for *data* (metrics and logs), because only it can force the
account into a query arg / header that a client cannot override — Traefik
cannot touch a query string. Grafana in the *browser* goes through
Traefik instead, because it needs websockets (Grafana Live) and vmauth
does not proxy them. Each does the one thing only it can.

**vmauth and Grafana are published; VictoriaMetrics and Loki are not.**
The two backends have no host port at all — anything able to reach them
directly would bypass the boundary, including another panel's container on
a shared host. Grafana having a bridge port is safe where theirs is not:
Grafana authenticates every request itself (OIDC); VM and Loki
authenticate nothing.

### The account boundary

The central can be shared: every panel given an account writes to and
reads from it. Neither VictoriaMetrics nor Loki has a notion of accounts, so
the boundary is imposed in front by **vmauth** — VictoriaMetrics' own
auth proxy, which forces the account per route from the authenticated
user:

| Path | Rule |
| --- | --- |
| `/w/api/v1/write` metrics write | the route forces `extra_label=ic_account=<user>`; a client's own copy is **dropped** for colliding |
| `/r/api/v1/{query,query_range,series,labels,label/<n>/values,…}` metrics read | the route forces `extra_filters[]={ic_account="<user>"}` the same way, so a client cannot widen the result |
| `/lw/loki/api/v1/push`, `/lr/loki/api/v1/{query,query_range,…}` logs | the `X-Scope-OrgID` header is **set** from the user, replacing whatever the sender supplied |

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
forced merges. The read list is what Grafana 11.2 and the panel were
measured calling (dashboards, Explore, both browsers, *Save & test*). A
Grafana upgrade that starts calling a new endpoint shows up as a 400 from
vmauth in that panel: add the endpoint to `_ACCOUNT_READ_PATHS`, never a
wildcard.

An agent runs on a machine its owner has root on. Its claim about who it
is can never be trusted, which is why the label comes from the
credential and not from the payload.

**Two credentials, and they are not interchangeable.** Each account has
one, scoped to itself, and it is written to that account's hosts. The
*operator* credential reads across every account (`/admin-r/`), deletes
series (`/admin-d/`) and is never written to any host. Grafana's own admin password stays on the
central: vmauth authenticates callers on `/gadmin/` with the operator
credential and swaps in the admin credential before proxying, so rotating
it is a redeploy rather than a change everywhere.

vmauth keeps its credentials in clear (it has no hash support). That is
mitigated the same way the agents' copy of the token is: the config lives
in the central's `0700` directory and every task that writes it is
`no_log`. The token already exists in clear on every agent and in each
Grafana datasource, so this adds no new exposure.

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
| Agents per host | ~50 MB RAM total |
| VictoriaMetrics | ~0.5–1 GB RAM; disk grows with series × retention |
| Grafana | ~300–500 MB RAM |

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
(`rate_limit_logs_per_min`). The central has no limit per account of its
own yet: a cap of concurrent queries per account in vmauth is planned.

Retention is set from Settings and applied on the next central deploy.
Shortening it does not delete existing data immediately — VictoriaMetrics
drops it as it ages out.

Logs have two retentions in Loki, both applied on the next central
deploy: **30 days** for the proxies' access logs (not longer, because the
lines carry the instances' visitors' addresses) and **90 days** for the
hosts' security logs (`log_type="security"`: SSH and sudo from the
journal, auditd, fail2ban), because an incident is often found weeks
after it began. Security logs are collected only where the panel turns on
*Send hosts' security logs* (`metrics_ship_security_logs`) and the agents
are reinstalled; a module that extends the panel may turn it on for its
own hosts. A host's
credential reaches only the push and read routes above — Loki's delete
API is not routed — so a host that is compromised cannot erase what it
already sent.

On each host the collector reads the proxy's access log from its file
(`/var/log/traefik/access.log`, rotated daily or past 100 MB, 30 days),
falling back to the proxy container's output on a host not set up since
the log moved there, and remembers its read positions on the host so a
restart does not send the file again.

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
   with the panel's public address (`/32`). Empty **Grafana base URL** if
   it pointed at the old central, so the deployment fills in the new one.
5. Re-run *Enable observability* against the new host. When the job
   answers, the panel's query URL, the agents' remote-write URL and (if
   it was empty) the Grafana base URL point at the new name.
6. Register Grafana as an OIDC client again: its redirect is the new
   Grafana URL.
7. Hosts enrolled before the move are re-applied on their own once the new
   central's host has applied the list and its direct name answers.
8. Remove `metrics-gateway.yml` from the old host's Traefik and
   decommission the old stack.

Historical series are not migrated by this procedure. If you need them,
copy the VictoriaMetrics data volume across before step 5 — otherwise
expect the dashboards to start from the moment of the switch.

What the deployment publishes under the name is what the hand-written
gateway publishes (next section), with one difference: Traefik drops
any `X-WEBAUTH-USER` header on its way to Grafana. Grafana's
`frame-ancestors` is the list `cloud.settings._grafana_frame_ancestors`
returns, written again at every deployment: the panel's origin, plus
whatever origins a module that extends the panel adds, such as other
panels that frame the dashboards. A list widened by
hand on the host would not survive the next run; widen it there instead.

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
operator's `/admin-d/` route, the way it swaps in Grafana's password, so
the panel never sends the key and no account can delete at all. Another
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

Agents and browsers that are not on the central's host reach it over
public HTTPS, terminated by the panel's existing Traefik. This is a
one-time edit to Traefik's dynamic config on the central's host — a
**separate file**, so a bad edit is rejected without taking the rest of
the proxy down. Its shape:

- `Host(metrics.<domain>)` and `/admin-r/`, `/admin-d/` or `/gadmin/` →
  **denied** from the public (an `ipWhitelist`). Every operator route
  vmauth serves must be on this list; a test pins it to what the code
  renders. Their only real callers — the panel
  and Grafana's datasource — reach vmauth over the bridge, never through
  Traefik.
- `Host(metrics.<domain>)` and `/grafana/` → Grafana's bridge port
  (websockets pass through; Grafana authenticates via OIDC). This router
  carries its own `frame-ancestors` when panels on other origins embed
  it, and the host's `frame_ancestors` field then stays empty: Traefik
  applies that one as an entrypoint default, and an entrypoint header
  overrides every router's (measured on Traefik v2.11).
- `Host(metrics.<domain>)` and `/w/ /r/ /lw/ /lr/` → vmauth's bridge
  port, which returns 401 without a credential. **Only those four
  prefixes**: vmauth answers its own `/flags`, `/metrics`,
  `/debug/pprof/` and `/-/reload` to anyone, and until 2026-09-27 a POST
  to them from outside got 200.

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

- **Per-instance HTTP is not available in v1.** Traefik reports per
  service, and the service name is a copier-time literal from each
  customer's own `prod.yaml`; across eight real doodba projects it bore
  no relation to `COMPOSE_PROJECT_NAME`, the key every other rule joins
  on. Guessing a mapping would attribute one customer's traffic to another,
  so the Traefik panels live on the **host** dashboard, grouped by
  service. Per-instance HTTP arrives with the blackbox probes of v2.
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
| `dashboards` | `collect` **and** a non-blank `grafana_base_url` | the panel's own settings |
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
- The **Monitoring page** states what is missing when `configure` is
  true, and states plainly that dashboards are unavailable when it is
  false. That branch is reachable only by typing the URL, and exists so
  that doing so is honest rather than misleading.
- The **instance Metrics tab** draws its own board (no embed) whenever
  `collect` is true: it reads the central with the panel's own account
  credential, so it needs no Grafana. With `collect` false it shows the
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
| Partner, observability off | ✗ | ✗ | ✓ | shown | "Enable observability in Settings" | prompt + readings + recent requests | shown |
| Partner, no Grafana URL | ✓ | ✗ | ✓ | shown | "Set the Grafana base URL" | board + readings + recent requests | shown |
| Partner, fully configured | ✓ | ✓ | ✓ | shown | dashboards | board + readings + recent requests | shown |
| Panel that pushes settings to others | ✓ | ✓ | ✓ | shown | dashboards | board + readings + recent requests | shown |
| Managed panel, granted, dashboards off | ✓ | ✗ | ✗ | **hidden** | (typed URL) "not available here" | board + readings + recent requests | hidden |
| Managed panel, granted, dashboards on | ✓ | ✓ | ✗ | shown | dashboards for their account | board + readings + recent requests | hidden |
| Managed panel, not granted yet | ✗ | ✗ | ✗ | hidden | (typed URL) "not available here" | no board; readings + recent requests | hidden |
