# Logs

Every instance keeps two kinds of log, and the panel reads both from
**Instance detail → Logs**.

## Odoo's log is kept day by day

Odoo writes its log to a file on the instance's host, and that file is
rotated every night into one dated file per day. You get:

- **Today (live)** — the log as it is being written, refreshed every
  few seconds while *Live* is on.
- **One file per previous day**, for as long as your retention window
  allows (60 days by default).

This is the part that matters when you notice a problem days later:
the log of the day it happened is still there. It survives rebuilds —
a rebuild replaces the instance's containers, and anything kept only
inside a container goes with them.

## Finding the day

The day picker filters as you type: type `08-17` and only the matching
days remain. **◀** and **▶** step one day at a time, which is the
quickest way to look around an incident.

When you do not know the date, type what you are looking for in the
filter box and press **Enter**. The panel searches *every* archived
day on the host and tells you which ones mention it, with the number
of hits, and marks them in the day picker. Click the message in the
footer to jump straight to the most recent match.

Searching within a day you already have open also runs on the host, so
a filter over a large day comes back with just the matching lines.

## Downloading a day

**Download** hands you the selected day as a compressed file, ready to
grep locally or attach to a ticket. Very large files are truncated at
the platform's download cap.

## The other containers

The database, backup and mail containers keep logging the usual way
(Docker's own log, capped so they cannot fill the host's disk). Pick
them in the service selector; they show their most recent output, not
a per-day archive.

## Who can read logs, and what is recorded

Reading logs requires the **Developer** role or higher, and you only
see instances you already have access to.

Log access is recorded in the instance's audit log
(**Instance detail → Audit Logs**):

- opening the log viewer for an instance,
- searching every archived day (the search term is recorded),
- downloading a day.

Routine refreshes of the live tail are not recorded — one entry every
few seconds would bury the entries that matter.

## Limits

Reading logs costs the instance's host work, so the platform bounds
it. Your operator can tune all of these in **Settings → General →
Instance Logs** and **Settings → Rates**:

| Limit | Default | What it does |
|---|---|---|
| Odoo log archive | 60 days | How many days are kept per instance |
| Download cap | 64 MB | Largest download served (compressed) |
| Search: days swept | 60 | How many days a cross-day search reads |
| Search timeout | 30 s | When a search is cut short — it says so |
| Log reads per user | 60/min | Live tail, day listing, opening a day |
| Log searches and downloads per user | 6/min | The expensive ones |

If a search is cut short by its timeout, the panel says so rather than
reporting "no matches" — the days it had not reached yet are simply
unknown, not empty.

## When something is wrong

If the log viewer offers no days at all, the instance has not been
rebuilt since per-day logs were introduced: its next rebuild starts
the archive. Your operator also gets an alert if an instance stops
writing to its log file or if the daily rotation stops running.
