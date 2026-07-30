# Observability — operations guide

How the metrics stack is put together, what it costs, and how to move or
resize it. User-facing documentation lives in
`docs/user/docs/reference/monitoring.md`; this file is for whoever runs
the platform.

## Shape of the system

```
每 host                                   central (one)
┌──────────────────────────────┐          ┌────────────────────────┐
│ node_exporter  (system)      │          │ VictoriaMetrics        │
│ cAdvisor       (containers)  │          │   ← remote-write (push)│
│ Traefik        (HTTP, :8082) │          │ Grafana (provisioned)  │
│ vmagent  ── pushes ──────────┼─────────▶│                        │
└──────────────────────────────┘   HTTPS  └────────────────────────┘
                                                    ▲
                                          PromQL    │
                                     panel cron ────┘  → cloud.alert
```

**Push, not scrape.** Hosts open no inbound port; `vmagent` dials out.
That is what makes bring-your-own-host and NAT'd boxes work without
firewall exceptions, and it is why the remote-write endpoint must be
reachable from every host.

**Alerts do not live in the metrics stack.** There is no Alertmanager and
no vmalert. A panel cron (`cloud.metric.rule._cron_evaluate`, every 5
min) runs each rule's PromQL, compares against its threshold and
raises/resolves `cloud.alert` — the same multichannel pipeline every
other alert uses. One place to look, one notion of "alert".

## Deploying

1. **Central** — run the *Deploy Metrics Central* job against the host
   that should hold it. Co-locating on the panel host is the default:
   VictoriaMetrics + Grafana sit around 1–1.5 GB.
2. **Settings** — fill in the backend URL, the remote-write URL and its
   token, retention, and the Grafana base URL. Then switch
   **observability on**.
3. **Agents** — run *Install Observability* per host. It is idempotent;
   re-run it whenever a host's instance list changes so the labels stay
   accurate.

Everything is a job: no manual SSH, no hand-edited config.

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
2. Run *Deploy Metrics Central* against it.
3. Point **Metrics backend URL**, **Remote-write URL** and **Grafana base
   URL** at the new box.
4. Re-run *Install Observability* on each host so the agents push to the
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

| Setting | Used by | Must resolve from |
|---|---|---|
| **Metrics backend URL** | the panel (PromQL queries) | inside the **panel** container |
| **Remote-write URL** | `vmagent` on every host | inside the **agent** container, on each host |

`127.0.0.1` is almost never correct for either: a container's loopback is
its own, not the host's. The central therefore binds to the **Docker
bridge gateway** (`172.17.0.1` by default, discovered from facts), which
is reachable by containers on that host and not routable from outside it.

- Agents **on the central's own host**: `http://host.docker.internal:8428/api/v1/write`
  (the agent compose adds the `host-gateway` mapping).
- Agents **on any other host**: the central's public HTTPS URL, published
  through the panel's proxy.

Symptom of getting it wrong: the backend is healthy and `curl` works from
the host shell, while `docker logs …vmagent` repeats `connection
refused`. Buffered samples are not lost — vmagent retries and backfills
once the URL is right.

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

Done **conditionally**, not by deletion — and only for what is actually
covered:

| SSH job | Status | Why |
|---|---|---|
| `host_metrics` | **Retired while metrics flow** | node_exporter supplies cores, RAM and disk; the disk-critical alert is now a `cloud.metric.rule`. The job self-skips when `last_probed` is fresher than 15 min and resumes automatically if the metrics stack is off or stalls. |
| `instance_health` | **Kept, but no longer the liveness source** | `running` now comes from cAdvisor (verified live in both directions: containers up → True, stopped → False after the freshness window). The job stays because it *also* does HTTP health probing and error-log scraping, which need blackbox and Loki (observability v2). Retiring it wholesale would be a capability regression, not a cleanup. |

The conditional form is deliberate: there is never a window where nobody
is collecting, and no flag day.

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
  first, then re-run *Install Observability* on every host**; in between,
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
