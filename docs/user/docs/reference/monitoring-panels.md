# Monitoring panels

What each chart shows, what a healthy one looks like, and what to do when it
looks wrong. For what is measured and how to switch it on, see
[Monitoring](monitoring.md).

Charts refresh every minute and open on the last 6 hours. Use the time picker to
change that: `1h` while watching a deploy, `7d` to tell a spike from a trend.

!!! tip "Alerts do the watching for you"
    Where a card says *this is alerted*, you need not keep the page open: the
    rule is checked every five minutes and reaches you through your
    [notification channels](notifications.md). Cards without that note are
    yours to read.

## Fleet

Open **Monitoring** in the sidebar. This tab answers "is anything wrong
anywhere?" and needs no selection — every host you manage is on it.

The top row reads left to right: three cards about your hosts, then two
about your instances.

| Card | What it shows | Healthy |
| --- | --- | --- |
| **Hosts reporting** | How many hosts sent a sample in the last five minutes | The number of hosts you manage |
| **Hosts above 90% disk** | Count of hosts whose root disk is nearly full | `0` |
| **Hosts above 92% memory** | Count of hosts nearly out of memory | `0` |
| **Instances deployed** | How many instances are installed on your hosts, awake or asleep | Your instance count |
| **Instances running** | How many instances have Odoo up right now | Your instance count, minus any that are asleep |

**Load per core, by host.** The 15-minute load average divided by the number of
cores, top ten hosts. A ratio, not a percentage: `1` means the host is using
exactly the CPU it has. Below `1` is comfortable, sustained above `2` is a host
that cannot keep up — *this is alerted*. Brief peaks during a deploy or a backup
are normal.

**Root filesystem used %** and **Memory used %, by host.** Top ten hosts, as
percentages. Disk is the one to act on early: it does not recover on its own,
and a full disk stops an instance dead. Alerted at 90% and 92% respectively.

**Ingest age per host.** One lane per host, green while it is reporting and red
once it has been silent for more than five minutes. Unbroken green is what you
want; a lane turning red means the machine is down, or just its agent. *This is
alerted.*

??? note "Why a lane per host and not lines"
    Every agent samples on the same clock tick, so on a shared axis the hosts
    drew identical lines on top of each other and all but one were invisible.
    Lanes cannot hide each other, and the colour change sits on the alert's
    own threshold — chart and alert can never disagree.

## Hosts

One host at a time. **Pick it with the Host selector** above the charts — it
starts on your first host, so check the selector before reading the numbers.

**CPU load per core.** The same ratio as the fleet card, for this host alone.
Around or below `1` is healthy; sustained above `2` is alerted.

**Memory used %** and **Root filesystem used %.** This host's totals. Alerted at
92% and 90% respectively.

**Disk used per instance.** Disk taken by each instance on this host, in bytes.
When the disk percentage climbs, this is the card that names the culprit —
usually a database that has grown or backups piling up locally, see
[Backups](../backups/index.md). There is no per-instance alert (a byte
threshold means nothing across instances of very different sizes); the host
disk alert is what warns you.

**HTTP requests by Traefik service** (requests per second) and **Request
duration p95 by Traefik service** (seconds). Traffic through this host's proxy,
grouped by service. p95 means 95% of requests finished faster than the line
shows; watch its shape, not its absolute value, since a heavy report is
legitimately slow. A p95 climbing while requests stay flat is the interesting
one: the same load is getting harder to serve.

!!! info "Why by service and not by instance"
    The proxy reports per service, and service names come from each
    deployment's own configuration, so they cannot be attributed back to a
    single instance. For one instance's traffic, use its **Metrics** tab
    instead.

## Instances

One instance at a time, with a **Host** selector and an **Instance** selector.
Set the host first: container names repeat across machines, and without the host
two instances with the same container name would be added together.

**CPU per container.** One line per container of the instance, in cores. A
container at `1.0` is using a full core. The Odoo container doing the work while
the others idle is the normal shape.

**Memory per container.** Working set per container, in bytes. A slow, permanent
climb in the Odoo container between restarts is worth investigating; a database
container that settles on a plateau is doing its job.

Neither card is alerted on its own. What is alerted is the instance going quiet
altogether: no container reporting for five minutes.

!!! info "IncubaCloud SaaS"
    An instance on a plan that sleeps when idle stops its containers on
    purpose. It disappears from these charts while asleep and does **not**
    raise an alert for it.

## An instance's Metrics tab

Open an instance and go to **Metrics**. The tab is a diagnosis of that
instance, already pinned to it and to its host: nothing to select. It reads
top to bottom as the questions you ask when an instance is slow.

### What is happening now?

A card with a coloured edge — green, amber or red — and a few sentences about
the last 15 minutes: which resource is short and what to do about it. The
sentences come from fixed rules over the charts below, with these thresholds:

