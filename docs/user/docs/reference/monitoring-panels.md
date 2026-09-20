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

| Card | What it shows | Healthy |
| --- | --- | --- |
| **Hosts reporting** | How many hosts sent a sample in the last five minutes | The number of hosts you manage |
| **Instances observed** | How many instances are currently reporting containers | Your instance count, minus any that are asleep |
| **Hosts above 90% disk** | Count of hosts whose root disk is nearly full | `0` |
| **Hosts above 92% memory** | Count of hosts nearly out of memory | `0` |

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

Open an instance and go to **Metrics**. The two charts are the same CPU and
memory cards above, already pinned to this instance and its host — nothing to
select.

**Recent requests.** Below the charts, read live from the host's proxy each time
you press *Refresh*, and never stored. Three summaries — **Status codes**, **Top
clients**, **Top paths** — over the last hundred requests, then the requests
themselves with time, client address, method, path and status.

This is the half the charts cannot give you: metrics say an instance is busy or
failing, only this says *what is being done to it*. During an incident, read it
in this order:

1. **Status codes** — a wall of `5xx` is the instance failing, not the load. A
   burst of server errors is alerted.
2. **Top clients** — one address dominating is a scraper or a runaway
   integration, not your users.
3. **Top paths** — the endpoint under pressure, which is usually enough to name
   the cause.

If the card says no requests were recorded and you expect traffic, the host's
proxy has not been redeployed since access logging was switched on.

## What you will not find here

Per-instance HTTP traffic — it lives in the host's charts, grouped by service,
for the reason above — and database metrics such as connections, cache hit ratio
and locks. Both are designed and deferred.
