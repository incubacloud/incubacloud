# Monitoring

The **Monitoring** section shows what your servers and instances are
actually doing: how loaded they are, how much disk they have left, and
how your instances are responding to traffic.

It is read-only. Nothing here changes your instances.

## What is measured

| What you see | What it means |
|---|---|
| **Load per core** | How busy the server's processor is. Sustained values above 2 mean work is queueing up. |
| **Memory used** | Percentage of RAM in use. Above ~92% the server starts trading speed for space. |
| **Disk used** | Percentage of the server's main disk in use. This is the one that eventually breaks things: a full disk stops backups, deploys and the database itself. |
| **Disk per instance** | How much space each instance's folder takes. Useful to find which instance is growing. |
| **CPU / memory per container** | The same, but broken down by the pieces of one instance (Odoo, database, backup). |
| **HTTP requests** | Requests reaching each instance, grouped by response code. A rising count of 5xx means errors your users are seeing. |
| **Response time (p95)** | 95% of requests finish faster than this. A good early warning: it climbs before anything actually breaks. |

## Alerts

You do not have to watch the dashboards. When something crosses a
threshold, the panel raises an alert and notifies you through the
channels you already use (in-panel, email, and any integrations you
configured).

Alerts resolve themselves when the situation recovers — you will not
have to dismiss them one by one.

Out of the box you are warned when:

- a server **stops reporting** at all (it may be down or unreachable),
- its **disk** goes above 90%,
- its **memory** goes above 92%, or
- its **processor** stays saturated.

!!! note "If the metrics system itself goes down"
    You get one alert saying so, and the existing alerts stay as they
    are. They are never cleared just because data stopped arriving —
    silence is not the same as "everything is fine".

## What you need for it to work

1. Observability enabled in **Settings**.
2. The metrics **agents installed on each host** (a one-click job from
   the host's page, repeatable at any time).

Until both are done, the section explains what is missing instead of
showing empty charts.

## What is not here yet

Database-level metrics, log search, and per-instance monitoring shown to
your own end customers are planned but not part of this version.
