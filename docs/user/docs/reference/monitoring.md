# Monitoring

Every host you manage reports its own health and that of the instances on
it. There is nothing to install: once observability is switched on, hosts
enrol themselves, and any host that fails to is retried automatically
until it succeeds.

## What is measured

**Per host** — CPU, memory, disk usage and free space, load, and whether
the host is reporting at all.

**Per instance** — CPU and memory of each container with their limits,
how often the CPU quota held it back, restarts and processes killed for
lack of memory, disk used by the instance's directory, and its HTTP
traffic: requests per second, server errors and latency.

**On demand** — when you ask, from the instance's **Metrics** tab: what its
database is doing (sessions, locks, connections) and where Odoo's time went
(slowest routes, scheduled actions, workers stopped by a limit), read from
the host and never stored.

**Per request** — the proxy also records who asked for what. That is what
the *Recent requests* panel on an instance shows: client IP, method, path
and response code. Metrics tell you an instance is under load; only these
tell you what is being done to it.

## Where to look

**One instance in trouble** → open it and go to the **Metrics** tab. This
is the right place during an incident: it says in plain words which
resource is short, shows since when, compares the instance with its
neighbours on the same server, reads on demand what its database and its
Odoo log are doing, and underneath lists the requests hitting it right now.

**The whole fleet** → the **Monitoring** entry in the sidebar, next to
Hosts. It appears only when observability is enabled.

For what each individual chart means, what a healthy one looks like and what to
check when it does not, see [Monitoring panels](monitoring-panels.md).

**Is a host being monitored?** → its page states it plainly: reporting,
agents installed but no data yet, install failed and retrying, or not
enrolled yet.

Both views need the `developer` role or above.

## Alerts

Rules are data, not code: thresholds can be tuned without a release.
Shipped rules cover a host that stopped reporting, a disk almost full,
memory nearly exhausted, sustained CPU saturation, an instance that
stopped reporting containers, an instance returning server errors, and
a process of an instance's Odoo, database or mail container killed for
lack of memory. The last one is critical.

Each instance is also checked every five minutes: its Odoo above 95 %
of its memory limit for thirty minutes raises a warning, and so do more
than twelve Odoo workers recycled for memory in an hour. See [Memory and resources](../instances/resources.md)
for what the panel sets so that Odoo stays below its limit.

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
host. Or give the central a direct name and its host a direct port: your
hosts then push to it without a CDN in between, on a port only they may open.

## What is not here

Database figures per instance over time (connections, cache hit ratio,
locks) are not charted or alerted: **Queries now** on the Metrics tab reads
them when you ask. There are no synthetic probes from outside.