| The card says | When |
| --- | --- |
| Odoo is held back by its CPU quota | Odoo was refused CPU by its quota more than 25% of the time |
| The server is saturated and most of it goes to another instance | The server's load is above 2 per core and another instance uses more than half of the server's CPU |
| Memory is short | The server's tasks waited for memory more than 10% of the time, or the kernel killed a process of this instance for lack of memory, in the last 15 minutes |
| The server's disk is the bottleneck | The server's tasks waited for the disk more than 20% of the time |
| Requests take long without using CPU or the database | The proxy's p95 is above 2 seconds while Odoo and the database each use less than 30% of a core: usually a call to an outside service |
| Odoo restarted N minutes ago | Odoo's container started in the last 15 minutes |
| No resource looks saturated | None of the above |

Zoomed into the past, the card does not judge: it only reads the last 15
minutes up to now. The buttons under it take you to **Queries now**, **Slow
routes** and **Recent requests**.

### Since when?

**Request time** (p50 and p95) and **Requests** (per second, with server errors
in red), measured at the proxy for this instance alone. Choose `1h`, `6h`, `24h`
or `7d` (it opens on `1h`); drag across any chart, with the mouse or a finger moving sideways, to
zoom every chart into that window, and *Reset zoom* to go back. `1h` and `6h`
refresh every 45 seconds while the tab is open.

Two marks run over the charts: a blue dot (●) where a container restarted and a
red triangle (▲) where the kernel killed one of its processes for lack of
memory.

### Which resource?

**CPU per container.** One line per container, `100%` being one core; the
dashed line of the same colour is the container's CPU quota (*Resources*). Odoo
doing the work while the others idle is the normal shape.

**Held back by the CPU quota.** The share of time each container wanted CPU and
its quota said no. A few percent is normal; Odoo above 25% for long means its
requests queue behind the quota.

**Memory per container.** Working set per container, with its limit dashed. A
slow, permanent climb in the Odoo container between restarts is worth
investigating; a database that settles on a plateau is doing its job. On the
7-day range each point is the highest value of its interval, so short peaks
still show.

**Server: waiting for resources.** The share of time some task of the whole
server waited for CPU, memory or the disk (the kernel's pressure figures).
Near zero is healthy. It tells a server that is short of something apart from
an instance that is merely busy.

**Neighbours.** CPU of every instance on the same server, this one in green.
When the server is saturated, this card says whether this instance is the cause
or another one is.

### Which process or query?

A line with the Odoo workers configured and an estimate of how busy they are
(Odoo's CPU over the last 15 minutes against one core per worker). Then two
readings taken from the host only when you press their button; one runs at a
time, and nothing of them is stored.

**Queries now** reads the instance's database: how many connections it has
against its maximum, how many are active, waiting on a lock or idle inside a
transaction, the longest of each, the cache hit ratio and the size. Then its
sessions — state, what they wait for, for how long, which program, who blocks
whom (a blocked row is tinted) and the start of their query — and the ten
tables with most dead rows with their last vacuum. A session *idle in a
transaction* that blocks others is the usual culprit behind a stuck import or
cron. Query texts can contain your users' data, which is why they are read on
demand and only by the `developer` role or above.

**Slow routes** reads Odoo's own log for the last 15 or 60 minutes: the ten
routes that took the most time in total, with their count, mean, p95, the share
spent in SQL and in Python and the mean number of queries; the scheduled
actions with their longest and total run, and those still running; the requests
stopped by a time limit; and how many workers were recycled for memory. Where a
stopped request was depends on how Odoo runs: without workers, Odoo logs its
stack, and the reading shows the URL it served and its innermost calls; with
workers, Odoo kills the worker without logging where it was, so there is only
the time and the limit. A route with a high SQL share points at the database;
a high Python share at the code. Odoo before 11.0 logs no timing: its requests
are counted, not timed.

### Size

The size of the instance's directory on the server, measured every 10 minutes.

### Recent requests

Below the board, read live from the host's proxy each time you press *Refresh*,
and never stored. Five summaries over the requests read — **Status codes**,
**Top clients**, **Top paths**, **Login attempts** (posts to the login page by
client address) and **Scanners** (addresses asking for other software's files,
such as WordPress pages, `.env` or `.git`, and getting a 404) — a bar of
requests per minute, with the minutes that had server errors in red, then the
requests themselves with time, client address, method, path and status.

This is the half the charts cannot give you: they say an instance is busy or
failing, only this says *what is being done to it*. During an incident, read it
in this order:

1. **Status codes** — a wall of `5xx` is the instance failing, not the load. A
   burst of server errors is alerted.
2. **Top clients** and **Login attempts** — one address dominating is a scraper,
   a runaway integration or somebody trying passwords, not your users.
3. **Top paths** — the endpoint under pressure, which is usually enough to name
   the cause.

If the card says no requests were recorded and you expect traffic, the host's
proxy has not been redeployed since access logging was switched on.

## What you will not find here

Database figures over time (connections, locks, cache hit ratio as charts and
alerts): today they are read on demand with **Queries now**.
