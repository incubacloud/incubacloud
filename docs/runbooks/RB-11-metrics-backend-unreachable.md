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
# From the panel host (adjust to your Metrics backend URL):
curl -sS http://127.0.0.1:8428/health

# Is the stack even running?
docker compose -p incubacloud-central ps
```

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
  host removes unused images; re-running *Deploy Metrics Central* pulls
  them again.

If in doubt, just re-run the **Deploy Metrics Central** job. It is
idempotent and rebuilds the stack from the declared state.

## 3. The central is up but the panel cannot reach it

- Check **Settings → Metrics backend URL**. When co-located it should be
  the loopback address (`http://127.0.0.1:8428`); a public URL that
  resolves elsewhere is a common mistake after moving the central.
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

- `vmagent` reports auth failures → the remote-write token changed.
  Re-run **Install Observability** on the affected hosts.
- `vmagent` cannot resolve or reach the endpoint → check the
  remote-write URL and that the host has outbound HTTPS.
- Agents are missing entirely → the host was never set up; run
  **Install Observability**.

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
