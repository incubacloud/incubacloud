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

### Adding or removing a tenant (SaaS)

vmauth's user list **is** the access-control list: a user in it may write
series labelled with itself and read those same series, and nothing else.
It is rebuilt from the tenant list every time the central is deployed, so
re-running that job is how a new tenant gains access and how a departed
one loses it. Their Grafana organisation and scoped datasource are
created in the same run.

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
│ Traefik        (HTTP, :8082) │   │    /admin-r/  /gadmin/           │
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
directly would bypass the boundary, including a tenant's own container on
a shared host. Grafana having a bridge port is safe where theirs is not:
Grafana authenticates every request itself (OIDC); VM and Loki
authenticate nothing.

### The account boundary

The central is shared: in SaaS every tenant panel writes to and reads
from it. Neither VictoriaMetrics nor Loki has a notion of accounts, so
the boundary is imposed in front by **vmauth** — VictoriaMetrics' own
auth proxy, which forces the account per route from the authenticated
user:

| Path | Rule |
| --- | --- |
| `/w/` metrics write | the route forces `extra_label=ic_account=<user>`; a client's own copy is **dropped** for colliding |
| `/r/` metrics read | the route forces `extra_filters[]={ic_account="<user>"}` the same way, so a client cannot widen the result |
| `/lw/` `/lr/` logs | the `X-Scope-OrgID` header is **set** from the user, replacing whatever the sender supplied |

None of that is caution. Measured against real VictoriaMetrics + Loki
(2026-08-10): vmauth drops a client's query arg that collides with the
one the route forces, so the account filter cannot be widened — and it
needs no hand-written reject rules that would rot when VictoriaMetrics
changes a query arg's meaning. A credential-less request is 401 because
the config declares no `unauthorized_user`.

An agent runs on a machine its owner has root on. Its claim about who it
is can never be trusted, which is why the label comes from the
credential and not from the payload.

**Two credentials, and they are not interchangeable.** Each account has
one, scoped to itself, and it is written to that account's hosts. The
*operator* credential reads across every account (`/admin-r/`) and is
never written to any host. Grafana's own admin password stays on the
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

Retention is set from Settings and applied on the next central deploy.
Shortening it does not delete existing data immediately — VictoriaMetrics
drops it as it ages out.

## Moving the central to its own VPS

The destination is a parameter, not a hard-coded host:

1. Add the new server as a host and prepare it as usual.
2. Re-run *Enable observability* in Settings against the new host.
3. Point **Metrics backend URL**, **Remote-write URL** and **Grafana base
   URL** at the new box.
4. The reconciliation cron re-applies the agents on each host so they push to the
   new endpoint.
5. Decommission the old stack.

Historical series are not migrated by this procedure. If you need them,
copy the VictoriaMetrics data volume across before step 3 — otherwise
expect the dashboards to start from the moment of the switch.

## Labels

Every sample carries `host`, `host_id`, and — for container metrics —
`instance`, `instance_id`, `project` (plus `tenant` in the SaaS build).
This is not cosmetic: labels cannot be added to series that were already
written, so anything not labelled at ingest is unattributable forever.
The panel generates the label map (only it knows which compose project
belongs to which instance) and passes it to the agent playbook.

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
the proxy down. A reference copy lives beside this guide; the shape is:

- `Host(metrics.<domain>)` and `/admin-r/` or `/gadmin/` → **denied**
  from the public (an `ipWhitelist`). Their only real callers — the panel
  and Grafana's datasource — reach vmauth over the bridge, never through
  Traefik.
- `Host(metrics.<domain>)` and `/grafana/` → Grafana's bridge port
  (websockets pass through; Grafana authenticates via OIDC).
- `Host(metrics.<domain>)` (everything else: `/w/ /r/ /lw/ /lr/`) →
  vmauth's bridge port, which returns 401 without a credential.

Then set the tenant-facing URLs in Settings to
`https://metrics.<domain>/w/api/v1/write` and
`https://metrics.<domain>/r`.

If the metrics hostname is proxied by Cloudflare (orange-cloud), a bot
challenge on the ingest path can silently drop `vmagent`. If ingestion
misbehaves, make the record a grey-cloud (DNS-only) A record straight to
the host.

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
takes the flag back by itself the moment the readings go stale. Without
that arbitration the two wrote on their own schedules and the state
flapped whenever they briefly disagreed.

`instance_health` cannot retire until synthetic probes cover its HTTP
check. Its error-log scraping now has a better source in the access logs.

## Gotchas

- **Traefik metrics need the config, and reach vmagent over docker.**
  Traefik exposes them on its `metrics` entrypoint (`:8082`). vmagent
  scrapes it **by service name over Traefik's own network**
  (`inverseproxy_shared`), not through a published host port: publishing
  on `127.0.0.1` is unreachable from a container, and publishing on
  `0.0.0.0` would expose instance names and domains on any host that is
  not hardened. The scrape job is only generated when that network
  exists, so a host without Traefik does not sit at `up=0` forever.
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
  on. Guessing a mapping would attribute one tenant's traffic to another,
  so the Traefik panels live on the **host** dashboard, grouped by
  service. Per-instance HTTP arrives with the blackbox probes of v2.
- **`running` comes from cAdvisor**, not from HTTP traffic. An idle but
  healthy instance must keep reading as running; sourcing it from traffic
  would eventually feed the 14-day auto-suspend with false idleness.
