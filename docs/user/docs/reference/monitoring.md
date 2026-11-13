# Monitoring

Every host you manage reports its own health and that of the instances on
it. There is nothing to install: once observability is switched on, hosts
enrol themselves, and any host that fails to is retried automatically
until it succeeds.

## What is measured

**Per host** — load, CPU, memory and swap, how long its tasks waited for
CPU, memory or the disk, its disks' activity and throughput, network
traffic, each instance's share of all of it, and whether the host is
reporting at all.

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

**One server** → open it under **Hosts** and go to its **Metrics** tab:
what is short, which instance uses it, its disks, network and requests.

**The whole fleet** → the **Monitoring** entry in the sidebar, next to
Hosts: **Fleet** for every server at once, **Hosts** for one server's board.
It appears once observability is enabled and has a central.

For what each individual chart means, what a healthy one looks like and what to
check when it does not, see [Monitoring panels](monitoring-panels.md).

**Is a host being monitored?** → its page states it plainly: reporting,
agents installed but no data yet, install failed and retrying, or not
enrolled yet.

Both views need the `developer` role or above.

## Alerts

Rules are data, not code: thresholds can be tuned without a release.
Shipped rules cover a host that stopped reporting, a disk almost full,
memory nearly exhausted, sustained CPU saturation, a host whose disk or
memory makes its tasks wait, an instance that stopped reporting
containers, an instance returning server errors, an instance held back by
its CPU quota, and a process of an instance's Odoo, database or mail
container killed for lack of memory. A host out of memory and a killed
process are critical.

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
stack. Settings → Monitoring, pick the host under *Where the central
runs*, press *Enable observability*. The endpoints and credentials are
filled in for you.

### Where to run the central

The central is always installed on a server the panel manages: added to
the panel, with Full Setup run on it. **We recommend a server of its
own**: it can be hardened without touching anything else, and it does not
compete with Odoo for resources. A server shared with Odoo instances the
panel manages also works. The server where the panel itself runs is not
supported.

Add the server to the panel like any other, run Full Setup on it, then
enable observability against it. The central installs no proxy of its
own: when you publish it under a public name, it uses the one that Full
Setup leaves on every managed server, so a server that has not been
through Full Setup refuses the job and says so.

When you pick a server, a note under it says whether the central fits
there: free disk for your number of servers and retention, memory and
cores, from what the server last measured. It is advice; nothing is
refused for it.

### The central's server keeps your history

The server running the central holds the metrics history and the
security logs, so the panel will not delete or archive it, and *Enable
observability* on another server is refused while the central runs.
Under *Where the central runs* you have two ways out, each asking you to
confirm:

- **Start an empty central** on another server: the panel moves to a new,
  empty central. The old one stays on its server with the history,
  unread; you can then delete that server.
- **Retire the central (the history is lost)**: for a central whose
  server is gone, or when your only server is going. The panel forgets
  it and observability is switched off; enable it again on another
  server whenever you want.

To keep the history while changing servers, see *Where the central runs*
in the [operations guide](https://github.com/incubacloud/incubacloud/blob/19.0/docs/observability-operations.md#where-the-central-runs).

### Hosted on Odoo.sh

Odoo.sh does not let you run the central, so a panel installed there
needs a metrics server of its own: a separate server, registered and
prepared as described above, chosen under *Where the central runs*. The
same holds for any self-hosted panel that cannot run the central beside
it. On IncubaCloud's SaaS the central is included for your tenants.

If you have hosts other than the one running the central, they need a
public HTTPS address to push to; set it under *Advanced*. The local
address the button fills in is only reachable from the central's own
host. Or give the central a direct name and its host a direct port: your
hosts then push to it without a CDN in between, on a port only they may open.

## What is not here

Database figures per instance over time (connections, cache hit ratio,
locks) are not charted or alerted: **Queries now** on the Metrics tab reads
them when you ask. There are no synthetic probes from outside.
