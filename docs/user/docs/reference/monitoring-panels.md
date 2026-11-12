# Monitoring panels

What each chart shows, what a healthy one looks like, and what to do when it
looks wrong. For what is measured and how to switch it on, see
[Monitoring](monitoring.md).

The panel draws every chart itself from the metrics central. A board opens on
the last hour and refreshes every 45 seconds; pick `6 h`, `24 h` or `7 d`
above it to tell a spike from a trend, or drag across any chart to zoom all of
them into that window (**Reset zoom** brings the range back).

!!! tip "Alerts do the watching for you"
    Where a card says *this is alerted*, you need not keep the page open: the
    rule is checked every five minutes and reaches you through your
    [notification channels](notifications.md). Cards without that note are
    yours to read.

## Fleet

Open **Monitoring** in the sidebar; **Fleet** is the first view. It answers "is
anything wrong anywhere?" and needs no selection. On the panel that runs the
metrics central it shows every account's servers (*Every account on the
central*, the account in brackets after a server of another one); on any other
panel, yours (*Your servers*).

The figures at the top:

| Figure | What it shows | Healthy |
| --- | --- | --- |
| **Servers reporting** | Servers that sent a sample in the last five minutes | The number of servers you manage |
| **Root disk over 90 %** | Servers whose root disk is nearly full | `0` |
| **Memory over 92 %** | Servers nearly out of memory | `0` |
| **Waiting over 20 % of the time** | Servers whose tasks waited for CPU, memory or disk more than a fifth of the time | `0` |
| **Instances deployed** | Instances installed on your servers, awake or asleep | Your instance count |
| **Instances running** | Instances with Odoo up right now | Your instance count, minus any asleep |
| **Active alerts** and **Active security alerts** | This panel's open alerts | `0` |

**Busiest servers.** Four charts, the ten servers highest on each: **Load per
core** (the 15-minute load average over the cores: below `1` is comfortable,
sustained above `2` is a server that cannot keep up — *this is alerted*),
**Memory in use** and **Root disk in use** (alerted at 92 % and 90 %), and
**Disk busy** (the share of the time the server's busiest disk had work).

**Are the servers reporting?** One lane per server, green for each step of
the window with a sample and red where there was none. A lane turning red
means the machine is down, or just its agent — *this is alerted* after five
minutes. Servers with a silence come first; your own open their board.

## Hosts

In **Monitoring**, **Hosts** shows one server's board: pick it with the
**Host** selector, which starts on your first server. The same board is the
**Metrics** tab of a server's page (**Hosts**, then the server).

### What is happening?

One card with the figures of the server (cores, memory, root disk) and a
verdict on its last 15 minutes, with the instance to look at when one is to
blame:

| It says | Because | What to do |
| --- | --- | --- |
| Memory is at N %, and which instance uses the most | Memory in use above 92 % | Lower that instance's workers or memory, or move an instance to another server |
| Tasks wait for memory | The kernel reports waits for memory over 10 % of the time | Move an instance or give the server more memory |
| The root disk is N % full, and the largest instance directory | Root disk above 90 % | Free space or grow the disk: a full disk stops every instance |
| The server is saturated, and which instance uses the most CPU | Load per core above 2 | See that instance's own **Metrics** tab |
| The disk is the bottleneck | The kernel reports waits for the disk over 20 % of the time | Wait for a backup or a migration to end; otherwise the server needs a faster disk |
| Normal | None of the above | — |

Under it, how long ago the server's agents sent their last sample, in amber
past five minutes.

### Load and memory

**Load per core** over the last minute and the last 15 minutes. **Waiting for
resources**: the share of the time some task of the server waited for CPU,
memory or the disk, as the kernel counts it; this is what makes instances slow
even when no percentage looks full. **Memory in use**, and the share of the
swap in use on a server that has one.

### Which instance uses it?

**CPU per instance** and **Memory per instance**, stacked so the top line is
the server's total; past nine instances the rest add up as *others*. **Right
now** lists every instance of the server over the last five minutes, busiest
first: CPU (100 % is one core), memory, the size of its directory, requests per
second and whether it runs. An instance you can open links to its page.

### Disk

**Root disk in use** (alerted at 90 %), **Disk busy** per disk, **Disk
throughput** read and written, and **Directory per instance**, measured every
ten minutes. When the root disk climbs, the last one names the culprit — usually
a database that has grown or backups kept locally, see
[Backups](../backups/index.md).

### Network and requests

**Network** received and sent by the server's own link, **Requests per
instance** at the server's proxy, and **Request time per instance** (p95: 95 %
of requests finished faster than the line). A p95 climbing while requests stay
flat is the interesting one: the same load is getting harder to serve.

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
