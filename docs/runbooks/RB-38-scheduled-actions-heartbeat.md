# RB-38 — The heartbeat of the scheduled actions

**Severity:** planned (setting it up) · critical (when its service warns)
· **When:** setting up a panel for daily use, or a heartbeat service
saying the panel's pings stopped.

The panel's scheduled actions (Odoo crons) start its background work,
start the job queues left waiting and close the jobs a lost worker left.
They usually run on one thread (`max_cron_threads = 1`, see
[INSTALL](../../INSTALL.md#the-odoo-settings-that-matter)): a pass that
never ends, a hook waiting on a server that does not answer for
instance, holds every other one, the sweep that would raise an alert
included. With `limit_time_real_cron = 0` nothing stops that pass, and
nothing inside the panel can say so. Only something outside it can
notice the silence.

## How it works

Settings › Logs & retention › **Scheduled actions** › *Heartbeat URL*
(`cloud.settings.cron_heartbeat_url`, empty by default).

- Each pass of **Close jobs whose worker died (reconciliation)** that
  ends sends one `GET` to the URL, at its end. The action runs every
  5 minutes.
- A pass that raises sends nothing. Nor does a pass that never ends, or
  a runner that is down: the service warns when the pings stop.
- The request waits 5 seconds at most at each step (connect, send,
  answer), and its answer is thrown away. A ping that fails is logged
  (`the heartbeat of the scheduled actions was not sent`, a warning of
  `odoo.addons.incubacloud.models.cloud_job`) and the pass ends as it
  would have; the next pass tries again.
- The URL must be `https` on port 443, to a public address: the same
  checks as the notification webhooks, at save time and at every ping. A
  monitor on a private network must be published under a public name.
- A neutralized copy loses the URL (`data/neutralize.sql`), so it never
  tells the check that production is alive.
- An agent through MCP changes or empties it only with your approval.

Why the reconciliation: it runs often, it is the action whose hooks
reach servers and providers and so the one most likely to hang, and with
one thread it waits behind any other action that hangs. Turned off, its
pings stop too, and that is worth a warning: a job whose worker died
then stays open for good.

## Set it up

1. In the heartbeat service, create a check (healthchecks.io: *Add
   Check*; Better Stack: *Heartbeats*; Cronitor: a heartbeat monitor).
   Give it the period and grace below.
2. Copy its ping URL, such as `https://hc-ping.com/<uuid>` or
   `https://uptime.betterstack.com/api/v1/heartbeat/<token>`. It is the
   check's credential: whoever has it can keep the check green.
3. Paste it in *Heartbeat URL* and **Save**. A URL the panel would not
   dial is refused under the field.
4. Within 5 minutes the check shows its first ping. If it does not, look
   for the warning above in the runner's log, and try the URL from the
   runner's container:

   ```bash
   odoo$ curl -fsS -m 10 -o /dev/null -w '%{http_code}\n' '<url>'
   ```

To turn it off, empty the field and save, then pause or delete the check.

## Period and grace

- **Period:** the action's interval, 5 minutes. If you change the
  interval of *Close jobs whose worker died (reconciliation)* (Odoo's
  Settings › Technical › Scheduled Actions), change the period with it.
- **Grace:** how late a ping may be before the service warns. With one
  thread, the reconciliation waits behind whatever other action runs:
  a daily one, or the hourly checks that visit every server, which take
  longer the more servers there are (about 10 to 15 seconds per server
  behind a CDN that refuses direct traffic, [RB-18](RB-18-tune-host-edge-protection.md)).
  Its own pass starts no clean-up after 60 seconds, but one clean-up
  may wait on a server. Give the grace the longest run of any other
  scheduled action plus 5 minutes: **15 minutes** suits a fleet of a few
  dozen servers. The runner's log gives each run's length:

  ```bash
  $ docker compose logs odoo | grep -E "Job '.*' \([0-9]+\) (fully|partially) done" \
      | sed -E "s/.*Job '(.*)' \(([0-9]+)\).*duration ([0-9.]+)s.*/\3 \1/" \
      | sort -rn | head
  ```

  A warning that comes at the same hour each day, and clears by itself,
  is a grace shorter than a daily action: raise it.

## When the service warns

1. Is the runner up? Restarted or stopped, the pings stop too.
2. Which action holds the thread? Its run started and never ended:

   ```bash
   $ docker compose logs --since 2h odoo | grep -E "Job '.*' \([0-9]+\) (starting|fully done|partially done|failed)" | tail -20
   ```

   and what its connection is doing:

   ```sql
   db$ SELECT pid, now() - xact_start AS running, state, wait_event_type, left(query, 120)
       FROM pg_stat_activity
       WHERE datname = current_database() AND xact_start IS NOT NULL
       ORDER BY xact_start LIMIT 10;
   ```

3. Restart the runner (the Odoo that runs the scheduled actions). The
   held run is rolled back and runs again at its next turn; what it had
   committed stays. If it hangs again on the same server, deal with that
   server first.
4. The pings came back but the reconciliation is off: Odoo turns off an
   action that failed 5 times over at least 7 days, and a person may
   have turned it off. Turn it back on in Settings › Technical ›
   Scheduled Actions.

## Rollback

Empty *Heartbeat URL* and save: nothing is sent from the next pass on.
