# Monitoring

Every host you manage reports its own health and that of the instances on
it. There is nothing to install: once observability is switched on, hosts
enrol themselves, and any host that fails to is retried automatically
until it succeeds.

## What is measured

**Per host** — CPU, memory, disk usage and free space, load, and whether
the host is reporting at all.

**Per instance** — CPU and memory of each container, disk used by the
instance's directory, and its HTTP traffic: requests per second, status
codes and latency.

**Per request** — the proxy also records who asked for what. That is what
the *Recent requests* panel on an instance shows: client IP, method, path
and response code. Metrics tell you an instance is under load; only these
tell you what is being done to it.

## Where to look

**One instance in trouble** → open it and go to the **Metrics** tab. This
is the right place during an incident: charts for that instance, and
underneath, the requests hitting it right now, with a breakdown by status
code and the client addresses sending the most.

**The whole fleet** → the **Monitoring** entry in the sidebar, next to
Hosts. It appears only when observability is enabled.

**Is a host being monitored?** → its page states it plainly: reporting,
agents installed but no data yet, install failed and retrying, or not
enrolled yet.

Both views need the `developer` role or above.

## Alerts

Rules are data, not code: thresholds can be tuned without a release.
Shipped rules cover a host that stopped reporting, a disk almost full,
memory nearly exhausted, sustained CPU saturation, an instance that
stopped reporting containers, and an instance returning server errors.

Alerts arrive through the same channels as everything else — in-app,
email, Telegram or webhook, as configured.

An instance whose plan puts it to sleep on idle does **not** raise a
"stopped reporting" alert. Sleeping stops its containers on purpose, and
alerting on it every night would train everyone to ignore the alert that
matters.

## What you need for it to work

**On IncubaCloud's SaaS** — nothing. Metrics are configured for you when
your panel is created, and hosts you add later are enrolled on their own.

**Self-hosted** — one decision: which of your hosts runs the central
stack. Settings → Monitoring, pick the host, press *Enable observability*.
The endpoints and credentials are filled in for you.

If you have hosts other than the one running the central, they need a
public HTTPS address to push to; set it under *Advanced*. The local
address the button fills in is only reachable from the central's own
host.

## What is not here yet

Database metrics per instance (connections, cache hit ratio, locks) and
synthetic probes from outside. Both are designed and deferred; nothing
already collected changes when they land.
