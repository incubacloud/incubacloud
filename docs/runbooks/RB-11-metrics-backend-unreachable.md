# RB-11 — The metrics backend is unreachable

**Trigger:** a critical alert with code `metrics_backend_unreachable`, or
the Monitoring dashboards showing no recent data.

**What it means:** the panel's rule cron could not query VictoriaMetrics.
While this lasts, **metric-based alerts are not being evaluated**.

!!! warning "Read this first"
    Standing alerts are deliberately **not** resolved while the backend is
    down. What you see on screen is the last known state, which may be
    stale. Conversely, a quiet panel does **not** mean the fleet is
    healthy — it means nobody is looking. Treat this alert as "monitoring
    is blind", not as "monitoring is broken but harmless".

## 1. Confirm the scope

```bash
# From the central host. The gateway is the only published port; nothing
# else in the stack has one, so this is the path to test.
curl -sS -u operator:<operator credential> \
  http://172.17.0.1:8428/admin-r/health

# Is the stack even running?
docker compose -p incubacloud-central ps
```

The operator credential is in **Settings → Monitoring → Advanced** on the
panel that owns the central. It is not the account credential the hosts
carry, and it is deliberately not on any host.

- `health` answers and containers are up → the panel cannot *reach* it:
  jump to step 3.
- Containers are down or restarting → step 2.

## 2. The central is down

```bash
docker compose -p incubacloud-central logs --tail 100 victoriametrics
docker compose -p incubacloud-central up -d
```

Common causes, in the order they actually happen:

- **Disk full on the central host.** VictoriaMetrics stops writing and may
  refuse to start. Free space, then start it. Check retention afterwards
  (`Settings → Metrics retention`) — this is the usual root cause of a
  central that fills up.
- **The host was rebooted** and the stack did not come back: the compose
  services use `restart: unless-stopped`, so a container stopped by hand
  stays stopped. `up -d` is enough.
- **The image was pruned.** A `docker system prune -af` on the central
  host removes unused images; re-running the deployment pulls them again.

If in doubt, press **Enable observability** in Settings again. It is
idempotent, rebuilds the stack from the declared state, and re-applies
the account list and Grafana organisations along the way.

## 3. The central is up but the panel cannot reach it

- Check **Settings → Advanced → Query URL**. Co-located it should be the
  docker bridge address ending in `/r/` (e.g.
  `http://172.17.0.1:8428/r/`). Two mistakes are common: a loopback
  address, which is the panel container's own namespace and not the
  host's; and a URL pointing straight at VictoriaMetrics, which no longer
  has a published port — every path goes through the gateway.
- A **401** means the panel's account credential and the central's
  account list disagree. Re-deploy the central: the list is rebuilt from
  the panel's own state, so that reconciles them.
- A **400** on a query that used to work means something is sending
  `extra_filters`/`extra_label`. The gateway rejects those by design —
  they are how the account filter would be widened.
- From the panel container, not the host:
  ```bash
  docker compose exec odoo curl -sS <metrics backend URL>/health
  ```
  A loopback URL that works on the host but not in the container means
  the URL needs to be reachable from the container's network namespace.

## 4. Hosts stopped pushing (dashboards empty, backend healthy)

The backend being reachable does not mean data is arriving.

```bash
# On an affected host:
docker compose -p incubacloud-observability ps
docker compose -p incubacloud-observability logs --tail 50 vmagent
```

- `vmagent` reports auth failures → the account credential on the host
  and the central's list disagree. Re-deploy the central, then let the
  reconciliation cron re-apply the agents (≤15 min), or wait for both.
- `vmagent` cannot resolve or reach the endpoint → check the write URL
  and that the host has outbound HTTPS. Remember the local bridge address
  only works for agents on the central's own host; every other host needs
  the public endpoint.
- Agents are missing entirely → check the host page. It states whether
  they are installed, never installed, or failing. There is no button to
  press: the reconciliation cron retries with a growing back-off, and
  raises an alert once failures persist. If it is not even trying, the
  host is not a target — most often it has no stored SSH host key, which
  means the panel never reached it.

Note that `vmagent` buffers to disk while the central is unavailable, so
a short outage backfills on its own once connectivity returns.

## 5. Close out

Once the query succeeds, the next cron tick (≤5 min) clears the
`metrics_backend_unreachable` alert automatically. Do not dismiss it by
hand — if it clears itself you know the fix actually worked.

Then confirm the fleet is genuinely healthy rather than merely quiet: the
alerts that were frozen during the outage are re-evaluated on that same
tick, so anything that broke while you were blind surfaces now.

## Related

- `docs/observability-operations.md` — architecture, sizing, moving the central
- RB-09 — Docker prune failures (a frequent cause of missing images)
