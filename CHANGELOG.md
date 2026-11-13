# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.0.228] — 2026-11-13

### Added

- **Start an empty central, and Retire the central (#61).** Settings ›
  Monitoring › *Where the central runs* has the two ways out for a
  central whose server must go, each after a confirmation. Picking
  another host offers *Start an empty central* there
  (`/cloud/monitoring/empty_central`): the panel moves to a new, empty
  central and leaves the old one, with its history, on its server; the
  job's log says so. *Retire the central (the history is lost)*
  (`/cloud/monitoring/retire_central`) forgets it, empties the query and
  remote-write URLs and switches observability off; it is refused while a
  deployment or an account sync of the central waits or runs.
- **Whether the central fits on a host.** Picking a host under *Where the
  central runs* shows a note from its last measures: free disk for the
  servers that would send and the retention, memory (4 GB alone, 8 GB with
  instances and 1.5 GB unassigned) and 2 cores, or *Not measured yet*.
  Advice only (`/cloud/monitoring/central_sizing`; figures and source in
  `models/_central_sizing.py`). Spanish translation included.

### Changed

- **Odoo 8.0 is offered** for new projects and instances: its run of the
  series passed with the fix of Connect as user, which opens the session
  with a password of its own on a series that has no session token. The
  panel now offers 8.0 to 20.0; 7.0 stays retired. The user guide gives
  the range 8.0 to 20.0 and the PostgreSQL range of each series.

### Fixed

- **A job cancelled while parked keeps its checkpoint until its close
  (#71).** The reconciliation let it go while cleaning up, before the
  close; a close that then found the job's rows held, or raised, left a
  job nothing named any more: it never closed, and the steps after it
  waited for good. The close now lets the row go with the job's end.
  One whose clean-up raises is closed all the same, its row closed for
  a later pass, instead of being closed again at every pass. A close
  tried and not done 30 minutes after the job was taken up has the row
  closed, with an error in the server's log; a job taken up again after
  more than that without a pass is tried once more first. A job whose
  type has no executor any more says once in its log that nothing can
  clean what it left, and a cancelled one raises `job_cleanup_given_up`.
  Spanish translation included.
- **The trace of a user's sessions goes with the user (#74).** Odoo
  keeps the trace of every session (`res.device.log`, with its address,
  country and browser) for good, and the privacy policy keeps it while
  the user's account exists. The daily vacuum now deletes the trace of a
  user who is deleted or archived, with the session guard's closes and
  holds of an archived one. Archiving a user deletes their stored
  sessions, and the vacuum deletes those the trace names before the
  trace: Odoo's session token includes `active`, so a session left on
  disk was accepted again once the user was reactivated, and with its
  trace gone *Log out from all devices* could not find it.
- **The central's server can no longer be deleted or archived (#61).**
  `metrics_central_host_id` let go of its host (`ondelete='set null'`),
  and nothing stopped deleting or archiving it: the panel lost sight of
  the central, its metrics history and its security logs. The model, the
  `/cloud/delete_host` route (and with it the MCP's `delete_host`) and a
  `delete_host` job queued before now refuse, naming the ways out above,
  and the foreign key is `ondelete='restrict'`, which the update applies
  with no step by hand. Every other change to that server and every job
  on it runs as before.
- **Enable observability no longer orphans the central (#61).** On a
  host other than the central's it started an empty central there and
  left the history behind, unread. The route and the job refuse it while
  the central runs elsewhere; *Start an empty central* is the only way,
  through a payload key (`discard_history`) that `enqueue` refuses to
  anyone without superuser rights, so `call_kw` and the MCP cannot set it.
  On the central's own host, or with no central, it deploys as before.

---

## [1.0.227] — 2026-11-12

A server's page has a **Metrics** tab, and **Monitoring** draws the fleet
and each server itself, with no Grafana frame: the same charts as an
instance's Metrics tab, read from the metrics central. Five alert rules
watch what makes instances slow without a percentage showing it: a disk
or memory the server's tasks wait for, an instance held back by its CPU
quota, queries waiting for a lock and a database near its connection
ceiling. Each server measures its instances' databases every minute.
A host's card shows the CPU and RAM the server really uses, read from the
metrics agents, instead of one bar that added up its instances' limits.

The update adds four columns to `cloud_host`: `cpu_usage`, `ram_usage`
and `usage_last_seen`, empty, and `metrics_agents_revision`, which starts
at 0 on every host, and needs no data migration; the module has to be
updated, which loads the five rules. The reconciliation cron then
re-applies the agents of every installed host, five per pass of 15
minutes, which installs the database collector; nothing has to be run by
hand. The central's deployment that the update queues (below) closes the
operator's read route, caps the operator's requests in flight and
replaces the deletion key, which moves into a file.

**Security logs on every server, and a metrics central without Grafana.**
Each server sends its security events to the metrics central: SSH
sign-ins and failed attempts, sudo, fail2ban's blocks and changes to
passwd, shadow, sudoers and the SSH configuration, kept there 90 days.
**This update turns them on for every server, also on panels that had
*Send hosts' security logs* off.** Each server's **Connection** tab has
the switch that stops them for that server. Fluent Bit sends them, in
place of promtail, and counts them for the charts and alerts to come; it
no longer mounts the Docker socket, and the proxy's access log stays on
the server, where the panel reads it when asked. The central no longer
runs Grafana: the panel draws every chart itself.

The update needs no step by hand. It adds `cloud.host.security_logs_enabled`
and `cloud.settings.metrics_central_revision`; the agents' revision is 2,
so the reconciliation cron re-applies the agents of every server, five
every 15 minutes. It empties Grafana's admin password and base URL, and
the central is deployed again once, which removes Grafana and its data;
VictoriaMetrics and Loki keep theirs. That deployment writes the
central's URLs again, as every deployment does. To keep what Grafana left,
create the system parameter `incubacloud.metrics_keep_grafana_data`
before updating (`docs/observability-operations.md`, "Undo").

### Added

- **A server's Metrics tab.** Under **Hosts**, a server's page has a
  **Metrics** tab (`developer` role and above), also shown in
  Monitoring's **Hosts** view. It says in plain words what is short over
  the last 15 minutes and which instance to look at (memory over 92 %,
  root disk over 90 %, load over 2 per core, the kernel's waits for
  memory or the disk), then its load per core, the waits (PSI), memory
  and swap, CPU, memory and directory per instance stacked to the
  server's total, the disks' activity and throughput, network traffic,
  requests and p95 per instance, how long ago the agents sent their last
  sample, and a table of every instance over the last five minutes,
  busiest first, linked to its page. `/cloud/host/resource_series`
  answers it in one call; `/cloud/hosts/<id>?tab=metrics` opens it.
- **Monitoring without a frame.** **Fleet** counts the servers that
  report and those over 90 % of disk, 92 % of memory or waiting over a
  fifth of the time, the instances deployed and running and the active
  alerts, draws the ten busiest servers by load, memory, root disk and
  disk activity, and shows which servers went silent in the window. The
  panel that deployed the central reads it with the operator credential
  through the central's read-only `/admin-r` route and sees every
  account (a central whose gateway does not let the panel's address read
  that route shows the panel's own servers instead); any other panel, a
  managed one among them, sees its own servers. **Hosts** shows one
  server's board. `/cloud/fleet/resource_series`
  answers it; `/cloud/monitoring/config` gives each host's `id`.
- **Rules `metrics_host_io_pressure`, `metrics_host_memory_pressure` and
  `metrics_instance_cpu_throttled`.** A server whose tasks wait for the
  disk over a fifth of the time for 15 minutes (warning), one whose tasks
  all stall for memory over a tenth of the time (critical), and an
  instance whose Odoo was held back by its CPU quota over a quarter of
  the time for 15 minutes (warning). Thresholds with their reasons in
  `docs/observability-operations.md`, "Tuning alerts".
- **Each instance's database, measured on its server.** The agents'
  install adds `collect_pg.sh`, run as root every minute from
  `/etc/cron.d/incubacloud-collect-pg`: each running instance's
  connections and their maximum, active queries, sessions waiting for a
  lock or idle in a transaction and the oldest of each, deadlocks,
  temporary bytes and cache hits (`ic_pg_*`), read with the host's psql
  through the socket of the instance's `db` container, in a read-only
  session that gives up after 3 s, within 45 s for the whole pass. 83 ms
  of CPU per instance, 2.8 % of a core with twenty. Every ten minutes the
  disk collector adds each database's size (`ic_pg_db_size_bytes`) and
  each filestore's (`ic_instance_filestore_bytes`, once an hour on a
  server under 6 GB). Full Setup installs `postgresql-client` and `cron`;
  the agents' install puts each in only when missing, and a collector that
  cannot be installed (no cron, no passwordless sudo, apt busy) still lets
  the agents come up and shows as *Database collector not installed* on
  the server's board, but fails the job with what to do, so that it is
  fixed then and not found later without the figures.
  **Collect database figures** (Settings → Monitoring, *Advanced*) turns
  it off on every server, and a server's teardown removes it.
- **Rules `metrics_instance_db_lock_wait` and
  `metrics_instance_db_connections`.** Queries of an instance waiting for
  a lock at every reading for 10 minutes, and a database using over 85 %
  of its connections twice in a row (warnings). Both read only a fresh
  reading of the collector: one that stopped raises nothing.
- **The server's "Right now" table** shows each instance's database and
  filestore size and its connections against their maximum, with the
  sessions waiting for a lock.
- **Hosts converge on the agents' playbook.** `cloud.host` records the
  revision of the agents' install it last completed
  (`metrics_agents_revision`, `AGENTS_REVISION` in the executor); the
  reconciliation cron re-applies, five per pass, the installed hosts on
  an older one, so a host installed by a panel still on older code is
  brought up to date without a burst of installs at upgrade.
- **A host's card shows the CPU and RAM it really uses.** Its one
  «CPU / RAM» bar added up the limits of the host's instances, so a nearly
  idle server could read 100 % beside a disk bar that was real. The card
  now has a CPU bar and a RAM bar with the use the metrics agents report
  (the cores' busy share over the last five minutes, `iowait` not counted
  as busy; memory in use as total minus available), each with the figure
  in cores or GB, and what the instances are allocated as a second line:
  *0.9 of 4 cores · allocated: 11.3 vCPU*. Without a reading of the last
  15 minutes the card shows no bar and says why: metrics off (on a
  managed panel, which cannot turn them on, not available there), agents
  not installed, their installation failed, or no recent reading and when
  the last one was. The specs cron reads both with two more queries and
  writes them only together; `/cloud/get_hosts` gives `usage_state`,
  `cpu_usage`, `ram_usage` and `usage_last_seen`. Adds three columns to
  `cloud_host` (`cpu_usage`, `ram_usage`, `usage_last_seen`); Spanish
  translation included.
- **Send security logs, per server.** On the server's **Connection** tab
  (`cloud.host.security_logs_enabled`, on by default): on, the server's
  agents run Fluent Bit 5.1.5, which sends the journal's SSH and sudo
  lines, auditd's records of its four keys and the files they watch, and
  fail2ban's lines to the central's log store, and exposes seven counters
  with a closed set of labels (`ic_ssh_auth_failed_total`,
  `ic_ssh_accepted_total`, `ic_sudo_total`, `ic_sudo_failed_total`,
  `ic_f2b_total`, `ic_audit_events_total`, `ic_security_lines_total`);
  off, the container, its cursors and its buffer are removed, so nothing
  of the time it was off is sent. A change applies at once to that
  server, also after a failed install, and is in the audit log; an agent
  that turns it off through `manage_host` asks for approval. A change of
  the server's key or user applies at once too: a sign-in with the
  panel's key counts as the panel's. While the central does not answer,
  the lines wait on disk, up to 64 MB. A server that keeps its journal in
  memory sends auditd's and fail2ban's lines only, and the install says
  so. It can always be turned off; with observability off, the change
  applies once it is on again. The install sends the logs only where the
  switch is on and the panel may send them. The connection guide has a
  section on what is sent.
- **The central converges on its playbook.** The panel records the
  revision of its central's last good deployment, and the reconciliation
  cron deploys the central again while it is behind, not while a
  deployment waits or runs and with a growing wait after a failure. Not
  with observability off.
- **A cancelled job reads *Cancelling…* until it has stopped.** Cancel
  marks the job cancelled at once, before its run has stopped what it
  left on the server. While its queue job has not ended, or its
  checkpoint is not closed, the job lists and the history say
  *Cancelling…* in the colour of a running job, and a job queued behind
  it names it as what it waits for. `/cloud/get_job_state`, and the MCP's
  `get_job`, `list_jobs` and `cancel_job`, answer `stopping`. Spanish
  translation included.
- **Alert `job_cleanup_given_up`.** When the reconciliation gives up on
  the server of a cancelled job, an hour past the cap of the step it
  ran, the server gets a warning, one per job: why the panel gave up
  (the server did not answer, a step past the point of no return still
  ran, something could not be stopped or removed), the step that may
  still run with its directory and the command that stops it
  (`bash <dir>/ctl.sh stop <dir> 20`) once it had started there, and the
  paths left to remove. It is raised with the deletion of the
  checkpoint: an alert that fails lets the row go all the same.

### Changed

- **Monitoring appears wherever there is a central to read.** The
  `dashboards` axis of `_observability_capabilities()` asks for a
  central URL instead of a Grafana URL, so a managed panel that reads a
  central now has a Monitoring entry showing its own servers.
- **The boards share their mechanics.** `ChartBoard` holds the range,
  the zoom, the refresh every 45 s and the words for an answer with
  nothing to draw; the instance's board, the server's and the fleet's
  extend it. The three routes share one budget of 20 boards a minute per
  user.
- **The operator's read route names its endpoints.** On the central,
  `/admin-r/` serves `/health` and the read endpoints an account's `/r/`
  serves (`_OPERATOR_READ_PATHS`); it served the whole of VictoriaMetrics,
  imports, exports, snapshots and flags included, and those now answer
  400. Under the central's public name it has a router of its own,
  `ic-central-read`, behind the same list as the series deletion: **Who
  may delete series and read the fleet on the central** now also opens
  the cross-account read to those addresses, as its label and help say.
  Empty, it answers the
  central's host only, as before.
- **Only the operator has a cap on requests in flight: 16.** vmauth
  counts per user and answers the next request 429 at once; an account's
  user is shared by its agents' writes and its reads, so accounts have
  none. A 429 is "busy", not "down": `promql_query` asks once more after
  a second, the rule cron skips the run without raising
  `metrics_backend_unreachable` or resolving anything, and a board says
  *The metrics central is busy* and asks again, keeping the charts it
  already shows. A deletion of series asks again three times, 5 s apart,
  before it warns.
- **Each server decides its security logs, and the update decides where
  they start.** The panel-wide *Send hosts' security logs* is gone from
  Settings, the routes and `update_settings`. The update sets every
  server on (the owner's decision of 10-nov-2026), unless a module that
  extends the panel says otherwise through
  `cloud.host._security_logs_after_upgrade`; such a module may also start
  new servers off (`_security_logs_default`) and keep every server off,
  refusing to turn one on by the form, the route or an agent
  (`_security_logs_available`), as a managed panel's does until whoever
  manages it allows them. A panel of its own is on from this update.
- **The central is VictoriaMetrics, vmauth and Loki.** Its deployment
  removes what Grafana left once the boundary answers, saying in its log
  what it could not remove instead of failing; `/gadmin/` and its router
  under the public name are gone (that recreates vmauth once, for
  seconds), and the account sync only rewrites the access-control list.
  Settings loses *Grafana base URL*. Loki keeps security logs 90 days and
  anything else 30, so the access lines already stored expire as before.
- **`/cloud/monitoring/config` and `get_monitoring`** say whether metrics
  are collected, whether a central is set and, per server, whether its
  agents are installed and its security logs sent; Grafana's URL and the
  instances' list are gone.

### Removed

- **promtail and the proxy's access log in the central.** The access log
  stays on each server (`/var/log/traefik/access.log`, 30 days); the
  agents no longer mount the Docker socket.
- **Grafana**, its dashboards and its organisations: the panel draws the
  instance's, the server's and the fleet's boards itself.

### Fixed

- **A failure while moving sshd puts it back.** On Ubuntu 24.04 the
  port drop-in of `ssh.socket` gave a bare port, and the distro's unit says
  `BindIPv6Only=ipv6-only`: the socket took IPv6 alone and IPv4 had no
  sshd. The 24.04 `sshd-socket-generator` hid it whenever the port was not
  22, since its own file, read after ours, lists both families; with port
  22 it writes nothing, and a run on a fresh server left it reachable on
  IPv6 only and stopped (12-nov-2026; the server was rebuilt). The drop-in
  is now `zz-incubacloud-port.conf`, read after the generator's, with
  `BindIPv6Only=both`: one socket for IPv4 and IPv6 on Ubuntu 22.04, 24.04
  and Debian 13, and IPv4 alone on a kernel without IPv6. The old
  `10-incubacloud-port.conf` is removed. Everything that moves sshd runs in
  one block that first keeps the SSH files, the state of `ssh.socket` and
  `ssh.service` and the port sshd uses, and waits for sshd's own greeting
  on the new port, not just an open port. When any of it fails, the play
  puts the files and units back as they were, waits for sshd on the port
  it had, and fails with the step that failed and whether sshd answers
  again; the job's error says the SSH configuration was put back, and
  nothing after SSH (firewall, fail2ban and the rest) was applied. Should
  the play's own connection be lost on the way, which leaves Ansible no
  rescue to run, a timer on the host puts it back three minutes later; the
  play stops it once sshd answers on the new port. A run that fails after
  that, at the firewall or a later step, leaves sshd on the new port: the
  job's error names it, and the panel takes it, opening it on the
  provider's firewall, so a retry reaches the host there and keeps it
  instead of drawing another. The recipe changes, so every hardened host
  reads `host_hardening_outdated` at the next hourly check until its
  hardening is re-applied.
- **The agents install on a server without cron.** Debian 13 no longer
  installs it, and the disk collector's schedule failed the whole
  install there; Full Setup and the agents' install now put it in. When
  it cannot be installed (apt busy, sudo asking a password), the job
  fails with *cron is not installed on the server and could not be
  installed: install it (apt install cron) or use the IncubaCloud image*.
- **A server without cron can be deleted.** The teardown removed the disk
  collector's schedule with the `cron` module, which fails without
  `crontab`, so `delete_host` failed on a Debian 13 that never had cron;
  it now says in its log that there is no schedule to remove. A database
  collector that cannot be removed (sudo no longer works) is said in the
  log instead of stopping the removal.
- **An install that ran while Collect database figures changed** records
  no revision, so the reconciliation cron applies the new value.
- **Docker not answering is not a sleeping fleet.** When `docker ps` or
  `docker inspect` time out, the database collector counts every
  instance in `ic_pg_scrape_errors` instead of leaving them out as
  stopped.
- **A job taken up by a pass that did not close it lets its checkpoint
  go (#66).** The next pass closes the job without cleaning up again,
  and now also deletes its checkpoint, or closes it for a later pass when
  the server was not reached; so does a job whose type has no executor
  any more. Kept unclosed, a job cancelled while parked was taken up at
  every pass of 5 minutes, its server cleaned again and a line added to
  its log each time, and it read *Cancelling…* for good.
- The Spanish texts call the metrics central *la central* throughout.

### Security

- **The central's deletion key no longer shows in `docker inspect`.** It
  was the value of `-deleteAuthKey` in the VictoriaMetrics container's
  command; the deployment now writes it to a file only the central's
  directory reaches, and passes the flag the file's path. The first
  deployment with this release replaces a key recorded before it, once.

---

## [1.0.226] — 2026-11-12

Every step of a job that may run long now goes on on its server apart from
the request, as the rebuild's image build did since 1.0.225 (the download
of the repositories, the pull and build of the image, the initialization of
a new database, the boot test, the module updates, the restores' downloads
and loads, the copies and downloads and their packaging, Full Setup's
install of the host's tools, and continuous recovery's base copies and its
wait for a restore), and the job comes back for it; every other step runs
as before, and the startup warning now accepts Odoo.sh's 900 s. The relays
through the panel by SFTP and the playbooks still run within the request.
A step left running past its job's point of no return holds the instance's
queue until it ends. A step run apart keeps its exit status when stopped on
a server whose `timeout` is uutils', as on Ubuntu 26.04.
`incubacloud_pitr` 1.0.14 comes with it.

A signed-in session that changes hands is closed on the server, without
asking anything: used from another country, from another browser family
or by a script, or idle past its limit. Off by default in core; each
signal is switched off, logged or closing by a system parameter.

The update adds five empty columns, `cloud_job.slot_wait_s`, `slot_load`,
`slot_channel` and `return_wait_s`, and
`cloud_job_checkpoint.holds_queue_since`, and two empty tables,
`cloud_session_closure` and `cloud_session_hold`, and needs no data
migration. The hosts need nothing: the scripts travel with each job. A
panel's own Odoo configuration gains `x_real_ip_country_header` on its
next deploy when its host is behind Cloudflare.

### Added

- **A rebuild's long steps run apart on its server.** «Fetch the
  repositories», «Test new image (safe boot check)» and «Update changed
  modules» are marked `detached` besides «Rebuild Odoo image», with the
  options they had, in core's rebuild and in every rebuild that inherits
  it (a plan change skips them all, and keeps no checkpoint). A rebuild
  whose step outlasts the attempt's budget leaves it to the server and
  comes back for it. Cancelled before its update, the step is stopped and
  the rebuild closes as it does today, leaving the instance on its
  previous image; from the update on, a stop is refused as before, and an
  attempt that takes the update up after a park follows it without
  deciding the stop again. An update the instance's crons hold back (75)
  starts the rebuild over five minutes later, as before, its checkpoint
  and step directory removed. «Ensure incubacloud_connect» stays attached:
  a rebuild's database nearly always has the module.
- **A deploy's long steps run apart on its server.** «Fetch the
  repositories», «Pull images», «Build Odoo image», «Initialize database»
  and «Initialize module checksums» are marked `detached`, with the
  options they had; «Pull images», capped only by the default hour
  before, gets 900 s. A move's deploy, which initializes nothing, keeps
  the others. A deploy stopped during one of them, parked or not, stops
  the step and its instance goes back to draft, as it does today.
- **The restores run their long steps apart on their server.** A restore
  of an archive runs apart its download from a link, the check of the
  archive, the load of the database, the update of its modules and the
  install of `incubacloud_connect`, which boots an Odoo when the module is
  missing; a restore from the backup store runs apart duplicity's restore,
  the import of the SQL and the update. The load, the import and the
  update are past the point of no return: a job closed while one runs
  leaves it to end, and its queue waits for it. A move's restore has no
  such point, so its load is stopped like any other step; the job decides
  that once and keeps it, so the move's rollback, which clears the move's
  target, does not make a later close read it otherwise, and it gives up on
  a server it cannot reach after 120 s instead of 600, as the instance is
  down meanwhile. The relays through the panel (an upload, an archive of
  another job, one handed over from another server) still run within the
  request, before the steps.
- **The copies and downloads run their long steps apart on their server.**
  A copy runs apart its dump, or the backup store's run for a production;
  a download its dump, its restore from the backup store and its
  packaging; a neutralized download its source, its neutralization and
  its dump. The handover of an archive for a chained restore, a `mv`, and
  the neutralized download's cleanup stay attached. The fetch of the
  archive into the panel cannot run apart: an attempt that has run a
  minute and has less than 85% of its request left leaves it to a fresh
  attempt, as the mechanism allows. A stopped neutralized download still
  drops its throwaway database.
- **Full Setup and continuous recovery run their long steps apart.**
  Full Setup's «Install host tools», which installs Docker and the
  packages of a new server, runs apart on its server, with the umask of
  the session as attached: what it writes through `sudo` keeps the modes
  it had. A server that lacks a tool the step needs runs it attached and
  the job log says why. With `incubacloud_pitr`, the daily «Base copy»
  runs apart in a scope of its own, beside a rebuild or a restore of the
  same instance, and so does the switch on's «First base copy», in the
  same scope, so two copies of an instance never run at once; and so
  does «Wait for the restore», the job parking while the restore runs on
  the host; a stop of the restore waits on the host within what a step in
  the request may last (240 s with a limit of 900, 300 as before with
  3600), the next job following one not undone by then. A restore taken
  up after a park keeps the base copy it chose and whether it only
  followed one already there, so a stop then hands the restore to the
  next job instead of asking the host again. What each reads last, the
  copies' window and the restore's result, comes in the tail of the
  output the step keeps.
- **Each job keeps how long it waited for a slot.** `slot_wait_s`, the
  seconds the channel gate held it back, written as it is handed to
  queue_job; `slot_load` and `slot_channel`, how many of the panel's jobs
  held a slot of its channel then, itself included, and which channel;
  `return_wait_s`, how late it came back from its parks, written as each
  attempt starts and never in a way that could keep it from starting.
  Hidden types keep none; a job that did not wait keeps 0. The query that
  reads them by week, and when they call for following jobs without a
  thread each, is in `docs/architecture.md` («The job queue»).
- **A step left past the point of no return holds its queue.** A step
  past the job's point of no return is never stopped, as before: it is
  left to end. Meanwhile the queue of its instance, or of its host, waits
  for it as for a running job, and the job first in it reads «Queued ·
  waiting for job #N to end on the server»: the reconciliation looks at
  the step at every pass, says in the closed job's log the exit status it
  ended with, and serves the queue; a server that cannot be reached holds
  it two hours and ten minutes at most. A step found ended, or stopped,
  lets go of the queue though its row stays for a directory it could not
  remove. A step before the point of no return never holds it, so the
  rollback of a move whose new server is gone starts at once; the point is
  the one the plan had when it ran, so a move's restore does not gain one
  once the rollback removes the move's marks, and a step on a server the
  instance does not live on holds nothing. A job whose own stop of such a
  step fails holds its queue as it ends, a step that never started holds
  nothing, and a job taken up again drops the hold its row kept.
- **The session guard** ([RB-33](docs/runbooks/RB-33-session-guard.md)).
  What a session is compared with is written into it on its first signed-in
  request: the country, the browser family as Odoo's parser names it, and
  its idle limit. A request that adds a trace to the session — a new
  address or browser — is compared; another country, Tor (`T1`), a request
  that did not come through the CDN, another family or a browser the
  parser cannot name close the session for whoever holds it, and the
  request is answered as an expired session. A country the CDN could not
  tell (`XX`) is only logged. The idle time is checked on the server on
  every request, which Odoo leaves to the cookie's lifetime. Switches
  `incubacloud.session_guard.country`, `.browser` and `.inactivity`, each
  `off`, `log` or `close`, all `off` in core: the module may be installed on
  somebody's ERP. Every close, and every request a signal in `log` would
  have closed, is a row of `cloud.session.closure`, with the time the user
  took to sign in again and from where; it is written on a cursor of its
  own before the request is refused, once per session however many
  requests of a page saw the change. After three closes of distinct
  sessions of one user by one signal within 24 hours, that signal only logs
  for them for 7 days, renewed while the same pair of values keeps showing
  up, with one `session_guard_antiloop` alert per user and pair. Rows are
  kept 90 days.
- **What the guard writes never stops a request.** Every row goes on a
  database connection of its own, in READ COMMITTED, so the parallel
  requests of a page see what the one before committed: in Odoo's
  REPEATABLE READ, a user whose signal only logs (the anti-loop) was
  closed by the second request of a page, and two sessions closing at
  once missed each other in the anti-loop's count. A write that still
  collides is tried again on a new connection, eight times in all, after
  a pause of 10 to 50 ms at random, with one line at INFO and no `bad
  query` ERROR. Any failure of the guard but a close is logged and the
  request is served, where Odoo would answer 403 on every route, the
  sign-out included, a database not yet updated among them. A close waits
  three seconds at most for another close of the same user; the
  anti-loop's alert, which can page somebody over HTTP, is raised once
  the hold is committed and outside that wait, so the other requests of
  the page are held and not closed. A failure of what a layer does on a
  close keeps the close's row, and a session whose files cannot be
  deleted is closed anyway.
- **A closed session on a page open to visitors** (the sign-in, the
  website) is served as a visitor, as Odoo serves a session it no longer
  accepts there, instead of being sent to the sign-in.
- **The visitor's country behind a CDN.** `x_real_ip_country_header`
  (`CF-IPCountry` on Cloudflare) is believed only on the proof the visitor's
  address is, and each request the correction saw says whether the CDN
  spoke. A deploy writes it for an instance on a host behind Cloudflare, and
  an export drops it with the other proxy options.

### Changed

- **The startup warning about `limit_time_real` starts below 900 s**,
  Odoo.sh's limit, instead of 3600: every step that may outlast 0.4 of
  900 s runs apart now. What is not a step on the server, the relays
  through the panel by SFTP (an upload to restore, an archive handed
  between servers, a copy fetched into the panel) and the playbooks, still
  runs within the request, and a higher limit gives it more room;
  `INSTALL.md` keeps 3600 in its example, and it and the READMEs ask for
  900 at least.
- **A step run apart runs its command with the umask of the session that
  starts it**, so what it creates on the server has the modes it would
  have attached, instead of the 077 the script keeps for its own directory
  and files, which stay `0700` and `0600`.
- **A hidden job's step run apart holds a scope of its own on its server**,
  per type: a base copy of PITR never makes a visible job of the same
  instance wait for it, nor waits for one.
- **A job cut by a restart resumes at the step it was in.** A worker that
  dies is taken up from the checkpoint, the step apart going on on the
  server and not run again, and the step it ran within the request, if
  any, running again. `INSTALL.md` says so for a job running during an
  upgrade, where it said the job ran again from the start; any other job
  still runs again from the start.

### Fixed

- **A step run apart keeps its exit status when it is stopped on Ubuntu
  26.04.** There `timeout` is uutils', which answers a TERM from outside
  with 124 whatever the step's trap exited with, so a stopped step read as
  cut by its cap. `detached_step.sh` runs the step under GNU's
  `gnutimeout` where the server has it, and `timeout` elsewhere; so do
  the calls of `deploy.sh` and `lib/common.sh` that tell a call out of
  time from a stopped one (the git helper's container, the query of the
  heads and the helper's pull).
- **A close reads a long log to its end.** A close reads the step's log
  for up to 30 s, where it read it eight times, before it takes the step
  for running, and a step found ended when the job is closed is said in
  its log with its exit status, which the closing hook gets as the step's
  result instead of a step that did not end.
- **A row a closed job kept between two steps no longer holds its host's
  build lock.** Only a row whose step apart was left started there holds
  it, not one kept for a directory it could not remove.

---

## [1.0.225] — 2026-11-11

Jobs that outlive the request that runs them, as on Odoo.sh, where a
request is cut at 900 s: one step runs apart from its request so far, the
image build of a rebuild, before its point of no return, and every other
step runs as before. Settings is a menu of sections, one page each, with a
search over every setting (issue #3). A restore to a moment updates its
database to the code the instance runs, as every other restore does since
1.0.224; a stopped update of a preview no longer lets a push start a second
one beside it; the rebuild of a neutralized staging or preview neutralizes
it again after its update; and the web terminals, a host's shell and an
instance's, keep what is typed when the proxy in front of the panel answers
429, make about half the requests, and take the page's size.
`incubacloud_pitr` 1.0.13 comes with it.

The update adds one empty table, `cloud_job_checkpoint`, and two empty
columns, `cloud_job.channel_wait_since` and
`cloud_instance.pr_update_cut_off`, and needs no data migration. It changes
the terminal pages' templates and adds one, `terminal_client_script`. The
hosts need nothing: the scripts travel with each job, and the proxy's limit
is unchanged.

### Added

- **A rebuild's image build runs apart on its server.** «Rebuild Odoo
  image» is the first step marked `detached`, in core's rebuild and in
  every rebuild that inherits it. A rebuild whose build outlasts the
  attempt's budget leaves the build to the server and comes back for it;
  cancelled meanwhile, the build is stopped and the rebuild closes as it
  does today, leaving the instance on its previous image. What an
  executor's hooks read that is set before the steps (`_carried`) now adds
  up over its classes, so a mixin declares its own without hiding its
  bases'.
- **The job lists say what a waiting job waits for.** A job parked
  between two attempts reads *Running · on the server, reconnecting at
  12:03:05* (*Running · reconnecting at …* when nothing of it runs on the
  server), and one the channel gate holds back reads *Queued · waiting
  for a free slot (root.user 1/1)*, in the recent activity of an instance
  or a host and in the job history. `_format` and `_format_history` give
  `on_server`, `parked_until` and `channel_wait`. Spanish translation
  included.
- **A step of a job can run apart on its server.** A step marked
  `detached` runs in a session of its own (`scripts/detached_step.sh`),
  capped on the server by its timeout, and is read every second: its lines
  reach the job log as an attached step's do, and its exit status and
  stdout (the last 256 KiB) are what the job gets. Cancelled, it is sent
  TERM, given 60 s for its trap and killed; its last lines still reach the
  log. One step at a time per instance, or per host: a job that finds
  another job's step still running there waits 30 s for it. A
  server that kills an account's processes when its SSH session ends
  (logind's `KillUserProcesses`), or that lacks `flock`, `setsid`,
  `timeout`, `pgrep` or `pkill`, runs the step attached, as before. The
  day's maintenance of a host removes the step directories no open
  checkpoint holds, an hour past their cap, unless their step still runs.
- **A job with a step run apart leaves it to the server and comes back.**
  Once its attempt's budget is spent, before a step, while it follows the
  step apart or before its closing hooks, the job parks: its checkpoint
  says where it is, the step goes on on the server, and queue_job runs
  the job again 5 s later, first of its channel and without counting the
  attempt. The next attempt goes on from there with the plan, the
  working directory and the results of the first: no step runs twice, no
  line of the log is lost or repeated. A worker that dies is taken up the
  same way. A server that cannot be reached while the step runs there
  makes the job wait 30 s and try again, for up to 10 minutes; past them
  the job fails and the step is left for later. The same holds between
  two steps, for an attempt that comes back and cannot connect. A step's
  directory goes only once its result is recorded, so an attempt that
  dies in between finds the step's end there. The cloud job stays
  running all along.
- **A parked build still holds its host.** A rebuild or a teardown takes
  its host's build lock in its transaction, which a park lets go: the
  checkpoint keeps the host from its first write, before any lock can go,
  and another build or teardown of the host waits 30 s while it is there,
  its deferral naming the job that holds the host.
  The parked job takes the lock again when it comes back, and waits 5 s
  if another one holds it at that instant.
- **A parked job is stopped and closed by itself or by the
  reconciliation.** Cancelled while parked, it comes back within seconds,
  stops its step (TERM, 60 s for its trap, KILL), removes its working
  directory and its secrets, and closes as a job cancelled while it ran,
  with the results of the steps that had ended. Its queue job cancelled,
  set done or deleted by hand from queue_job's views is not taken for a
  job that never ran: the reconciliation of lost jobs stops what it left
  on the server (20 s for the trap) before its hook; one that had been
  cancelled meanwhile closes cancelled, as a stop. A job that ended with
  something left on a server it could not reach is picked up by the same
  reconciliation after the jobs it has to close, every 10 minutes, and
  given up an hour past the step's cap, counted from the job's close; its
  log says so the first time and when it gives up. A step past the job's
  point of no return is never stopped: it is left to end and removed
  afterwards, as is a step's directory a process of it kept. A job run
  again over a checkpoint it does not take up — kept by the hook that
  closed it, or written by another queue job — stops and removes what
  that checkpoint says first, and waits 30 s while it cannot.
- **A parked job keeps its slot in its queue_job channel.** queue_job's
  runner stops counting a job that waits between two attempts, and would
  give its slot to another job of the channel. The panel hands a job to
  queue_job only while its channel, and every channel above it, has room
  for one more of its jobs, parked ones included, counted as the runner
  reads its channels (`ODOO_QUEUE_JOB_CHANNELS`, else `[queue_job]
  channels`, else `root:1`). A job held back waits in its queue, saying
  since when, and the channel with its load and capacity; when a job ends,
  or one that is not parked sleeps until later, those held go first, by
  priority and age, looked for once that end is committed and served
  before the next job of the queue it left; once the channel refuses one,
  the others held in it wait for the next slot. A step of a chain that
  ends done leaves its slot to the next step and wakes nobody. A job
  asleep until a later time takes no slot, nor does a step of a chain
  waiting for the one before it. The jobs a transaction hands to queue_job
  count for the next one it asks for, before they are committed. A job
  held two minutes no longer waits for the probes and metrics that are
  due and have not started: queue_job then orders it among them. The
  lists read the channels once for all the jobs they show. The place is kept
  however late the parked job is to come back: 600 s late, its log says
  it waits for a free slot and in which channel, and the slow job warning
  is raised. `channel_gate = off` in odoo.conf's `[incubacloud]` section
  dispatches as before.
- **Where a job with a step run apart keeps its place.**
  `cloud.job.checkpoint`, one row per job: the step that comes next, what
  the steps before it answered, how much of the running step's log is in
  the job log. The plan and the results are encrypted. A neutralized copy
  of the database has none.
- **How long an attempt of a job runs before it leaves its step to the
  server**: 0.6 of the server's `limit_time_real` (540 s with 900), or
  `attempt_budget_seconds` in odoo.conf's `[incubacloud]` section, for
  tests. A step that runs in the request must last less than 0.4 of the
  limit (360 s with 900).

### Changed

- **Odoo 9.0 to 15.0 are offered again** for new projects and instances:
  their run of the series passed on the production image of 9-nov-2026.
  8.0 stays out until its run is repeated with the fix of Connect as user;
  7.0 stays retired. The user guide gives the range 9.0 to 20.0 and each
  series' PostgreSQL range.
- **From zero to Odoo sizes the users at once leaving a worker for the
  scheduled actions.** The guide's figure is (workers - 1) x 6, as in the
  example of Odoo's deployment guide (owner, 11-nov-2026): 24, 48, 96 and
  192 users at once for 2 vCPU/4 GB, 4/8, 8/16 and 16/32, where all the
  workers were counted before (30, 54, 102, 198). An instance's default 2
  workers still serve about 12 users at once: its configuration sets
  `max_cron_threads` apart, so both workers answer people.
- **The startup warning about `limit_time_real` says the attempt's
  budget** and what a step in the request has left. It still starts below
  3600 s: only the rebuild's image build runs apart so far, and with 900 s
  a deploy's database initialisation or a long restore can outlast the
  request and die. The log says the budget at every start. It also says
  when `root.user` has no capacity of its own among queue_job's channels,
  and which channels other modules' jobs ran in over the last 30 days: a
  parked job may wait for those. These two are only logged, never part of
  the alert `job_server_limits_too_low`: they kill no job, most servers
  have one of them and never park, and a parked job that does find no
  slot raises the slow job warning by itself, naming the channel.
- **The slow job warning measures the attempt**, from its own start,
  against 0.75 of the server's `limit_time_real` (45 minutes with 3600, as
  before; 45 minutes without a limit), and says the limit.
- **Restore to a moment updates the modules.** Once the files are back, with
  Odoo still stopped, the restored database's modules are updated to the code
  the instance was last built from (`cron_guard.sh update`, as a rebuild
  does), unless **Auto-update modules on rebuild** is off. `pitr.sh
  restore-start` takes the database and the flag, copies `cron_guard.sh` next
  to the copy of itself the restore runs from (only then: a caller that asks
  for no update needs no guard beside `pitr.sh`), and records the outcome in
  the result (`modules=updated|failed|skipped`). The update runs only on a
  database that replaced the previous one, after its promotion: a cancel no
  longer undoes anything there and does not cut it, and the panel says the
  restore is finishing rather than being undone (`phase=update`). It has an
  hour, as a rebuild's update does: then its one-off Odoo container is
  removed (`docker rm -f`; a TERM to the client does not stop an Odoo
  running as PID 1) and the instance starts. One that fails or is cut still
  starts the instance, on the database partly updated, and raises
  `pitr_modules_not_updated` (a warning naming what to do); a restore that
  updates its database, or a rebuild whose module update ended well,
  resolves it, and a flow that skips the update or a move's deploy does not.
  The job log names the code the database is updated to, and the restore
  dialog says that undoing a deployment needs its code reverted first, or
  its migration runs again.
- **Settings is a menu of sections** (issue #3), one page each at
  `/cloud/settings/<section>`, grouped as Platform (General, Instances,
  Logs & retention, Network edge), Integrations (GitHub, Git providers,
  Monitoring, Agents (MCP), Assistant) and Limits (Rate limits); a module
  adds a section, or a block to another's, through the registries in
  `settings_registry.js`. **Search settings** atop the menu, and a Settings
  group in Ctrl K, find a field by its label, synonyms, block or help. The
  save bar shows only with unsaved changes, counts them, checks every field
  before sending and puts a server error under its field; leaving the page
  with changes asks first. A section sends only the fields changed since it
  loaded or saved, so it never puts back another's settings or a value a job
  wrote meanwhile. Older `?tab=` links land on their section.

### Fixed

- **An auto-approval rule without charges asks only for the calls that
  charge.** A tool can be declared `charges_per_call`: a
  `_charges_<name>` method of `incubacloud.mcp.tools` says whether a call
  charges an account, and a call it cannot judge does. Under a rule that
  does not include charges that call asks and counts no use; the rest of
  the tool's calls run as any other. The tool's listing and the grant's box
  follow it. Core declares none: a panel that bills (the hosted platform's)
  marks the tools whose calls do.
- **An instance saved from its form shows «Changes not deployed» at
  once.** `/cloud/save_instance` answers `config_dirty` and the form puts
  it on the instance, where the pill appeared only after a reload.
- **The labels of the assistant's provider, models, allowed models and
  users name their control.** A settings row takes a `control` id for a
  control whose slot draws an id of its own; the label's `for` pointed to
  an id that did not exist. A row whose control is a button (the models'
  «Read the models») takes `button`: its label describes the button
  through `aria-describedby` instead of naming it, so the button keeps the
  name its text gives.
  The stored secrets' «Replace» is such a button: its row takes `button`
  while the secret shows as stored, and names the input once replaced.
  Pressing «Replace» moves the focus to the input that opens, and
  «Keep stored» returns it to «Replace», so the focus no longer falls to
  the page when the button it was on goes away. A label that names no
  control no longer carries `for="null"`, nor every other label `id="null"`.
- **Connect as user lists the users of an Odoo 8.0 instance.** That
  version's `res_users` has no `share` column (base adds it from 9.0), so
  its internal users are taken from `base.group_user`.
- **A Settings save with a value refused keeps nothing of itself.**
  `save_general_settings` wrote each field in a savepoint of its own, so
  the page said the save failed while half of it was kept. The whole save
  now runs in one savepoint and a refusal undoes it, named under its field.
  An agent's `update_settings` sends only the values it gives.
- **A preview's update cut off holds the push.** The restore of a pull or
  merge request preview whose «Update changed modules» never answered (the
  connection dropped, the step ran out of time) counted as a failed update,
  so the rebuild of the next push passed its hold while `cron_guard.sh
  update` could still be running on the host, unlocked: two updates of the
  same database at once. It is kept apart now (`pr_update_cut_off`), the
  hold stays until someone dismisses the alert, and the request's comment or
  note still gives the reason and says the preview is rebuilt once the alert
  is dismissed.
- **A rebuild neutralizes a neutralized copy again.** The update of a
  staging's or a preview's rebuild rewrote the records its modules' data
  files leave unprotected, scheduled actions among them, on a database
  copied neutralized. Outside production, a rebuild that updates the modules
  now runs `restore.sh renew-neutralization` after the update: on a database
  that says it is neutralized (`database.is_neutralized`), Odoo's
  neutralization again (the panel's SQL before 16.0) and the mail catcher's
  step; any other is left as it is. A query that fails stops the rebuild
  rather than pass for «not neutralized».
- **The web terminals no longer lose what is typed.** The host shell and an
  instance's terminal read the output every 80 ms and sent the keystrokes in
  requests of their own: typing at a normal pace went over the per-address
  limit of the proxy in front of the panel (`ratelimit`, 300 a minute,
  bursts of 100), which answered 429, and the page dropped the input that
  failed: `ss -tln | grep ":22 "` reached the shell as `s-n| ep2`. The page
  now sends again a batch that fails, after the wait the proxy's
  `Retry-After` asks for, in order and numbered, so the session applies
  each batch once; while it waits, the status bar says how many bytes are
  waiting, and what never reaches the shell is said when the session ends.
  It reads the output again soon while output keeps coming and less often
  when nothing happens (100 ms to 1 s), and the answer to what is typed
  brings the echo that follows, waited for up to 200 ms on the server
  (`/input` takes `seq`, `stream` and `after_seq`; the session's `/output`
  takes `wait`, capped at 0.5 s); requests with input start at least
  200 ms apart, keys typed meanwhile going in the next. Typing makes about
  half the requests and an idle terminal about one a second. Both pages call one script,
  `terminal_client_script`.
- **The web terminals take the page's size.** A shell stayed at 80×24
  whatever the page measured, and long lines were drawn over each other:
  the session handed asyncssh's `change_terminal_size`, a plain method, to
  `run_coroutine_threadsafe`, which raised, so no resize ever reached the
  shell; and the panel took the error the session answered for a session
  gone, dropped its route and closed it. The size now reaches the shell
  when it connects, after the connection comes back and when the window
  changes, and an error the session answers is reported without closing
  it.
- **The relay's test mail.** A test the queue refuses, because one already
  waits, no longer counts among the hour's five; a deferral no cause names
  says the test mail was taken out of the relay's queue and to send another,
  instead of that the health probe watches it; and an instance's
  job list offers Cancel and Retry only on jobs whose type the reader may
  ask for (`may_control` in the job rows), so a consultant is no longer
  offered them on the test mail, a manager's type.
- **A pull or merge request closed while its preview was being created
  gets no «ready» comment and loses its preview.** An open and a close
  delivered at once left the close without a preview to close, and the
  restore announced it on a closed request. The job of the comment, or of
  the note, now asks GitHub or GitLab whether the request is still open
  before writing: one closed or merged has its preview closed and its
  delete queued, and its comment or note deleted. A read that fails
  writes as before.
- **A pull request closed and reopened at once gets a new preview.** The
  closed preview still being deleted counted as its own: the reopened
  request was left without one. It now takes `pr-<n>-2` while the old
  one holds its name, as a merge request does, and a push rebuilds only
  the open one. Two deliveries of one pull request at once leave one
  preview and no alert.
- **A merge request's push keeps the rebuilds it could queue.** When the
  rebuild of one of its previews could not be queued, the others were
  undone with it and waited for the daily retry. They stay queued now,
  the event keeps its error, and the retry queues only what is missing.
- **A restore of a backup that ends before its first step says so.** The
  job's log held only the exception; it now adds that the restore did not
  start, that the instance stays as it was and to try again or contact
  support.
- **A refused second exclusive job asks to cancel only who can.** The
  refusal of a job whose type lets one wait at most (the relay's test
  mail, a restore, a delete) says «cancel it first» to a reader who may
  cancel that type, and «wait for it to finish» to one who may not, a
  consultant on the relay's test mail.
- **A refund that fails keeps the refusal.** When giving back the hour's hit
  of a test mail the queue refused fails, the asker still reads the queue's
  refusal and the failure is logged.
- **The relay's test mail waits for the relay to start.** Right after a
  rebuild the relay's container may not accept connections yet, and «Send
  test mail» failed its job instead of giving a result (issue #51); and
  Amavis, the content filter every mail goes through, starts later than
  Postfix, so a test mail taken then was deferred with «connect to
  127.0.0.1[127.0.0.1]:10024: Connection refused» and raised the critical
  alert «smtp_relay_failing» with the content filter as its cause.
  `relay_test.sh` now reads the content filter Postfix hands mail to
  (`postconf -h content_filter`) and waits for its port to accept
  connections before the mail goes in; it tries again every 3 s, for up to
  90 s, while the container, the content filter or port 25 gives no answer
  (connection refused, no banner, the container not up), and then fails
  saying the relay is still starting and to try again in a minute, which
  the job shows as its error (exit status 75) without touching the relay's
  state or its alert. A relay that answers and refuses the mail is a result
  and is not tried again. The step's timeout grows from 150 s to 275 s.

---

## [1.0.224] — 2026-11-10

Agents' actions can be auto-approved per person and credential (issue #8);
a production's mail relay can sign in to Microsoft 365 with OAuth2, before
Microsoft turns SMTP AUTH with passwords off by default at the end of
December 2026 (issue #6); GitLab merge requests get previews; 19.0
downloads its repositories on the server too, as every other series has
since 1.0.222; and the user guide takes someone who never set up a server
to Odoo in production, or from another host. An instance's **Metrics** tab
is drawn by the panel instead of embedding a dashboard, and reads its
database and its Odoo log when asked. `incubacloud_assistant` 1.0.4 comes
with it.

The update adds two tables, `incubacloud_mcp_auto_approval` and
`incubacloud_mcp_credential`, and empty columns: `approval` and
`approval_rule_id` on `cloud_audit_log` and `cloud_job` (rows written
before it have no approval mark, which the panel does not show), and on
`cloud_instance` `pr_provider_id`, `pr_state`, `pr_installation_id`,
`pr_update_error`,
`smtp_relay_oauth_directory_id`, `smtp_relay_oauth_client_id`,
`smtp_relay_oauth_client_secret`, `smtp_relay_state`,
`smtp_relay_checked_at` and `smtp_relay_error`. It also adds
`cloud_instance.smtp_relay_auth`, filled with `password` for every
instance, the job type `relay_test_mail` and the alert policy of
`smtp_relay_failing`. No instance shows changes to apply: a relay with a
password renders as before. A 19.0 instance moves to downloading at its
next deploy or rebuild, which downloads all its repositories once; a
server keeps one tree of each commit of Odoo its instances are on, about
1.2 GB for 19.0.
The Metrics tab needs no change on the hosts: its charts read the series
the agents already send. The hardening recipe changes (fail2ban, below), so
every hardened host reads `host_hardening_outdated` at the next hourly
check until its hardening is re-applied (**Re-apply Hardening**); nothing
re-runs by itself.

### Added

- **Auto-approval rules.** An Administrator lets one credential of one user
  — an API key, an OAuth client, or the panel's assistant — run without
  asking until a date or with no end date (Settings → Agents, **Grant
  auto-approval…**). While the rule is active, every call of that credential
  that would wait for an approval runs at once, production included; every
  other agent keeps asking. The role, the routes' checks, the secrets and the
  limits stay. Granting shows a notice of what the panel stops protecting
  from, whose box must be ticked; the panel keeps who accepted which version
  of it, when and from where. A rule is granted only to a credential that
  already connected (`incubacloud.mcp.credential`) and, for an API key,
  still exists; the list and the notice name each one with its id and when
  it last connected, so two keys with the same name are told apart. A rule
  cannot be extended or changed, and ends when its date (if it has one)
  passes, when it is revoked (an Administrator any rule, a person their
  own, also from Connect an agent), when the user is archived or the API key
  deleted; a role that goes up pauses it.
  `cloud.settings._mcp_auto_approval_allowed()` (True, defined
  in `incubacloud`, so a layer that overrides it answers after it whatever
  the order the modules load in) pauses every rule and refuses new ones when
  a layer says False. A *Decline* or
  *Cancel* the person answers to a question asked before the rule existed
  still runs nothing.
- **The end date is optional.** The grant offers 7, 30 or 90 days, **No
  end date** or a date, none chosen beforehand, and recommends 90 days, the
  usual rotation period of a credential; there is no longest period. A rule
  with no end date (`expires_at` empty) lasts until it is revoked, its API
  key deleted or its person archived; its notice, its audit row, the rules'
  list, Connect an agent, the listing and the instructions say so.
- **Charges are left out unless the rule includes them.** A tool declares
  that it charges money (`ToolSpec.charges`); under a rule, such a tool
  still asks unless the Administrator ticked **Include charges** when
  granting it (`include_charges`, unticked by default; the box is offered
  only when some tool of the panel charges). The rules' list, the grant's
  audit row and the notice say whether a rule includes charges; the
  listing, the server's instructions and the assistant's rules say which
  tools still ask. No tool of this repository charges money.
- **The approval is marked** in the audit and on the jobs: `approval` is
  `person` or `auto`, with the rule. The audit tables show
  *· auto-approved* or *· approved*; the jobs' history and an instance's
  activity show *via agent … · auto-approved*; `get_audit_log`, `get_job`
  and `list_jobs` answer it. A call someone approved is audited even when
  the tool says it changes nothing, and a rule's **Activity** lists what it
  approved, with its grant and its end, also when a call another rule
  approved ended it.
- **What the agent is told is true.** With a rule, `tools/list` says which
  tools run without asking and until when, and the server's instructions say
  that no action waits for an approval.
- **The assistant (`incubacloud_assistant` 1.0.4)** is a credential of its
  own: with its rule, no card is shown, also after it read content others
  write; each action's line says *ran without asking*, and its rules say
  changes run without a card. A rule of a key never covers the assistant,
  nor the reverse. Uninstalling the assistant revokes its rules, which stay
  as history.
- **Routes** `/cloud/mcp/auto_approval/list`, `candidates`, `grant`,
  `revoke` and `activity`, excluded from the agents' catalog; each refuses a
  request that carries an agent's mark. Spanish translation included.
- **OAuth2 – Microsoft 365 for a production's relay.** Under *Networking*,
  **SMTP Authentication** chooses between **Password** and **OAuth2 –
  Microsoft 365**. With OAuth2 the relay sends through
  `smtp.office365.com`, port 587, with STARTTLS, as the mailbox the form
  calls **Sender mailbox**, signing in with an app registered in Entra ID:
  its **Directory ID**, **Application (client) ID** and **Client
  secret**. The secret is encrypted like a password, saved by a Developer,
  never shown again, readable only by the system, as a host's password,
  and not revealed by `/cloud/get_secret`; the password kept is not used
  meanwhile. A staging, which captures its mail, cannot choose it. The
  deploy runs the relay image `ghcr.io/incubacloud/relay-oauth`
  (`RELAY_OAUTH_IMAGE`, by digest, built on `SMTP_RELAY_IMAGE`'s
  docker-mailserver), ships the credentials as `.docker/smtp-oauth.json`
  (0600, mounted read-only) and recreates the relay when they change; a
  deploy or rebuild without OAuth2 removes that file. Odoo does not
  change: it keeps sending to the local relay. Spanish translation
  included.
- **Send test mail.** Under the relay's fields, a production with a relay
  sends one mail to the address of the person who asks, or of the manager
  who retries it (`/cloud/instance_relay_test_mail` and
  `cloud.instance.send_relay_test_mail`, five an hour per instance; with
  MCP, `send_relay_test_mail`); the job type itself is a manager's. The
  job ends with `delivered` and the server's answer; a temporary answer of
  no known cause leaves the verdict to the health probe. A mail that did
  not leave is removed from the queue, also when the job is cancelled
  while it waits, and one that did flushes it. The button is disabled,
  with its reason, while the instance is not deployed, is stopped or
  asleep, or has changes to apply; a test refused so does not count
  towards the five. Spanish translation included.
- **The panel says when a relay does not deliver.** The health probe reads
  a production relay's queue, its OAuth2 token and the last mail it
  delivered, and raises `smtp_relay_failing`, critical, after two probes
  in a row, with the cause and what to do: the server refusing the
  connection or not answering, its name not resolving, TLS, credentials or
  a token refused, SMTP AUTH disabled in Microsoft 365, a sender without
  Send As, Entra ID refusing the secret, the relay's Amavis not taking the
  mail, or mail waiting more than 30 minutes. A deferred mail counts once
  Postfix has tried it again (11 minutes), so a single passing deferral
  raises nothing. A server refusing one recipient is not one, and mail it
  refused does not hide the relay's own failures. Only a delivery resolves
  it: a mail delivered after the last failure the probe saw, or a test
  mail that went out. The relay's state shows under its fields. With a password, a refusal
  from Microsoft 365 says to switch to OAuth2. Spanish translation included.
- **Merge request previews on GitLab.** With **Preview instances for pull
  requests and merge requests** on in a project, opening or reopening a merge
  request on a GitLab repository creates a staging `mr-<number>` from each
  production whose own repository lines follow it, unpinned, on the
  production's host, with the merge request's branch and a neutralized copy
  of its data. New commits rebuild it; a new title, labels, reviewers or
  approvals do nothing; closing or merging deletes it, also while it is still
  being created, once the step running then ends. Closed and reopened at
  once, the merge request gets a new preview. It comes through the
  repository's webhook, with its merge request events; a group's webhook
  leaves alone a project with an active webhook of its own, so a merge
  request is never cloned twice. The project's list marks it **MR !number**.
  Once the preview is up, the panel writes a note on the merge request with
  its address, and takes it off when the merge request closes; the note needs
  a credential with the `api` scope (without one the preview works without
  its note), and a note GitLab did not take is tried again for about 20
  minutes before an alert, which the next note that goes through, or closing
  the merge request, resolves. Spanish translation included.
- **User guide: From zero to Odoo in production** (Get started), for someone
  who has never set up a server, a domain or a mail account for an
  application: the words it uses, which server for how many people, the
  backups, the project and the first deploy, the company's own domain and its
  DNS record, and the mail Odoo sends with its SPF and DKIM records.
- **User guide: Migrate from another host** (Migrations): what to check first
  (version, edition, own modules, size), the three ways to bring the project
  (a doodba project on a server, a Git repository, or none), the data, a test
  in a staging while the old Odoo keeps working, and the switch.
- **Order a VPS** says how the panel's catalog recommends a size for a number
  of people, and offers the managed backup storage when no backup destination
  exists yet.
- *From zero to Odoo* says to type a passphrase of one's own on the managed
  backup storage before the first deploy, since the generated one is never
  shown. *Migrate from another host* says to switch off the scheduled actions
  and the outgoing and incoming mail servers right after the test restore,
  before two Odoos send the same mail.
- *From zero to Odoo* sizes the server by Odoo's own formula from its
  deployment guide: (vCPU × 2) + 1 workers at most, about 325 MB of memory
  each after 2 GB kept for the system, the database and the proxy, and about
  6 users at once a worker. Its table gives vCPU and RAM for the usual sizes,
  and it says that users at once are not everyone with an account. An
  instance's default 2 workers serve about 12 users at once, and the guide
  says to raise its **Resources** and `workers` above that. *Order a VPS*
  names the question **How many people will use Odoo at once?**
- *From zero to Odoo*, on the mail Odoo sends, says that with Microsoft 365
  the mail relay can sign in with OAuth2 instead of a password, and links to
  how.
- **The Metrics tab of an instance as a diagnosis.** A card says in plain
  words which resource is short over the last 15 minutes — Odoo held back by
  its CPU quota, a server saturated by another instance, memory or disk
  pressure, slow requests that use neither CPU nor the database, a recent
  restart — and what to do. Below it, request time (p50, p95) and requests
  with server errors, CPU per container with its quota, time held back by
  the quota, memory per container with its limit, the server's pressure
  (CPU, memory, disk), CPU of every instance on the same server, and the
  instance's directory size; restarts and out-of-memory kills are marked on
  the time axis. Ranges of 1 h, 6 h, 24 h and 7 days; drag across a chart,
  with the mouse or a finger, to zoom every chart. Dates, numbers and units
  follow the user's language and time zone. Spanish translation included.
- **Queries now** (`/cloud/instance/db_activity`): the instance's database
  sessions, what they wait for, who blocks whom, connections against the
  maximum, cache hits, size and the tables with most dead rows, read from the
  host when the button is pressed and never stored.
- **Slow routes** (`/cloud/instance/slow_routes`): the last 15 or 60 minutes
  of the instance's Odoo log, added up into its slowest routes with their SQL
  and Python share, scheduled actions with their duration, requests stopped
  by a time limit and workers recycled for memory. An Odoo without workers
  logs the stack of a request it stops, and the reading shows its thread's
  URL and innermost frames; with workers, Odoo kills the worker without
  logging where it was, and the reading says so.
- The board and both readings are limited per user: 20 boards a minute,
  and the readings count with the log reads (*Log reads (per user)*). A
  reading takes 20 s at most, the SSH connection included; each chart is
  asked of the central once, within 8 s.
- **A pull request through another installation of the App is said aloud.**
  The panel acts on the pull requests of one installation of its GitHub
  App. One opened through an installation on another account, on a
  repository a production of the panel would preview, was ignored with a
  line in the server's log. It now raises the warning
  `github_pr_other_installation` on that production, keyed by the
  installation, naming the account and saying that no preview is created;
  choosing that installation under Settings → GitHub resolves it, as does
  dismissing it. A repository no production follows, and a fork, make no
  noise. Settings → GitHub says so under the connected App.

### Changed

- **19.0 downloads its repositories on the server.** Its deploys and
  rebuilds run *Fetch the repositories* and build the image with
  `AGGREGATE=false`; *Record repository heads* and *Trust the Git servers*
  are gone, and the image no longer carries Odoo's `.git`. The system
  parameter `incubacloud.repos_in_image_series` still sends it back to
  cloning in the image; its next deploy or rebuild then removes the
  downloaded trees.
- **A build leaves no copy of the repositories in Docker's build cache.**
  Docker kept the last build context of each instance, the trees included,
  until the daily maintenance: some 1.2 GB for each 19.0 instance built
  that day. On every series that downloads on the server, a deploy's and a
  rebuild's build now removes the contexts no build is using once it ends,
  whether it built or not. The cache of layers stays, and the next build
  of each instance sends its context whole again.
- **Recent requests** add requests per minute, posts to the login page by
  client address and the addresses probing for other software's files.
  `/cloud/instance/access_log` checks the record rules of the instance as
  well as the role.
- The instance's Metrics tab no longer embeds the Grafana instance
  dashboard; the **Monitoring** page keeps its dashboards for now.

### Fixed

- **A repository with several merges whose head moves while it downloads
  is not shared.** The commit each merge brought is not known then, so its
  tree stays the instance's own and the next deploy or rebuild fetches it
  again, instead of being kept on the server under the commits read before
  the download for other instances to take.
- **The project's list marks a pull request preview again** with **PR
  #number**: the badge was in the list, but the project's data did not
  carry the number.
- **A pull request closed while its preview is still being created deletes
  the preview** once the step running then ends. Before, the panel tried to
  delete the record at once, which the running job refused, and the preview
  stayed up.
- **A restored database is updated to the code of the instance it lands on.**
  A pull or merge request preview restored production's database over the
  request's code and started Odoo on it without updating the modules: what
  the request changed did not show until its first push rebuilt the
  preview, and a stored field it added had no column, so every screen that
  read it failed. The same held for a staging cloned or refreshed from
  production, an uploaded archive and a restored backup, a revive included.
  These restores now run the rebuild's own module update
  (`click-odoo-update` through `cron_guard.sh`) after the copy is
  neutralized and before Odoo starts, unless the instance's "Auto-update
  modules on rebuild" is off. A move does not: it is the same instance on a
  new host, built from the same code. In a preview, a staging or an upload
  the update runs after the database is loaded, where a stop of the restore
  is already refused. When it fails, Odoo is left stopped, the instance in
  error, and the job log says so under the update's own output.
- **A pull request preview's comment is written with the installation the
  pull request came through, and a comment GitHub refuses is seen.** The
  panel's GitHub App may be installed on more than one account. The comment
  that says a preview is ready, and its removal when the pull request
  closes, used the token of the installation the panel keeps; on a
  repository of another account GitHub refused it, the refusal went only to
  the server's log, and the preview ended ready with no comment and no
  alert. The preview now keeps the installation its pull request's event
  came through, and writes and deletes its comment with that one's token;
  GitHub issues it only for an installation of the panel's own App. The
  comment is written by a job of its own, as a merge request's note: GitHub
  unreachable or answering 5xx or 429 is tried again after 60, 300 and
  900 s, and then, or at once on another refusal, the warning
  `pr_preview_note_failed` goes on the production the preview was cloned
  from. A comment that cannot be deleted goes to the log, as before. A
  preview created before this version uses the panel's installation.
- **A preview whose update fails says so on its request, and the push of the
  fix brings it up.** The restore of a pull or merge request preview that
  failed in "Update changed modules" left Odoo stopped and the request
  without a word, and its failure, a step of the clone's chain, held the
  preview's queue against the rebuild of the push with the fix. Now the
  request's comment or note says the preview could not be updated and why
  (`pr_update_error` on the preview, the text it publishes); any push's
  rebuild passes that hold, and its success resolves the restore's alert
  and turns the comment or note into «ready». A rebuild that succeeds on a
  preview without its comment writes it. A pull request's comment carries
  the hidden marker of a merge request's note, so one written by a try
  whose answer never came is found and rewritten instead of written twice;
  a 401 is tried once more at once, GitHub's secondary rate limit later,
  and anything else that fails raises the alert instead of ending the job
  in silence. The alert is keyed by the pull request
  (`github_pr:<repo>:<number>`), so two pull requests of one production
  have one each, and the close of the pull request resolves it. A comment
  that cannot be deleted is a warning in the log, not an error.
- **What a restore leaves when it is cut off or updated.** A restore whose
  connection dropped, or whose step ran out of time, ended with no outcome:
  Odoo stopped and the instance with no error. A restore into an instance
  and a backup's restore now fail as one whose step failed, naming the step
  that never ended. A backup's restore refuses a stop from "Drop database"
  on, with a reason of its own: stopped there, the database is left
  missing, half loaded or half updated, and `psql` goes on inside the
  server. A neutralized copy is neutralized again after its update, which
  rewrites scheduled actions and mail servers its data files do not
  protect, and drops the list of paused crons (`incubacloud_cron_pause`) a
  dump taken during an update of the source carries, which the update
  would have switched on. The job log names the branches and pinned
  commits a restored database is updated to, and the backup's restore
  dialog says that its modules are updated to the code the instance runs
  now: to undo a deployment, revert its code first. A failed update says to
  dismiss the job's alert before the rebuild, and that a rebuild with
  "Auto-update modules on rebuild" off does not update.

### Security

- **fail2ban counts the failed SSH logins to accounts that exist.** In the
  `normal` mode Debian ships, a key login to root that fails logs only
  `Connection closed by authenticating user root … [preauth]`, which the
  stock filter does not count: on Debian 13 three such attempts gave no
  `Found`, and only made-up user names were ever banned. The sshd jail now
  runs in `aggressive` mode, which also counts disconnections before
  authentication, a scanner's `[preauth]` close and a malformed banner. Each
  attempt counts once: the `Invalid user …` and `User … not allowed because …`
  lines that precede the counted close are ignored (an
  `/etc/fail2ban/filter.d/sshd.local` on top of the stock filter), and
  OpenSSH 10's login-grace timeout (`Timeout before authentication for
  connection from …`), which the stock filter cannot read, counts too. The
  jail names `sshd-session`, the process OpenSSH 10 logs authentication as,
  in its `journalmatch` beside `ssh.service` and `sshd`. A login that
  succeeds never counts, and the addresses the firewall trusts, the panel
  included, stay exempt. Measured with fail2ban 1.1.0 against OpenSSH 10.0's
  lines: three attempts as root with a stranger's key, three with a made-up
  user, one with a user outside `AllowUsers` and one timeout count 3, 3, 1
  and 1; the panel's and a legitimate user's logins count 0.
- **A neutralized copy revokes its live auto-approval rules.** An API key
  is copied with the users: without it, an agent holding a credential of
  production would act on the copy without asking.
- **No Git credential in a 19.0 image either.** A 19.0 instance deployed
  or rebuilt with this version holds no token in its image, its build, its
  container or the `repos.yaml` on its server. One not rebuilt yet keeps the
  token it was built with until then.
- **No merge request preview from a fork.** Only a merge request whose
  branch is in the project it is opened on, the one the webhook belongs to,
  gets a preview; one from a fork gets a note saying why. A preview runs the
  branch with a copy of production's data. As for GitHub, the production's
  own repository lines decide, not the project's.
- **A GitHub pull request and a GitLab merge request of the same name and
  number stay apart**: GitHub's events never rebuild nor delete a merge
  request's preview.
- **Neutralizing a database now also clears the MCP module's signing
  secret**, its `/mcp` URL and the agents' issuer. The copy creates a key
  of its own on first use, takes its URL from `web.base.url`, and its
  agents connect with API keys until an issuer is set again.
- **Neutralizing a database now also gives it its own `database.secret`.**

---

## [1.0.223] — 2026-11-10

The update deletes the personal default values saved on the panel's
records; only an Administrator sets them from this version on. It adds two
empty columns, `cloud_github_app.owner_login` and
`incubacloud_assistant_message.card_arguments`. An App whose account is not
kept (one set up by hand, or created before this version) has its
installation taken by a manager's **Check installation**, never by the
webhook. `incubacloud_assistant` 1.0.3 comes with it.

### Security

- **Stricter permission checks** on repository reads, the import and its
  preview, and on creating instances, hosts and jobs, and on defaults and
  internal context keys sent by clients.
- **Below Project Manager, a repository line's URL cannot be changed**, nor
  the line moved to another project or instance; the editor shows the URL
  read-only, and `/cloud/get_config` answers
  `permissions.can_change_repo_url`.
- **A pull request preview only for the panel's own repositories.** A
  `pull_request` event counts only when GitHub sends it for the panel's
  installation of its App, whatever its action. A preview is cloned only
  from a production whose own repository lines follow the repository the
  pull request is opened on, compared in their canonical form; the
  project's lines, a template for new instances, do not decide. A pull
  request from a fork, or from a deleted one, gets no preview.
- **The App's installation is the panel's only on the account that created
  the App.** The setup keeps that account from GitHub's answer; any other
  installation is a manager's choice: **Check installation**, on the GitHub
  step of Settings, takes the only installation and names its account, or
  lists them when there are several, and closes the alert a deleted or
  suspended installation raised. Spanish translation included.
- **Agents ask before changing the code a production deploys from.** With
  MCP, `update_instance` asks when its `repos` give a production a
  repository, branch or pinned commit none of its lines holds, and the
  approval lists them; so does `apply_config` when it takes them from the
  project, and `freeze_repos` before it pins or releases the commit of a
  production's line. The same lines in another spelling, a GitHub
  repository's or a Git provider's, a line taken out and any change on a
  staging ask nothing. `harden_host` asks before it
  applies the hardening and the full setup it chains, with the words
  `run_host_action` uses for those jobs; checking what it would change
  asks nothing. Spanish translation included.
- **An assistant card shows every argument before the yes.** A card that
  cuts the call's arguments says where and how many characters follow, and
  **All arguments** under it shows them whole, masked as the card is.
  Spanish translation included.
- **The assistant asks before every change after reading what a lower role
  writes.** `get_project` and `list_jobs` mark the task when the project
  has a member who writes in it (Consultant or above) with a lower role
  than the person's. A message written while the assistant works leaves
  the mark when it enters the conversation; one written while nothing
  runs lifts it.

### Fixed

- **A Consultant or a Project Manager can create an instance in a project
  with repositories**; its repositories come from the project's. Below
  Project Manager, the new instance's lines name only the project's
  repositories, with the branch and modules the user chooses, and the form
  shows their URL read-only.
- **Selects and searchable lists near the bottom of the window open
  upwards.** The list of a select in the panel (the language, a
  credential's type), and of a searchable list (a repository's branch, the
  assistant's users), always opened below it and went off the screen when
  it was at the foot of the window. It now opens upwards when it does not
  fit below and there is more room above, and a long list scrolls inside
  it. An open list of several choices is placed again when typing or a pick
  changes the options it shows.

## [1.0.222] — 2026-11-10

Every series but 19.0 downloads its repositories on the server before the
image is built, instead of cloning them inside the build. 19.0 follows in a
later version. The update needs no data migration: an instance moves to the
new way at its next deploy or rebuild, which downloads all its repositories
once. The new checks under Security apply to values written from this
version on. `incubacloud_pitr` 1.0.12 comes with it; a host receives its
restore script with its next restore.

### Changed

- **The repositories are downloaded on the server.** A new step, *Fetch the
  repositories*, downloads each repository of `repos.yaml` with doodba's own
  `gitaggregate`, in a helper container (doodba's 19.0 image, pinned by
  digest), and the image is built with `AGGREGATE=false`. The steps *Record
  repository heads* and *Trust the Git servers* are gone from those series.
  Only a repository whose head moved is downloaded again; a repository with
  several merges is supported. The trees have no `.git`, nor does the image,
  which no longer carries Odoo's.
- **One tree per commit on each server.** The trees are kept in
  `~/.incubacloud/git-trees` and each instance's is made of hard links to
  one, so the instances of a server at the same commit of Odoo hold a single
  tree of it. The daily maintenance removes the trees no instance uses, and
  waits for the next night while a job is linking one. Two downloads of one
  instance never run at once: a new one stops the one an earlier job left.
- **The step has a fixed budget**, 1 200 s, whatever the number of
  repositories, with 600 s for each and one retry while time is left. The
  seconds it took count for the build's repetition, which is now decided on
  the download and the first build together.
- **A series can go back** to cloning in the image with the system parameter
  `incubacloud.repos_in_image_series` (series separated by commas), except
  8.0 to 10.0. Its next deploy or rebuild removes the downloaded trees.
- **The git helper image is kept** under a local tag with the label the
  daily prune spares, so it is not downloaded again every day. A full setup
  prepares it, and so does the daily maintenance on a server with an instance
  that downloads its repositories, so no job pays its download; a job that
  has to has its own limit of 300 s for it. When core moves to another digest,
  the tags of the old ones are removed.
- **The GitHub App is asked for its token only when a job needs it.** An
  instance with no repository on github.com besides Odoo's deploys and
  rebuilds without it, and an App not installed yet counts as no App: the
  personal access token is used, as before. When GitHub refuses the App's
  token, or fails to issue it twice (a server error or a timeout is asked
  again once), the job goes on without it and its log says why: a public
  repository needs no credential, and the personal access token says which
  repositories are private. An instance whose repositories are all public
  deploys as usual.
- **A refused save says why.** Creating or saving a project or an instance
  shows the reason the server gives, such as a value it does not accept,
  instead of only «Failed to save».

### Security

Upgrading is recommended.

- **Project names, the SMTP relay fields and the administrator email are
  validated** before they reach the deployment. A value saved by
  an earlier version is written out as it was saved. The relay user may be
  a Windows login (`DOMAIN\user`).
- **Project membership is checked on every change.** Instances, their
  repositories, domains and egress whitelist rows, and a project's
  repositories, are checked against the record rules whenever they are
  created or changed. A Project Manager or above, whom the rules give every
  project, is not limited.
- **Point-in-time recovery ignores server settings stored inside the
  restored copy**, and runs only the panel's own commands.
  `incubacloud_pitr` 1.0.12.
- **No Git credential in the image or the instance.** For the series that
  download on the server, a repository's credential travels only to the
  helper container that downloads it, as a header scoped to that
  repository's URL, and is removed with it: `repos.yaml` on the server, the
  build context, the image, its layers and the instance's container hold
  none. The staged list of URLs with their credentials is removed as soon as
  the step ends. The helper container runs without capabilities or new
  privileges.
- **Scoped GitHub App tokens.** A server is handed a token of the App limited
  to the instance's private repositories the App reads, with read access to
  their contents only, asked for each job. The installation's own token stays
  in the panel. The personal access token stands in for the App only where
  it does not stay in the image. When GitHub does not issue the App's own
  token, a series that downloads on the server hands the personal access
  token to the private repositories it reads, and the job's log names each;
  a series that clones in the image (19.0) refuses the job if a repository
  is private. When GitHub refuses the scoped token, a series that downloads
  on the server hands the personal access token if it reads every one of
  those repositories, with the log naming each, and refuses the job, naming
  the one it does not read, otherwise; a series that clones in the image
  refuses the job.
- **No token where none is needed.** A public repository gets no credential,
  and one neither the App nor the personal access token reads gets none
  either; the job's log names it. The decision is made per repository. Only
  GitHub's «not found», or a refusal that is not a rate limit, means a token
  does not read it: any other failure of its API is asked again once and then
  refuses the job, naming the repository. A repository GitHub says was
  renamed or moved refuses the job at once, asking for its new URL.
- **Access is checked on every job.** A tree another instance of the server
  downloaded is linked only when this instance's own credential still reads
  the repository: a branch's head is read with it, and a pinned commit's
  repository is asked with it first. A repository taken away from the App's
  installation, or a rotated token, stops the deploy at once.
- **Build records are cleared daily.** The daily maintenance removes Docker's
  record of past builds, which kept their output.

## [1.0.221] — 2026-11-09

GitLab: deploy from gitlab.com or a GitLab server of one's own, and a push
rebuilds. The update needs no data migration for it: the tables are new, the
new columns accept nulls, and the two new daily crons are given to the cron
bot.

The update also rewrites the record rules of `cloud.job.log.chunk` and adds
two rules on `ir.attachment`. A panel whose server has no `sshpass` now
refuses to create a host that logs in with a password, or to switch one to
it; hosts that already do are left as they are.

### Added

- **Settings → Git providers.** A manager adds a GitLab server (gitlab.com or
  one's own, https only, with its CA when a private one signs its certificate)
  and its credentials: personal, group, project or deploy tokens, each scoped to
  a namespace. A repository uses the token of the longest namespace that covers
  it and meets what the operation needs; a deploy token never reaches the API.
  The token is write-only and encrypted, audited without values, and travels
  only to its own server: the clone URL is rebuilt from the provider's base URL,
  always ending in `.git`. *Test connection* reads the repository with git and
  the token's scopes, expiry and user from GitLab's API, and says what failed
  in each. New routes: `/cloud/get_git_providers`, `/cloud/save_git_provider`,
  `/cloud/delete_git_provider` and `/cloud/test_git_provider`;
  `/cloud/get_config` answers `features.git_providers.network`.
- **GitLab repository lines.** A project's or an instance's line of a
  configured GitLab is stored canonical with its provider and path; a host with
  no provider is refused, naming it. The editor lists its branches and modules,
  reads its `requirements.txt` and pins its head with the panel's own git (no
  token in argv, no redirects, private HOME), counted against a new cap,
  *Git provider reads (per user, per hour)*, 120 by default. The line shows
  **GitLab · host**, and that merge request previews come in a next version.
- **Deploy and rebuild from GitLab.** `repos.yaml` carries each line's own
  credential; the host asks each line's head with its own token, from a helper
  container (`alpine/git`, pinned by digest and pulled when a host is set up)
  when the address is pinned; the build trusts the server's CA and refuses
  redirects (`build.d/060-incubacloud-git`). On 8.0 to 10.0, whose git sends a
  token in clear after a redirect, every repository is fetched on the host
  before the build, atomically and out of the instance's history, and the image
  is built with `AGGREGATE=false`: no token reaches it. The ISRG roots of Let's
  Encrypt are added to those images.
- **A daily check of tokens about to expire** raises an alert two weeks before.
- **The assistant reads GitLab repositories**: `repo_read`,
  `project_connect_repo` and `github_repo_info` take a GitLab address;
  `get_project` and `get_instance` give each line's `repo_capabilities`. The
  tools that write answer that GitLab comes in a next version.
- **GitLab webhooks.** `cloud.gitlab.hook` holds a webhook of the panel for a
  project or a group (`/cloud/gitlab/webhook/<hook_key>`, a 32-hex key, and an
  encrypted secret); `cloud.gitlab.event` records each delivery without its
  body or any e-mail address. The receiver counts the per-IP bucket at READ
  COMMITTED, refuses an unknown key or a wrong `X-Gitlab-Token` with 401
  before reading the body, and answers 200 to everything after: a repeated
  delivery (same `Idempotency-Key`, which GitLab's «Resend» keeps) or the same
  push (`push:<path>:<ref>:<before>:<after>`) is not dispatched again, and a
  dispatch that fails keeps its error for the daily cron (up to 3 attempts) or
  `/cloud/reprocess_git_event`, since GitLab never retries and counts errors
  towards disabling the webhook. A push rebuilds every instance whose GitLab
  line follows the branch, unfrozen, with `push_provider: 'gitlab'`.
- **Registering and verifying.** `/cloud/register_git_hook` creates the webhook
  in GitLab with the line's credential (`api`, Maintainer), adopts the panel's
  own when it is there, and otherwise says why it must be set up by hand;
  `/cloud/verify_git_hook` asks for a tag-push test and returns at once, one
  every 15 s; `/cloud/get_git_hooks`, `/cloud/reveal_git_hook_secret` (rotation
  rewrites GitLab's webhook with URL and token together) and
  `/cloud/delete_git_hook`. `repo_capabilities.push_rebuild` of a GitLab line
  follows its webhook's state.
- **The panel shows the webhooks.** Settings → Git providers lists them with
  their state, last delivery and failed deliveries: «Verify» asks GitLab for
  its test and reads `/cloud/get_git_hooks` every 1.5 s for up to 15 s until
  the delivery shows; managers show, rotate and delete them and reprocess a
  failed delivery. A saved GitLab line in the repository editor says what a
  push does and offers «Register webhook» (or why to set it up by hand), and
  the instance's «Auto-rebuild on push» counts its GitLab lines without a
  webhook that works. `get_git_hooks` gives managers the last five failed
  deliveries of each webhook.
- **Daily checks.** `cron_check_git_providers` also reads each panel's webhook
  in GitLab and raises `gitlab_hook_failing` when GitLab disabled it (it never
  sends a test nor re-enables it), and deletes the panel's webhooks no line has
  used for 7 days. `cron_purge_gitlab_events` deletes processed events after
  `gitlab_event_retention_days` (30). A project renamed in GitLab raises
  `gitlab_repo_moved` and rebuilds nothing.
- **The encryption key can come from a file or from Odoo's configuration
  file.** The panel reads the first of these that is set:
  `INCUBACLOUD_SECRET_KEY` in the environment, as before; the file
  `INCUBACLOUD_SECRET_KEY_FILE` names (a Docker secret, a systemd
  credential); the file `secret_key_file` names, or else `secret_key`, in an
  `[incubacloud]` section of odoo.conf, where Odoo.sh lets a key be set. Each
  takes the comma-separated list a key rotation uses. A place that is set but
  empty, unreadable or holding something other than a key fails, and the
  next one is not tried. At start the log says which place the key was read
  from, never the key. INSTALL.md lists the places and how safe a key in
  odoo.conf is. A key file must be an absolute path to a regular file of at
  most 64 KiB, and one every user of the server can read is read with a
  warning. While no key is usable, a process logs the error and reads the
  places again at most every 30 seconds, not on every encrypted field.

### Changed

- **Odoo 16.0 and 17.0 are offered again** for new projects and instances:
  their run of the series passed on every step the panel takes.
- **Without `sshpass`, hosts log in with a key.** Ansible needs `sshpass` to
  log in to a host with a password. On a panel whose server has none, the
  host form offers "SSH Key" only and says why, and the panel refuses a
  password host from the form, the API or the MCP tools with a message that
  names `sshpass`. A host that already logs in with a password keeps the
  option in its form, where a warning says the panel has no `sshpass`, and
  the Ansible jobs that reach it fail with a message saying so. Spanish
  translation included.
- Importing an instance from a host drops the user and token of each remote,
  and leaves out a remote of a host with no Git provider, naming it in the
  answer, instead of failing the whole import.
- A panel under the `public` network policy reaches only GitLab servers on a
  public address, pinned in the panel, on the host and in the build.
- A neutralized copy loses the Git tokens and the GitLab webhooks; the
  instance export drops any user and token of a URL and the downloaded trees.
- Every quota of a request counts with one function, `consume_quota`, in a
  transaction of its own at READ COMMITTED: the hourly caps of GitHub previews
  and imports and of git reads (`consume_hourly_quota`), and the assistant's
  tool calls and failed authentications, which had a copy each.
- **The rebuild loop of a push is shared** (`cloud.repo.event.mixin`):
  `_rebuild_job_type`, `_pr_preview_allowed` and `_rebuild_for_push` live on a
  mixin both event models inherit, and each instance's enqueue runs in a
  savepoint of its own, so a database error on one no longer aborts the
  others, GitHub's included.
- **A push already in a waiting rebuild is not added twice**:
  `coalesced_pushes` compares `(push_repo, push_branch, push_sha,
  push_provider)`; GitHub's pushes carry `push_provider: 'github'`, and a job
  queued before this version, without the key, counts as GitHub's.

### Security

Upgrading is recommended.

- **Server-only methods.** The methods that handle the GitHub credentials,
  and those of the scheduled actions of hosts and instances, run only inside
  the server: the panel and its scheduled actions call them as before, and so
  does "Run manually" in Settings → Technical. The connection test of the
  GitHub App's backend form is an Administrator's, as the panel's own test in
  Settings already was.
- **A job's raw output and files follow the role design.** The log of a job
  keeps the panel's own messages for every role that sees the job; its raw
  output (`stdout`/`stderr`) is for Developers and Administrators, who read
  logs. A Project Manager now reads the panel's messages of every job, and no
  longer the raw output. The files a job makes (backups, neutralized copies,
  exports) are readable only by Developers and Administrators, who make and
  download them; below that, the job list offers no download. Other
  attachments are unchanged.

## [1.0.220] — 2026-11-09

The update queues `push_edge_guard` on every active host with the panel's
proxy whose `edge_guard_shipped_hash` is empty, and a Check (`host_probe`) on
every active `Unsupported` host, which the old Check marked so for little
disk, and on every active host whose newest `missing_deps` was dismissed by
hand, which from this version keeps a host `Degraded` until a Check finds the
tools. A provenance entry of `pip_dependency_sources` can now carry
`rejected` and an empty `repo` (the operator's line); entries written before
keep working as they were. Nothing else changes in the database.

### Added

- **"Sync with project" has a "Select all" box.** Each section of the modal
  (Python dependencies, system dependencies, repositories) has one in its
  head, and a "Select all changes" box above them all when more than one
  section has rows. A box shows a dash when only some of its rows are ticked,
  ticks the rest on a click, and clears them when all are ticked. They leave
  the "differs" rows alone: each is a choice of a winner, and ticking all of
  them would decide every one. Each box is named with its section's title for
  a screen reader, and works from the keyboard. Spanish translation included.

### Changed

- **"Sync with project" follows the panel's colours.** The package names,
  versions and URLs set their own colour and monospace face, the panel's
  primary text on a faint tint with no ligatures, where they took the colour
  of the Bootstrap theme underneath (pink by default, dark green in the
  website theme, 1.9:1 on the dark panel) and drew `==` as one bar and `>=` as
  `≥`. The "instance only" label is 4.5:1 or more in both themes (it was 2.6:1
  in the light one), and the secondary text, the push and pull directions and
  the "(instance)" and "(project)" marks, is at full strength: at 70% opacity
  it was 2.9:1 in the light theme and 3.6:1 in the dark.

- **The primary button is the theme's dark green in the light theme.** Its
  white label now reads at 5.3:1 (it was 3.3:1, under the 4.5:1 that WCAG AA
  asks of text), and hovering darkens it further. The dark theme already
  passed and keeps its colours.

- **The panel no longer asks for Ansible collections.** The host playbooks
  use only `ansible.builtin`, which comes with `ansible-core`:
  `incubacloud/ansible/requirements.yml` is gone, and INSTALL.md no longer
  runs `ansible-galaxy`.

### Fixed

- **Cancelling the agents' install no longer counts as a failed attempt.** A
  person who stopped an `install_observability` job while it ran read
  "Cancelled while it ran." followed by "Attempt 1 failed; the reconciliation
  cron will retry automatically.", and the host's count of failed installs,
  which raises the `observability_agents_failed` alert at the third, went up
  by one. The stop now leaves the host's agent state and count as they were
  and the log says so. No other executor counts attempts in `on_failure`.
- **A request for a sourcemap no longer raises `instance_error_logs`.** Odoo
  logs as ERROR `.map should have been generated through debug assets` when a
  browser with developer tools open, or a crawler, asks for the sourcemap of
  an asset that was not built in debug mode: a failure of neither the instance
  nor its owner, and the alert had to be dismissed by hand each time. That
  record joins the known ones left out before the errors are grouped; any
  other ERROR of the same logger still counts.
- **A job the server's code does not know yet waits for newer code instead of
  failing.** A rebuild updates a panel's database with `click-odoo-update` in
  a container of the new image while the old server keeps serving, and
  restarts it only afterwards. The update to 1.0.214 queued `push_edge_guard`,
  and on a panel updated so the old server's runner took it within the second
  and failed it for good with `No executor registered for 'push_edge_guard'`:
  nothing queued it again, the host stayed without the edge guard and the
  `job_failed` alert stayed open. `cloud.job.execute` now puts such a job back
  in the queue every 30 s without spending its retries, for 10 minutes from
  its dispatch, and only then fails, saying the server's code is older than
  its database. Nothing runs on the host meanwhile. Takes effect from the
  server that runs 1.0.220 on: a server with older code still fails such a job
  at once.
- **The hosts 1.0.214 left without the edge guard get it.** The update queues
  `push_edge_guard` again on every host that should carry it and never had it
  written. Its success resolves the `job_failed` alert the first one left.
- **A Check that measured the host ends well and gives its verdict.** Under 10
  GB free the Check failed its job, raised `job_failed` and marked the host
  `Unsupported`, though its instances ran on; and since the job failed, it did
  not queue the edge guard it found missing. Now a host short of disk is
  `Degraded` with the warning `host_low_disk` (the free space, and that no new
  instance is placed there and a build may run short), one that does not run
  Linux is `Unsupported` with the critical alert `host_unsupported`, and the
  edge guard is queued whatever the verdict. The job fails only when the Check
  could not measure: the host unreachable, the playbook stopped, a disk
  reading that is not a number. A Check or a Full Setup that finds the host
  well resolves both alerts.
- **Full Setup on a disk under 10 GB leaves the host `Degraded`.** It still
  fails with `setup_failed`, but raises `host_low_disk` as the Check does and
  no longer marks the host `Unsupported`, which is kept for a host that does
  not run Linux or could not be read.
- **The disk readings no longer undo the Check's `Degraded`.** Every five
  minutes, and right after the Check, a disk under 95% used put a degraded
  host back to `Compatible`, missing tools included. They now restore only
  what they degraded: not while `host_low_disk` is active, or was dismissed by
  hand and the disk is still under 10 GB free, nor while `missing_deps` is
  active, or was dismissed by hand until a Check or a Full Setup finds the
  tools. Dismissing an alert frees no disk and installs nothing. They resolve
  `host_low_disk` themselves from 11 GB free, one more than the Check asks
  for, a dismissed one included: a reading is rounded and may be older than
  the Check, and the margin keeps a disk hovering at 10 GB from closing and
  reopening the alert. They do so too on a disk over 95% used that still has
  the room.
- **A deploy or a rebuild empties Docker's build cache under 10 GB free, not
  8.** The figure under which the Check marks the host `Degraded`.
- **Two pip lines that can both hold are no conflict.** The re-sync before a
  deploy or rebuild marked any two different lines of one package as a
  conflict: an Odoo 18 instance of the panel, with core 18.0 pinning
  `requests==2.34.2` and OCA/queue 18.0 asking for `requests`, failed every
  rebuild on it. Lines are compared with `packaging` (`pip_spec_relation`): a
  line the stored one already meets is skipped and logged («… is already met
  by …»), a stricter line that meets the stored one replaces it when the
  stored line is a bare name, and only two different pins, a pin outside the
  other's range, ranges neither of which contains the other, a URL on either
  side, different markers or extras, or a stricter line against a version or
  range the operator or another repository wrote still ask a person: a pin
  taking `foo<3` would hand the line to its repository, whose next bump to
  `foo==3.0` would go in unasked. A stored line with blanks or a trailing
  comment is found and replaced (its comment kept) or marked; before, the text
  kept it while the job log said it was updated. The project and instance
  editors, which merge a repository's `requirements.txt` in the browser, spare
  the bare name the same way, only against a name with version clauses (not a
  marker, a URL, an option or a continuation), and find a commented line too.
  `packaging` is in `requirements.txt`.
- **A resolved pip conflict stays resolved.** The answer is remembered on the
  package's provenance entry (`rejected`): the side not chosen is skipped at
  the next re-sync («… was turned down when a conflict was resolved»),
  whichever repository proposes it again, until someone edits the line by
  hand. Before, keeping the stored line dropped its entry, the repository that
  wrote it adopted it again, the other repository raised the same conflict and
  the job failed again; siding with the repository's line ended the same way
  the other way round. Keeping the stored line keeps its owner: a repository's
  line stays that repository's, an unowned one becomes the operator's, as does
  a third line typed in the dialog, so no repository adopts it and an upstream
  change to it is asked. Removing the markers by hand («Edit Manually») and
  leaving one of the two lines, once and with no piece of a marker left, is
  the same answer; a package still in its markers keeps its entry when another
  line is edited. A line becomes acceptable again when the package's line is
  edited by hand, or when its owner moves to it.
- **Resolving a pip conflict closes the failure it caused.** A warning
  `pip_conflict` lets its failed job raise `job_failed` too, which kept
  holding the instance's queue, and stayed in the alerts panel, once the
  conflict was answered. Closing a conflict alert that names a job now closes
  that job's `job_failed`, with the same resolution: from the dialog, or when
  the next deploy finds the list clean.
- **«Edit Manually» in the pip conflict dialog takes you to the list.** It
  carried a class no stylesheet defines and drew as the browser's own button;
  it is the panel's secondary button now. Clicked, it navigated to the
  instance's Overview under the alerts panel, which stayed open over it, so it
  seemed to do nothing, and a project's conflict had no button. It now closes
  the dialog and the panel and opens the Dependencies tab of the instance, or
  of the project, with the package's lines selected and a line above the list
  saying what to leave; the panel closes once the page has moved. Opening one
  project from another's page now reloads the page.

## [1.0.219] — 2026-11-08

The update creates the `cloud_job_no_return` table and, with `incubacloud_pitr`
1.0.11, turns `failure_holds_queue` off for `pitr_enable`; nothing else changes
in the database. No instance shows changes to apply for the SMTP relay's pin:
each takes it at its next deploy, rebuild or move.

### Added

- **The panel says at startup when Odoo's limits cut its jobs short.** A
  server that runs `queue_job`'s runner logs a warning per setting and raises
  the provider alert `job_server_limits_too_low` when `limit_time_real` is
  below 3600 seconds, the longest a job's command may run, and, with
  `workers = 0`, when `limit_memory_soft` or `limit_memory_hard` is at Odoo's
  per-worker default or lower: in a threaded server they measure the whole
  process, and past `limit_time_real` or `limit_memory_soft` Odoo reloads it
  with every running job (measured on Odoo 19). The alert closes at the next
  start with values that fit. Only a server started by `odoo server` is
  judged, and among those not a web-only one (`--load=web`): `odoo shell`,
  `click-odoo-update` and scripts read the configuration file, whose
  `limit_time_real` is the web's beside a dedicated runner. Reported by a
  self-hosted panel whose deploys ended `JobFoundDead` under Odoo's defaults
  (issue #4).

### Fixed

- **An instance's CPUs fit the server it is deployed on.** Docker refuses a
  container whose `cpus` is above the daemon's count of CPUs, and by then
  `docker compose up -d` had recreated `db`: a limit of 2.5 CPUs applied on
  a server of 2 left the database `Created`, never started, and the panel
  down. Every deploy and rebuild, and a layer's own deploys, now write each
  service's `cpus` as at most the host's CPUs, read from `docker info` when
  the job uploads its files (15 s at most), with the host's measured
  `cpu_cores` as the fallback and no limit when neither is known. The
  instance's fields, the host's accounting and the configuration's hash keep
  the value set; each limit is a line in the job's log, and the end hooks
  hand it to `_report_cpu_clamps` for a layer to tell its operator. A
  rebuild whose failure a layer put back as it was can say so
  (`_failure_leaves_error`) and leave the instance's status alone, and a
  layer can give the refusal to stop a job of its own type its own reason
  (`cloud.job._no_return_refusal`). See *CPUs an instance is deployed with*
  in `docs/architecture.md`.
- **Cancelling a running job stops what it runs on the server.** The stop
  cancelled the job's task and closed the connection, which signals nothing: the
  step in progress went on on the host until it next wrote to its output, or to
  its end — a silent boot test copying and booting for as long as it took, the
  update of the live database through. The transport now sends the command TERM
  (`process.terminate()`), waits up to 60 s for its trap to put things back, with
  what the trap writes still reaching the job's log, and stops the job. OpenSSH
  delivers the signal to the session's whole process group, so a script and its
  foreground children end together, with or without a trap; `docker compose run`
  passes it on to its container. Measured on a test host on 8-nov-2026.
  Not covered: what a `docker exec` runs inside a container, which ends on its
  own; root sessions on OpenSSH 8.9 and servers older than 7.9, which ignore the
  signal; and a worker that dies.
- **A rebuild cannot be cancelled once it updates the instance's database.**
  «Update changed modules» stores each module as it goes, so a stop there left
  the database half updated under the image that runs, and the rebuild keeps no
  copy to go back to. The stop and that step are decided under one advisory lock
  of the job: a stop asked for before it ends the job without starting it; once
  it started, cancelling is refused with a message saying why, in that attempt,
  in any later one and between two of them (the job back in the queue): the
  database it updated stays ahead whichever attempt updated it. The mark goes
  once the job ends done or cancelled. Code that is about to discard the
  instance (a layer's cleanup) can force the stop with the
  `ic_stop_past_no_return` context key; a person cannot. A deploy has no such
  step: a new instance stopped halfway is removed. The warning a rebuild that
  ended unapplied writes now says when its database was already updated, by
  this attempt or an earlier one.
- **A restore cannot be cancelled once it loads the database.** «Restore
  database» drops the instance's database (`--force`) before it loads the dump:
  with the TERM, a stop there left it empty or half loaded, where before the
  load ran on to its end in its container. The step is the restore's point of
  no return, under the same lock and mark as the rebuild's update: a stop asked
  for before it ends the job without touching the database; from it on,
  cancelling is refused with a message saying why. A move's restore has none:
  it loads a copy on the new host that a rollback removes, the source
  untouched.
- **A deploy tries a failed image build once more.** The deploy had no build
  step: the first `docker compose run` or `up` that found the image missing
  built it, never twice, so a passing failure at GitHub while the image cloned
  its repositories (`curl 55`, `100-repos-aggregate`) failed the whole deploy,
  in «Start instance» for a move (QA, 8-nov-2026). A new step, «Build Odoo
  image», runs `docker compose build odoo` between «Pull images» and «Start
  database», cached and without a pull as before, and stops the deploy if it
  fails; a failed build waits 60 s and runs once more, as the rebuild's has
  since 1-nov-2026. A first attempt that ran 900 s or more is not repeated, in
  the deploy or the rebuild: the job runner kills a job at 3600 s, and that
  kill leaves the job orphaned and the instance in `deploying`. Staging and
  moves' deploys, and those a layer runs, get the step too.
- **An addon conflict is named once in its alert,** where a build repeated
  once printed it, and the alert listed it, twice.
- **An agent's parallel calls no longer fail on their quota.** The counters
  of the MCP tools (the user's terminal and host-console buckets, the user's
  MCP bucket) and of the authentication failures counted on a cursor at
  Odoo's REPEATABLE READ: a call that met a row another call had just created
  or committed was aborted with «could not serialize access due to concurrent
  update», logged at ERROR, and the tool call failed. They now run at READ
  COMMITTED (`read_committed_cursor`), as do the GitHub preview and import
  quotas, which had the same pattern: the second call waits for the row and
  adds to it, and the atomic upsert hands each call its own count, so parallel
  calls never pass the cap. No new row lock.
- **A failed switch on of continuous recovery no longer leaves the database
  keeping its log** (`incubacloud_pitr` 1.0.11). The rebuild before
  `pitr_enable` starts the database with `archive_mode` on and no archive
  command, which keeps every WAL segment until a command works: a download of
  wal-g that GitHub answered with 503s (QA, 8-nov-2026), or an upload test that
  failed, left it so with nothing to undo it, and the job's failure held the
  instance's queue, so the retry its alert promised waited behind it with the
  base copies and the backups. A new first step, «Settle archiving»
  (`pitr.sh settle`, no network), parks archiving with `archive_command =
  '/bin/true'` and a reload, never a restart, unless the cluster the panel has
  base copies of archives with its own command; a switch on that fails or is
  cancelled before archiving works asks the host to settle again on a
  connection of its own. Parked, it is off with a warning and retried in a
  day; kept, the archiving it had goes on, and the alert says that a new
  objective was not applied and that retrying the job applies it; a host that
  does not answer leaves a critical alert, also for a cancelled job, as the
  database may be keeping its log. `settle` leaves the agent as it is, and
  neither it nor switching off stops a database it started while Odoo runs:
  that one was restarting, not stopped. A failed `pitr_enable` no longer
  holds the queue. The state no longer reads «off» when a new objective failed
  to apply while the previous archiving went on. Switching off now sets
  `/bin/true` before anything else, so a cut halfway, or a rebuild that drops
  the mount first, no longer leaves a command that cannot run. The wal-g
  download retries for up to 15 minutes, its wait doubling from 1 s, instead
  of six times 10 s apart. Not covered: a rebuild of that chain that fails
  after starting the database, or a `pitr_enable` cancelled while it waits,
  runs no hook of the module (documented in `docs/architecture.md`).
- **An import no longer puts code on a series it was not written for.** The
  GitHub importer read a series the panel does not offer as no series, so a
  project imported from an Odoo.sh or simple repository on 17.0 or older was
  created on the default series, 20.0, without a word; so was one whose
  repository names no series, and a server's instance whose
  `.copier-answers.yml` names none outside a project. The importer now reads
  every series the panel knows (8.0 to 20.0), and both imports (Import from
  GitHub, Import Instance, and the MCP's `import_project` and
  `import_host_instance`) stop before creating anything when the series is one
  the user may not choose, or when none is found, naming the series found and
  the ones the panel offers. An Odoo administrator may still import a series
  not offered, as the project form lets him. A branch or a
  `.copier-answers.yml` of any `X.0` is read as that series, so one the panel
  has no card for (7.0, 21.0) is said not to be offered, rather than not
  found; a manifest names a series only by a version of at least four parts
  (`19.0.1.0.0`), so an add-on's `"12.0"` is not read as the series 12.0; and
  an `odoo_version` in `.copier-answers.yml` that reads as no series is told,
  not dropped with the rest of the answers.
- **Import Instance leaves no half-made project.** A domain another instance
  holds, or a failure creating the instance, answered with an error after the
  project and its repository lines were created, and the request kept them.
  The domains are checked before anything is created, and the project, its
  lines and the instance are made or none is.
- Spanish: «salva» reads «copia de seguridad», as in the rest of the panel.
- **A production instance's SMTP relay runs docker-mailserver 16.0.1, not
  whatever `latest` is.** The copier answers say `smtp_relay_version: latest`
  and every deploy pulls, so since docker-mailserver v17.0.0 became `latest`
  on 2026-11-05 each deploy, rebuild or move started v17 while the instances
  deployed before kept the release they had pulled. The deploy override now
  names `ghcr.io/docker-mailserver/docker-mailserver:16.0.1`, a release already
  proven in production, next to `ENABLE_UPDATE_CHECK=0`. The answers are
  unchanged, so the configuration hash of every instance is too. A staging's mail
  catcher keeps its image. v17's breaking changes (LDAP, Rspamd's Redis to
  Valkey, Dovecot's Xapian) do not reach a relay with `SMTP_ONLY=1`, and its
  relay script is the same as 16.0.1's: an instance already on v17 goes back
  to 16.0.1 at its next deploy, its queue kept in the mail state volume.

## [1.0.217] — 2026-11-08

The update creates the `cloud_job_outcome` table; nothing else changes in the
database.

### Fixed

- **A job whose success was saved is not run again.** When the store of a job's
  `done` lost a serialization race after its `on_success` had committed, or the
  worker died in between, queue_job ran the whole job a second time: a move's
  restore looked for the archive its first success had removed and the move was
  rolled back; rebuilds built and restarted twice. The success hook now leaves a
  mark (`cloud.job.outcome`) on its own cursor; the next attempt finds it, runs
  nothing, writes back what the lost transaction held on the job and arms the
  unlink a delete or a purge left for its commit. A stop that arrives after the
  mark, while the store of `done` loses its race, no longer ends the job
  `cancelled` and cuts its chain: it is stored `done`. The reconciliation of a
  lost worker closes a job with the mark `done`, without its abort or stop hook.
- **The store of `done` writes only the job's rows.** The instance's state probe,
  the success's mail, Telegram, webhook and bus event, and the layers' end hooks
  wait for its commit, on cursors of their own: a store that does not commit no
  longer announces a success, nor announces it twice, and queuing the probe no
  longer makes the store fail. A callback after the commit that raises is logged.
- **The probe of running panels no longer fails a job keyed on the instance.**
  It locked the instance's row `FOR UPDATE` before writing it, and any job whose
  transaction began before the probe committed failed to store a row keyed on
  that instance (a move failed so on 7-nov-2026). It locks `FOR NO KEY UPDATE`,
  as do the direct-name probe on the settings row and the assistant on its
  conversation. A test fails the build on any new `FOR UPDATE` not marked as
  needed.

### Added

- `tests/idempotency_harness.py`: runs a job type once, twice and cut off at each
  call to a host, and compares the result; `delete_instance` and
  `restore_instance` are specified, and every other type is listed with the
  reason it is not yet.

## [1.0.216] — 2026-11-08

The update needs no migration.

### Fixed

- **The metrics and Docker prune crons run again.** They pick the hosts the
  panel can reach by their SSH credentials, which 1.0.215 keeps for system
  administrators, and they run as the cron bot, which is not one: every pass
  failed with *Access Denied by ACLs* on `cloud.host.key_file`. The hosts are
  now chosen elevated and the jobs queued as the bot, as before; the
  credentials stay for system administrators only.

## [1.0.215] — 2026-11-07

The update needs no migration, and has none for an instance on 7.0 or
PostgreSQL 9.6, which the panel no longer knows, nor for a series from 8.0 to
15.0 on a PostgreSQL below 12, where they no longer run.
`incubacloud_connect` 1.0.3 reaches an existing instance with its next
rebuild.

A module that extends the panel and reads a host's `key_file` must read it
elevated from this version on.

### Added

- **The panel generates a host's SSH key.** A new host with Login Type *SSH
  Key* offers *Generate a key for me*, selected by default, or *Upload my own
  private key* as before. The panel's server makes an Ed25519 pair as the form
  opens (`/cloud/generate_host_key`), so its public half can be pasted into the
  provider's form before the server, and its address, exist. The private half
  reaches the browser only sealed with the panel's secret key and is stored in
  `key_file` when the host is created, as an uploaded key is.
- **A host's public key stays on its Connection tab,** derived from the stored
  key (generated or uploaded), with copy buttons for the key and for a command
  that adds it to a server's `~/.ssh/authorized_keys`. `/cloud/get_host`, and
  so the MCP's `get_host`, answer it as `ssh_public_key`; the private key is
  never answered. The line always ends with the comment `incubacloud`, not
  the uploaded key's own, and the command quotes it. Once the generated key
  is copied, leaving the new-host form asks first.
- CI compiles what a deploy ships of `incubacloud_connect` with Python 2.7 and
  3.5, in their official images (`connect-old-python`).

### Changed

- **A series runs with every service of the panel or is not offered** (7-nov-2026). **7.0 is retired**: its click-odoo can neither dump, restore nor
  update its database, so its card is gone from `_odoo_versions.py` and no
  picker, field or tool names it. **PostgreSQL 9.6 is gone** too: doodba's
  template renders no backup service for it. **No series runs below
  PostgreSQL 12**, the first that continuous recovery serves: 8.0 to 10.0, for
  which the template allows 9.6 alone, run on 12 alone, 11.0 and 12.0 on 12 to
  13, with 13 by default, and 13.0 to 15.0 on 12 up to 16 or 17, as they had,
  without the 10 and 11 that the template also allows them. 8.0 to 12.0 were
  each checked on 12: its database created, its modules installed and updated,
  copied and restored with click-odoo and with the backup service, its login
  served, and recovered to a point in time; 13.0 to 15.0 are tried on 12 by the
  run of the series.
  Copier would refuse 12 for 8.0 to 10.0, so the copier run of their deploy and
  rebuild reads a settings file whose defaults give the template's
  `pg_per_odoo` that range (`postgres_beyond_template`, `IC_COPIER_SETTINGS`):
  an answer would not reach the replay that `copier update` starts with.
  The rebuild's boot test no longer turns replication on nor asks
  `pg_basebackup` for `--no-sync`, which only 9.6 lacked.

### Fixed

- **8.0 to 12.0 deploy again** (QA of the series, 7-nov-2026). The image of 8.0
  to 10.0, on Debian 8, reads apt from Freexian's Extended LTS of jessie
  (`debian_archive` on their cards), whose keyring the build checks against a
  fixed sha256, so the packages of an `apt.txt` install with their signatures
  checked; archive.debian.org's keys have expired, and apt refused them.
  Instances get `incubacloud_connect` without its tests or bytecode, which
  Python 2.7 and 3.5 refused to compile; a deploy or a rebuild replaces the
  copy an instance had instead of copying over it. The checksums are stored
  with a plain `click-odoo-update` where it has no `--only-compute-hashes`.
- **Connect as user works on 8.0 to 12.0** (`incubacloud_connect` 1.0.3): the
  module imports on Python 2.7 and 3.5 and with the `openerp` package of 8.0
  and 9.0, which read its manifest from `__openerp__.py`. On 8.0, which checks
  the session's password on every call and has no session token, the session
  holds a signed password of its own, valid a week, for that user and database
  only, and ended by a change of the user's password or login; the module
  accepts it in `res.users.check_credentials`, Odoo 8's place for other ways to
  authenticate, and signs it with a key in Odoo's data directory.
- **Installing `incubacloud_connect` on a restored database works on every
  series.** A restore, a staging and a rebuild run Odoo headless with the
  option its series' card names (`headless`): `--no-http` from 11.0,
  `--no-xmlrpc` before. `restore.sh ensure-connect` takes it as an argument
  instead of reading `odoo --help`, which from 19.0 lists no server options.

### Security

- **A host's SSH credentials are for system administrators only.**
  `cloud.host.key_file` and `password` were readable by a Developer or a
  Manager through Odoo's generic endpoints (`/web/dataset/call_kw` `read` and
  `search_read`, export). Both now carry `base.group_system`. The panel reads
  them elevated where it connects (`_get_ssh_private_key_bytes`,
  `_get_ssh_password`), `ssh_connect_kwargs` is no longer callable over RPC,
  and `create`/`write` store them elevated once the manager check passed. The
  host page's button that reveals the SSH password is unchanged.
- **A host's default certificate key is for system administrators only.**
  `cloud.host.tls_default_key` carried no group, so every role down to a
  Stakeholder read the private key of the certificate the host's proxy serves
  through the same endpoints. It now carries `base.group_system`; the panel
  reads it elevated (`_effective_tls_default`, the pair's check, the host
  page's `has_tls_default_key`) and `write` stores it, with the certificate
  it pairs with, elevated once the manager check passed.
- **A user's Telegram bot token and webhook signing secret are for system
  administrators only.** `res.users` is readable by every internal user, and
  `cloud_telegram_bot_token` and `cloud_webhook_secret` carried no group, so a
  Stakeholder read a Manager's in clear through the same endpoints. Both now
  carry `base.group_system`. Their owner still sets them in the notification
  preferences, which store them elevated and only say whether each is set,
  and the senders of jobs and alerts, and the preferences' Telegram buttons,
  read them elevated (`res.users._get_cloud_telegram_bot_token`,
  `_get_cloud_webhook_secret`).
- **A host's Traefik dashboard password is for system administrators only.**
  `cloud.host.traefik_panel_password` carried the Developer group, so a
  Developer read it through the same endpoints while `/cloud/get_secret` only
  reveals it to a Manager. It now carries `base.group_system`, as `password`
  does: Full Setup reads it elevated (`_shipped_inverseproxy_yaml`), the host
  page's `has_traefik_panel_password` too, and `create`/`write` store it
  elevated once the manager check passed. A Manager still reveals it with
  `/cloud/get_secret`.
- **The metrics central's credentials are for system administrators only.**
  `cloud.settings.metrics_remote_write_token`, `grafana_admin_password`,
  `metrics_operator_token` and `metrics_delete_auth_key` carried no group, so
  every Manager read them through the same endpoints, the deletion key that
  only the gateway presents among them. They now carry `base.group_system`;
  the panel already read and minted them elevated (`_get_system`), and the
  Settings page only says whether the account's token is set.
- **A generated key opens only as one, and for a day.** What is sealed starts
  with `host-genkey:v1:` and `/cloud/create_host` refuses any other value this
  panel encrypted, or one sealed more than 24 hours earlier.

## [1.0.214] — 2026-11-07

The update queues `push_edge_guard` once per active host whose proxy the
panel set up, except those the `_edge_guard_leaves_alone()` hook names, which
a module that extends the panel can override; it runs no Full Setup.

### Added

- **Odoo's `/website/info` is refused at the edge.** Every host's proxy reads
  `~/traefik/dynamic/ic_guard.yml`, a router on `https` at priority 2000 that
  answers 403 for the page, with a trailing path and behind a language code
  (`/es/website/info`), before any instance sees it. Full Setup writes it before
  the proxy starts and fails if it cannot; `push_edge_guard` writes it to a host
  already set up, without restarting the proxy; the host Check queues that job
  when the file is missing or differs. A proxy that does not mount the dynamic
  directory gets a warning until a Full Setup.
- **An instance can allow the page:** *Allow Odoo's public website info page*
  (`website_info_public`, Networking tab, MCP `update_instance`), off by default
  and not copied to a staging; a rebuild applies it.

## [1.0.201] — 2026-10-28

Nothing changes for the direct path until a host is given a direct port and
the central a direct name; the update re-applies no agent and marks no host
pending.

### Added

- **Agents connect to the panel (`incubacloud_mcp` 1.0.0, a new module).**
  `POST /mcp` speaks the Model Context Protocol (2026-07-28, and 2025-03-26
  to 2025-11-25 through `initialize` and a signed session id) and lets AI
  agents operate the panel as the user who connected them, with that user's
  role and project rules. Credentials: OAuth access tokens of the configured
  issuer (RS256 `at+jwt`, audience the panel's MCP address; RFC 9728 document
  published), or Odoo API keys where the panel allows them; never the cookie,
  never the superuser. Fifty-eight tools call the panel's own routes through
  the routing map, so overrides apply; every `/cloud/*` route is a tool or
  excluded with its reason, and a test fails on one left out. Destructive and
  security-sensitive actions wait for the user's approval — a form in the
  client, the panel's approval page, or a link — bound to the user and to the
  agent's credential, ten minutes, once; it protects from the model's
  mistakes and from injected instructions, not from a malicious client.
  Secrets never go in or out: keys named like an encrypted field are refused
  and dropped, and every known secret is masked in what agents read. Answers
  keep to 64 KB (and 1 KB more for a page's paging keys): logs, mail and
  lists come in pages and a command's output is cut, marked as untrusted
  data. `run_command` and `run_host_command` run one command with
  the terminals' controls, a minute at most (`timeout` inside the container;
  what it leaves in the background is not stopped). `download_backup` hands
  out a single-use link for 15 minutes. Audit rows and jobs say which agent
  (`via_client`). *Connect an agent* in the user menu; the agents' issuer, the
  MCP address and their limit in Settings. See `docs/mcp.md` and the user
  guide (`docs/user/docs/agents/`).
- **`incubacloud_mcp_pitr` 1.0.0**: the continuous recovery tools
  (`get_pitr`, `configure_pitr`, `pitr_restore`), installed where both modules
  are.
- **A direct entrypoint per host** (`cloud.host.direct_port`, applied by
  Full Setup): a port of its own for the panel's own traffic, with a Let's
  Encrypt certificate by DNS-01 (`acme_dns_cloudflare_token`, a root-only
  file on the host), no trust in forwarded headers, a per-source rate limit
  and an allowlist in nftables (`ic_direct_v4`) and in Traefik
  (`direct-peers`). Full Setup checks the port is free first and never
  leaves the proxy down.
- **`sync_direct_path`**: applies the list without restarting Traefik, reads it back,
  and probes the direct names the host publishes; queued when a host comes or goes and
  at least hourly. Its button shows only on a host that publishes a direct port
  (`cloud.host._offered_actions()`, which a layer can extend to drop an action from
  the hosts it does not apply to).
- **The central's direct name** (`metrics_central_direct_host`), published by the
  central's deployment on the direct entrypoint, with a probe state (`direct_path_down`,
  `direct_certificate_expiring`).
- **Per-host agent URL**: each host's agents are offered the direct name, try it from
  the host and fall back to the remote-write URL (`direct_path_unreachable_from_host`);
  the reconciliation re-applies them when the path changes, and re-applies a host gone
  quiet.
- **An AI assistant inside the panel (`incubacloud_assistant` 1.0.0, a new module).**
  A button in the top bar opens a chat on the side of any page; Settings →
  Assistant (managers) sets the AI provider — Anthropic, or OpenAI, Google Gemini
  and OpenRouter in beta, at fixed addresses — with the customer's own key
  (encrypted, write-only, tried before it is kept), the models allowed among the
  provider's own list (Recommended or Untested), the users who may use it, an
  optional monthly limit in approximate euros, the same for each user, and a cap of
  steps per task. With a limit, a model whose price is unknown cannot be used. The
  four providers are called over HTTP, the Anthropic API version pinned; each
  request names only the allowed model, with no fallback, and an answer from
  another model is set aside and audited. Switching it on needs a manager to
  accept section 12A (AI features) of the Terms of Service, each time; the panel
  keeps who, when and from where. A label next to the message field and a notice
  before the first message say that it is an AI and where the messages go.
  It answers from the user documentation and the panel's public code (core's
  repository only, from a closed list), reads logs and jobs, operates the panel and
  writes Odoo modules on a branch of the project's repository, tested on a staging,
  with a pull request. Its tools are the MCP's, run in the server as the user —
  same routes, role checks, secret filter and audit (*via agent* `Assistant
  (conversation N)`) — plus the documentation and code searches. Every change
  other than trying code on a staging waits for a confirmation card in the chat,
  which only the user's browser answers, and so does anything that reaches
  production, creating instances, changing a project's repositories and writing
  to a branch that existed before; after the task read content others can write,
  every change asks, the assistant's own branch included. Each task runs as queue
  jobs (channel `root.assistant`; give it a capacity line in `[queue_job]
  channels`, or its jobs share root's), one step at a time, and pauses while a
  deploy runs. Conversations are kept 30 days after their last activity and read
  only by their owner, who can still read and delete them while the assistant is
  off. Usage is counted per user and month. See `docs/assistant.md` and the user
  guide (`docs/user/docs/assistant/`).
- **Code tools for agents (`incubacloud_mcp`)**: `repo_read`, `repo_create_branch`,
  `repo_write_files`, `repo_open_pull_request`, `repo_create` and
  `project_connect_repo`, for developers, on repositories of the user's projects
  and instances only. Reads go through the GitHub App, the personal access token,
  then anonymously; writes through the App and, when GitHub
  refuses it, the token (the App the panel creates only reads, so the token
  writes). A new branch needs no approval; writing to a branch that
  existed before does, and the approval says when production deploys from it; so
  do anything in a public repository and a pull request that makes a preview of
  production.
  Never under `.git/` or `.github/`, never a panel secret in a file. Creating a
  repository needs the personal access token; connecting one keeps the project's
  other repositories.

### Dependencies

- **`joserfc` 1.6.8 is a core requirement** (`requirements.txt`): agents'
  tokens are validated with it. A self-hosted install rebuilds its image.

### Changed

- **A host key is trusted only if it is the one approved.**
  `/cloud/trust_host_key` takes an optional `expected_fingerprint`: the
  key is read and compared before any credential is sent, and the login
  that follows accepts that key only. A server presenting any other key
  is refused (`{ok: false}`), the refusal is logged and audited (`SSH host
  key refused`), and nothing is stored; an empty fingerprint approves
  nothing. `cloud.host._peek_known_host_key()` reports the key a server
  presents without logging in or trusting it. Agents use both (see
  `incubacloud_mcp`): the person approving sees the old and the new
  fingerprint, and what is stored is what they approved.
- **A webhook secret or a Telegram bot token is 1024 characters at most.**
  `/cloud/save_user_preferences` refuses a longer one (`{ok: false}`,
  saying the limit) and stores nothing. Every stored secret is masked in
  what agents read (`incubacloud_mcp`), at a cost that grows with its
  length: a real secret fits well within this, and a huge one would weigh
  on every call.
- The DNS token travels as a file named in `CF_DNS_API_TOKEN_FILE`, never in the compose
  file.
- The dates of the last probe of each direct name and of the last good read of the
  central are system parameters (`incubacloud.direct_checked_at.*`), not columns of the
  settings row, read and written with SQL: `set_param` would empty every worker's
  caches at each probe and each pass of the specs cron. A neutralized copy forgets them,
  the DNS token and the central's direct name.
- Changing the remote-write URL in Settings now re-applies the agents of every installed
  host at the next reconciliation (every 15 minutes): each host's recorded URL is
  compared with the one it should have. Before, hosts kept the old URL until their
  agents were applied again by hand.
- Settings shows *Save changes* only on a tab with something to save: not on one whose
  sections failed to load (they show Retry), nor on a read-only one. A section drawn by
  a module names its tab when it registers
  (`env.registerSettingsSave(key, save, state, writeOnly, tab)`); one that names none
  keeps Save on every tab, as before.
- The host's action bar holds two groups, what is done with the host and its upkeep
  (ending with the trash), and breaks between them when it does not fit on one line.
  Buttons a module appends inside `ic-action-bar` sit between the two. The host key, the
  shell and the trash form one unit of the upkeep group, with the separators between them,
  so a separator never ends a line when the group wraps.
- The probe of a direct name takes 403 or 421 as the port's refusal of `/flags`: from
  Traefik 2.11.47, a connection negotiated with the `direct` TLS options gets 421 at
  `direct-deny` before its list runs, and hosts pull different 2.11 releases. A check of
  `direct_probe.sh` may name several statuses (`GET:/flags:403,421`); 200 or any other
  answer still fails it.

---

## [1.0.200] — 2026-10-27

A module that draws its own Settings tabs into the panel's template must
anchor on `openedTabs` from this version on: an anchor on the old `t-if`
breaks Settings. `$state-warning-text`, which a module's stylesheets may
use, exists only from this version on.

### Added

- **A host said to refuse direct traffic is checked, hourly, and so is
  its hardening.** `block_direct_access` closes the proxy at the next
  Full Setup or proxy push, but the firewall only at the next hardening
  run, which nothing starts by itself: a host whose posture changed after
  its hardening, or whose box was ticked by hand, said "closed" while
  its origin answered anybody, and no host had taken the hardening
  changes made since 15 October. `cron_check_host_posture`
  (`cloud_host_posture.py`) now raises `host_origin_open` when the
  panel, connecting straight to a host behind a CDN that refuses direct
  traffic, is let in on 443 or 80 for a name the host serves (confirmed
  over two runs; a dropped connection counts as closed only once the
  same name answers through the CDN, so a panel whose own way out drops
  everything reads no host as closed), and `host_hardening_outdated`
  when the last successful hardening of a host — now recorded in
  `hardening_recipe`, a fingerprint of the playbook without its
  comments, and `hardening_cdn_only` — is an older recipe or another
  posture than its settings ask for. Both say who can act and how;
  neither re-runs anything, and both clear themselves. Every host
  hardened before this version reads as outdated until its hardening is
  re-applied. The 1.0.200 post-migrate hands the new cron to the cron
  bot.

- **A panel can list an alert without announcing it.**
  `cloud.alert._silent_alert_codes()` names the codes that reach nobody
  — not the mail, not the external channels, not the daily digest — and
  still show in the Alerts list, the bell and the host's card. Core
  silences nothing; a module that extends the panel lists its own codes
  there. `cloud.alert.for_provider` is declared in core, which never
  sets it: a module's record rule on it can be in the database
  while core loads alone (its tests, a migration), and a rule on a field
  the model lacks fails every read of every alert.

### Changed

- **The panel, the GitHub setup page and the user guide load no font
  from Google.** The panel's three families (Space Grotesk, Hanken
  Grotesk, JetBrains Mono) are served by the module from `static/fonts/`
  with their licences, the GitHub setup page uses them too instead of
  Inter, and a visitor's browser no longer contacts
  `fonts.googleapis.com` or `fonts.gstatic.com`. The user guide builds
  with mkdocs' `privacy` plugin, which downloads its fonts and external
  files at build time, and no longer names a `repo_url`, whose script
  asked `api.github.com` for the repository's stars from every visitor's
  browser.

### Fixed

- **What is typed on a Settings tab survives a visit to another tab,
  and Save sends it from any tab.** The Rates tab (and any tab a module
  adds) is a component with its own form and a save handler it
  registers with Settings; drawn with `t-if`, it was unmounted on
  leaving, so its edits were lost — back on it the stored values
  showed — and its save handler went with it: Save pressed on another
  tab never sent them. Such a tab now stays mounted once opened, hidden
  while another one shows (`openedTabs`).
- **An unsaved edit on the Rates tab, or on a tab a module adds, is an
  unsaved change.** Settings compared only its own form, so these tabs showed
  no "Unsaved changes" in the footer, and leaving Settings, or the
  page, did not ask. A tab now registers its state
  (`registerSettingsSave(key, save, state, writeOnly)`), and Settings
  counts its form as changed until it is saved — footer, leave dialog
  and `beforeunload` alike, as for the General, GitHub and Monitoring
  forms. It registers once mounted: a tab Settings cancels by drawing
  again while it loads never mounts, so it cannot stand in for the one
  on screen. And only if it loaded: a tab that failed to load shows the
  error and Retry instead of a form of defaults, and is never sent;
  once a retry loads it, it registers.
- **Save sends only the tabs that changed, and what it sent is what is
  saved.** It called the save of every registered tab, so an untouched
  form — a module's whole tab — went back to the server, which can act
  on it. A tab is now sent only while its form differs from what was
  loaded or last saved; a tab that registers no state is sent every
  time, as before. The reference copy, of a tab as of Settings' own
  form, is what was sent, taken before the request, with the
  write-only fields the tab empties once saved (`writeOnly`, the
  secrets) empty: an edit made while the request is out stays unsaved. A
  token of spaces alone is no token: it is emptied on save like one,
  instead of staying a change.
- **Amber text reaches WCAG AA in the light theme.** Amber was both a
  mark and the colour of some thirty text rules — pills, badges,
  warnings, the staging label, the job history's active chips, the
  warning toast — which measured 2.8 to 4.0:1 in `#b7791f` and
  `#9a6614`. Text in amber now uses `--rl-amber-ink` (`$state-warning-text`):
  `--rl-amber-text` in the light theme, `#865602` (4.6:1 on its 20%
  tint, its worst case), and the accent itself in the dark theme, as
  before. `--rl-amber` stays the mark's colour — dots, bars, borders,
  icons — darkened only to reach 3:1 (`#b27418`; `#b7791f` measured
  2.9–3.0:1 on its own tint and on the gauge's track), with
  `--rl-amber-rgb` and `--rl-amber-soft` following it. Text on solid
  amber is dark, `--rl-amber-fg` `#1c1408` (white reaches 3.9:1 on it),
  for a module's filter chips to use; a near-black label on amber goes
  from `#06140d` to `#1c1408`. The dark theme's colours are otherwise
  unchanged.
- **A move whose removal of the old copy fails no longer holds the
  instance's queue.** `move_cleanup_source` runs after the cutover, on
  the source host, when the instance already lives on the destination;
  its failure held the instance's queue (`failure_holds_queue`), so its
  scheduled backups and every other job waited until someone retried it
  or dismissed the alert. The type no longer holds it: the failure is
  still announced, its `job_failed` alert stays, and it can be retried.
  The data file says `False` rather than dropping the field, since an
  update writes only the fields listed; no migration is needed, the
  record is not `noupdate`. A move that fails before the cutover is
  unchanged: the step that failed holds the queue, and the cleanup ends
  cancelled without running.
- **A retry runs with the credential the failed job held.**
  `cloud.job.retry_job` copied `payload` and left `secret_payload`
  behind, so the retry of a restore from a URL that needs a login
  downloaded without it. The retry now takes it too, encrypted at rest
  and out of the payload as on the original, so neither the lists nor
  the log show it. A module that copied it in its own `retry_job` no
  longer needs to.
- **A restore to a moment tells the addons, as every other restore does.**
  `PitrRestoreExecutor` recorded the new timeline and queued its base copy,
  but never called `_after_restore_hook`: an addon that acts on a replaced
  database did not hear of it. A module that hands an instance new secrets
  after a restore left one brought back to a moment before its last deploy
  with a secret that module no longer accepted. The hook is now called with
  a new `kind`, `pitr`, once the restore has ended with the database
  restored — also when its files were not, which the job reports as a
  failure — and not while the next job still follows it on the host.
  `incubacloud_pitr`'s own hook does nothing for `pitr`: the restore's job
  already took the full base copy of its new timeline, and the hook's would
  have run beside it. A restore that outlived the jobs following it was
  recorded by the watch (`pitr_status`), which took no base copy of it and
  told nobody: whichever records a restore first, its job or the watch, now
  takes that copy and calls the hook, and the other repeats neither
  (`_pitr_record_restore` says whether it recorded it). A restore whose new
  timeline `pitr.sh` could not read after the promotion cannot be recorded;
  its job still takes the copy and calls the hook, and the watch does
  neither. `incubacloud_pitr` 1.0.8.
- **A rebuild no longer fails because continuous recovery wrote its spool.**
  `pitr.sh install` puts wal-g, the host's libraries it runs with and the
  pitr agent's spool in `.walg/`, inside the copier project directory, which
  is a git repository, and nothing kept it out: `rebuild.sh commit-dirty`
  committed it — wal-g and libc on the first rebuild, the marks that changed
  on every one since — while the agent keeps writing it: a mark per uploaded
  segment, its removal an hour later, `spool/fs-ok` after each filestore
  copy. One of those landing between the commit and `copier update`'s own
  check made copier refuse the tree ("Destination repository is dirty"): the
  rebuild ran its other steps and failed at the end, and a job type that
  holds the queue held the instance's queue with it (seen in QA).
  `commit-dirty` now keeps `.walg/` out as it keeps `logs/` out, before it
  stages anything, so the same rebuild is clean: `ic_git_exclude`, which
  `ic_git_exclude_logs` now calls, writes `/.walg/` to `.git/info/exclude`
  and untracks what earlier rebuilds committed, leaving every file on disk.
  Every `copier update` goes through it — rebuilds, template upgrades and
  those a layer runs.
- **A change made to the project after the rebuild's commit is named.**
  copier only says the tree is dirty. `rebuild.sh copier-update` now
  looks first and stops with the paths that changed (up to ten): what
  the product writes on its own is kept out of the repository, so what
  it lists was changed by someone else.
- **A delete whose second purge of the continuous recovery archive fails
  no longer reports the instance removed.** The purge after the agent
  and the database stop ended in `true`, so what arrived between the two
  purges stayed in the bucket after the delete. It now stops the delete,
  as the first purge and the duplicity one do, and starts again what it
  stopped, so the instance stays running for the delete to be retried.
  The stop no longer fails, unseen, on a stack whose agent was switched
  off, and a composition it cannot read stops the step before anything
  is stopped or purged. `incubacloud_pitr` 1.0.8.
- **With no base copy left, the recovery window is empty and a copy is
  due at once.** A listing that found no base copy left the oldest and
  the last base where they were, so after a purge the panel still
  offered a window that no longer existed and asked for a new base only
  hours later. An empty listing now clears both; a listing that failed
  leaves them as they were. `incubacloud_pitr` 1.0.8.
- **A move's rollback no longer tears down the instance it was meant to
  bring back.** When a move fails before its cutover, the rollback
  starts the instance again on its source and queues
  `move_rollback_cleanup` for the target, to remove the half-built copy
  there (`docker compose down -v` and `rm -rf` of its directory). Since
  1.0.198 the queue sends a lone job, when its turn comes, to the host
  its instance lives on, so that a move made while it waited is followed
  (`_entry_verdict`). The cleanup was such a job, and the instance still
  lived on its source: it ran there and removed the live stack, its
  database volume and its directory, and the instance had to be restored
  from its backup. A job aimed at a host on purpose now
  says so (`_pinned_to_host`) and keeps that host, in its retries too:
  the rollback's cleanup, the cutover, the removal of the old copy after
  a move, and the upload keys' jobs. Before it connects, the cleanup
  refuses a host the instance lives on, a host that is not the target of
  the move being rolled back, and any rollback but the one that queued
  it (the payload names its chain); it reads the instance again after
  each wait, and once more before it removes anything. The removal of
  the old copy refuses the host the instance lives on or is moving to,
  and a cutover refuses a move that is being rolled back or is over. A
  refused job fails without reaching a host; the watchdog ends the
  rollback and says the target may need cleaning by hand. Before
  deploying, check that no instance has a rollback in progress
  (`move_rollback_in_progress`): a cleanup queued by an older release
  names no chain and is refused.

- **The Retry of a move's step no longer acts on the wrong host.** A
  retry is a lone job, and it followed the instance. Retried while the
  move went on or was being rolled back, a step aimed at the target ran
  on the source, by then the only live copy — a restore put back the
  dump taken before the rollback over the live database, a stop stopped
  the live odoo — while the steps after it had already ended "Not run".
  Retried once the move was over, a step acted on wherever the instance
  lived then. The Retry of a move's step is now refused while a move of
  the instance is in progress or being rolled back, and once it is over
  unless the step is aimed at its host on purpose (the removal of the
  old copy), which keeps that host (`_check_retry_of_a_move_step`). A
  retry is judged by the job it retries, followed back to the first.

- **The Retry of a move's step queued before 23 October is refused
  too.** A move's step is told by the chain it belongs to, which has a
  cutover: `queue_chain_key`, which steps queued before 23-oct-2026 lack
  (they were a queue_job graph). Such a step was taken for a lone job
  and its Retry followed the instance to the host it lives on now, where
  a stop stops the live odoo. `_is_a_move_step` also follows the
  `graph_uuid` of the step's queue.job when it has no chain key.

- **Every role cancels a dispatched job.** A job dispatched but not
  started was cancelled through its queue.job with the caller's rights,
  which no role of the panel has: Cancel answered "not allowed to access
  Queue Job", and a manager's move rollback left that step pending,
  silently. The queue.job is now cancelled with `sudo`, after the role
  gate and the write check.

- **Consultants and project managers create stagings again, and every
  role opens the forms that choose a backup backend.** A new staging
  takes the host's egress whitelist as its own list, and below Developer
  the host's entries are not readable: creating a staging failed for a
  consultant or a project manager, and so did deploying one. The host's
  list is read with `sudo`; only its hostnames are taken.
  `/cloud/get_backup_backends` answered managers alone, so the forms of
  a new instance, a new project and a project's settings failed to open
  below one: below a manager it now names the backends and nothing else,
  as the project page does, and their bucket, path and endpoint stay a
  manager's (SEC-009). The Backups tab is offered to whoever may manage
  backups instead of answering everybody else with the server's refusal,
  and the project page shows its delete buttons to whoever may delete a
  project (`can_delete_project`).

- **Whoever adds a domain or a repository can remove it.** A consultant
  adds an instance's domains, a developer its repositories and a project
  manager the project's, but removing one was a manager's (`unlink`): a
  save of the form that dropped one failed. Each of those roles now
  removes what it adds, in the projects it is a member of.

- **An archived host is never chosen, and never revived onto.**
  `select_best_host` left `active` to `active_test`, so a caller running
  with it off — a script, or code reached from an archived record —
  could place an instance on an archived host; it now asks for `active`
  itself. `revive` deployed onto the archived instance's host even when
  that host had been archived since; it now refuses, naming the host,
  and asks for another, as its dialog does.

- **An import that no host could take says so.** With automatic
  assignment on and no host to give it, the GitHub import created the
  instance without one and reported success; it still does, and the
  dialog now says the instance has no host and needs one before
  deploying.

- **A delete's purge of the backups no longer recreates the database
  under a live instance.** `backup_purge.sh` runs the instance's backup
  container with `docker compose run`, which starts the service's
  dependencies and recreates every one whose definition changed since
  the stack started: on a live instance whose compose file had been
  rewritten, its `db` was recreated under its workers, and stayed so
  when the purge then failed. The purge runs with `--no-deps`: it talks
  to the bucket alone, with `backup.env`.

- **An upload key works, and the restore dialog shows what arrived.**
  The key's `authorized_keys` line was written between double quotes,
  and the shell split it at the quotes the line holds: sshd accepted
  none of the pieces, and no upload through a key could start (28
  October). It is written as one quoted word, and the revocation also
  removes the pieces an earlier install left. The command the dialog
  shows sends the file as `restore.zip`, a path inside the key's
  directory; it named an absolute path that did not exist there. And the
  check of what arrived is a job, which may wait in the queue: the
  dialog read the grant at once and showed the previous check's file,
  one the uploader may have replaced since. It now shows the check's
  answer once its job has ended, and closing the dialog abandons the
  wait.

- **The visitor's address behind the CDN is the visitor's.**
  `CF-Connecting-IP` was believed only on a connection from Cloudflare,
  which never happens with Traefik in between, so a panel behind the CDN
  recorded Cloudflare's edges (sessions, device log, login lock-out). It is
  now believed when the connection to Odoo is Traefik's — named by the new
  `x_real_ip_proxy` option, which the panel's deploy writes into an
  instance's configuration (`inverseproxy-proxy-1.`) and a self-hosted panel
  sets in its own — and Traefik was reached from a public address in the
  CDN's ranges; any other caller has its `X-Forwarded-For` replaced by its
  own address, so nothing else on Odoo's network can choose the address it
  is recorded as. Private ranges in the CDN's list are ignored. If the
  proxy's name stops resolving, its last address is kept for five minutes; a
  name that never resolved, or past that, falls back to taking any private
  address for the proxy rather than recording every visitor as the proxy,
  and is asked again every five seconds. Not resolving is a warning at
  start-up — the job runner, off Traefik's network, logs only that — and an
  error when proxied traffic finds the fallback in use (RB-25). A
  self-hosted panel needs its image and its configuration together, then
  odoo and its job runner restarted; an instance the panel deploys gets the
  option with its next deploy or rebuild.

- **A failed hardening is one alert.** It left `hardening_failed`
  (critical, "check the job log") and `job_failed` (what failed) side by
  side for the same run. `hardening_failed` now
  says what failed, and `job_failed` is not raised while it is open: an
  executor names its own failure alert (`_failure_alert_code`), honoured
  by `_create_job_failed_alert` for a job type whose failure does not
  hold its queue, as it already was for a critical conflict alert.

- **Hardening takes an SSH allowlist with an IPv6 address in it.** The play
  put the whole list in one IPv4 set (`ic_ssh_allow`), and nft refuses an
  IPv6 address there: a host whose `allowed_ssh_ips` held the IPv6 address
  of the operator's browser stopped at the ruleset check (nothing applied;
  its firewall stayed as it was). The list is now split by family: IPv6 goes
  to `ic_ssh_allow6`, with its own `ip6 saddr` rule; a family with no
  address keeps its set, empty; and the catch-all the panel sends when it
  knows no operator address (`0.0.0.0/0`) opens IPv6 too (`::/0`).
  `ssh_allowlist.sh` takes IPv6 addresses and splits the list the same way;
  on a host hardened before this version, its first change adds the IPv6 set
  and rule, live and in `/etc/nftables.conf`. `allowed_ssh_ips` refuses an
  entry that is no address or network, and keeps each one in a single form
  (an address compressed, alone also when written as a network of one; an
  IPv4-mapped address as its IPv4), since the script compares entries as
  text. The firewall CI job parses an IPv6 list and a mixed one with every
  supported OS's nft.

- **The hardening dialog no longer runs a count into the words after
  it** ("interrupt 2running instances", "This host runs 2production
  instances"), and its production gate is one sentence to translate:
  "Type" was a term of its own, the noun of every type column, and
  Spanish read "Tipo <host> para confirmar:".

- **A proxy push refreshes the firewall's IPv4 ranges on a host without
  the IPv6 set.** The IPv6 lines went in the same nft document, and nft
  refuses a whole document that names a set the host lacks (one hardened
  while the ranges had no IPv6). They now join it only where the set
  exists; elsewhere the step says the IPv6 ranges wait for the next
  hardening. Where the set exists and the ranges no longer have IPv6, it
  is emptied.

### Security

- **A job's credential is the server's alone.** `cloud.job.secret_payload`
  had no `groups`, unlike the secrets of instances, hosts and backup
  backends: any member of a project, a stakeholder included, reads its
  instances' jobs, and `read(["secret_payload"])` returned it decrypted
  — the password of a restore from a URL a developer had typed. It is now
  `groups="base.group_system"`: asked for through `read`, `search_read`
  or the record, it is refused, and it no longer appears in
  `fields_get`. The server reads it with `sudo`: the restore's download
  (`_restore_url_credential`), which runs as the person who asked for
  it, and `retry_job`. The panel's lists never read the field.
- **A rebuild no longer commits the project's secrets.** Every rebuild
  commits the copier project before `copier update` (`git add -A`,
  since the first release), and that took in `odoo/custom/ssh/`, which
  doodba copies into the image as root's `~/.ssh` and where a deploy
  key goes; `odoo/custom/src/repos.yaml`, with the repository tokens
  the panel writes into its URLs (a GitHub App installation token, or
  the personal access token when the App cannot read the repository);
  and `odoo/custom/conf.d/*.conf` and `odoo/custom/dependencies/pip.txt`,
  whose contents are the user's — an `admin_passwd`, a database
  password, a token in a git URL. Committed, they stay in the
  repository's history on the host whatever later rotates the file.
  `commit-dirty` now keeps the four out as it keeps `logs/` and
  `.walg/` out, before it stages anything (`ic_git_exclude` takes a
  file or a pattern as well as a directory), and takes out of the index
  what earlier rebuilds committed, forcibly (`--cached` never touches
  the disk); every file stays on disk for the build. It fails closed:
  a secret still in the index after `git add -A` — an exclude file it
  could not write — stops the rebuild before the commit, naming it.
  copier does not need them in the repository: it renders `ssh/`,
  `repos.yaml` and `pip.txt` only when they are missing (the template's
  `_skip_if_exists`) and never the conf, which a real `copier update`
  confirmed, also across a template upgrade. What earlier rebuilds
  committed stays in their history; no flow of the panel copies an
  instance's `.git` off its host (an export leaves it out).
- **A job, and what cancelling it ends, is stopped only by whoever may
  ask for it.** `cancel_job` checked the role gate of enqueue
  (`_check_job_type_allowed`) only for a job waiting in its queue, and
  only on the job pressed: a dispatched, running or blocked one needed
  write access alone, and cancelling the head of a chain ended the steps
  after it unchecked. A consultant stopped a restore of their project's
  production after its dropdb, or its delete, or aborted a manager's
  move by stopping its first step; a project manager or developer, who
  write every job, stopped any host's hardening. The gate now applies in
  every state, to the job and to every step its cancellation ends
  (`_ended_with`), and one the caller may not ask for refuses the whole
  cancellation before anything ends; what a role may ask for, it still
  stops.

- **Deleting, archiving or removing an instance no longer ends work its
  asker may not stop.** Enqueuing `delete_instance` empties the
  instance's queue, archiving it has the dispatcher cancel what waits
  there, and unlinking its record cancels it at once — each job with the
  rest of its chain, wherever that waits — and none of them asked
  whether the caller could stop that work: a consultant who may delete a
  staging cancelled, by deleting it or archiving it, a developer's
  restore or a manager's move waiting on it, and could then cancel their
  own delete or bring the instance back. Each of the three now holds the
  caller, for every job it would end and the steps that end with it, to
  the gate `cancel_job` holds them to (`_check_may_end_waiting`), and
  refuses whole before anything ends, naming the job. A delete's own end
  archives or removes the record as the platform: what was asked for
  after it waits on an instance being removed. Superuser, the cron bot
  and managers clear as before. A refusal while a move is rolled back is
  raised, no longer taken for a step already ended.

- **A move's markers, and what decides a teardown or a purge, are no
  longer a member's to write.** `move_origin_host_id`,
  `move_target_host_id`, `move_chain_job_ids` and
  `move_rollback_in_progress` had no guard: a consultant wrote on their
  staging a chain naming a production restore waiting elsewhere and a
  target host of their choosing, and the watchdog, as the cron bot,
  cancelled that restore and queued the rollback's cleanup — `compose
  down -v` and `rm -rf` — on that host; the same markers opened any host
  to their chain's steps. Only the platform writes them now
  (`cloud.instance._check_guarded_vals`; `move_to_host`, the rollback,
  the watchdog and the move's executors with `sudo`), and a move's chain
  is only its instance's jobs. The same guard keeps to the platform a
  pull request preview's identity — the GitHub webhook rebuilt with the
  request's branch, and deleted on close, any instance a member stamped
  with it, production included — its comment id, and the removal and
  purge stamps; and keeps to an administrator an instance's directory on
  its host and the place of its copy, which a consultant's own delete
  removed or emptied wherever they pointed, and a project's remote
  folder, which names its instances' directories. Past draft, an
  instance's host is an administrator's too — its stack is there and its
  waiting jobs follow it, and Move to host moves both — and where an
  instance's or a project's backups are kept is a developer's choice, as
  backups are. Creating an instance already past draft, as an import
  does, is an administrator's. None of these fields is copied with an
  instance any more.

- **An instance's environment is chosen when it is created, and nobody
  changes it afterwards.** `environment` had no guard, and it decides who
  may delete an instance, whether its egress is filtered and whether a
  module that counts productions counts it: a consultant relabelled a
  production as staging and deleted it, or a staging holding a copy of
  production as production to lift its egress filter, and a production
  relabelled as staging went on serving uncounted. A change is now refused
  to everyone but the platform, administrators included
  (`cloud.instance._check_guarded_vals`), and `/cloud/save_instance` — which
  the panel's form and the MCP's `update_instance` call — says why instead
  of dropping it. Creating a staging or a production is unchanged.

- **No project lives in a directory the platform keeps on every host.**
  A project named "Traefik" had the folder `traefik`, so an instance of
  it named "dynamic" lived in `~/traefik/dynamic`, and its deploy or its
  delete removed the proxy's routes and certificates for the whole host.
  A project's remote folder — given, or derived from its name — may no
  longer be `traefik`, `globalwhitelist`, `incubacloud`, `observability`
  or `observability-central`, for anyone, the platform included
  (`RESERVED_REMOTE_FOLDERS`).

- **A remote folder is one project's.** Nothing made it unique, and two
  projects with one folder removed, overwrote or moved onto each other's
  instance directories. A unique index over `lower(remote_folder)` now
  holds it. A folder derived from the name takes the first free one of
  `<slug>`, `<slug>-2`, `<slug>-3`… (a create that loses the race for
  one takes the next); a folder chosen that another project holds is
  refused without naming that project; and renaming a project that has
  no folder chooses one, checked as choosing it. The 1.0.200 pre-migrate
  stops the upgrade, naming them, if two projects already share a folder
  whatever its case: Odoo would only log a warning and leave the index
  out.

- **Only an administrator deletes an archived instance, and only a
  developer revives one.** `cloud.instance.delete_archived` is public,
  and for an archived instance without a copy it removed the record with
  `sudo`: through `call_kw` a consultant deleted an archived production
  that way, past the role check and the unlink's guard. It now asks for
  the administrator's role, as its route did; the platform's own callers
  keep it. Reviving deploys an instance and restores its copy, and
  restoring is a developer's: `revive` and its route now ask for it
  before anything is written, and the route runs it in a savepoint, so a
  refusal after the record was un-archived leaves it archived.

- **Every value that reaches a shell is checked whole.** The validators
  of an instance's name, a remote folder or directory, a database
  identifier, a service, a git ref or SHA, a host name, a log archive, a
  mail id, a container id and a few more used `re.match` with patterns
  ending in `$`, which also accepts a final line break: such a value
  passed, and reached the commands run on the hosts as two lines, `rm
  -rf` of an instance's directory among them. They now match the whole
  value (`fullmatch`), and the delete passes the instance's directory to
  `rm -rf` as one quoted argument (`"$HOME"/'…'`).

- **Every job stages its files in a directory of its own, secrets
  owner-only.** Scripts, copier answers, `repos.yaml` (the repositories'
  tokens), `backup.env`, the modules a deploy installs, the TLS key and
  certificate store, the proxy's compose file (the dashboard's password
  hash), the archived-backup purge's environment, a restore's archive,
  and the manual backups, backup downloads, neutralized copies and
  exports went to fixed paths in `/tmp`, 0644: readable by every account
  on the host, left there by a run that ended early, and on a host
  shared with other accounts open to whoever created the path first — a
  script the job then ran, the modules it installed, a certificate the
  proxy job installed because a shell found a file where it looked. A
  job now creates `/tmp/.incubacloud-job-<random>` with `mkdir -m 700`
  (refused if anything is there), stages everything in it, creates
  secrets 0600 (exclusive create, replacing what an earlier run left,
  naming the path when another account holds it) and removes it when the
  run ends, failure included. A container writes only into the job's
  `out/` subdirectory, mounted alone — the dump's container mounted the
  whole of `/tmp` — and a file is downloaded only if it is a regular
  file, not a link a container left to be followed. Full Setup and the
  proxy push decide from what they uploaded whether to install or remove
  the default certificate and the CDN authority. `repos.yaml` and the
  proxy's compose file end 0600 where they are moved. The SSH allowlist
  change creates its directory without `-p` and checks it.

- **A backup handed over for a restore is named with the panel's secret,
  and goes when the restore will not come.** A `backup_download` that
  hands its archive to the restore after it — a production database —
  left it at `/tmp/.incubacloud-handoff-<job id>.zip`, a name anybody on
  the host could tell, and create first, in advance. It now goes in a
  0700 directory named with an HMAC of `database.secret`
  (`handoff_dir`), which the restore recomputes. A chain cancelled
  before its restore ran had the archive stay for good: the step that
  will never run now lets go of it (`on_never_run`, which queues
  `discard_handoff` on the download's host), and a handoff a day old
  whose restore is not waiting in its own chain is swept by the next
  download, restore or daily prune on that host. The restore no longer
  prints the archive's path in the job log.

- **An upload key writes only into its own directory.** Its forced
  command was `rrsync -wo`: one session could store a link to any
  directory of the account, and the next write through it —
  `authorized_keys` among them, a shell as the account the panel
  connects as (often root). It is now `rrsync -wo
  -munge -no-del`: a link arrives munged and leads nowhere, and nothing
  sent can be deleted by a later session. Only the rrsync of rsync 3.2.4
  and later knows both options, so the key is installed only where that
  one is, and the grant is closed otherwise, saying why (an older rsync,
  none, or an account that cannot install it without a password). The
  grant's directory is created only under a `/tmp/incubacloud-restore`
  this account owns, 0700; the check of what arrived lists only regular
  files whose name is one safe component (`SAFE_UPLOAD_NAME`), and the
  restore copies that file into the job's own directory and checks it
  again against the digest the dialog showed. A job acts only on a grant
  of its own instance and host, of the shape the panel gave it
  (`_check_job`). People read grants, never write them: the panel writes
  them with `sudo`, and below project manager a record rule shows only
  the grants of one's projects' instances. The 1.0.200 post-migrate
  closes every grant an older release left open, through the daily
  sweep.

- **A restore from a link fetches the name it checked, runs nothing its
  link carries, and an upload key's archive does not stay on the host.**
  The password of a link was written into the download's command line,
  inside single quotes, as a netrc line: readable in `ps` by every
  account on the host, and a quote in it ended the quoting. It now
  reaches the host as a curl configuration file created 0600 in the
  job's directory; the link and every other argument are passed as
  quoted words, with `--proto` keeping curl to the link's schemes; and
  `restore_db` refuses a link, which only `restore_from_url` checks (the
  restore job checks it again). A user name with a colon is refused,
  since curl splits the value at its first one. A link whose host name
  is not plain ASCII is refused, with the advice to write its punycode
  form (`xn--…`): Python (IDNA2003) and the hosts' curl (IDNA2008) could
  turn it into different ASCII names, so the name checked and pinned was
  not the one curl dialled, and curl then looked it up by itself. curl
  now gets the link rebuilt from the name checked (`curl_url`), so the
  `--resolve` pin always matches it, and reads nothing the host
  configured (`-q`, `--noproxy '*'`). `-C -` is gone: each attempt
  downloads into a directory of its own, so it never resumed anything. A
  restore through an upload key revokes the key and keeps the grant's
  directory for a retry; nothing removed it afterwards, and the archive
  — a customer's database — stayed in `/tmp/incubacloud-restore/` for
  good. The daily grant sweep now revokes a consumed grant three days
  after its restore, directory and all, and the daily sweep of `/tmp`
  (`docker_prune`) removes this account's grant directories three days
  old, under a root of its own, for a host where that job never ran. And
  the restore names the SHA-256 the dialog showed (`expected_sha256`):
  the panel and the restore job both refuse it if a later check recorded
  another file. A restore through a key asked for before this version,
  or its retry, names none and is refused: ask for a new key.

- **What releases before this one left in `/tmp` is swept.** The daily
  prune (`docker_prune`, and the maintenance playbook) removes this
  account's files and directories a day old under the fixed names
  earlier releases used — copier answers, `repos.yaml`, `backup.env`,
  dumps, exports, the proxy's staging, the scripts' directories — also
  root's and the odoo container's user's (uid 1000), through `sudo -n`
  when the account is not root; a job's directory three days old, whose
  worker died; and handoffs nobody waits for.

- **A host's whitelist proxies run a pinned build, and a hostname cannot
  rewrite their compose file.** The compose file `setup_whitelist` and
  Full Setup write named `ghcr.io/tecnativa/docker-whitelist` with no
  tag, so each host ran whatever `latest` was when it was set up. It is
  now 0.3.2 by digest (`_WL_IMAGE`); a host's
  next whitelist setup recreates its proxies on it. Each of the host's
  entries becomes a service and a quoted alias in that file, and nothing
  checked them: a quote or a line break in one rewrote it. An entry must
  be a bare hostname, as an instance's must
  (`cloud.host.whitelist._check_hostname`), the file refuses one stored
  before the check, and two names that map to the same service key no
  longer make compose refuse the whole file: the later one takes `-2`,
  `-3`…

- **An instance can no longer read the proxy's metrics, nor feed the
  host's metrics agent.** Traefik's metrics entrypoint listened on
  `:8082`, every interface of the proxy, `inverseproxy_shared` included,
  which every instance on the host joins: any instance could read the
  routers and request counts of all the others. vmagent joined that
  network to scrape it and listened on `:8429` of every interface: any
  instance could push series it forwarded to the central under the
  host's credential. The entrypoint is now bound to the proxy's address
  on its `public` network (`proxy.inverseproxy_public:8082`,
  `PROXY_PUBLIC_HOST`), the agent scrapes it there and listens on its
  own loopback. A host takes it with its next Full Setup or proxy push
  (a stored `traefik.yml` is rebound as it is shipped) and its next
  install of the observability agents. Traefik does not start when that
  name does not resolve: a self-hosted `inverseproxy.yaml` must keep the
  project `inverseproxy`, the service `proxy` and the network `public`.

- **Nothing joins a host's SSH allowlist by itself, and the hardening
  dialog says who will be able to open SSH.** An empty `allowed_ssh_ips`
  could be filled with the address of the browser that asked for the
  hardening, unannounced (the `_before_host_hardening` hook, now gone):
  an IPv6 a host reached over IPv4 could not use, or, behind a CDN, an
  address of the CDN's.
  `/cloud/preflight_host_hardening` now returns `ssh_access`
  (`cloud.host._ssh_access_preview`) — open to any address, only this
  panel's server, this panel's server and the list, or closed by an edge
  this panel does not govern (a provider's firewall another panel
  manages) —
  and the dialog says it in every mode. It offers the asker's own
  address with an unticked "Also allow my IP" only when that address is
  public (or of the host's LAN), not a proxy this panel believes
  (Settings or `x_real_ip_trusted`) and of the host's address family,
  and says why otherwise. `/cloud/apply_host_hardening` takes it as
  `allow_ip`, checks it against the address it sees itself and adds it
  in the savepoint that queues the jobs; without it the list is left
  alone. The panel's own address is `cloud.host._hardening_panel_ip()`
  (none in core), which a module that extends the panel can give.

## [1.0.199] — 2026-10-27

### Fixed

- **An instance's page opens in debug mode again.** With
  `?debug=assets` Owl checks every prop a component gets, and the
  repositories' module selector refused the `error` RepoEditor gives it
  until a module fetch fails, `null`, which is also its own default:
  the page raised "Invalid props for component 'ModuleSelector': 'error'
  is not a string or boolean". The prop accepts `null`.
- **Switching continuous recovery on or off no longer reads the instance
  as stopped.** On a stack whose database does not archive from its
  command line, switching on (or off) restarts the database with odoo
  stopped around it, which took odoo's metrics series away: the liveness
  cron could write the instance stopped, and the panel showed "Stopped"
  for up to two minutes, as after a restore before 1.0.198. `pitr.sh`
  prints `IC_PITR_ODOO_RESTARTED` on a line of its own as soon as it
  has started odoo again, also when the database does not come back and
  odoo is started on the way out. The job confirms the instance running
  (`running_confirmed_at`, written durably with `_persist`) the moment
  that line arrives, while its steps go on, and not when it ends:
  switching on takes a first base copy next, for minutes, longer than
  the two minutes of grace the confirmation opens. The end of the job
  confirms nothing again. A row still locked after the durable lock
  timeout leaves the instance unconfirmed, says so in the job's log, and
  the switch goes on. `incubacloud_pitr` 1.0.7.

## [1.0.198] — 2026-10-23

### Changed

- **An instance runs one job at a time; the rest wait in its queue.**
  `cloud.job.enqueue` and `enqueue_chain` no longer refuse a job
  because the target is busy ("A job is already running…"): the job is
  created `queued` and the dispatcher starts it when the running one
  ends, first come first served, with what the running job queued for
  itself (`bypass_running_check`, now "I am part of the job running")
  first. Only the platform may ask for that head of the queue: both
  methods are reachable through `call_kw`, and a caller without
  superuser rights who passes `bypass_running_check`, or `move_rollback`
  in a payload, is refused (`_check_internal_queue_flags`); a move's
  rollback, a restore's follow-ups and the PITR restore that follows
  itself queue theirs with `sudo`. Such a caller is refused, too, a
  target they cannot read — an instance of a project they are not in —
  before its queue is looked at (`_check_target_access`), since
  collapsing into a waiting job, merging a push into a waiting rebuild
  and the queue's refusals read the queue with `sudo`. Nor may such a
  caller create a job, or write what the dispatcher and the executor
  read — the state, the head, the chain, the retry, the blocking alert,
  the queue.job, the type, the payload and its secret, the instance and
  the host, the rights it runs with and when it ended — with
  `create` or `write` (`AccessError`, `_check_queue_control_vals`): a
  job is queued, retried, unblocked and cancelled only through
  `enqueue`, `enqueue_chain`, `retry_job`, `unblock_and_enqueue` and
  `cancel_job`, with their checks, which write those fields with `sudo`
  once the caller's own access to the job is checked (the caller stays
  its creator), and only a job waiting for an alert can be unblocked.
  A host serialises its host-level jobs the same way. A chain is one
  entry, dispatched whole with its queue.job dependencies when every
  instance it touches is free and it is first in each of their queues;
  cancelled while it waits, every step goes. `cloud.job.state` is the
  job's own field (it mirrored `queue_job_id.state`), with `queued`;
  `queue_job_ext` mirrors every transition the runner writes through
  the ORM. Jobs left with an empty state when their queue.job was
  deleted end `cancelled` with a note.
- **What a job type does in the queue is the type's to say.**
  `cloud.job.type` gains `queue_collapse` (asked for again while an
  identical one waits, the waiting one is returned — payload and secret
  alike), `queue_exclusive` (one waiting at most: restores, deletes) and
  `failure_holds_queue` (a failure of a deploy, rebuild, restore, delete
  or move step holds the target's queue until it is retried successfully
  or its alert — the `job_failed`, or the critical conflict alert raised
  instead — is dismissed; the retry goes to the head, one per failure).
  A queue holds five entries; a sixth is refused. `install_observability`
  collapses through the type instead of a search of its own.
- **A GitHub push merges into the rebuild already waiting.** The pushes
  that arrived during a rebuild were kept apart
  (`cloud.instance.pending.push`, with the one-minute cooldown) and
  folded into a rebuild when the running one ended. A push now always
  queues its rebuild: with a push's rebuild waiting, the push joins its
  list (`coalesced_pushes`, head unchanged); with one running and none
  waiting, a new one waits. The model, its card and its cooldown go;
  the migration drops the table.
- **Cancelled before it started, a job leaves nothing behind.** A job or
  chain cancelled while it waits — by whoever may ask for it, by a
  delete, by its instance being archived or deleted — is cancelled on
  the spot, with a note (a type meant for archived instances, the purge
  of an archived instance's backups, is cancelled only once its instance
  no longer exists: `_runs_on_archived_instance` on the executor), and
  its executor's `on_cancelled_queued` puts back what was written when
  it was asked for: a deploy's instance to 'draft', a move's markers
  cleared and a layer above told. A `delete_instance` cancels everything
  waiting on the instance first and waits only for the job running;
  `/cloud/delete_instance` no longer refuses while jobs are active. A
  waiting job's instance moved to another host starts on the new one.
  The deploy and rebuild endpoints return the job's `state` with its id
  (`_job_response`); the others that enqueue return the id alone. The
  rollback jobs of a move go to the head of the queue and start once the
  cancelled step has stopped; a revocation of an upload key asked for
  outside a restore (the daily sweep) waits its turn like any other job.
- **Readers of a busy instance.** `_active_states` includes `queued`:
  the host endpoint guard, the PITR checks, the docker prune's
  per-instance check and the hardening preflight (`has_active_job`)
  count a waiting job; the health probe does not (a job that has not
  started silences nothing), and the backups tab shows a queued listing
  as waiting. The move watchdog counts only a waiting step of the move
  or of its rollback (below). A sweep every two minutes
  (`cron_job_queue_dispatch`) serves any queue a lost dispatch left
  waiting.
- **The queue moves after the commit, on a cursor of its own.** The
  runner that ends a job reads the snapshot it took when the job
  started, and would not have seen what was queued during the run: the
  end of a job, a cancellation, a dismissed holding alert, an unblock or
  a retry now register a dispatch of each queue concerned for after
  their transaction commits, run on a fresh READ COMMITTED cursor that
  logs whatever goes wrong instead of raising it. The jobs it starts run
  as their creator, with superuser rights only when they were asked for
  with them (`asked_as_superuser`, from the caller's environment), as
  until 1.0.197, when `enqueue` handed queue_job the caller's own
  environment: a consultant's job runs under their record rules, and
  what the platform's code asks for with `sudo` (an executor's
  follow-ups, a layer's flows, a cron that elevates) runs as the
  superuser. A job waiting for an alert runs as its creator too, with
  the rights (superuser or not) of whoever sets it going, where until
  1.0.197 it ran as them. The upgrade adds the column, true for every
  existing job, without rewriting the table. A failing dispatcher,
  or a layer's `_after_job_terminal` that raises (now run in a savepoint,
  and also for jobs cancelled while they waited), no longer turns a job
  that succeeded into a failure. `enqueue` starts its own job at once
  only when the instance is free both in its snapshot and in a fresh
  read, so two requests at the same moment no longer start two jobs; the
  five-entry cap, the one-waiting rule of exclusive types and the one
  retry per failure count both too — the fresh read only adding the jobs
  the snapshot does not see at all, so a delete that cancels what waits
  is not refused by what it has just cancelled. The rollback of a move
  passes a queue held by the failed step it recovers from, as the
  failure's retry and a delete do; the alert stays open. And the
  stuck-move watchdog no longer waits for a job asked for after the
  failure, which waited behind that hold for good: only a job running,
  or a waiting step of the move or of its rollback, means the move is
  still going on. A dispatched chain cancelled before it started ends
  its next steps with it (queue_job cancels their queue.jobs with SQL
  nothing mirrored, and they held the instance for good). The steps
  after one that failed or was cancelled end `cancelled`, with a note,
  not `failed`: only the step that failed is a failure, and a step that
  never ran offers no retry (after a failed move cutover, a retry of the
  source's cleanup would have torn down the running instance). Only the
  chain's own graph is ended: a probe that fails beside a chain no
  longer ends its steps. `cloud.job.instance_id` and `host_id` are
  indexed.
- **A neutralized copy empties the queues.** It cancels what waits in
  them too, not only what production had dispatched.
- **Continuous recovery says why it refuses a switch:** whether its job
  runs or only waits in the queue.
- **The panel shows the queue.** The job lists of an instance and of a
  host, and the job history, show a waiting job as "Queued · N ahead",
  with a class of its own (an amber dot that does not pulse: nothing
  runs yet), and name every other state in the reader's language as the
  history did ("Running" where the lists showed `started`). The two
  lists count a waiting job as active and offer to cancel it, saying it
  has not started and only leaves the queue, and for a step of a chain
  that the rest of its chain goes with it; a refusal by role, and a
  failed retry on a host, are shown. A job waiting behind a failure
  that holds the queue reads "Action required", which opens the alerts,
  as a blocked job's does, and on an instance its Resolve button does
  too; what passes the hold (a retry, a delete, a move's rollback) does
  not. The history's "Queued" was queue_job's `enqueued`, a job a runner
  is about to start: it reads "Starting" now. Every button that
  enqueues says what became of its job — "Started: <type>", "Queued
  behind <type running>", or "Queued" — where some said "queued" for a
  job that started ("Migration started." for a move that waited), or
  nothing at all (the host's check and Full Setup, its whitelist, the
  clone, the download of a backup, the SSH restore's upload key, whose
  dialog now says the key works once its job has started); reviving and
  deleting an archived instance keep their own words. None opens a tab
  any more: the deploy, the rebuild, the restores, a retry and the check
  of an upload opened the job's log in a new one, which for a job
  waiting its turn showed nothing; the log opens from the lists.
  `_job_response` adds `job_type` and `queued_behind` (the visible name
  of the type running on the target), `/cloud/get_job_state` answers
  the same for the buttons whose request answers the id alone, and the
  conflict resolutions answer the id of the job they put back in its
  queue. The hardening dialog tells a hardening waiting in the host's
  queue from one running. The history's rows carry `queue_position`,
  the lists' `in_chain`; every row of the lists and of the history
  carries its job's id (`data-job-id`).
- **Job types speak Spanish.** `cloud.job.type`'s `name`,
  `description` and `action_confirm` are translated (the columns become
  jsonb on update, the English texts kept as `en_US`), with core's and
  `incubacloud_pitr`'s in Spanish: the lists, the history, the job's log
  page, the job that resolved an alert and "Queued behind <type>" read
  "Reconstruir instancia" for a Spanish user, and the confirmation
  before pushing the trusted proxy settings is no longer English inside
  a Spanish dialog. Nothing compares them; `code` is the identity. A
  job's name, its audit action and its queue.job's description stay
  stored in English now that the type's name is translated (the
  reader's language would split the audit's filter and mix languages in
  mails and alerts).
- **Odoo 20.0 is offered, and a new project starts on it.** Its card
  says `offered`: the pickers show it, the importer recognizes it and
  anyone may choose it, on PostgreSQL 16 to 18, with `odoo db` and
  doodba template v10.0.3 or newer. A new project gets the newest
  series offered (`default_version`), not a fixed 19.0, and a new
  instance its project's unless it names one, whoever creates it (an
  import from a host into an existing project took the default). A
  panel offers only what its template pin can render: below v10.0.3 its
  pickers, its default and `may_choose` stop at 19.0 (anyone else who
  sends 20.0 reads "not offered on this panel"), while an administrator
  may still pick 20.0 and its deploy refuses with RB-15. Existing projects
  and instances keep their series, and a save that sends back the
  series a record holds is no choice: after a rollback of the pin
  below v10.0.3, anyone who could save a 20.0 project or instance
  still can.
- **A template pin too old for 20.0 is raised on upgrade.** A panel
  whose stored `copier_template_ref` is a version tag below v10.0.3 (a
  panel installed with a core older than 1.0.195, 21-oct-2026, holds
  v9.6.1, unless someone changed it) moves to v10.0.8, with a log line
  naming both; an empty pin, a branch or a commit stays. Both tags are
  written in the migration, which reads neither the card nor the
  field's default. Nothing is rendered then: each stack takes template
  v10 at its next deploy or rebuild, as after a bump by hand (RB-15),
  and a staging with an allowlist closes its egress with the gatekeeper
  from then on. A staging whose own allowlist is empty takes its host's
  list under v10, so it shows "unapplied changes" until its next
  deploy: true, since that deploy changes its compose.

### Removed

- **The hourly cron of continuous recovery's base copies.** Since
  `incubacloud_pitr` 1.0.5 the watch (every ten minutes) queues the
  daily base copy when it is due (`_pitr_base_due`), in place of the
  reading; the hourly *Continuous recovery: daily base copies* cron
  followed the same rule and always found the copy already queued
  (every copy was queued at a watch's tick). A
  copy that failed is still tried again an hour later, now at the first
  watch after that hour. The upgrade deletes the cron.
  `incubacloud_pitr` 1.0.6.

### Fixed

- **A job can no longer be run again through `call_kw`.**
  `cloud.job.execute`, which queue_job calls by name, was public: anyone
  who could read a job ran it again, outside its queue and without the
  role gate of `enqueue`. It is private (`@api.private`), as are
  `unblock_and_enqueue` (it would have queued a job past the alert it
  waits for, still open), `cron_cleanup_backup_attachments` and
  `get_audit_log` (served by `/cloud/get_audit_log`).
- **A step of a chain runs where its instance is.** A caller without
  superuser rights could name any host in a step of `enqueue_chain`
  through `call_kw`, and have their staging deployed on a server that is
  not its own: the dispatcher puts a lone job on its instance's host,
  but a chain's steps keep the hosts they name, and `cloud.host` has no
  record rule. A step of an instance must now name its host or, while it
  moves, the move's origin or destination (`AccessError`,
  `_check_step_host`); a clone, a refresh, a restore, a revival and a
  move name no other.
- **A restore takes its data only from a job its creator may read.**
  `restore_instance` in `from_host` or `from_job` mode looked its source
  job up with `exists()`, which ignores record rules, and read a
  `from_job` archive with `sudo` whatever job it named: the source is
  now read in the job's environment first (`_source_job`), and one the
  creator cannot read fails the restore with an `AccessError`.
- **Cancelling a job needs write access to it, in every state.** A job
  waiting in its queue, or dispatched and not started, was cancelled by
  anyone who could read it and ask for its type.
- **A job's name is text in its mail.** The job notification put the
  job's name, which whoever may write the job writes, and the host's and
  instance's names into the mail's HTML as they were, so a link forged
  into one was live in the mail of everyone who follows the project; they
  are escaped, as the alerts' mail does, and the subject keeps the name
  on one line. A job's end (`date_done`, which the health probe reads)
  can no longer be written outside the queue's own code either.
- **A name is text in Telegram, too.** The job and alert notifications
  sent to Telegram as `Markdown` put the names of the job, the host and
  the instance, and the alert's message, in it as they were: a link
  forged into one was live in the chat, and a lone `_` or `*` made
  Telegram refuse the message. They are escaped (`telegram_markdown`).
  The resolution of an alert whose failed job was purged is sent again:
  the link to the run that resolved it read a base URL never set.
- **A compose that cannot be read no longer passes for a stack without
  the gatekeeper.** In the pull request preview walk 6f6 (QA,
  22-oct-2026) `docker compose config --services` failed once on the
  host: the rebuild's safe boot test took the staging for one without a
  gatekeeper, put the throwaway PostgreSQL on a network the boot
  container could not join, and failed with nothing to say why.
  `compose_op.sh` would have skipped in silence the same way, restarting
  the egress sidecar after odoo, and took a failed `docker compose ps`
  for an odoo not running. Both scripts now read the compose through
  `ic_compose_services`, which retries once and logs docker's own error.
  The boot test reads it before copying the database and stops there,
  saying it could not tell whether it needs the gatekeeper: a rebuild
  that cannot check its boot fails. When `compose_op.sh` cannot read
  what a step after the operation needs (the compose, what runs, odoo's
  routes, the latter in 30 seconds at most), the operation has still
  run, so the script names the step it could not check and what may be
  wrong (odoo's unfiltered route, or odoo left with no network at all)
  and exits 24 (`COMPOSE_OP_EXIT_UNCHECKED`); a stack whose compose,
  read then, shows no gatekeeper has nothing to check for it. The start,
  restart, restore and backup restore jobs take 24 for their step done:
  they succeed, with a warning in the job log, and everything a success
  brings runs as usual. What may be left wrong is the health probe's: an
  unfiltered route through `odoo_net_setup`'s healthcheck
  (`instance_egress_filter_lost`), an odoo outside its gatekeeper's
  network through the check below.
- **An odoo left outside its gatekeeper's network is put back, and the
  health probe sees it.** Under template v10 odoo runs in the
  gatekeeper's network namespace, and the gatekeeper gets a new one
  every time it starts: restarted alone, recreated, or started again by
  an `up`, it leaves a running odoo in the old namespace, while Traefik,
  which reaches odoo through the gatekeeper, gives every visitor a 502
  (checked on Docker 29, Compose 5). That namespace loses Docker's
  interface and every route through it, but keeps the rest: a bridge the
  gatekeeper made, the tunnels the kernel adds to each new namespace,
  the IPv6 link-local and multicast routes of those that are up. So odoo
  is taken to be in the right network when it has a route, IPv4 or IPv6,
  other than those, through an interface other than `lo`. After an `up`,
  `start` or `restart`, `compose_op.sh` reads odoo's routes and restarts
  odoo when it has none, whatever the operation named; before, only a
  start or restart naming the gatekeeper did, and an `up` never. A
  restart of the whole stack is a `stop` and a `start`: `docker compose
  restart` restarted odoo and the gatekeeper at once and left odoo
  exited, unable to join a gatekeeper not running yet (5 of 5 on Docker
  29), or running in the old namespace. `start` goes in dependency order
  and now waits for what odoo depends on to be healthy (`db`, and the
  gatekeeper on a v10 staging): if one stays unhealthy, the restart
  fails with odoo stopped. A stop that fails still gets its start and
  the steps after it, and the restart then ends with the stop's status.
  The probe saw none of it: its HTTP check runs inside odoo's network,
  where Odoo answers, and the gatekeeper stays healthy. For a stack with
  a gatekeeper it now reads the same routes: odoo without one, or with
  its gatekeeper not running, counts as Odoo not answering,
  `instance_unresponsive`, critical, on the second probe in a row (a
  restart caught midway lags for seconds), with the cause and "Restart
  the instance". The probe repairs nothing. Routes it could not read are
  logged as not checked and change nothing: the streak and the alert
  stay as the last probe left them. Stacks without a gatekeeper do not
  read them.
- **The metrics read an instance's odoo by its freshest series.** On
  22-oct-2026 (QA) a production showed "Stopped", without its Connect
  button, for four minutes after a backup, every container up and its
  status ok. The metrics cron, which owns `running` while its readings
  are fresh, read how long since the instance's `odoo` container was
  seen by the oldest of its series (`max by`), and a recreated
  container leaves the old one's series reported, ageing, for minutes
  beside the new one's. It now reads the freshest (`min by`), leaving
  out by name the containers that run odoo without being the
  instance's own and would otherwise pass for an odoo that is stopped:
  the one-offs of `docker compose run odoo …`
  (`<project>-odoo-run-<hash>`) and the rebuild's boot test
  (`ic_boot_odoo_<id>`). The instance dashboard's CPU and memory panels
  leave out the same two, the boot test included now. Which hosts
  still report is read by the freshest series too: by the oldest, a
  host went quiet whenever a container on it had just been removed.
  The health probe, which leaves `running` to the metrics while they
  are fresh, now writes it True when it sees odoo running and answering
  its HTTP check, if it was False. It does not hold it there: the
  metrics cron's next reading writes what that reading says. A stopped
  or not yet answering odoo is still the metrics' to report while they
  own the flag.
- **An instance a job has just brought up no longer reads as stopped.**
  On 26-oct-2026 (QA) an instance restored to a point in time showed
  "Stopped", without its Restart and Shell buttons, for over two
  minutes after the restore had confirmed it running. A job that starts
  or recreates odoo takes its container's series away at once, and the
  new one reaches the metrics backend about a minute later (a 30 s
  scrape, then VictoriaMetrics' 30 s latency offset): the liveness cron
  read that gap as a stop half a minute after the restore. A job that
  brings odoo up — a start, a restart, a deploy, a rebuild, a backup
  restore, a restore, a move's cutover, a point-in-time restore, one
  that failed and put the data back included — now stamps
  `cloud.instance.running_confirmed_at`, and for two minutes after it
  the cron does not write `running` False, whether the instance's odoo
  is missing or its whole stack is (`_CONFIRMED_GRACE_SECONDS`). It
  reads the stamp again from the row, locked, before it writes a stop,
  so a job confirming the instance meanwhile is not overwritten. A rise
  the cron reads itself opens no grace, so an odoo that keeps going
  away still reads as stopped, and a stop written by anyone — a Stop
  job, the probe that found the container down — closes it. The
  rebuild now writes `running` True, as the deploy does: its restart
  step is `docker compose up -d`, and without it a stop the cron had
  read during the rebuild stayed until another job came by. Not for an
  instance whose stop is expected (`_stop_is_expected`): a layer above
  may hand its rebuilt stack back stopped.
- **A new website visitor's race no longer reads as an error of the
  instance.** Two requests of one new visitor at once (bots, mostly)
  race on its `website_visitor` row, and Odoo logs the loser's insert as
  a `bad query` that could not serialize: Odoo's own race, which nobody
  running the instance can act on (four nights out of thirty on one
  production). The health probe leaves that record
  out (`_error_block_is_expected`); a serialization failure on any other
  table is still an error. A layer that overrides the hook asks super
  first.
- **The health probe no longer reports what a job that stops the database
  leaves in the log.** A rebuild, a restore or switching continuous recovery
  cuts every connection Odoo holds, and Odoo logs each one as an ERROR: the
  job runner retrying, the cron worker exiting, the server refused while
  `db` shuts down. The probe skipped its pass while the job ran, but the
  first pass after it read from the previous check, less five minutes of
  clock-skew grace, and raised `instance_error_logs` on those lines. Its
  error window, and the fallback's `--since`, now open no earlier than one
  minute after the `date_done` of the last such job that ended done; a
  failed one moves nothing. New hook
  `cloud.job._get_health_blocking_host_job_types()` (none in core): host
  jobs that stop the instances of their host. While one runs, the probes of
  those instances skip their pass, and its end moves their window too: a
  layer lists there, for instance, a host-wide backup that stops them. A
  pass skipped for such a host job alone leaves the streaks and the alerts
  as they were: the job changes nothing in the instance, and an open
  `instance_unresponsive` it closed was raised again, with a new notice, 10
  to 15 minutes later; a job of the instance's own resets them as before.
  `cloud.job.job_type_id` is indexed: the probe of every instance filters on
  it twice a cycle.

- **A backup no longer recreates the services of a running stack.** The live
  dump (`backup_download.sh live-dump`: non-production backups and
  downloads, a staging's copy of production, a move's dump of its source, a
  layer's host-wide backup) and the archive taken before a delete
  (`backup_archive.sh`) start their one-off with `--no-deps` when the
  instance's database runs. Without it Compose recreates every dependency
  whose definition changed since the stack started: a host's backup
  recreated a staging's live `db` under its workers and started a
  `gatekeeper` its running `odoo` was not on (template v10). A stopped
  database is still started with what it depends on, as before (a move's
  source a layer keeps asleep, a stopped instance's backup); the archive
  also goes without `--no-deps` when what runs could not be read, so a
  failed read cannot block the delete. With `--no-deps` the live dump
  replaces doodba's wait for Postgres, which has no deadline, with one of 60
  seconds (`WAIT_DB=false`, `UNACCENT=false`, as in the rebuild's boot
  test): a database that runs but does not answer fails the dump with "the
  running database did not answer within 60s" instead of holding it until
  the SSH timeout. A stack whose compose declares a `gatekeeper` that is not
  running (files rewritten by template v10, not rebuilt) is refused before
  the dump with its own message rather than docker's. `live-dump` takes an
  optional `running`, which a layer's host-wide backup may pass: a stopped
  database is then refused with a clear error and exit code 25 instead of
  started. `ic_compose_run` is unchanged: most of its callers need what the
  service depends on.

---

## [1.0.197] — 2026-10-22

### Changed

- **Each Odoo series has a card, and the panel reads from it.**
  `models/_odoo_versions.py` describes every series in one place: whether
  it is offered, the PostgreSQL range and default, the tool that dumps
  and loads its database, and the lowest doodba template able to render
  it. The PostgreSQL picker offers the series' range and `cloud.instance`
  refuses any other; the deploy and the rebuild refuse, before uploading
  anything, a template pin older than the series needs; and the
  operation scripts no longer name `click-odoo-backupdb` or
  `click-odoo-restoredb`: the executor hands them the series' tool
  (`IC_DB_TOOL`) and `scripts/lib/common.sh` builds that tool's argv
  (`ic_db_dump_args`, `ic_db_load_args`), implementing `click-odoo` and
  `odoo db`. The `odoo_version` field holds every series with a card,
  while the pickers and the importer show the offered ones, so an
  instance keeps a valid value when its series stops being offered.
  The live backup of a non-production instance runs the same
  script as the download's live dump. Nothing changes for the series
  offered today, which keep click-odoo and PostgreSQL 14 to 18; the
  table is the ground the next series stands on.

### Added

- **Odoo 20.0 has its card, not offered yet.** PostgreSQL 16 to 18, `odoo
  db` (click-odoo-contrib 1.24 refuses its database commands on 20) and
  doodba template v10.0.3 or newer, so a panel pinned to v9.7.0 refuses
  a 20.0 deploy before copier runs. A series that is not offered is chosen
  by an Odoo administrator only (`may_choose`): the pickers hide it, RPC
  cannot reach it, and QA can deploy it before customers. Every dump and
  load of the scripts was run on a real Odoo 20 instance with `odoo db`;
  its database-only dump is `odoo db dump --no-filestore`, the same zip
  as click-odoo's, which the download expects.

### Fixed

- **Continuous recovery takes its daily base copy again.** The watch
  (every ten minutes) and the base copy cron (hourly) run in the same
  tick, the watch first; the status reading it queued counted as a job
  of the module in flight, and the cron skipped every instance, so no
  base copy was ever queued after the one switching on takes ("Base copy
  missing" on every archiving instance).
  When a base copy is due the watch now queues it in place of the
  reading, and both follow one rule (`_pitr_base_due`); a copy that
  failed is tried again an hour later, not at every watch.
  `incubacloud_pitr` 1.0.5.
- **A staging rendered before template v10 can be rebuilt under it.** Its
  first rebuild finds the files rewritten by copier and the old stack
  still running, with no gatekeeper, and the safe boot test refused to
  start (acme-staging, the first staging rebuilt under v10). The
  test now starts the gatekeeper alone, and removes it again if the boot
  fails, so the stack is left as it was found. The boot container runs
  with `--no-deps`: without it Compose recreated any dependency whose
  definition the template changed, restarting the live database in the
  middle of the test.
- **A one-off container no longer passes for its service.** A `docker
  compose run` container inherits its service's labels. A scale-to-zero
  proxy that groups a stack by its labels (Sablier) counted it in a sleeping
  instance's group and answered visitors with its waiting page for as long
  as it ran: an `odoo shell` of a few seconds met a visitor opening the
  instance and left it on "Oops!". The same group stopped a long one-off — a
  PITR recovery, a module update — when its session expired or Sablier
  restarted, and Traefik could route visitors to a container that serves
  nothing. And from compose 2.29 on (the hosts run 5.x) `docker compose ps
  --services --status running` names a one-off after its service, so the
  scripts that asked whether `odoo` ran — to park a stack again, drain a
  panel, restart the egress sidecar or put services back after a boot test —
  took an `odoo shell` for the panel, and the health probe took it for a
  running odoo, or an unhealthy one. Every one-off now starts through
  `ic_compose_run` (scripts) or `COMPOSE_RUN` (executors), labelled out of
  Traefik and Sablier; `ic_running_services` reads the running services by
  the `com.docker.compose.oneoff` label, the only thing every compose
  version agrees on; the health probe reads the stack's own containers the
  same way. A test in core and `incubacloud_pitr` fails on the raw commands
  (`tests/oneoff_rules.py`), which a module that extends the panel can run
  too.

## [1.0.196] — 2026-10-22

### Added

- **The panel hears when the metrics gateway accepts new accounts.** Both
  jobs that write the gateway's access-control list, the central's
  deployment and the account sync, now call
  `cloud.settings._metrics_accounts_shipped()` right after recording it.
  Core has nothing to do there; it is the hook a layer that hands
  accounts out uses to act at once instead of at its next
  reconciliation (a new account could show no dashboards for up to half
  an hour).

## [1.0.195] — 2026-10-21

### Fixed

- **A staging's rebuild passes its safe boot test under template v10.** The
  test boots the new image against a throwaway Postgres on a network of
  its own; under v10 `odoo` shares the gatekeeper's network namespace and
  Docker refuses to connect it to any other network, so every rebuild of
  a staging with an allowlist failed there (the first pull request
  preview under v10, QA of 21-oct-2026). The throwaway Postgres now
  joins the gatekeeper's namespace and the boot container finds it on
  the loopback; the gatekeeper accepts the loopback and the private
  ranges, so the staging's egress stays filtered while the test runs.
  Stacks without a gatekeeper are tested as before.

- **The tests of a staging's stack say which template they render.** They
  relied on the code's default pin, which only a fresh database takes:
  in a database that stored v9.7.0 the pin hid that seventeen of them
  failed under v10.0.8, the default the CI's database got. They now pin
  the template they test (`tests/_template.py`), and template v10's own
  test checks what they checked for the old model: the routers'
  middlewares, the login router, the prune label and the log rotation,
  on the gatekeeper.

### Changed

- **The firewall counts its verdicts on new web connections.** On a
  host behind the CDN, the forward chain's accept for each family of the
  CDN's ranges and its drop for everyone else carry a counter, as do the
  per-source cap's drops on a host reached directly; the input chain
  counts what reaches its drop policy. `nft list ruleset` then says
  whether a panel that waits on new connections had them dropped, or
  never got them; QA keeps that listing when a walk fails. The counters
  take effect on a host's next hardening.

- **The continuous recovery card looks like the rest of the panel**
  (`incubacloud_pitr` 1.0.4). Its state is a panel pill that says `OK ·
  Archiving` for a healthy one ("Archiving" alone read as something in
  progress); the facts sit in a labelled grid with the window's two ends
  and the days kept; the objective is the panel's own select with its
  label, not a control inside a sentence; the restore opens in the same
  dialog as a backup restore, on the present rather than on a guess;
  every date is in the panel's format, in local time. The card now says
  when the host was last read: "last archived 10 min ago" with a 5-minute
  objective was the watch's ten-minute cadence, not the archive behind.

- **Doodba template v10.0.8 is the default pin** (was v9.6.1), the
  first that supports Odoo 20. It is the default of a new panel's
  setting only: a running panel keeps the version stored in **Settings →
  Doodba template version** until it is changed there, the canary of
  RB-15. Production stacks are rendered the same; a staging
  with an allowlist closes its egress with the template's `gatekeeper`,
  which owns odoo's network namespace and filters with nftables, instead
  of the `proxy_general` + `odoo_net_setup` pair. Each stack keeps the
  model of the template that rendered it until its next rebuild
  (`stack_template_ref`), and the health probe, the deploy and
  `expected_services()` read it. The override pins the gatekeeper's
  image by digest and gives it the restart policy the template omits;
  `compose_op.sh` restarts odoo after any start of the gatekeeper so it
  joins the namespace the gatekeeper has now; an empty allowlist renders
  the host's list, the meaning it always had, since the shared whitelist
  network is gone. The probe grades an unhealthy gatekeeper as the
  egress filter lost.

### Documentation

- The user guide gains *Restore to any minute* (what the card shows, how
  a restore runs, what it undoes), and the runbooks RB-23 for its three
  alerts, a restore that never ended and an instance that moved.
- `architecture.md` describes both egress models of a staging, and RB-15
  what the v10 bump taught.

## [1.0.194] — 2026-10-20

### Changed

- **The metrics gateway ships in the module.** The Traefik routes of a
  central next to its panel (`observability-traefik-metrics-gateway.yml`)
  moved from `docs/` to `incubacloud/data/`, so the release that changes
  them carries them, and a deploy can install them instead of a copy
  kept by hand drifting from the code that renders the other half of
  these routes.

- **Hardening a server just bought waits for its first boot.** cloud-init
  runs apt on a new server and holds its lock, and the cache update waited
  on it until the task's 600 s ran out (QA, 20-oct-2026). Hardening now
  waits for cloud-init to finish, where there is one, and bounds each apt
  update on its own (3 minutes, 3 retries) rather than the task as a whole.

- **A host move cuts shorter.** The 78 s a customer waited through in QA
  (20-oct-2026) included work that needs no stopped panel. The
  destination's Odoo now stops before the source's, outside the cut;
  stopping it no longer marks the instance stopped. A restore installs
  `incubacloud_connect` only when the database lacks it, skipping a full
  Odoo start (about 7 s) on every move and rebuild of an instance that
  has it. A new hook, `_move_cut_steps`, lets a layer run steps as the
  cut starts, once the source is stopped and before its data is taken:
  a layer can re-point a name there, so its propagation runs during the
  restore instead of after the cutover.

- **Other panels can frame the dashboards of a published central.** A
  central the panel publishes under a name of its own let only the panel
  frame its Grafana, so a panel on another domain showed a blank frame,
  and nothing said so: a refused frame loads all the same (QA,
  20-oct-2026). The list now comes from
  `cloud.settings._grafana_frame_ancestors`, which a layer can widen
  with the domains of the panels it serves.

- **A page reloads when a job says something, not on every line it
  writes.** A running job notifies its watchers every half second it
  wrote output, and every page answered each one with a full refetch:
  the host page reloaded its data and its job list twice a second
  through a deploy of an instance it does not even list, the hosts
  dashboard and a layer's lists on any job in the fleet (QA,
  20-oct-2026). The event now says what it is (`kind`: created,
  progress, state) and whether a progress carries a message of the
  job's own (`system`); the pages refetch on those and drop the rest.
  The live log page keeps every chunk. A static test refuses a page
  that watches the events without the filter, and QA's walk 7 bounds
  the host page's reloads through a move.

### Tests

- The gateway's tests (who may frame Grafana, which operator routes are
  denied) no longer skip where `docs/` is stripped: they read the file
  from the module, and so run in the deploy's own check of the image too.
- The host-key tests that ask OpenSSH itself (`ssh-keygen`) for the
  fingerprint and the `known_hosts` lookup no longer skip in CI: its test
  job installs `openssh-client`, and without the tool they fail.
- **Every test runs.** Fifteen classes based on `unittest.TestCase`, which
  Odoo's runner never collects, held 50 tests nobody had run; 13 had
  drifted from the code (the prod services now come from the instance,
  pip conflicts are written as markers, the transport awaits its line
  callbacks). They run on `BaseCase` now, and a CI step refuses a test
  class Odoo would not collect.
- **The front end's tests run.** The 824 hoot tests under `@incubacloud`
  only ran when someone opened `/web/tests`; an HTTP test now runs them
  headless with the Python suite (and in the deploy's check of the
  image), failing without a browser instead of skipping. Six of them read
  translated strings with no catalogue loaded.
- The key rotation tests build their own two-key state instead of
  skipping on an environment that holds one key, the Traefik relabelling
  check runs whether or not a panel filters by instance yet, and the
  export's bats tests fail when copier, rsync, Docker Compose or GitHub
  is missing instead of skipping.

## [1.0.193] — 2026-10-20

### Changed

- **Warnings reach new users too.** The default notification level was
  "Failures only", which sent failed jobs and critical alerts: a failed
  backup archive, a backup or database container down or a production
  instance without backups were warnings that reached nobody. A new user
  now starts at "Failures and warnings"; an existing user keeps the level
  they had.
- **Every alert goes through the same engine.** Fourteen producers created
  their alerts by hand, each with its own search for an open one: no
  confirmation policy, no new notice when a warning turned critical, and
  identities that drifted apart. They all call `raise_alert` now, which
  learned what they needed: a key for what the target alone does not tell
  apart (two backup backends, two types of failed job), a project for an
  alert about a whole project, and an instance alert that stays the same
  alert when the instance moves. The database's unique index follows the
  same identity, and the upgrade gives the open alerts their key.
- **A health check that finds nothing keeps only its verdict.** Its log is
  held until the check ends: one that passed and raised nothing writes its
  last line, any other writes every line, as before. Written in full every
  five minutes for every instance, those logs were 83% of the panel's job
  log rows.

### Fixed

- **Restore and Download line up in the backups list.** Download sits at the
  edge of every row, with or without a Restore beside it, and both buttons
  carry an icon.
- **The cards that create a project's first instances are centred**, side by
  side, and the members and SSH allowlist panels use the whole width like
  the rest of the forms.
- **A development export leaves continuous recovery out**: `.walg/` (wal-g,
  the host's libraries it runs with, the logs of past restores) no longer
  reaches the archive. `DEVELOPMENT.md` initialises the database before
  starting Odoo, since `invoke resetdb` stops it, and says where it answers.
- **A neutralized copy no longer reaches out of itself.** A copy of a panel
  (development, QA, a neutralized download, a partner's staging) lost its
  secrets but kept three ways to act on the real world: the users' webhooks,
  called unsigned by its jobs and alerts; production's queue, whose
  unfinished jobs would run there; and its metrics, still pushed to
  production's central. Neutralizing now clears the webhooks, cancels the
  unfinished jobs with the `cloud.job` that mirrors each, switches metrics
  off and closes the consoles left open. The file's header names the
  commands that really run it.
- **A neutralized copy keeps its crons off through its next update.** Odoo's
  neutralization switches every cron off, and 34 of the panel's (32 in
  `incubacloud`, 2 in `incubacloud_pitr`) declared
  `active` in updatable data, so the copy's first module update switched
  them back on. They no longer declare it: a new cron is still born
  active, and a cron someone paused stays paused through an update.
  `incubacloud_pitr` 1.0.3.

### Tests

- `export_starts.bats` renders the project as the deploy does (the pinned
  doodba template, the answers the panel emits), exports it and runs
  `docker compose config` on the copy; a test keeps its pinned version in
  step with the panel's. The CI installs copier for it.
- `NeutralizeInventory`, which a repository mixes in to check its own
  modules: every stored field that points outside the copy is cleared by a
  script or exempted with its reason, and no updatable cron declares itself
  active. A test neutralizes a copy for real and checks what it can no
  longer reach.
- The hourly pass that closes unreadable-secret alerts is checked for its
  schedule only: on the copy a deploy tests against, its crons are paused,
  and an update no longer switches them back on.

## [1.0.192] — 2026-10-20

### Fixed

- **A host's SSH allowlist is kept over a busy row.** After a change to a
  host's SSH allowlist the panel keeps the list the server now allows on
  the host's record. It kept it in the transaction that changed the
  firewall, which can be long (a caller that changes several servers in
  one shell), while other work wrote that record: the write failed as a
  serialization conflict and the caller's work did not go on (QA,
  20-oct-2026).
  The list is now kept at once on a cursor of its own at READ COMMITTED,
  which waits for the other write instead.

## [1.0.191] — 2026-10-19

### Fixed

- **A console ended with `exit` no longer blocks the next one.** The server
  keeps one open console per user and host, and the web terminals ended
  their session only when their tab was closed: a shell left with `exit`
  stayed open on the server, and a second console on that host was refused
  ("You already have an open console") until the tab was closed or the
  routes' clean-up came. Both terminals, a host's and an instance's, now
  end their session as soon as the shell ends, and still when the tab
  closes.

## [1.0.190] — 2026-10-19

### Fixed

- **A copy taken before 1.0.189 is neutralized again.** A copy is
  neutralized before it is upgraded (a QA refresh, a neutralized download, a
  restore into development), and clearing the instance key named a column
  such a copy does not have yet: one unknown column aborts the whole
  neutralization, Odoo's own included. That column is now cleared on its
  own and skipped where the copy predates it.
- **Switching continuous recovery on no longer restarts the instance just
  announced ready.** Switching it on as soon as a new instance was
  deployed, as a layer may, restarted its database with Odoo stopped: a
  customer opening the instance from the mail that said it was ready met
  an error and a page that never loaded. A stack with continuous
  recovery now starts its database archiving, and switching on is a reload;
  switching off lets the segments go until the rebuild that follows, instead
  of a restart. `incubacloud_pitr` 1.0.1.
- **Continuous recovery could read a running database as stopped.** Whether
  a service runs was read with `grep -q` behind `docker compose ps` under
  `pipefail`: a `grep` that stopped at its match while `docker` was still
  writing failed the pipe, and switching on refused with "the database is
  not running" (seen in the tests under load). The whole list is read now.
- **The continuous recovery card ends where its jobs leave it.** It read
  the status once, as a switch was queued, and never again: switched off,
  it still said "Archiving" until the page was reloaded. While a job of
  the module runs for the instance it now reads the status every few
  seconds, and says "Switching off…" while it switches off.
  `incubacloud_pitr` 1.0.2.

## [1.0.189] — 2026-10-19

### Added

- **Continuous recovery (`incubacloud_pitr`, a new module).** A production
  instance can be restored to any minute of its backup window: its database
  hands its write-ahead log to its own backup bucket (wal-g, compressed and
  encrypted on the host, with a daily base copy) and its filestore is copied
  as it changes (rclone, encrypted). The credentials live in a container of
  their own, the `pitr` agent, never in the database's, whose SQL Odoo runs
  as a superuser. A restore runs detached on the host, puts the previous data
  back if the restored copy does not come up, and is followed by the panel
  across jobs. Off until a manager switches it on for an instance, from its
  Backups tab, which then shows the window and restores to a moment
  confirmed by the instance's name; a staging never archives. The objective
  (how much it may lose) is 1, 5, 15 or 60 minutes. Uninstalling is refused
  while an instance archives. See `docs/architecture.md`, "Continuous
  recovery".
- **The panel keeps each instance's encryption key.** An instance running
  this module encrypts its secrets with the key in its
  `.docker/incubacloud.env`, which existed on its host only: a move or a new
  host generated another and the restored database could read none of its
  secrets (seen on two panels). `cloud.instance.secret_key` is now born with
  the instance, merged with what the host holds and written back on every
  deploy and rebuild, and a layer's own (no key is ever dropped), read on
  the origin during a move, taken from production by a refresh left live (a
  neutralized copy keeps its own), and captured once a day for instances
  deployed before (`capture_secret_key`, `secret_key_not_escrowed`).
- **An alert on a secret that will not decrypt closes itself** once the
  value reads again (hourly, and at startup), as does a stranded rotation.
- **Who may open SSH on a host changes live.** The hardening ruleset keeps
  the list in a named set (`ic_ssh_allow`); `cloud.host._ssh_allowlist_change`
  adds or removes addresses without Ansible and keeps them in
  `allowed_ssh_ips`. A host hardened before is converted on its first change.
- **Addon hooks on an instance's stack**: `_extra_services`,
  `_compose_override_fragments`, `_extra_env_files`, `_teardown_pre_steps`,
  `_after_stack_rendered`, `_after_restore_hook`, and
  `cloud.job._get_health_blocking_job_types`. Without an addon nothing
  changes. A hook that raises from a job's success is rolled back and
  logged; the job's outcome stands.
- The executor write rule's checker takes an addon's own hook helpers
  (`check(..., helpers=)`), which may call each other.
- **Provider alerts are inventoried** (`PROVIDER_ALERT_CODES`), and a test
  fails on one raised without being listed.

### Changed

- `_on_move_rolled_back(target_host=None)`: the hook is told where the move
  was going, read before the markers are cleared.
- Restored copies of production are neutralized of the new keys too.

## [1.0.188] — 2026-10-17

### Fixed

- **A deploy that cannot start waits on its alert instead of failing,
  and the instance is not stranded in 'deploying'.** The addon
  pre-flight raised: the customer got the conflict alert, then a
  `job_failed` for the same news, and the support mailbox a third mail
  from queue_job's chatter; `on_failure` only follows steps that ran, so the
  instance stayed in 'deploying' and could be neither deployed again nor
  deleted. The pre-flight now runs as the
  executor's `blocking_alert`: the job ends blocked on one
  `addon_conflict` alert, the instance goes back to 'draft', and
  resolving the alert dispatches the job again — on the host the
  instance is on by then, cancelling the other jobs of its type that
  waited on it. Deploying again while it waits says so; once the alert
  is closed it dispatches the waiting job, and while it is not, looks
  for conflicts again first: the repositories may have been fixed in
  GitHub. The browser is told the job is blocked, not that it completed;
  a blocked job can be cancelled, from its timeline too; and a deploy or
  rebuild that goes through cancels the jobs that waited on the conflict
  alert it closes, which used to wait on a closed alert for ever. If two
  transactions file the alert at once, the one that cannot see it
  refuses the deploy rather than run it.
- **A step of a chain is never left waiting.** queue_job stores a
  blocked job as done and starts the next step: a move would have
  stopped its source and restored onto a target never deployed. A step
  whose `blocking_alert` answers fails instead, the chain is cancelled
  as before, and its alert is raised on a cursor of its own so the
  failure does not roll it back.
- **A job that ends with no outcome gets `on_abort`.** A check that
  refused, before or inside the run, a host that would not connect, a
  terminal hook that raised: none reached `on_failure`. A failure before
  the run is now logged as one inside it is — a refusal as a warning,
  anything else with its traceback. For a deploy,
  `on_abort` is now that failure path (status `error`, back to 'draft',
  and the layers above hear of it); a rebuild leaves the instance as it
  was. Not called for an attempt that will be retried nor for a
  cancellation. Migration: instances left 'deploying' with no job in
  flight go back to 'draft'.
- **One alert per failure.** A job whose own critical conflict alert
  explains its failure raises no `job_failed`; a `pip_conflict`, a
  warning the default notification level does not send, still does.
  queue_job's "job failed" message — mailed to every Job Queue Manager
  and the job's creator, outside every notification setting — is left
  out for platform jobs. The cron bot reads its notifications in Odoo:
  it has no address.
- **A refusal is a warning, not a traceback.** The executor logs a
  failed step (whose output is in the job log), a `UserError` and a
  check of its own (`JobRefused`) without their traceback, and a
  cancellation at INFO. An `AccessError` or a `MissingError`, and
  anything a terminal hook raised, a `UserError` included, keep their
  traceback: they are defects.
  queue_job still logs its own traceback of every failed job.
- **The webhook silence check ignores GitHub's pings.** GitHub pings
  the webhook as it creates the App, before the callback stores the
  secret; refused for want of one, the ping was reported for two days
  as pushes that never arrived. A ping carries no push.
- **The import's branch field waits for the branches.** A branch typed
  before they arrived was replaced by the default one when they did,
  and the import took `main` (QA walk 6g2).

### Added

- **Addon conflicts are checked where repositories are set** — after an
  import, within the time the import has left, every GitHub request
  included (its summary says so), and within 20 seconds when an
  instance is created, when its repositories are saved and when a
  deploy that waits is asked for again — instead of at the first
  deploy. A save
  that leaves none closes the alert and dispatches what waited; a
  repository GitHub would not list closes nothing.
- **Hooks**: `on_unblocked` on executors, the counterpart of
  `on_blocked`; `_move_post_restore_steps`, for the layer above to run
  steps on the target between a move's restore and its cutover; and
  `_error_block_is_expected` on the health probe, which leaves an
  ERROR record out before records are grouped and capped.

## [1.0.187] — 2026-10-16

### Fixed

- **A job cancelled while it runs ends cancelled, instead of running
  again from the start.** Cancelling a started job raises a flag on its
  ``cloud.job`` row that the executor stops on. The job's own
  transaction began before that flag was written, so storing its end
  updated a row changed since: PostgreSQL refused it, queue_job put the
  job back in the queue five seconds later, and the retry wrote
  ``started`` over the flag and ran the whole job again. No cancellation
  of a running job had ever ended cancelled; in QA one was retried 24
  times. A started job queue_job puts back in the queue while its cloud
  job carries the flag is now stored as the cancellation it is.

### Tests

- **The pinning tests for a panel without GitHub credentials remove the
  App too.** They cleared the PAT only, so on a copy of a panel that has
  a GitHub App, where a deploy may run the suite, the branch head came
  through that App and the anonymous path they test was never reached:
  the deploy stopped there.

## [1.0.186] — 2026-10-16

### Fixed

- **An instance without a backup container can move to another host.** The
  move took a duplicity snapshot through the instance's own ``backup``
  container before carrying it over, and an instance may have none (a layer
  can leave it out to save memory): such a move stopped at "service backup
  is not running" and was undone. With ``odoo`` stopped on the source the
  database is quiet, so the move now carries a live dump of it with its
  filestore (``backup_download`` with ``time='live'``), handed to the
  restore through the panel's disk as before. It needs neither the backup
  container nor a bucket, so ``move_to_host`` no longer asks for a backup
  backend, and the data skips the round trip through the bucket.
- **A one-off dump container stays out of Traefik.** The live dump and a
  non-production backup run ``click-odoo-backupdb`` in a ``docker compose
  run`` container, which inherits the service's labels: the proxy could
  send visitors to it while it ran, and they got a 502.

## [1.0.185] — 2026-10-15

### Fixed

- **Pinning a repository works from a panel with no GitHub credentials,
  and says what is wrong when it cannot.** Saving a project deleted and
  recreated its repository rows, so the editor's rows carried ids that no
  longer existed and the pin answered "Repo not found"; rows are now kept
  by id, as an instance's are, and a new row gets its id back. The
  branch-head route tried the GitHub App and the PAT only and gave up
  with "Unexpected error"; it now reads public repositories anonymously,
  like the branch list, and otherwise answers the same message as the
  branch list, with the link to the settings. A branch that is not in a
  repository GitHub does show is named as the problem.

## [1.0.184] — 2026-10-15

### Fixed

- **Moving an instance to another host works again.** A move deploys the
  instance on its target while it is still live on its source, so it is
  already `deployed`; the deploy's success hook still asked the state
  machine for `deployed` -> `deployed`, which it refuses. Every move failed
  at its first step from 1.0.20 (the state machine, 2026-07-30) until it
  was found on 2026-10-15. The deploy now
  leaves the state alone during a move; the cutover is what moves it. A
  new test closes every job of a move in order on the same instance,
  through the executors the job runner picks, which is what none of the
  move tests did.

### Added

- **A hook for a move that was undone.** `cloud.instance._on_move_rolled_back()`
  fires once a rollback has cleared the move markers -- after the target
  is cleaned, or when the watchdog gives up cleaning it -- with the
  instance running on its source again. Core does nothing with it; an
  inheriting layer can give back what the move took.

## [1.0.183] — 2026-10-14

### Added

- **A login limit of its own for every instance.** The proxy limited
  each client to 300 requests a minute on every path alike, which let one
  address try 300 passwords a minute, and Odoo's own cool-down counts per
  worker process. Every instance now gets a login router beside each of
  its routers -- POSTs to `/web/login`, `/web/login/totp`,
  `/web/session/authenticate`, `/web/reset_password` and
  `/xmlrpc/2/common` -- with the host's new `loginlimit` middleware first
  (20 a minute, burst 10, tunable live) and otherwise the template's own
  middlewares, so the login is never less protected than the rest of the
  site. Behind a CDN it keys on the real client like the general limit.
  Priority 15: above the template's routers for the same paths, below any
  router set in front of an instance at 30 or more. It lands on each
  instance's next deploy or rebuild.
- **Repeated failed logins are banned at the host firewall.** fail2ban
  watched SSH only. A new jail bans for an hour an address that fails ten
  web logins in ten minutes, on the forward hook and on 80/443 only. It
  reads the proxy's access log, not Odoo's: Odoo writes the login it was
  sent as-is, so a login carrying a newline could forge whole lines and
  get anyone banned, whereas Traefik writes JSON and escapes what the
  client sends. A form login that fails is answered 200; one that
  succeeds is redirected. Off on a host behind a CDN, where the connecting
  address is the CDN's; whatever the proxy trusts as a proxy is exempt.
  The jail has a chain of its own in fail2ban's table: with the action's
  default name its rule landed in the chain the sshd jail creates on the
  input hook wherever the distro bans with nftables (Ubuntu 24.04), where
  instance traffic never passes, and banned nothing.
- **Authenticated Origin Pulls on hosts behind a CDN.** A host can be
  given the authority the CDN's client certificate is signed by
  (`cdn_client_ca`); with direct access refused, the proxy then requires
  that certificate on every TLS connection. The CDN's addresses are shared
  by all of its customers, and only the zone's own certificate tells its
  traffic apart from anybody else's pointed at the same origin.
- **Hosts' security logs can be sent to the central** (SSH and sudo from
  the journal, auditd and fail2ban), where they are kept 90 days and a
  host's credential can add lines but never delete them. Off by default
  (`metrics_ship_security_logs`).

### Changed

- **The connection-rate cap is on by default (50 per second per
  source).** It shipped off since 2026-08-14 pending a rehearsal, and its
  rule also counted the containers' own outbound 80/443, so an Odoo
  calling an API in a burst was throttled like a visitor. It now weighs
  only connections redirected to the proxy, with the same guards as the
  CDN allowlist. New hosts get 50; hosts at 0 are moved to 50 and the cap
  applies on their next hardening run. Behind a CDN no cap is rendered,
  whether or not the host also refuses direct traffic: every visitor
  arrives from the same handful of edge addresses there, and a host still
  open at the origin -- the state a host is in between moving its names
  behind the CDN and closing its firewall -- would have throttled the CDN
  itself.
- **The proxy's access log has its own file, kept 30 days.** It went to
  stdout, where Docker kept 30 MB in all -- hours of history on a busy
  host -- under a path that changed with every recreate of the container.
  It is now `/var/log/traefik/access.log`, rotated daily or past 100 MB
  (looked at every ten minutes by a timer of its own, since the distro's
  daily run alone would let a flooded host fill its disk between two
  runs), compressed and removed after 30 days; the panel's "Recent requests",
  the log collector and the web-login jail read it ("Recent requests"
  also reads the end of the previous file, so it is not empty right after
  the nightly rotation, and reads the container until the first Full
  Setup fills the file). Hosts move on their next Full Setup. The central
  keeps access logs 30 days (was 14).
- **The proxy's file provider reads a directory.** The default
  certificate and the CDN's client-certificate requirement are dynamic
  documents of their own, and Traefik reads either one file or one
  directory: with the shipped `filename` form they were written and never
  read, so on a core-only install a host said to require the certificate
  required nothing. The template now names `/etc/traefik/dynamic`, the
  compose file mounts it, Full Setup copies `config.yml` in (the
  middlewares every entrypoint names live there), and a host stored with
  the single-file form is rendered in the directory form on its next
  setup. The proxy push, which ships no compose file, keeps the
  single-file form on a host whose container has no such mount yet.
- **auditd keeps weeks, not days.** It rotated by size in five 8 MB
  files, which on a busy host was two or three days, three quarters of it
  BPF events systemd raises for every container start and stop. Those are
  excluded and the log holds twelve 25 MB files.

### Fixed

- **A hardened host audited nothing until its next reboot.** On Ubuntu
  22.04 a journald started without an `Audit=` setting switches the
  kernel's auditing off, and hardening restarts journald to apply its
  limits -- after auditd has loaded its rules. auditd kept running and
  wrote nothing. The journald drop-in now says `Audit=yes`, which keeps
  auditing on and turns it back on for hosts hardened before, on their
  next hardening run.
- **A private key pasted along with the CDN authority is refused.** The
  parser read the first certificate and ignored the rest, so the key
  would have gone unnoticed and landed on every host in a readable file.

## [1.0.182] — 2026-10-14

### Added

- **A notification level for failures and warnings.** The level was one
  setting for jobs and alerts alike, and "Failures only", the default, let
  through critical alerts and nothing else: a warning reached no email,
  Telegram, webhook or digest, only the panel, and most close on their own
  before anyone opens it. They went unseen — an instance's database
  container down, disabled crons, a failed backup archive. The new level,
  between "All" and "Failures only", sends failed jobs and every alert,
  critical and warning, and the daily digest follows it. Existing users keep
  their level and "Failures only" stays the default. The selector names what
  each level covers, and its labels are now translated: they sat inside a
  template expression, where no string is.

### Fixed

- **A warning that turns critical while open is announced again.**
  Notifications went out when an alert was created, so an open warning
  refreshed as critical — a metric rule raised to critical, say —
  reached nobody whose level had
  filtered out the warning. Refreshing at the same level, or stepping
  down, still sends nothing.
- **A resolution notice reaches whoever was paged.** An alert that turned
  critical, stepped back to a warning and then cleared closed without a
  word to the people whose level had been reached by its critical turn.
  The alert remembers it was critical, and its closure goes to them.
- **The notification texts no longer speak only of jobs.** The field,
  the email and webhook help in the notifications dialog and the user
  guide said "job notifications" for a setting that also decides which
  alerts arrive.

## [1.0.181] — 2026-10-14

### Added

- **Which alerts confirm is one table.** `cloud.alert.policy` holds,
  per alert code (or per family, `instance_service_*_down`), how many
  sightings in a row make a measured condition an alert and how fresh
  each must be. `raise_alert` reads it for every caller that brings no
  count of its own, so the producers that measure — the health probe,
  the SSH disk probe, the metric-rule cron's backend check — raise their
  code and nothing else, and 1.0.180's "never on the first sample" no
  longer depends on each producer remembering to ask. Shipped rows, all
  at two sightings within three ticks: the metrics backend (moved out of
  the code), `instance_down`, the companion containers and
  `disk_critical`. A code with no row is an event and raises on the spot;
  metric rules keep their own `confirm_cycles`, because their codes are
  the operator's.

### Changed

- **A stopped container is an outage on the second probe, not the first.**
  A deploy, a rebuild or an instance waking up restarts odoo for seconds,
  and a health probe landing inside that window mailed the customer a
  critical "Container 'odoo' is not running" that the next probe
  resolved. The same for the companion containers and for the SSH disk
  reading, which a backup dump or an image pull spikes for minutes. The
  instance's status still turns to error on the first probe; the alert,
  and the mail, wait for the second, about five minutes.
- **The webhook-silence alert measures what GitHub tried against what
  arrived.** Silence alone could not tell a broken edge from a quiet
  repository: a customer who pushes twice a week was told their pushes
  were not reaching the platform. When nothing has been recorded for an
  hour, the check now reads the App's delivery log at GitHub and
  compares it, by delivery id, with the events recorded in the window.
  A window with no attempts raises nothing; attempts old enough to have
  landed that have no row here raise the alert, which says how many of
  how many and what GitHub was answered (a 403 names the edge allowlist,
  a 200 says the delivery was lost past it). A log GitHub cannot serve
  decides nothing that hour.

---

## [1.0.180] — 2026-10-14

### Added

- **A measurement confirms itself before it becomes an alert.**
  `cloud.alert.raise_alert` takes `confirm_after`: the first sightings of
  a condition are kept as a `cloud.alert.suspicion` -- not listed, not
  counted, not notified -- and the alert only exists once the condition
  has been seen that many times in a row, each within `confirm_window`
  seconds of the last. A clear reading, or the alert's own resolution,
  forgets the suspicion. Events keep raising on the spot. This is now
  the one place the rule "never on the first sample" lives, instead of
  a streak counter in each producer that remembered to have one.
- **Metric rules declare how many evaluations confirm them**
  (`confirm_cycles`, default 2). The shipped rules on an instant
  reading -- disk, memory, load, instance silence -- confirm over two
  ticks; those that already measure over a window -- host silence
  (1 h), instance 5xx (`rate[5m]`) -- keep raising on the first.
- **An alert says who can act on it.** `cloud.alert.audience` is
  `fleet` -- whoever runs this panel's hosts, instances and repositories
  -- or `provider` -- whoever runs a service the panel depends on but
  does not own. On a panel that owns everything both are the same person
  and nothing changes. A hosted panel's provider layer overrides
  `_route_provider_alert()` to take provider alerts to the provider; the
  panel still shows them, but its operator is no longer paged about
  something they cannot touch. Marked `provider`: the metrics central
  when none was deployed from this panel, an undecryptable secret
  (`INCUBACLOUD_SECRET_KEY` is the platform's), an instance record that
  could not be deleted after its stack was, and managed containers or
  networks a prune swept away.

### Changed

- **One slow answer from the metrics central is not an outage.** A panel
  that reaches its central through a CDN saw one edge deliver a query 9 s
  late while the origin answered in milliseconds: the 10 s read timeout
  fired, the panel raised the critical "metrics backend unreachable" alert
  on that single failure and its operator was paged by email, five minutes
  before the next tick resolved it. `promql_query` now asks once more on a
  fresh connection after a transport failure or a 5xx (never after a 4xx or
  a PromQL error, which would answer the same twice), for the rule, liveness
  and specs crons alike, and the backend alert needs two failed evaluations
  in a row, about ten minutes.
- **Alert mails say which panel sent them.** Every panel of a fleet
  mails the same operator mailbox with an identical subject and a
  relative log link that opens nothing. The subject now carries the
  panel's host (`[IncubaCloud] arn-corp.example.com · Critical alert:
  …`), the body names it, and the log links are absolute, as the
  Telegram and webhook ones already were.
- **Alert mails show the message and the names as text.** The body
  interpolated them raw; now that a provider alert can cross from one
  panel to another carrying names someone else chose, a
  link forged into an instance name would have been live in an
  operator's mail. Escaped, for local producers too.

---

## [1.0.177] — 2026-10-13

### Fixed

- **An address that is not a repository is answered as such.** A
  customer pasted a branch folder from the browser
  (``…/tree/staging/addons``) into the repository editor. The branch
  list parsed it inside each credential attempt, took the parse error
  for a missing GitHub App, answered "No GitHub credentials configured",
  and logged the error with its traceback, which raised a log alert on
  the customer's panel. The routes that read a repository (branches,
  branch head, modules, requirements) now check the address first and
  answer with the repository form's own message, without asking GitHub
  and without an error in the log.
- **A job cancelled while it runs no longer raises a log alert.** 1.0.175
  stored the stop as a cancellation, but it still reached queue_job as an
  exception, which queue_job logs with its traceback before storing the
  job: the log scraper reported every cancellation as an
  ``instance_error_logs`` alert on the customer's panel. ``execute`` now
  returns the stop with a marker, queue_job stores it as done without a
  traceback, and ``queue.job.write`` records that ending as the
  cancellation it is. A job that finished by itself as the person pressed
  cancel carries no marker and stays done.

## [1.0.176] — 2026-10-12

### Fixed

- **Connecting a GitHub App no longer fails now and then with "invalid
  state".** The setup kept its ``state`` in the session, and Odoo
  rewrites a session whole whenever it records a new device for it —
  behind the CDN, any request arriving from another edge address. One of
  those landing on another worker while the setup page saved the state
  erased it, and the customer came back from GitHub to "invalid state"
  (3 setups in 30 on three workers). The state is now signed with the
  database secret for the user who started the setup and expires after
  ten minutes, so the callback checks it without reading the session.

## [1.0.175] — 2026-10-12

### Fixed

- **A job cancelled while it runs ends cancelled, not failed.** queue_job
  cannot stop a started job, so ``cancel_job`` sets a flag the executor
  polls and the executor stops by raising, which queue_job stored as a
  failure: a customer who cancelled a rebuild got a failure alert and a
  "failed" email, then a "Resolved" one when the next rebuild finished.
  The executor now stops with ``JobCancelled`` and ``queue.job.write``
  records the stop as the cancellation it is — no alert, no failure
  email, no queue_job failure message — and closes what waited on the
  job in its chain as cancelled, as a failure already did.

## [1.0.174] — 2026-10-12

### Added

- **Hosts can be hardened from the host page.** The security recipe has
  been part of this module, but nothing in its interface queued it, so a
  host was only hardened if something above this module did it. The host
  page now has **Apply Hardening** (**Re-apply Hardening** once done) in
  its action bar, for whoever manages hosts. It opens a confirmation that
  checks the host first — running and production instances, a hardening
  already queued, an untrusted SSH key — warns that services installed
  outside Docker lose their ports and that the first run restarts Docker,
  asks for the host's name when production runs there, and offers
  hardening and Full Setup as one chain when a root host was never set
  up. Behind it, ``/cloud/preflight_host_hardening`` and
  ``/cloud/apply_host_hardening``; ``_before_host_hardening`` lets a
  downstream module prepare the host before the job is queued.
- **A newly connected host is offered the hardening.** Trusting a host's
  SSH key for the first time opens the same confirmation, which says the
  hardening is optional and can be dismissed with "Not now".
- **A host that has not been hardened says so.** Its page shows "Not
  hardened" next to its name, and ``/cloud/get_host`` returns
  ``hardened`` and ``hardened_at``.

## [1.0.173] — 2026-10-12

### Fixed

- **"Export for Development" no longer ships credentials.** The export
  rewrote three env files with placeholders but copied the rest as they
  were: ``.docker/smtp.env`` reached the tarball with the instance's
  real SMTP relay password, the project's ``.env`` with its production
  compose project name, and ``odoo/custom/conf.d`` with whatever the
  instance's Odoo conf held, an ``admin_passwd`` too if one was set. No env file
  is exported any more, the Odoo conf loses every option whose name
  speaks of a password, secret, token or key, and the copier answers
  lose their backup and SMTP relay settings.

### Changed

- **The export is a development copy.** devel.yaml reads no env file, so
  ``.docker/`` and every ``*.env`` stay out instead of travelling as
  placeholders; the instance's ``logs/`` stay out too, and the
  production proxy options leave the Odoo conf. ``docker-compose.yml``
  points at ``devel.yaml``, and a ``DEVELOPMENT.md`` gives the steps to
  start the copy, load a neutralized backup and, for whoever wants to
  run ``prod.yaml`` elsewhere, which env files to write.

## [1.0.172] — 2026-10-12

### Changed

- **A host renders its proxy's compose file in one place.**
  ``cloud.host._shipped_inverseproxy_yaml`` joins
  ``_shipped_traefik_yml``: Full Setup uploads what the host renders, so
  a module that adds to either file extends the method instead of
  rewriting the uploaded file after core. Such a rewrite after core had
  dropped the CDN's trusted ranges from hosts set up behind the CDN.

## [1.0.171] — 2026-10-11

### Fixed

- **A pull request preview keeps the id of its comment.** GitHub's
  comment ids are past 2,147,483,647, the largest value of an Odoo
  integer column, so storing the id of the "preview ready" comment
  failed with "integer out of range": the preview ended in error
  although it was running, and its comment could no longer be edited or
  deleted. The id is now kept in a 64-bit column (``BigInteger``), which
  Odoo converts in place on upgrade.

## [1.0.170] — 2026-10-11

### Fixed

- **A clean delete closes the backup alerts an earlier attempt raised.**
  The archive and purge alerts are raised for the instance on its host,
  and an alert's identity includes the host, but a clean run closed them
  for the instance alone, so it never found them and they stayed lit.
  The same for the purge of an archived copy.
- **Archiving an instance closes every alert about it.** Nothing probes
  an archived instance, so an alert still lit about it (down, error
  logs) stayed lit for ever. Its host's own alerts are left alone.
- **An archived instance no longer shows its last health check.** Its
  services and status are dropped when it is archived: nothing checks
  it any more, so a deleted instance showed its database and
  mail relay in green, and "error", after its containers had been
  removed. A migration clears the instances archived before.

## [1.0.169] — 2026-10-11

### Fixed

- **Deleting an instance while keeping it in the panel archives its
  backups.** The archive step started the backup container with
  `--entrypoint sh`, which skips the step where the image installs the
  PostgreSQL client matching its `DB_VERSION`, so the dump died with
  `psql: not found` and the deletion stopped before touching anything.
  It now goes through the image's own entrypoint, as the nightly
  backups do, and the dump waits up to a minute for the database the
  step starts on a stopped instance. A failed archive also leaves the instance as it found it:
  the services the step started (the database, the mail relay) are
  stopped again, and only those, when what was running before could be
  read.

## [1.0.168] — 2026-10-10

### Fixed

- **An instance under a long domain deploys.** doodba names the odoo
  container after the instance's domain, and Docker refuses a hostname
  over 64 bytes, so a production under a long domain
  (`<instance>.<subdomain>.<customer>.example.com`) stopped at
  "Initialize database".
  Only the backup container's name was shortened. Every container
  hostname over 64 bytes is now cut at a label boundary, keeping its
  leftmost labels, on deploy and on rebuild, in every environment.
- **The panel can delete series on a central behind a CDN.** The
  central's series-deletion route only answers the panel's address, and
  behind a CDN the connection comes from the CDN's edge, so every
  deletion was refused and `metrics_purge_failed` raised. When the
  central's public name reaches its host through a CDN, the route now
  checks the address the CDN forwards, as the host's rate limit already
  does. A caller reaching the host directly still cannot hand it that
  address: Traefik only keeps the header from the ranges it trusts.

## [1.0.167] — 2026-10-10

### Fixed

- **A host's scheduled reboot no longer shows up as an error in the log.**
  A host with automatic security updates reboots at 04:00 when a kernel
  update lands, and refuses connections for a few minutes before. The
  monitoring probes retry through it, but every attempt was logged as an
  ERROR with its traceback, so whoever watches this database's log saw an
  incident each time. An attempt that will be retried is now a warning;
  a host that stays away still raises `host_unreachable` once the retries
  run out, and any other failure is still an error.

## [1.0.166] — 2026-10-09

### Added

- **Every email of the module goes out in one company layout.** Alerts,
  job notifications and the daily digest were bare HTML, the digest with
  one brand's green written into it, and the backup quota template had no
  layout. They now share `incubacloud.mail_layout`: the company's logo
  above the message, its email button colour as the accent, and its name,
  website and contact address in the footer. Nothing in it names a brand,
  so each installation's emails carry its own. Other modules' mail
  templates can use it through `email_layout_xmlid`. A database that
  cannot serve its own logo to mail clients, such as one that sleeps,
  names another address in `incubacloud.mail_logo_url`.

## [1.0.165] — 2026-10-09

### Changed

- **A project starts with production; staging waits until there is one.**
  A customer used to platforms where an instance starts on a development
  copy made a staging the only instance of their project. Here a staging
  is a copy of production: an empty project now offers production alone,
  with the staging card explaining that, the sidebar's add button for
  staging is off until production exists, and a new instance form opens
  on production until the project has one.

## [1.0.164] — 2026-10-09

### Changed

- **Delete is in the header of a host, a project and a backup
  destination, as on an instance.** It sat only at the foot of their forms,
  below every field, and a host page opens on Overview, which has no foot:
  a customer could not find how to delete a host. The header now ends with
  the same trash icon an instance has; the button at the foot stays.

### Fixed

- **A host's address must be one SSH could reach.** The panel took any
  text as a host's IP address; a server registered at `232.123.321.22`
  (which reads as an IPv4 address and is not one) then failed its setup
  asking to trust an SSH key no such host could have. The address must now
  be an IP address or a host name.
- **The panel has its icon and its name in the browser tab.** Its pages
  declared no icon, and a panel without the website module had none to
  serve, so the tab showed the browser's globe; the main page was also
  titled "Cloud". They now declare IncubaCloud's icon, and the main page is
  titled "IncubaCloud".
- **A stalled apt download no longer holds a host's hardening.** The cache
  update waited on a connection for as long as it stayed open: on a new
  host it hung 17 minutes on one the mirror had already closed. apt's
  downloads now time out after 30 seconds and are retried, and the cache
  update itself is stopped and retried after 10 minutes.

## [1.0.163] — 2026-10-09

### Changed

- **No host to pick: a button, not a sentence.** The new-instance form now
  shows a short note and an "Add a host" button that opens Hosts, where a
  server is added. A customer asked how to pick a host
  with the sentence pointing there in front of them.

## [1.0.162] — 2026-10-09

### Fixed

- **A host still being set up is not enrolled in monitoring.** The
  reconciliation cron took a host as ready for the agents once the panel
  had its SSH key, which a new host can have minutes after it is added,
  long before Full Setup installs Docker. It queued the agents
  beside the hardening, and they failed on a missing `docker`. The cron
  now waits for Full Setup to have run, which itself queues the agents as
  it ends.

## [1.0.161] — 2026-10-09

### Fixed

- **The new-instance form says what to do.** The name's example read
  `myproject-staging` on a production instance too; it now follows the
  environment (`myproject` for production). With no host ready, the form
  said only "No compatible hosts available"; it now says to add a server,
  or finish its setup, in Hosts, and links there.

## [1.0.160] — 2026-10-08

### Fixed

- **An alert that resolves itself says so.** A failed job's alert is
  dismissed when a later run of the same job on the same target succeeds,
  and that dismissal was silent: the failure had reached email and
  Telegram, and the Alerts page, opened later, showed only a dismissed row,
  the same as one a user had silenced. An alert now records how it closed
  (resolved automatically, or closed by a user), when, and which job
  resolved it; the Alerts page shows it as resolved, with a link to that
  job; and the resolution is announced by email, Telegram and webhook,
  wherever the alert itself was. A condition that clears on its own
  (`resolve_alert`) now also announces its end by email.
- **A staging's egress filter comes back after a host reboot.** The
  `odoo_net_setup` sidecar, which keeps a staging with an allowlist off the
  open internet, had no restart policy: after a host's maintenance reboot
  odoo came back and the sidecar stayed exited, leaving the staging
  unfiltered until someone restarted it. It now restarts with the host
  (`unless-stopped`); existing stagings take it at their next deploy or
  rebuild.
- **Starting odoo no longer drops a staging's egress filter.** The sidecar
  points odoo's default route at the allowlist proxy once, when it starts,
  inside odoo's network namespace; starting odoo again gives it a fresh
  namespace with Docker's own route, straight out. A restore (which stops
  and starts odoo) did that every time, and a Restart that happened to
  start the sidecar before odoo did it too. Every start, restart or `up`
  that may start odoo now restarts the sidecar once odoo runs.
- **The health check says when the egress filter is not in force.** It read
  only whether each container was running, and the sidecar runs whether or
  not odoo's route is the filtered one. It now reads the sidecar's own
  healthcheck and raises a critical alert while it is unhealthy, which
  closes by itself once the filter is back.

## [1.0.159] — 2026-10-08

### Fixed

- **A pull request preview can say where it is.** The preview comments its
  URL on the pull request, and GitHub only lets an App with write access to
  pull requests do that. The App manifest asked for read, so every comment
  failed with a 403 that only reached the log. New Apps ask for write; an
  App created before keeps read until its owner accepts the new permission
  in GitHub (Settings → GitHub Apps → the App → Permissions).

## [1.0.158] — 2026-10-08

### Fixed

- **A web terminal ends when its shell does.** After `exit` (or the
  container stopping), the output reader stopped but the input writer kept
  waiting for the session to be marked closed, and nothing marked it: the
  page said "Connected" until the idle timeout and the session's process
  lived on. The reader now closes the session at the end of the output, so
  the page says "Session closed." (both terminals).

## [1.0.157] — 2026-10-08

### Fixed

- **The web terminals keep what is typed in order.** Each keystroke was a
  request of its own, sent without waiting for the one before, and the
  server's workers answer requests in parallel: fast typing, or a slow
  link, reached the shell scrambled (a typed `echo OK-$((40+2))` arrived
  as `eco hOK-…` or `echo OK-$4(02()+`). Both pages, the
  instance's containers and the host, now send one request at a time;
  what is typed while one is out goes, in order, in the next.

## [1.0.156] — 2026-10-08

### Fixed

- **The host shell and the container shell start outside devel.** Their
  pages load xterm.js from `static/lib/xterm`, and `.gitignore` ignored
  every `lib/` directory: the library was never committed, so every image
  built from GitHub (every panel deployed from it) served
  a 404 for it and the terminal stayed at "Establishing SSH connection…"
  with `Terminal is not defined`. The three vendored files (xterm 5.3.0,
  xterm-addon-fit 0.8.0, byte-identical to the published packages) are
  committed with xterm's MIT licence, and a test fails when a page names
  a static file the module does not hold.

- **The import dialog lists a public repository's branches without
  credentials.** `/cloud/get_repo_branches` tried the GitHub App and the
  PAT only, so a panel with neither (every new one) answered "No
  GitHub credentials configured ... to access private repositories" for a
  public repository, while the import itself reads public repositories
  anonymously. It now tries GitHub's anonymous API last; a private
  repository still gets the same message.

## [1.0.155] — 2026-10-08

### Fixed

- **Restart, Stop and Start on an instance did nothing since 3-sep.** The
  three buttons passed the job type's code as a bare string, and the
  handler reads `action.code` since the pluggable actions learnt to carry
  a confirmation notice: every press reached the server as "Job type with
  code None not found". They pass an action object now, and a structural
  test refuses any handler call with a bare string.
- **A neutralized copy keeps no GitHub credential** (`auth_oauth_github`
  19.0.1.0.1). Stock neutralization only switches OAuth providers off, so
  the GitHub OAuth App's client secret and the token stored on every user
  who signed in with GitHub (plain text, and GitHub's do not expire)
  survived into every neutralized copy of production, stagings
  included. The module's `data/neutralize.sql` clears both; the client id,
  which is public, stays.
- **The Odoo.sh migration guide and the private-repo note** described a URL
  with a token inside it, which the importer refuses, deploy keys that do
  not exist and a "New Project" button that is "Import from GitHub".

## [1.0.154] — 2026-10-07

### Changed

- **Notices appear below the header, not over it.** An error notice
  stays until it is closed, and it sat on top of the alerts bell, the
  job history and the user menu: the places it sent the user to. On a
  phone a notice now keeps a margin on both sides. Each notice is its
  own live region, an error as an alert that a screen reader announces
  at once, everything else as a status.
- **A failed job's notice says on what and why, and opens its jobs.**
  "Full Setup failed — check the job log" becomes "Full Setup on host-01
  failed: port 443 is already in use", with the same one-line
  excerpt the failed-job alert shows, and a **View** button that opens
  the job history of that instance or host. `/cloud/get_job_brief`
  returns the target and the excerpt for it; who gets an answer at all
  is unchanged.

### Added

- **Escape closes the latest notice** when no modal, side panel or
  open menu takes the key first.
- **Notices no longer pile up.** One identical to a notice already on
  screen is not shown twice, and at most five stay visible: the oldest
  makes room. Every failed job is still in the alerts bell.

## [1.0.153] — 2026-10-07

### Added

- **The metrics central can be published from a host of its own.**
  Settings → Monitoring gains **Central's public name** and **Who may
  delete series on the central**. With a name, deploying the central
  also publishes it through that host's Traefik: the four account
  prefixes (`/w/ /r/ /lw/ /lr/`), `/grafana/` with the panel's origin in
  `frame-ancestors` and any `X-WEBAUTH-USER` header removed, series
  deletion (`/admin-d/`) for the listed sources only and the other
  operator routes for the host itself. The certificate comes from Let's
  Encrypt, or from the host's own store behind the CDN. When the job
  answers, the panel and every new agent are pointed at the name, and the
  Grafana base URL too if it was empty. With the name empty, a central
  next to its panel, the compose file and the URLs written are exactly
  as before: a central already running next to its panel does not
  change. `docs/observability-operations.md` describes moving a central
  this way.

## [1.0.152] — 2026-10-04

### Fixed

- **A neutralized staging sends its mail to MailHog again.** Odoo's
  neutralization archives the real outgoing servers and adds an active
  placeholder, `neutralization - disable emails`, on host `invalid`.
  Odoo uses any active server before the SMTP in its configuration, so
  every mail of a staging cloned or refreshed from production failed
  instead of reaching the stack's mail catcher (`smtplocal:1025`), and
  whatever waited on a mail broke with it. After a neutralized restore
  on a staging, `restore_instance` now removes that placeholder and only
  that one: the real servers stay archived and mail falls back to
  MailHog. A production never gets the step, because there `smtplocal`
  is the real relay, and the neutralized backup download keeps the
  placeholder because it can be restored anywhere. Stagings created
  before this version get it at their next refresh from production.
  `data/neutralize.sql` documents the same `DELETE` for a local devel.

## [1.0.151] — 2026-10-04

### Fixed

- **Stagings answer again.** Since 1.0.128 a staging's mail catcher is
  taken off Traefik so its mailbox is not published on the internet.
  The copier template, though, declares the middlewares a staging's
  Odoo routes use (`addSTS`, `buffering`, `compress`,
  `forbid-crawlers`, `forceSecure`) on that same `smtp` container.
  Ignoring the container made them disappear, Traefik dropped every
  Odoo route that named them, and every staging — pull-request
  previews included — answered `404 page not found`. The override now
  declares the same middlewares on Odoo, with the template's names and
  values, so the routes come back while the mail catcher stays off
  Traefik. Production declares them on Odoo already and is unchanged.
  Verified against Traefik v2.11 with the labels of a real staging:
  404 before, routed to Odoo after, and `/smtpfake/`
  still does not reach the mailbox.

## [1.0.150] — 2026-10-03

### Fixed

- **A staging can be rebuilt.** Before swapping images, a rebuild boots
  the new one against a throwaway copy of the database. That copy was
  published on the gateway of the project's network, which a staging
  cannot reach: its compose file makes that network `internal`, and
  Docker drops what leaves an internal bridge. Doodba then waits for
  its database with no deadline, so a staging rebuild hung for good
  instead of failing. The throwaway database
  now runs on a network of its own, which the boot container joins —
  the same path for every instance, production included, and still
  never an endpoint on the project's network (2026-08-13). Verified end
  to end against both network layouts. The wait for the database now
  gives up after two minutes with a clear message, so a database that
  never answers fails the step instead of hanging the job.

## [1.0.149] — 2026-10-03

### Fixed

- **Cloning a production to a staging works before its first backup.**
  The clone copied production's latest backup whenever a backup
  destination was configured, but a destination does not mean a backup
  exists: a production created the same day has none until its first
  nightly run. The download failed with "No backup chains found" and
  the new staging was left deployed but empty. The clone now always
  takes a live dump of production, which also gives the staging the
  data as it is now rather than as it was last night. Refresh from
  production keeps its choice of source.

## [1.0.148] — 2026-10-03

### Fixed

- **A job on a staging no longer runs twice.** Since 1.0.130 a job a
  person starts on a staging seals its autopurge clock when it
  finishes. The seal wrote the instance row inside the job's own
  transaction, after the job's hooks had already written and committed
  that same row on cursors of their own, so the job lost a
  serialization race at the very end and queue_job ran it again from
  the start. A new staging hit it: the
  deploy succeeded, ran a second time — tearing the fresh staging down
  and rebuilding it — and then failed on `deployed → deployed`, which
  cancelled the restore of production's data. A rebuild or a restore
  would have repeated indefinitely. The seal now runs after the job
  commits, on a cursor of its own, and a failure there is logged
  instead of failing a job that already finished.
- **A failed job cancels everything chained after it, on any
  instance.** The cascade only looked at the failed job's own
  instance, but a staging built from production downloads the backup
  on *production*: that download waited forever and kept production
  from running any other job until it was cancelled by hand. Jobs still
  waiting in the failed job's chain are now cancelled too.

## [1.0.147] — 2026-10-02

### Added

- **A host's default certificate is set on its Traefik tab.** The
  certificate a host serves when a router has none of its own — what a
  CDN in front connects to, and what *Existing certificate* serves — was
  a pair of fields nothing in the panel could reach, so on a core-only
  install that option always ended up self-signed. The tab now takes
  the certificate and its key, describes what is stored (names, expiry,
  issuer), never shows the key back, and refuses a pair that cannot
  serve: half of one, an unreadable PEM, or a key from another
  certificate. Saving it marks the host's configuration as not
  deployed; *Push Trusted Proxy Settings* or *Full Setup* ships it.
- **Every deploy says how each domain gets its certificate.** One line
  per domain in the job log — including what *Automatic* resolved to on
  that host — so the decision is visible without reading code. A domain
  set to *Existing certificate* on a host that holds none — no default
  certificate and none declared by hand in its `config.yml` — stops the
  deploy or rebuild before anything reaches the host; a
  default certificate that does not cover the name is warned about.
  RB-12 is rewritten around the Traefik tab.

### Fixed

- **Saving an instance no longer turns *Automatic* certificates into
  Let's Encrypt.** Since 1.0.109 a domain's certificate defaults to
  *Automatic*: the host decides, and behind a CDN it serves the
  certificate it holds instead of asking an authority that cannot reach
  it. The endpoint that saves an instance kept its own list of accepted
  values without *Automatic* and rewrote it to Let's Encrypt every time
  the Networking tab was saved. It now reads the options from the
  field. Rows it rewrote on hosts behind a CDN go back to *Automatic*
  on upgrade; elsewhere both values deploy the same and rows are left
  as they are.
- **The certificate picker offers *Automatic*.** It showed every
  *Automatic* domain as Let's Encrypt, and new domains were created as
  Let's Encrypt, skipping the host's decision. *Automatic* is now the
  first option and the default for a new domain, and the picker is the
  panel's own dropdown, like the rest of the form.
- **The domain editor on an instance's Networking tab is usable
  again.** The certificate select took the whole row and squeezed the
  hostname and redirect fields to empty slivers, with the remove button
  pushed off screen. Its hostname, redirect and certificate fields now
  look like every other field on the form in both themes; their old
  translucent fill read as disabled on the light theme. The shared
  form-row style that sizes a row's field
  to 100% reached every control nested inside the row and outweighed
  their own rules. It now styles only the row's own caption and field,
  so controls that belong to a component keep their own look: a number
  stepper no longer stretches, a long SearchSelect
  value no longer runs under its arrow, the tag selector's input no
  longer draws a box inside its box, a stored password shows its green
  "value set" field again, and a group of checkboxes reads as normal
  text instead of uppercase captions.

## [1.0.146] — 2026-10-01

### Changed

- **The Fleet tab counts instances in two ways, next to each other.**
  Its only instance card, *Instances observed*, counted instances with
  any container up. An instance put to sleep, as a layer can do, stops
  all its containers, so the card dropped to the few instances awake and read
  as if the rest had vanished. It is replaced by *Instances deployed*
  (every instance installed on your hosts, awake or asleep, from the
  disk usage each host reports per instance) and *Instances running*
  (instances whose Odoo container is up — the same rule the panel uses
  for an instance's own `running` state). The top row now shows the
  three host cards first and the two instance cards after them.
  Reaches Grafana with the next central deploy.

### Fixed

- **Deleting an instance with no backups really removes it from the host.**
  The delete clears the instance's backups first and stops if that fails, so
  the backups are never stranded. "Nothing to clear" (exit 10) was meant to
  let the teardown go on, but it was only forgiven after the step had
  already stopped the run: the containers, volumes and directory stayed on
  the host while the job ended `done`, said "removed from host" and dropped
  the record. The purge started `db` and `smtp` for its own run, so they
  stayed up too — one host was left with nine such stacks using ~1 GB of
  memory nobody could see. "Nothing to clear" no longer stops the run.
- **A job that skipped steps can no longer report success.** Whatever
  an executor's `parse_results` forgives, a run in which a step cut the
  remaining ones off now fails and says which steps never ran. Retries
  (the rebuild's exit 75) are unaffected.

## [1.0.145] — 2026-10-01

### Changed

- **The Monitoring picker says which instance it is.** It showed each
  instance by its compose name — `acme-034a549f`, or a dozen
  `production` — which is what Grafana filters on and meant nothing on
  its own. It now shows `project / instance` (a layer above can name its
  instances better), followed by `— Asleep` or `— Stopped` when the
  instance is not running, which is why its charts are empty; the value
  sent to Grafana is still the compose name. The tab opens on the first
  running instance instead of on an empty chart.
- **The instance dashboard charts services, not one-off containers.**
  `docker compose run` names every container it creates anew
  (`<project>-odoo-run-<hash>`), so each deploy step and each `odoo
  shell` added a series that ended minutes later and stayed in the
  legend. The CPU and memory panels now group by compose service
  (`odoo`, `db`, `smtp`, …) and leave `-run-` containers out. Reaches
  Grafana with the next central deploy; the instance Metrics tab embeds
  the same dashboard.

### Fixed

- **`running` falls as soon as an instance's whole stack stops.** The
  metrics cron only wrote the flag for instances that reported some
  container, and a stack stopped whole reports none within a scrape — so
  the flag stayed True until the SSH probe took it back, up to twenty
  minutes later. An instance the backend used to report on and now
  reports nothing of, on a host that still reports containers, is now
  written as not running. Never one it never reported on, and never when
  the host itself is quiet.

## [1.0.144] — 2026-09-30

### Fixed

- **Finished queue jobs are cleaned up again, and cloud jobs keep their
  outcome when they are.** queue_job's autovacuum only deletes jobs on
  channels that have a record, and `root.user` and `root.bg` — where
  cloud jobs run — had none: a panel had kept 184,516 finished jobs
  since June. Both channels now ship as data. Cleaning them up would
  have wiped history: `cloud.job.state` and `date_done` are stored
  related fields on the queue job, and deleting it recomputed them empty
  (some jobs already showed that). Deleting a queue job now
  cuts the link first, so a cloud job keeps the last state it reached
  for the whole 180-day timeline. `cloud_job.queue_job_id` is indexed:
  each deletion nulled it with a full scan of `cloud_job` (200 deletions
  took 1.57 s; with the index, 1,000 take 0.29 s).

## [1.0.143] — 2026-09-30

### Fixed

- **Host hardening now really turns SSH passwords off.** sshd keeps the
  first value it reads for each keyword, and reads its drop-ins in name
  order. Cloud images ship their own — Hetzner's `50-cloud-init.conf`
  says `PasswordAuthentication yes` — and the hardening drop-in was
  named `99-…`, so on such a host passwords stayed on, and the key login
  the play proves at the end could not tell. Measured on 2026-09-30.
  The drop-in is now `00-incubacloud-hardening.conf`, the old file is
  removed on the next run, and before sshd is restarted the play reads
  `sshd -T` and stops unless the values in force are `PasswordAuthentication
  no`, `PermitRootLogin prohibit-password` and the new port. Applies on
  the next hardening run.
- CI parses the firewall ruleset with Debian 13's nft as well.

## [1.0.142] — 2026-09-29

### Fixed

- **Instances built from this repository's requirements resolve again.**
  `asyncssh` moved to 2.24.0 on 2026-09-19, and that release requires
  `cryptography` 48.0.1 or later while `requirements.txt` still pinned
  46.0.7: pip
  could not resolve the file and every new instance that took it failed
  its image build. `cryptography` is now 50.0.1, with
  `pyOpenSSL` 26.4.0 alongside: the release the base image ships
  crashes Odoo's `base` import against `cryptography` 49 and later. It
  is the pair the panel image already runs. Instances whose repository
  is pinned keep the dependencies they were built with.
- **The wait for an instance's public URL gets past the CDN.** A
  hostname behind a CDN that challenges bots answers every `GET` from a
  non-browser with a challenge page,
  so `wait_public_url.sh` never saw the instance behind its
  `/web/health` probe and always timed out. It now asks Odoo over a
  `POST` JSON-RPC call to `/web/webclient/version_info`, which the CDN
  never challenges and the catch-all page cannot answer.

## [1.0.141] — 2026-09-29

### Changed

- **A firewall ruleset is parsed on the host before it goes live.**
  Hardening renders the ruleset to `/etc/nftables.conf.new`, runs
  `nft -c` on it there, and only then installs and applies it. The
  kernel already loaded rulesets atomically, so a bad one never half
  applied — but the file it was read from is what the nftables service
  loads at boot, and the run 1.0.140 fixed had left an unloadable file
  there. Such a ruleset now fails with the parser's message and the
  live file is never touched.
- **CI parses every variant of the ruleset with the nft of each
  supported OS** — Ubuntu 22.04 and 24.04, Debian 12 — in that OS's
  own container: the check that would have caught 1.0.139 before it
  shipped. `incubacloud/tests/shell/render_firewall_ruleset.py`
  renders the variants; add a container to the job when the preflight
  admits a new OS.

## [1.0.140] — 2026-09-29

### Fixed

- **The CDN allowlist loads on the fleet's nftables.** The rules 1.0.139
  added to the forward chain name the port the visitor dialled with
  `ct original proto-dst`, which nftables 1.0.2 (Ubuntu 22.04) can only
  type once the transport protocol is stated. The first hardening run
  with them was refused by `nft -f` ("Can't parse symbolic invalid
  expressions") and applied nothing, leaving the previous firewall in
  force. The rules now start with `meta l4proto tcp`, checked against
  nftables 1.0.2 as well as the newer release the lab ran on.

## [1.0.139] — 2026-09-29

### Fixed

- **The CDN allowlist now guards the hook the traffic crosses.** A host
  behind a CDN that also refuses direct access gets its 80/443 filtered
  to the CDN's ranges in the firewall, not only at the proxy. The rule
  was written in the input chain, which instance traffic never crosses:
  it is DNAT'd to the Traefik container and crosses the forward hook.
  Measured on the fleet's iptables-nft backend, a source outside the
  list went straight through. The list is now enforced in the forward
  chain, on the first packet of a connection redirected from the host's
  80/443 only (`ct state new ct status dnat ct original proto-dst`), so
  replies and the containers' own outbound traffic are never weighed
  against it; and the per-source connection-rate cap, which behind a
  CDN counts edges rather than visitors, is no longer rendered alongside
  it. Applies on the next hardening run. A name that resolves straight
  to such a host, bypassing the CDN, stops answering — ACME challenges
  included. RB-18 documents the mode.

## [1.0.138] — 2026-09-29

### Added

- **A shell on the host itself.** **Host Shell** in a host's action bar
  opens a login shell on the machine, as the host's SSH user, for
  Administrators — the role that already runs root there through every
  host job, with the same key. It lived in an extension until now; a
  panel run on your own servers is now complete without it. Same
  subprocess isolation as the instance terminal, its own cap
  (`rate_limit_host_console_per_min`, 3 per minute per user and per
  host, in Settings → Rates), one open shell per user and host, and an
  immutable `cloud.host.session` row per session with who, when, client
  address and browser. What is typed is never recorded.
  `test_terminal_session.py` now requires the command-less PTY to exist
  in exactly one file, `host_terminal_session.py`.

### Fixed

- **The confirmation no longer claims the session is logged.** It said
  "The full session is logged"; only its opening and closing are, on
  purpose. It now says what is recorded and what is not.
- **Deleting a never-set-up host with shell history archives it.** Such a
  host was unlinked, and the shell's audit rows hold the host with
  `ondelete='restrict'`, so the delete failed. It is archived, like a
  host with jobs.
- **The Rates tab saves the mailbox cap.** "Mailbox reads on staging"
  was loaded and editable but left out of the save, so a change to it
  was silently dropped. A test now holds the tab's defaults and its
  save payload to the keys the backend serves.

### Migration

- `1.0.138` pre-migrate takes over the host shell's identifiers (models,
  fields, selection values, ACL lines, the GC cron and its action, the
  cap field, the page) and the reflected foreign keys of its two tables
  from whichever module shipped them, before either module loads. Left
  behind, that module's next upgrade would delete what it no longer
  declares. The audit rows never move. The post-migrate puts the GC cron
  on the platform bot where it arrives new.

---

## [1.0.137] — 2026-09-28

### Fixed

- **A new central saves the credentials it deploys.** The central
  deployment and the account sync minted this panel's metrics
  credential, the operator's and Grafana's admin password in the job's
  asynchronous phase, whose writes the runner drops (`cloud.job.execute`
  ends with `env.clear()`). Measured in devel: all three were generated,
  handed to the playbook and lost. A panel whose central already had them
  saw nothing; on a new install the panel's own account would
  never have been saved (no host enrolled, and a critical
  `metrics_backend_unreachable` with a healthy central), purges would
  never have been enabled, and Grafana's password would have rotated on
  every deployment. They are now minted on a durable cursor before the
  playbook, together with any credential a layer above mints while
  listing the accounts.
- **The deletion key follows the same pattern.** 1.0.136 decided it in
  memory and recorded it on success; it is now minted durably before the
  playbook like the rest, so every credential of the central is handled
  one way. A deployment that fails after minting leaves a key the backend
  may not have yet: purges report `metrics_purge_failed` until the next
  good deployment, which starts VictoriaMetrics with that same key.
- **A deployment uploads the dependencies it has just merged.** The
  requirements re-sync saved the merged list on its own cursor and
  invalidated the job's cache — and the job's REPEATABLE READ snapshot
  served the pre-merge list back, so `pip.txt` went out without the new
  requirement in the very deployment that added it (measured in devel
  with two real cursors). The committed values, and the
  `rebuild_fingerprint` the full-rebuild decision reads, now go straight
  into the job's cache.
- **A restore through a temporary key closes it for good.** Marking the
  grant used and queueing the key's revocation happened in the job's
  transaction: a restore failing afterwards took both back, and even on
  success the runner's final `clear()` dropped the revocation's link to
  its queue job (measured in devel).
  Both are now committed together on a durable cursor.

### Added

- **One rule for what an executor writes, enforced by tests.** "The
  job's transaction is not written." `AbstractExecutor._durable_env()`
  (a fresh READ COMMITTED cursor, committed on exit, lock wait capped at
  30 s) and `_persist(record, vals)` (the same, plus the committed values
  and their stored dependents copied into the job's cache) replace the
  hand-written `registry.cursor()` blocks of every executor and the
  internal log, bus, cancel and alert cursors. The success and failure
  hooks run inside one too: a hook that would have hung on a row the job
  holds now fails after 30 s with a traceback.
  `tests/executor_write_rules.py` checks every executor class by AST (no
  cursor of its own, no write outside a durable block or a hook), and
  `tests/test_executor_durable_writes.py` gives tests `end_job()` /
  `stored()` to assert what survives the runner's `env.clear()` — the
  check whose absence let 1.0.135 through. See *Transaction model* in
  `docs/architecture.md`.

## [1.0.136] — 2026-09-28

### Fixed

- **The metrics deletion key reaches the database.** 1.0.135 minted it
  while building the central deployment, which runs in the job's
  asynchronous phase — and nothing written there survives it. Measured
  in production: VictoriaMetrics started with the key, the operator's
  deletion route carried it, and the database never received it, so the
  panel could not delete anything (safe, but useless). The key is now
  decided while building the run and recorded by the success hook, the
  way the list of granted accounts already was: the database holds
  exactly the key the backend was started with, and a failed deployment
  records nothing. A redeployment keeps the recorded key.

## [1.0.135] — 2026-09-27

### Added

- **A removed instance or host lets its metrics go.** Nothing deleted
  series before: an instance removed from the panel kept its series on
  the central until the global retention (90 days) dropped them, which
  with stagings and PR previews coming and going is disk spent on things
  that no longer exist. `cloud.instance.unlink()` — where every removal
  path ends — and the retirement of a host (a host leaves the fleet by
  being archived, never unlinked) now queue the deletion. Three rules
  shape it:
  - **The selector always names the account.** `instance_id` and
    `host_id` are row ids of the panel that owns them, and several
    panels share one central, so `instance_id="5"` exists in many
    accounts at once. `series_selector()` is the one thing that builds a
    selector and refuses to build one without an account. A host's
    selector also carries `instance_id=""`, so an instance that moved
    elsewhere keeps its history.
  - **It waits 30 minutes.** Measured in production: a removed
    instance's disk series kept receiving samples for three minutes
    after the record was gone (the collector rewrites its file every ten
    minutes), and a late sample recreates a deleted series. The job is
    queued inside the removal's own transaction, so a rollback drops it.
  - **Only the panel that owns the central deletes**, through a new
    operator route; elsewhere the hook does nothing and a layer above can
    forward the request. A failure raises `metrics_purge_failed`
    (warning) — the series then expire with the retention.
- **A retired host stops shipping metrics.** The host teardown now takes
  the agents down, removes the disk collector's cron and deletes the
  agents' directory, which held this panel's metrics credential in
  clear. Before, a BYOH server removed from the panel kept pushing for
  ever and would have recreated every series deleted for it.
- **Grafana organisations of accounts that no longer exist are deleted.**
  They were created and never removed. The central deployment and the
  account sync now converge against the whole account list; only
  organisations named like an account are candidates, never the default one,
  and an empty list deletes nothing.

### Fixed

- **An account could write series as another account.** vmauth forwarded
  every path under an account's prefix, and VictoriaMetrics serves
  writes and its admin endpoints on the same port as reads. Measured in
  the lab against the production configuration: through its READ route
  an account posted to `/api/v1/import/prometheus` with another account's
  `ic_account` — that route forces a read filter, not a write label — and
  the other account then saw the series as its own. The same route
  created snapshots, forced merges and read the backend's flags. Each
  account route now names the exact endpoints it serves: the agents'
  push, and the reads Grafana 11.2 and the panel were measured using.
- **Any account could delete its own series, and the central had no
  deletion lock at all.** VictoriaMetrics now starts with
  `-deleteAuthKey`, minted by the central deployment, and only the
  operator's deletion route (`/admin-d/`, exactly `delete_series`)
  carries it — vmauth adds it the way it swaps in Grafana's password,
  so the panel never sends it. The account sync, which rewrites the
  whole access-control document, keeps the route but never mints the
  key: only a deployment restarts VictoriaMetrics with it.
- **vmauth's own endpoints answered anyone on the internet.** `/flags`,
  `/metrics`, `/debug/pprof/` and `/-/reload` need no credential, and the
  public gateway handed vmauth the whole host: a POST from outside got
  200 (the CDN challenges GET, not POST). The gateway fragment
  (`docs/observability-traefik-metrics-gateway.yml`, installed by hand)
  now routes only the four account prefixes to vmauth and denies the new
  operator route with the other two. A test pins both lists to what the
  code renders.

## [1.0.134] — 2026-09-27

### Added

- **A staging reaches what its operator wrote down, and nothing else.**
  Egress used to be decided per *host*: one proxy container per hostname
  in `~/globalwhitelist`, shared by every test instance on the machine.
  That answers "what may stagings on this box reach", which is never the
  question a customer asks — theirs is "this staging has to reach *their*
  API". Each staging now carries its own list
  (`cloud.instance.whitelist`, a **Whitelist** tab on the instance), and
  a non-empty list makes the doodba template render that instance a NAT
  gateway of its own. A new staging is born with the list its host
  publishes, so the common case costs nobody a decision, and an empty
  list still means what it always did: fall back to the host's shared
  whitelist. Production is untouched — it is the customer's own
  deployment and this platform does not filter its traffic.

### Fixed

- **The host's docker socket no longer reaches a staging's sidecar.**
  The template mounts `/var/run/docker.sock` into `odoo_net_setup`
  unconditionally — no copier question turns it off — and `:ro` marks
  the *mount*, not the protocol: talking to a unix socket is `connect()`
  plus `send()`, and the Docker API has no read-only mode. The same
  channel accepts `POST /containers/create` with a bind of `/` and
  `privileged`. On a host shared between customers that is not "can see
  other containers", it is root over the machine and everyone on it. The
  deploy's override re-points the mount at `/dev/null` and switches off
  the one lookup that wanted it (`DNS_INTERNAL_FROM_DOCKER=0`, which is
  the default in the image's own entrypoint; the template is what turns
  it on). Nothing is lost: the DNS that lookup built is already served
  by Docker's resolver at `127.0.0.11`, which the sidecar forwards to
  and which answers service names and network aliases alike. An empty
  `volumes:` list would **not** have worked — compose merges volumes by
  target path, so the base file's mount survives it.

- **The egress sidecar is no longer allowed to die quietly.** The
  template ships it with `healthcheck: disable: true`, while the route
  injection it performs happens once, at start. Restart the odoo
  container and Docker hands it a fresh netns with the default NAT
  route: the injection is gone, the staging has free egress, and the
  sidecar stays `Up` because its last line is `tail -f /dev/null`. Every
  light green. A staging once spent three and a half hours sending
  Telegram messages to a live channel that way, which is why
  `devel.yaml` has carried a route check ever since. The override now
  ships the same check to every staging, and `expected_services()` names
  both sidecars so the health probe grades them, the daily prune cannot
  sweep them, and their logs are rotated like everything else.

- **A staging-expiry test no longer sits on an exact day boundary.**
  `_warned_with` wrote the activity clock with `fields.Datetime.now()`
  and `_autopurge_days_left` read it with a second one, so a single tick
  of the wall clock between the two turned "7 days left" into `6 days,
  23:59:59` — and `timedelta.days` truncates. It passed on a fast
  machine and failed on a loaded one, which is how it came to abort a
  production deploy on the clone rehearsal rather than in CI. The helper
  now leaves an hour of slack, which is also the truthful setup: no real
  instance sits on the boundary.

- **The shell selector is no longer the only thing deciding which
  container a user may sit inside.** `terminal_open` validated the
  *shape* of a service name with a regex and opened whatever matched, so
  the browser's list was the gate. Which services are a customer's is
  now the instance's answer (`_shell_services()`) — `odoo`, `db`, `smtp`
  and `backup`, an allow-list rather than a deny-list — and the route
  refuses everything else, including the egress gateway and its sidecar.

## [1.0.133] — 2026-09-27

### Added

- **The health probe can be told a whole stack is stopped on purpose.**
  The probe already had a way to hear that a stopped `odoo` is normal —
  an instance put to sleep on idle — but only `odoo`: the companions are
  still graded, because they must stay up while it sleeps. An instance
  that is parked whole, every container stopped and meant to stay that
  way, had no such answer, so it raised a critical "down" alert every
  five minutes for as long as it stayed parked, plus a warning for
  whichever companion the probe happened to catch mid-stop — and that
  one never closed, because a stopped `odoo` returns before the
  companions are looked at again.

  A new `_stack_stop_is_expected` hook, False in core, lets a layer that
  parks instances say so. The probe then grades nothing, reports the
  instance as stopped rather than broken, and closes the `down`,
  `unresponsive` and per-service alerts left standing. A missing `odoo`
  container still alerts: parked and pruned are different things.

## [1.0.132] — 2026-09-23

### Added

- **A "Mails" tab on every staging.** A staging never emails anybody: the
  doodba template wires Odoo to a mail catcher that runs beside it, so a
  restored copy of production cannot reach real customers. That has been
  true since the first deploy and there was no way to see it — you had to
  trust it, or go and look on the host. The tab lists what was caught
  (when, from, to, subject, size, attachments), opens one message with
  its HTML and plain-text bodies and its attachments described, and
  empties the box on request. Odoo.sh calls the same thing Mails.

  The catcher is not published — core 1.0.128 took away its Traefik
  router, because the template serves that inbox at
  `/<domain>/smtpfake/` with no authentication — so the read goes
  through `curl` inside the instance's own `odoo` container, and the
  parsing happens on the **host**, on the right of a pipe. A staging can
  be any Odoo from 7.0 up, so nothing is assumed about its Python; and
  only the projected JSON crosses SSH, where the raw mailbox would be
  megabytes of attachments the panel never shows.

  Developer and up, the same floor as the logs, capped per user by a new
  **Mailbox reads on staging** rate limit (Settings → Rates, default 30
  a minute). Nothing is stored in the panel. Clearing is audited.

### Changed

- **The Overview no longer says a staging has no SMTP.** It said "not
  configured", which was true of the relay and false about the mail: a
  staging captures it. It now says so, and the Networking tab states in
  one line what this environment does with outgoing email — captured on
  staging, real on production.

## [1.0.131] — 2026-09-23

### Fixed

- **Fields are as wide as their form again.** The same screen used to show a
  select at 575px, a number at 120px and a textarea at 100%, and none of
  them was the width of the form. It reads as carelessness and it is the
  first thing a partner sees. Four rules were fighting, and all four are
  gone: the 460px cap every `.rl-frow` carried, the hand-set `width:120px`
  on number and text fields, seven `.rl-fgrid` two-column grids that held a
  single field and pinned it to the left half, and a `width:100%` that
  reached the checkboxes too.

- **A checkbox on the host form was a text box.** "Exclude from
  auto-assignment" was an `<input>` with no `type` at all — so a text
  field carrying `checked` — which the width rule then stretched across
  the form. The missing attribute, not the CSS, is why it looked like
  an empty box with its label off to the side.

### Added

- A structural guard over every SPA template in core, which a module
  that extends the panel can run too: no form control may carry an
  inline width, and
  no two-column grid may wrap a single field. Layout regressions that
  need a screenshot to notice are layout regressions nobody notices.

### Notes

- `.rl-frow-inline` replaces the ad-hoc flex style on the one row that
  pairs a field with a button ("Purge old logs now").
- No cap was put back at the form level. If the Rates tab reads badly
  with fifteen numbers across 1170px, the answer is one
  `max-inline-size` on the form — never per field, which is how the
  three different widths happened in the first place.

---

## [1.0.130] — 2026-09-23

### Added

- **A staging nobody uses is deleted, after two warnings and never
  without them.** Nothing pushed anyone to delete a staging, and they
  are not free: they sit on the
  customer's own host, they generate probes and series, and each one
  holds a copy of production data nobody remembers is there. A staging
  with no activity for `staging_autopurge_days` (default 90, `0`
  disables) now goes. Production is never a candidate, nor an archived
  instance, nor one marked `autopurge_exempt`.

- **The clock measures use, not age**, and is the later of two signals.
  `last_touched_at` is sealed when a person acts from the panel — a
  visible job they started reaches `done`, they connect as a user, or
  they open a terminal. `last_login_seen_at` is the last login read from
  the staging's own database by a hidden daily job. Two things
  deliberately do not count: the background probes, which finish
  successfully every few minutes on instances nobody is looking at, and
  anything the platform bot starts on its own — a rebuild off a push
  above all. Either one counting would keep every staging alive forever
  and leave the feature dead code that looks alive.

- **The ladder climbs one rung per tick**, and each rung requires the one
  below it: warning at 14 days out, a second and critical one at 3, then
  deletion — which additionally waits a full day after that final
  warning. The naive version, evaluating every condition and acting on
  all that match, warns twice and deletes in one sub-second pass when
  the cron comes back from an outage. That is notice in form and nothing
  in substance, and it is the specific failure this ordering exists to
  make impossible.

- **A countdown and a one-click way out.** An amber *Expires in N days*
  badge (red over the last three) appears on the instance and in the
  project sidebar from the first warning on, carrying a **Keep** button
  that starts the window over. Per-instance *Never purge automatically*
  for the permanent ones, and the window itself under Settings →
  General.

### Changed

- `docs/roadmap.md`: "Login with GitHub" moves from medium term to
  shipped, where it has been for some time.

### Notes

- The migration starts every existing instance's clock at **now**, not
  at `create_date`. `create_date` was available and looks like a
  reasonable proxy, and it would have made the first tick after this
  deploy find every staging older than the window already past its
  deadline — the feature would have opened by doing the one thing it
  exists to promise never to do. Nothing can therefore be deleted until
  90 days after this release, and the first warning is possible at 76.

- Two alert codes rather than one that escalates: `cloud.alert` only
  dispatches notifications when a row is **created**, so re-raising the
  same alert at a higher level reaches nobody — and `critical` is the
  only level the default notification preference delivers.

- The login reading is deliberately asymmetric. A real answer may extend
  an instance's life; nothing may ever shorten it. A stopped container,
  an unreachable database, a garbled answer, a host clock skewed into
  the future and a restore that rolled the instance's history backwards
  all leave the field exactly as it was. "We could not ask" is never
  recorded as "nobody logged in".

- The reading has to work from Odoo 7.0 to 19.0, so it assumes neither
  `python3` in the image nor `to_regclass` in PostgreSQL, and it decides
  which table holds the answer by asking the catalogue rather than
  trusting the panel's `odoo_version`, which an operator fills in.

---

## [1.0.129] — 2026-09-21

### Added

- **A switch for pull request previews, where a project is configured.** A pull
  request on a repository a production instance follows creates a staging with
  the pull request's branch and a neutralized copy of production data, rebuilds
  it on every push and deletes it when the pull request closes. That has worked
  since early on, and the field that governs it (`pr_reviews_enabled`) had never
  been on a screen nor in the API: it was missing from `/cloud/get_project` and
  from the write allowlists of `/cloud/create_project` and
  `/cloud/save_project`, so it could only be changed from a shell. It is now
  read, saved and shown in the project's settings.
- **A hook to keep instances out of previews.** `cloud.github.event.
  _pr_preview_allowed(inst)` is asked before a preview is cloned and says yes by
  default. A layer on top overrides it for instances it manages itself,
  whatever their repositories look like — the same pattern as
  `_rebuild_job_type`.

### Changed

- **Previews are opt-in.** New projects start with the switch off: a preview is
  an instance nobody asked for by name, on the production's host, carrying a
  copy of production data. Projects that already exist keep the value they have
  stored — a default does not touch rows.

### Fixed

- **A preview that cannot be created says so.** The webhook caught every
  exception from `clone_to_staging` and wrote a log line, so a pull request that
  produced nothing looked like a feature that did not work. The pull request now
  gets a comment and the production instance a `pr_preview_failed` alert, closed
  by the next preview that does get created. Only a `UserError` or
  `ValidationError` message is published; anything else becomes a generic line
  with a log reference, because the comment lands in a repository that may be
  public.
- **A clone that fails half-way leaves nothing behind.** The clone now runs in a
  savepoint. Without it, a failure after the staging record was created (the job
  chain refused, say) was swallowed and the half-made row committed — and every
  later "reopened" then found the preview as already existing and did nothing.

---

## [1.0.128] — 2026-09-21

### Fixed

- **A staging no longer publishes its mailbox on the internet.** The copier
  template wires a staging's Odoo to an in-stack MailHog so a copy of
  production never mails real customers, and then gives that container a
  Traefik router on `<domain>/smtpfake/`. MailHog asks for a password only
  when `/etc/mailhog/auth` exists and nothing here writes it, so the whole
  mailbox of an instance carrying production's data — password resets,
  invitations, customer addresses — was one URL away from anyone who knew the
  hostname, which Certificate Transparency publishes. The deploy/rebuild
  override now gives that service `traefik.enable=false`: compose merges
  labels by key and the override wins, so the router labels stay in the file
  and Traefik ignores the container, while Odoo keeps reaching the catcher
  over the internal network. Production's `smtp` is the real relay and is
  untouched.
- **`expected_services` told the truth about staging.** It claimed staging ran
  `odoo` and `db` only, while the template renders `smtp` there
  unconditionally — so the mail catcher was never probed (Odoo fails to send
  and nothing says so), never limited by `smtp_memory_limit` / `smtp_cpus`,
  never log-rotated, and out of reach of the override above. The deploy
  executor's separate `_TEST_SERVICES` constant, which is where the two
  answers diverged, is gone: both environments now read the model.
- **Stagings stopped inheriting an egress policy nobody chose.** From template
  v9.6.0 on, `whitelisted_hosts_test` defaults to a 32-entry list — payment
  gateways and tax agencies among them — and a non-empty list makes the
  template render a NAT gateway plus a sidecar sharing the odoo container's
  network namespace, both `NET_ADMIN`, the sidecar mounting the host's docker
  socket. `copier --defaults` takes the default for every question the answers
  file omits, so every staging got all of that, unasked, alongside the host
  whitelist network it already joins. The answers now carry
  `whitelisted_hosts_test: []` and `whitelisted_hosts_devel: []` explicitly,
  the same reason `backup_backend_password` is answered. A per-instance
  allow-list is a feature in its own right and is not this change.

Migration `1.0.128` re-anchors production instances behind a binary gate: a
record is re-stamped only when its snapshot minus exactly the two new answers
still hashes to the anchor it carries, so real drift is never masked. Stagings
stay dirty on purpose — their compose genuinely changed.

## [1.0.127] — 2026-09-20

### Added

- User docs: a card-by-card reference for the monitoring dashboards
  (`reference/monitoring-panels.md`) covering Fleet, Hosts, Instances and an
  instance's Metrics tab — what each chart shows and in which unit, what a
  healthy one looks like, what to check when it is not, and which shipped alert
  rule already watches that symptom

### Fixed

- **A teardown's unlink now waits for the job's commit, so a successful
  `delete_instance` runs once.** 1.0.126 found why every successful delete
  ran twice and defused the re-run's side effect; this removes the re-run.
  The success hook unlinked the instance on its own cursor; `cloud_job`
  references the instance `ON DELETE SET NULL`, so that commit updated the
  job's own row; and the job's transaction — whose snapshot `job.lock()`
  had fixed before the run — then failed to mark itself done
  (`could not serialize access due to concurrent update`) and queue_job
  ran the whole job again. Nothing inside the transaction could fix that:
  a commit is forbidden by queue_job (it would release the job lock), a
  fresh cursor for the terminal `UPDATE` still leaves the stored related
  `cloud.job.state` to the ORM on the old snapshot, `allow_commit` on the
  job function changes nothing because the snapshot predates the run, and
  touching the job row *before* the hook would make the hook's cascade
  block on it — a hang, not an error. So the hook now stops one step
  short: `_finalize_removal(unlink=False)` marks the record
  `removal_finalized`, and `AbstractExecutor._unlink_after_job_commit`
  arms the unlink on the job's cursor's `postcommit`, where it runs on a
  cursor of its own once nobody holds the row. A rollback drops it; the
  re-run finds the marker, touches nothing on the host, and arms it
  again. A failure inside the deferred unlink is logged and raised as
  `instance_unlink_failed` rather than turning a finished job into a
  failed one after the fact. `purge_archived_backups`, which unlinked
  from its hook the same way and would have re-run the same way the day
  it ran, defers the same way.

## [1.0.126] — 2026-09-20

### Fixed

- **A backup purge whose compose read failed was reported as drift.**
  `backup_purge.sh` and `backup_archive.sh` piped `docker compose config
  --services` straight into `grep` with stderr to `/dev/null`, so a read
  that failed looked exactly like a list without a `backup` service: exit
  20, the `backup_purge_service_missing` alert, and "rebuild the instance,
  then delete it again" as advice. Several deletes started in the same
  millisecond on one host; one of them saw the read die in under 110 ms (its
  sibling's took ~500 ms and succeeded) and was reported as drift — for a
  compose the same command had listed correctly hours earlier and listed
  correctly again minutes later, when the delete was retried. The read now
  goes through `ic_compose_services` in `lib/common.sh`: it retries once
  after two seconds and says so in the job log, and when both attempts fail
  the scripts exit **23** with docker's own error in the log — the new
  `backup_purge_compose_unreadable` / `backup_archive_compose_unreadable`
  alerts, whose advice is "try again", not "rebuild". Exit 20 now means what
  it says: the command answered, and the answer had no `backup` service. The
  retry covers the read only; a transient during the purge itself still
  lands as 22 and needs its own retry.

- **Every successful `delete_instance` ran twice, and the second run
  queued a second `install_observability`.** The teardown's `on_success`
  unlinks the instance on the hook's own cursor; `cloud_job.instance_id`
  is `ON DELETE SET NULL`, so that commit updates the job's own row; and
  the raw `UPDATE cloud_job SET state='done'` that mirrors the queue job's
  terminal state then loses a serialization race against it, which
  queue_job answers by running the job again. Measured over three days:
  `delete_instance` 2 re-runs of 3 jobs, every other job type 0 of
  ~10,000. The re-run was already a no-op for the host (1.0.58) but still
  called `refresh_observability_labels`, five seconds after the first
  run's refresh had started and could no longer be collapsed onto — two
  identical playbooks per delete. The re-run now returns before touching
  anything, and no longer logs `✓ Instance '?' removed from host.` for a
  removal it did not perform. The re-run itself remains: removing it
  means changing where the terminal state is written, for every job type,
  and is left for its own change.

### Changed

- **Instance teardowns take the per-host lock the builds take.**
  `cloud.job.enqueue` serialises per instance, so two deletes of two
  instances on one host ran side by side; `DeleteInstanceExecutor` now
  mixes in `HostBuildLockMixin`, and a teardown waits 30 s for its
  sibling (or for a build) rather than sharing the daemon with it. The
  lock is keyed on the host the *job* runs against instead of the
  instance's host: for every build the two are the same, but the move
  cleanups tear down a copy on the host the instance does *not* live on,
  and keyed the old way they locked the machine they never touched.
  `move_rollback_cleanup`, which can wait ten minutes for the move chain
  before touching the host, opts out (`_takes_host_build_lock = False`)
  so that wait does not park every build on the target.

## [1.0.125] — 2026-09-18

### Changed

- **The webhook source allowlist leaves core, and the panel-route
  settings go with it.** What the allowlist protects is decided by
  walking a host's deployed instances and treating each one as a panel
  that answers `/cloud/github/webhook`. That holds only where every
  instance a host serves *is* a panel: a plain installation serves its
  customers' Odoo projects, which never answer that path, and its own
  panel sits behind a proxy core neither deploys nor describes. The four
  settings 1.0.104 added to work around that — host, hostname, backend
  URL, wildcard — fit one topology only, and where the instance walk
  applied they were shadowed by it (a panel that is one of a host's
  instances had the route built from the fields rendered and then
  overwritten by the same hostname's instance route). Core keeps what
  every installation uses: the trusted-proxy ranges, the record of what
  each host's Traefik was last given, and the alert when deliveries go
  quiet. A module that still needs the allowlist re-homes the job type,
  the cron and the stored ranges in its own migration, before the orphan
  cleanup at the end of the upgrade would delete them.

### Removed

- `cloud.settings.github_webhook_allowlist`, `panel_host_id`,
  `panel_hostname`, `panel_service_url`, `panel_tls_domain`;
  `cloud.host.github_webhook_edge_hash`; the `push_github_webhook_edge` job
  type; the *GitHub: Refresh the Webhook Source Allowlist* cron; the
  *Webhook Source Allowlist* section of Settings → GitHub. The upgrade drops
  their columns by itself; where a module declares the switch and the digest
  again, those two keep their values. The job type is the exception:
  `cloud.job.job_type_id` restricts deletes, so on a database holding any
  job of that type the end-of-upgrade cleanup would abort the whole upgrade.
  A pre-migrate flags its identifier `noupdate` while jobs still use it,
  which keeps it out of the cleanup; unused, it is deleted as before.

## [1.0.124] — 2026-09-15

### Fixed

- **1.0.123 could not be deployed: its new test errored in the boot test.**
  `tests/test_metrics_gateway_frameguard.py` reads the metrics gateway
  fragment from the repository's `docs/`, and an image built from the
  addons alone has no `docs/`. Devel and CI both use the full checkout, so
  neither saw it; a deploy's boot test on such an image did, and refused
  the release before anything was touched.
  The tests now skip when `docs/` is absent and still fail when `docs/` is
  there but the fragment is not.

## [1.0.123] — 2026-09-15

### Fixed

- **Panels on other origins can show their dashboards again.** A host's
  `cloud.host.frame_ancestors` is an https-entrypoint default: it reaches
  every router on the host, Grafana included. On the host of a published
  central, a list that named the operator's panel only left every panel
  that frames Grafana from its own origin with "refused to connect"
  where the charts of its Monitoring page and instance Metrics tab
  belong. Nothing reported it, because a frame blocked by CSP still
  fires `load`.

  Measured on Traefik v2.11 before choosing the fix: an entrypoint header
  overrides whatever a router or the application sets. So no single list
  on the entrypoint can keep the panel strict and let other panels in.
  The host field stays empty on such a host and each router carries its
  own policy. The metrics gateway fragment gives Grafana's router
  `frame-ancestors 'self'` and the origins of the panels that frame it —
  a wildcard over their zone (`https://*.example.com`) lets a new one in
  without re-shipping anything. The panel's own routers, in the panel's
  deployment rather than here, keep `'self'` and gain
  `X-Frame-Options: SAMEORIGIN`.

  The price of the wildcard is written next to it: any site in the zone
  can frame Grafana signed in as whoever views it. The users of those
  panels are Viewers there. Listing the panels one by one would not
  avoid it, since each would be on the list.

  `tests/test_metrics_gateway_frameguard.py` pins the fragment, the
  help of `frame_ancestors` now says when to leave it empty, and the
  runbook for missing dashboards gains the step that would have found
  this in a minute: look at the frame, not at the page holding it.

## [1.0.122] — 2026-09-10

### Added

- **A migration folder Odoo would never run now fails the suite.**
  `tests/test_module_hygiene.py` walks every module in the repository
  and refuses three shapes of migration that load, upgrade and test
  green while doing nothing.

  The one that prompted it: `migrate_module` executes a folder only
  when `installed < folder <= manifest`, so adding
  `migrations/1.0.N/post-migrate.py` without bumping the manifest in
  the same commit strands the script permanently — bumping later does
  not rescue it, because by then the module is installed at that
  version and the left-hand comparison excludes it. Nothing anywhere
  reports this: the module still loads, so CI cannot see it.

  Two smaller variants of the same silence ride along: a folder name
  `VERSION_RE` rejects, which `_verify_upgrade_version` drops with a
  log warning nobody reads during an upgrade, and a folder holding no
  file whose name starts with `pre-`, `post-` or `end-`, which is the
  only kind Odoo collects.

  The guard reuses Odoo's own `VERSION_RE`, `adapt_version` and
  `parse_version`; only `convert_version` is reproduced, because it is
  a closure inside `migrate_module`. A repository of modules that extend
  the panel can carry its own copy, so its CI does not depend on this
  one.

  All 28 folders here pass today, so this is a ratchet rather than a
  fix.

---

## [1.0.121] — 2026-09-09

### Added

- **A key rotation can now tell when it has finished.**
  `cloud.settings.rotation_pending_count()` reports how many stored
  secrets are still not encrypted with the primary key, per column and
  in total, and every rotation pass logs that number.

  The rotation already reported how many rows it rewrote, and RB-01 told
  the operator to wait for that to reach zero before dropping the old
  key. It never reaches zero: a Fernet token carries a timestamp and a
  random IV, so re-encrypting the same secret with the same key yields a
  different string, the sweep's "did it change?" guard always passes,
  and every pass rewrites every row for as long as the cron runs.
  Measured on devel: two consecutive passes over six seeded secrets
  reported 12 rotated values each. An operator following the runbook
  literally would either wait forever or retire the old key on a hunch —
  and any value that had not moved becomes permanently unreadable. Those
  values are host passwords and SSH keys.

  Asking which key opens a ciphertext is a property of the data rather
  than of the last write, so it converges. A value nothing can decrypt
  counts as pending on purpose: that keeps the old key in the chain
  instead of stranding the row. RB-01 step 5 rewritten around it.

  Found by the FINAL-001 rotation drill, which is what the drill was
  for. The sweep itself was correct throughout: it moved every value and
  preserved all of them, and it does not disturb the instance config
  hash (measured — the snapshot reads the decrypted value).

## [1.0.120] — 2026-09-09

### Fixed

- **Deleting an instance told the caller whether the id existed before it
  checked whether they were allowed to ask.** `/cloud/delete_instance`
  ran `exists()` first and answered "Instance not found" for an unused
  id, while an id in use raised an access error — two distinguishable
  answers, which is an oracle. `exists()` is raw SQL, so neither the
  model ACL nor a record rule narrowed it: the oracle covered every
  instance on the platform and any authenticated user could ask,
  including a portal customer, who has no business reaching this route
  at all. The role floor (`group_cloud_consultant`) is now checked
  before the lookup; the per-record check that separates production
  (manager) from the rest still runs afterwards, since it needs the
  record to make that call. Found by the FINAL-001 pentest against
  devel; every sibling delete route already gated first.

## [1.0.119] — 2026-09-08

### Added

- **A host can now say who may put its pages in an iframe.** The public pages
  of a host sent neither `frame-ancestors` nor `X-Frame-Options`, so anything
  served there could be embedded and overlaid — the login routes were covered
  by Odoo's own headers, the marketing and legal pages were not. `cloud.host`
  gains `frame_ancestors`: a CSP source list rendered as a `frameguard`
  middleware and referenced from the https entrypoint, which is the only place
  a response header can be added once and reach every copier-generated router.

  **Empty by default, and that is the point.** An instance's own site may
  legitimately be embedded by its customers, and this control reaches every
  router on the host, so the capability ships here and the policy belongs to
  whoever runs the host.

  Two details that are deliberate. It **sets** rather than adds, like the
  trusted-proxy render: the source list moves, and clearing it takes the header
  back out. And it never sends `X-Frame-Options` alongside — that header cannot
  express "myself and that other origin", so pairing them would block exactly
  the embed the source list was written to allow, which matters when something
  else on the same host is embedded by the panel.

---

## [1.0.118] — 2026-09-08

### Fixed

- **The error scrape kept reading past the end of the traceback.** `grep -A`
  prints a fixed number of lines after each ERROR header, and the parser
  filed everything that was not another ERROR as context — including the
  INFO records logged after the failure. Those spent the tail budget that
  1.0.115 introduced to keep the exception: in a real alert the exception
  was line 1 of a 14-line tail, thirteen werkzeug requests behind it. A
  group now closes at the next log record of any level, as it already did
  at grep's own `--` marker.
- **The probe could not read an instance still logging to its container.**
  `docker compose logs` prefixes every line with `odoo-1  | `, and both
  readers of that output are anchored to the start of the line on purpose:
  one so asyncssh's echo of the scrape is never harvested as an error, the
  other so only a real Odoo record is counted. The fallback error scrape
  therefore produced no groups at all. Until 1.0.75 the parser had no
  notion of a header — it fingerprinted every line the grep returned, so
  the prefix did not matter; the instance still on that branch raised 73
  error alerts up to 16 August 2026 and not one since, while the rest of
  the fleet kept alerting. The counter behind `instance_logs_unhealthy`
  (`fallback`, Odoo unable to write to the log file) has read zero on
  every instance since the day it shipped. Both readings now pass
  `--no-log-prefix`.

## [1.0.117] — 2026-09-07

### Fixed

- **Every instance's mail container announced its own updates, under the
  same name.** docker-mailserver ships an `update-check` service that asks
  GitHub for the latest release and mails postmaster once per container
  start. Which tag an instance runs is the panel's call
  (`smtp_relay_version`), so the announcement is advice the container cannot
  act on — and it arrived at fleet scale: copier renders the hostname as
  `smtp.<relay domain>`, so all twelve instances relaying through one domain
  reported themselves as the same host, and an instance put to sleep
  restarts on every wake, turning "once per start" into dozens of identical
  mails a day at the postmaster address. The compose override the panel
  writes now sets `ENABLE_UPDATE_CHECK=0` on the `smtp` service. It goes
  there rather than in `.docker/smtp.env` or the compose files because
  `copier update` regenerates all three on every rebuild, which would drop
  it. The switch lands on each instance at its next deploy or rebuild.

## [1.0.116] — 2026-09-07

### Fixed

- **An instance handed over opened on months of somebody else's history.** A
  pre-built instance a layer hands to someone keeps its `cloud.instance`
  record, so the job list of an instance an hour old began with weeks of
  rebuilds and health checks logged before the handover — 107 foreign rows
  ahead of its own 9, with nothing marking the boundary.
  `cloud.instance._job_history_floor()` is a new hook (`False` in core: an
  instance normally owns its whole history) that `cloud.job` now honours in
  both the instance timeline and the history page. Jobs older than the floor
  are hidden, counted, and one click away — they stay in the database,
  because the machine is the same machine and those failures still explain
  today's.
- **Work a layer does for the platform inside a user's request is not that
  user's.** `sudo()` raises privileges without changing `env.uid`, so an
  instance a layer builds ahead of time from inside one user's request
  carried that user on its project, instance and build job, and whoever
  later received it found a stranger listed as its author. Such work runs
  as the platform's own bot, the same user the crons use:
  `res.users._get_cron_bot()` exposes it (XML-id first, login fallback,
  empty rather than raising).
- **The same bug, twice more, found by auditing every platform table.**
  `sudo()` raises privileges but leaves `env.uid` alone, so any work the
  platform does inside somebody else's request is authored by them — and
  that author reaches `queue_job.user_id`, meaning the executor *runs* as
  them and every row it writes inherits it.
  - The **GitHub webhook** is `auth="public"`, so a push that rebuilt many
    instances produced hundreds of rebuild jobs and `queue_job` rows and
    tens of thousands of log chunks, all authored by the public user.
    Elevated in the controller and again in
    `cloud.github.event._dispatch`, so a replay or a shell reprocessing a
    stuck event is covered too.
  - **Alerts** inherited whichever producer tripped them: 16 `job_failed`
    from the public user, and a customer listed as the author of a
    `metrics_acl_sync_failed` on infrastructure they never saw. Elevating
    inside `raise_alert`/`resolve_alert` covers all 19 call sites at once.
  `as_platform()` is the single helper for all of this: elevate once at the
  entry point and the whole chain below inherits it. A structural test
  (`test_platform_actor_invariant`) now fails the build if an
  `auth="public"`/`"none"` route enqueues, dispatches or raises an alert
  without it — verified to flag the webhook as it was written before this
  release.

## [1.0.115] — 2026-09-06

### Fixed

- **The error-log alert kept the useless half of the traceback.** The probe
  stored 25 lines after each ERROR header, which in an Odoo cron failure is
  `ir_cron` → `ir_actions` → `safe_eval` and nothing else: the exception type
  and message, the only lines that say what broke, fell off the end. Capture
  is now 60 lines and a group is compacted to head + `… N line(s) skipped …`
  + tail, so trimming always eats the middle and the last line survives.
  This is what left a fleet-wide broken cron undiagnosable for a week.
- **The panel never rendered the traceback it had.** The alert card showed
  the ERROR headers and dropped `context` on the floor, so reading a stack
  meant querying the database by hand. It is now shown under each group.
- **An error alert was dismissed five minutes after it was raised.** The
  probe's window is the time since the last check, so an error that fires
  every six hours produced an alert the next clean cycle closed — two
  notifications minutes apart, and nothing left in a panel that filters to
  active by default. `instance_error_logs` now closes only after
  `_ERROR_QUIET_HOURS` (24 h) without the error being seen again.

### Added

- `cloud.alert.last_raised_at`: when the condition was last observed,
  stamped on every raise and shown as "last seen" in the alert history, so
  an alert that keeps being re-raised no longer reads as days old. Nullable;
  consumers fall back to `create_date`.

## [1.0.114] — 2026-09-06

### Fixed

- **The job that retires stored certificates did not check that it had.** The write reports the exit status of a shell redirection, which succeeds over a store that ends up truncated and says nothing about whether the proxy came back — so the job could report success while the host went on serving exactly the certificates it was told to stop serving, which is the silence the retirement exists to end. It now reads the store back once the proxy is up and fails the job if a retired name is still there, if the document does not parse, or if it cannot be read at all. Reading it through the container also answers a question nothing asked before: a container that is not running cannot be read from, so a restart that did not take is now a failure rather than a green step.

- **The scheduled-action watchdog cried wolf on every deploy.** It measured how long a cron had been stopped from `lastcall` — when it last *ran* — so a cron that runs once a day was six hours stale while working perfectly, and the deploy pipeline pausing it for a few minutes was enough to report it. Measured: one alert naming twenty-nine scheduled actions, raised mid-window, with the fleet healthy. Noise on this particular alert is expensive, because it is the only thing that notices work silently not happening. It now measures against `nextcall` — when the cron was due to run next — which a pause leaves untouched: a daily cron paused during a deploy window is still hours from due and says nothing, while one left switched off falls behind and speaks up. Crons that have never run stay excluded, as before.


### Removed

- **The hourly watchdog for switched-off scheduled actions, and its
  `crons_disabled` alert.** It existed to notice the one thing a killed
  deploy could leave behind: the panel's crons paused, and nobody left to
  switch them back on. That is the deploy tooling's to prevent — it can arm
  a timer on the host before pausing anything, so the host switches them
  back on if the run never gets to — not a detector in every database this
  module is installed in, where it ran every hour watching thirty crons that
  nothing ever pauses. The alert history
  keeps the rows it raised; nothing raises that code any more.

---

## [1.0.113] — 2026-09-06

### Fixed

- **The watchdog's own tests read a panel's history as their own.** `cloud.alert` rows for `crons_disabled` survive in any database the check has run on, and what a leftover does depends on its state: an *active* one is reused rather than added, so "it raised an alert" finds a row the test never caused; a *dismissed* one is not reused, so a second row appears and "exactly one alert" counts two. Development databases carry the first, a production panel's carried the second, and between them every assertion in the suite was resting on whichever the database happened to hold. Caught by the deploy's boot test against a clone of production, where four of them failed at once. The tests now clear those rows in `setUp` rather than filtering around them — the reuse means a leftover changes what the code under test *does*, not merely what the assertions can see.

---

## [1.0.112] — 2026-09-06

### Fixed

- **Handing a host a certificate did not stop it serving the old ones.** Traefik publishes everything in its own ACME store under the default TLS store when it starts, and the handshake picks by server name before any router is consulted — so a name whose router was changed to serve the host's certificate went on being answered with the stored one, indefinitely. Measured on 2.11: an entry in the store wins even when its only router asks for the default, and even when it has no router at all. Six instances moved behind a CDN were still presenting certificates they can no longer renew, with correct routers, correct labels and nothing in the logs to say so. The two jobs that install the certificate and restart the proxy now retire the entries the host has started serving itself, immediately before the restart. An entry is only retired when *every* name on it is one this host covers and a CDN answers for — a certificate covering a mixture is left alone, because retiring it would take the names still reached directly down to a throwaway. A store that cannot be read, or cannot be parsed, is left exactly as it is: the names on it cannot obtain another certificate from behind a CDN, so losing one is not recoverable.

---

## [1.0.111] — 2026-09-06

### Fixed

- **A sleeping instance read as running, and everything behind that flag stopped happening.** `running` was derived from "has any container of this instance been seen recently", and an instance put to sleep keeps its database and backup containers up — so the answer was yes all night and the flag never fell. It now asks about the `odoo` container specifically, which is what `running` has always meant and what the SSH probe measures. Measured on six instances, four of them asleep: all six reported running. Downstream, none of them was ever marked sleeping, and a layer's activity clock was being refreshed as though somebody had visited. Three fail-safes, not two: a fleet that reports containers but not one `odoo` among them is a broken query far more plausibly than every instance stopping at once, and changes nothing.

- **The instance card showed one reading printed once per service.** Every service dot was drawn from the single `running` flag, so `db` and `odoo` could never disagree and a stopped companion was invisible. The health probe has always computed the state of each container and threw it away after alerting on it; it is now kept in `service_states` and the card draws each dot from its own reading, falling back to the old behaviour until the first probe lands.

### Added

- **"Asleep" is now a thing the panel can say.** Being down on purpose and being down are not the same thing to show an operator, and until now both read as "Stopped". Instances answer `_stop_is_expected` — always False here, since base knows of no legitimate reason for a deployed instance to be stopped — and the layer that schedules sleep overrides it.

- **An alert when one of the panel's scheduled actions is switched off and stays off.** A cron that is off raises nothing and queues nothing; the work simply never happens. A deploy pauses these for the length of its window and a run that was killed rather than aborted never resumed them: twenty-three of a panel's twenty-four scheduled actions stayed off for five days, its nightly backups among them, with nothing to report it. The rule is "has run before, is off now, and has been for longer than a deploy window" — no list of what should be running, which would drift away from what the modules declare.

---

## [1.0.110] — 2026-09-05

### Fixed

- **Installing the host's certificate failed on every hardened host.** Traefik runs as root inside its container and owns `certs/` on any host it has already written an ACME store into, so a host whose jobs run as the unprivileged operator the hardening playbook creates could not move a file in there. Measured on a real host: every other step of the job succeeded and this one died on `Permission denied`, leaving the host with the CDN's ranges and without the certificate it is supposed to serve — the exact half-state that makes a proxy answer handshakes with a throwaway. The plain move is still tried first, so a host without sudo behaves exactly as before; only the fallback is new. Exercised against real shells with the directory owned both ways, and on the removal path.

---

## [1.0.109] — 2026-09-05

### Added

- **A host can say whether a router should ask for a certificate or serve the one it holds.** Asking a certificate authority for a name that now answers through a CDN can never succeed — the challenge reaches the CDN, not the host — and nothing reports it: the proxy keeps serving the certificate it already obtained until that one expires, then starts answering handshakes with a throwaway. Serving the held certificate too early is worse and faster, because a certificate issued for a CDN's benefit is trusted by that CDN and by no browser. `cloud.host._router_tls_mode` answers both at once, and only says `default` when a CDN stands in front of the name *and* the held certificate covers it. Coverage is read from the certificate's own subject alternative names rather than derived from a base domain, so the answer stays right for a purchased wildcard or an internal CA as much as for a CDN's origin certificate.
- **A domain's certificate now follows its host by default.** `cert_resolver` gains `auto`, which asks the host serving the domain instead of being maintained row by row — the previous default meant every domain had to be revisited by hand whenever a host moved behind a CDN, and forgetting one failed silently months later. Existing rows sitting on the old default are moved to `auto`, which resolves to exactly what they did before on every host reached directly. An explicit `letsencrypt`, `custom` or `none` is still honoured as typed.
- **The firewall and the proxy stay on the same list.** Both have to trust the same CDN ranges, and only one of them was being kept current: the proxy is re-shipped by a daily job over SSH, while the firewall's copy was written by the hardening playbook, which runs on Ansible at host creation and then effectively never. A range the CDN published afterwards was accepted by the proxy and dropped before reaching it. The ranges now live in named nftables sets rather than written into the rule, and `push_trusted_proxies` refreshes them — as one `nft -f` document, because flushing and refilling in two commands leaves a window where the set is empty and the rule matches nobody. A host whose ruleset predates the sets is left alone, and its inlined ranges keep working until the next hardening run. Both paths and both rulesets were exercised against a real `nft`.
- **Odoo records the visitor again, not the proxy in front of them.** Odoo reads the last entry of the forwarded chain, with `x_for=1` fixed in its own source. A reverse proxy appends the address it was reached from before passing the request on — and does so *after* its middlewares run, measured on a real Traefik v2.11, so nothing configurable inside the proxy can change what ends up last. Behind a CDN the chain therefore reads `<visitor>, <edge>` and every session is recorded against one of the CDN's own addresses, with nothing to say so: the site works. A `post_load` hook now corrects the chain from the header the proxy states the visitor in, and only when the connection came from an address configured as trustworthy — an unproven header is written by whoever is calling. Off unless both are configured, so an installation reached directly is untouched. Traefik's own middlewares were measured too and turned out to be *correct* already: they read the chain before that append, so the rate limit and the allowlists were keying on the visitor all along.
- **The webhook routers follow it too.** A route may carry `tls_mode`; `default` renders an empty TLS section, which turns TLS on and leaves the certificate to the host's default store. Verified against a real Traefik v2.11 rather than reasoned about, because a resolver named in that section makes Traefik ignore the store it was just given a certificate in.

### Changed

- `cloud.instance.domain.cert_resolver` defaults to `auto` instead of `letsencrypt`. No router changes on this release: `auto` resolves to Let's Encrypt for every host that is not behind a CDN holding a certificate covering the name.

---

## [1.0.108] — 2026-09-04

### Fixed

- **Neutralization was aborting, so restored copies stayed armed.** A copy of production restored for development holds the SSH keys that open every host and the tokens of the services the panel uses, and `odoo neutralize` is what removes them. It runs as a single transaction, and this one named `cloud_settings.vps_restore_public_key` — a column removed some time ago. Postgres rejected the statement, the whole script rolled back including Odoo's own half, and the log line saying so scrolled past. Copies believed to be neutralized were fully capable of reaching production, and one did.
- **Secrets survived neutralization even when it succeeded.** The list of what to clear is written by hand, so it drifted behind the fields: the host's default TLS key, a job's secret payload, the metrics and Grafana credentials, the backup request credential buffer, and the host terminal sessions, among others. Restore upload grants are now dropped as well, since they name hosts that the copy must not reach.

### Added

- **The list is checked against the registry instead of by hand.** `TestNeutralize` reads every encrypted field of every installed module and requires some module's script to clear it, and separately refuses any script naming a column the database no longer has — the failure that made the whole mechanism a no-op. Both halves were verified to fail before they were left passing.
- **[RB-20](docs/runbooks/RB-20-neutralize-restored-copy.md)** — restoring a copy of production without arming it, and how to confirm it worked rather than assume it did, including the trap that a module update reloads cron data and switches them all back on.

---

## [1.0.107] — 2026-09-04

### Added

- **A host says whether a CDN answers for it.** What defends a host depends on how it is reached, and that was inferred rather than recorded: declaring trusted proxy ranges was enough to make the rate limit key on the forwarded chain. On a host reached directly that chain is absent, so Traefik keyed every visitor under the same empty value — one bucket shared by everyone, measured against a real Traefik 2.11 rather than reasoned about. The two facts are now separate: ranges decide whose forwarded headers are believed, `behind_cdn` decides whether the limit reads them. A host that declared ranges by hand keeps its behaviour through the upgrade.
- **A host can be given its certificate instead of obtaining one.** Behind a CDN the challenge never arrives — the proxy terminates TLS first — so ACME cannot work there. Traefik now serves a certificate supplied on the host record as its default, shipped and removed by the same jobs that ship the proxy settings. Generic on purpose: an origin certificate from a CDN, an internal CA or a purchased wildcard all fit.
- **The firewall knows the difference too.** On a host behind a CDN the per-source connection cap on 80/443 becomes an allowlist of the CDN's ranges, because behind a proxy every visitor arrives from a handful of edge addresses and a per-source cap would throttle the CDN and protect nobody. It waits for the host to also be refusing direct traffic at the proxy, so the two halves cannot be enabled in an order that leaves the host answering no one.
- **Backups too large for one request can be uploaded from the browser.** The archive is sent in 32 MiB pieces and rebuilt on the staging path the restore already used, so no proxy in front of the panel caps it. An interrupted piece is re-sent on its own: the server keys on the offset, acknowledges one that already landed, and refuses one that would leave a hole. The ceiling is now the panel's disk, and configurable (`incubacloud.restore_upload_max_bytes`).
- **A one-use SSH key for archives that should not pass through the panel at all.** The old rsync flow printed a command and assumed the operator could authenticate to the host — true on a machine they own, false on a managed one, which left no route at all for a large archive there. The platform now lends its access instead of sharing it: a generated key confined by `rrsync -wo` to a single directory, `restrict`, and an expiry OpenSSH enforces by itself. The private half is shown once and never stored. Before anything is restored, the host reports the name, size and SHA-256 of what actually arrived — the control that makes a file somebody else could have placed there useless.
- **Restore from a link.** `https`, `sftp` or `ftp`: the host downloads the archive itself, resumably, with no size limit and nothing crossing the operator's connection. The address is validated here, not there — allowed scheme, every resolved address public, no redirects followed — and pinned into the download so the name cannot answer differently by the time the host dials it. Credentials are split off the URL, stored encrypted apart from the job payload, and delivered as a `netrc` file rather than a command line `ps` would show.

### Fixed

- **The proxy ranges reached a host that has no proxy in front of it.** A layer's policy treated every host it operates as one the CDN answers for, which is false of a host whose DNS records are created unproxied on purpose — as this changelog stated in the same release that shipped `ipStrategy`. The policy now turns on the host's own flag.

## [1.0.106] — 2026-09-03

### Added

- **A disruptive action warns before it runs.** `push_trusted_proxies` ends by restarting Traefik, and on the host that serves the panel that takes the panel's own requests and its bus connection down for a few seconds — so the operator's reward for pressing it was a "connection interrupted" error and no explanation. Job types can now carry a confirmation notice, which the host and instance action bars show before enqueueing anything; the one on this action says what is about to happen. The mechanism belongs to the job type so an extension can warn without touching the front end.

### Fixed

- **A pluggable action gave no sign it had been triggered.** `enqueue` only creates the job and hands it to the worker, so the acknowledgement was a row appearing in Recent Activity 300 ms later via the bus, at the bottom of the page, while the operator watched the button at the top. Pressing again looked like the only option — and on this action the second press lands exactly in the window where the proxy is restarting, which is how a job that succeeded came to look like a button that was broken twice over. The host bar now reports the queueing, refreshes the activity list eagerly rather than waiting for a bus event it is about to lose, and refuses a second press while the first is in flight. The instance bar had the guard and the eager refresh already; only the host was missing them.
- **The extension actions were unlabelled icons.** Every other button in those bars carries its name, so a shield with no text next to a shield with text ("Re-trust Key") was unreadable — the operator could not find the action at all. They now render like their neighbours, with the icon, the name, and a spinner while the job is being queued.

## [1.0.105] — 2026-09-03

### Fixed

- **The panel rendered a blank page.** Two methods added to the host form and the settings screen were written with a trailing comma, which separates entries in an object literal and is a syntax error between class methods. Odoo bundles this JavaScript at runtime rather than building it here, so nothing failed until the bundle was parsed in a browser: one console message, a white page, and everything upstream green — the boot test, the suite, the health check. CI now parses every front-end file as an ES module, which is the cheapest check that would have caught it and needs no toolchain. It was missing entirely: nothing in this repo looked at JavaScript before.

## [1.0.104] — 2026-09-03

### Added

- **The panel can protect its own webhook endpoint.** The allowlist walked a host's deployed instances, which on a plain installation means it protected the customers' instances — which do not serve the endpoint — and left unprotected the one thing that does: the panel itself, which is not a `cloud.instance` and so was invisible to anything looking for one. Four settings now describe it (host, hostname, backend URL, optional wildcard) and only the named host publishes the router. A layer that already knows the answer fills them in instead of making an operator type them, which is the difference between core being usable alone and core being a component of something else.
- **The host form shows the ranges actually applied, not just the override.** The box on the form is an override; on a host where a policy supplies the list, it is empty by design and said nothing about what was in force. It now sits above a read-only line naming the effective ranges and where they come from, and warns when the host's Traefik is still running the previous list. The direct-access note was reasoning about the box rather than the applied list, which is exactly backwards for the one control that can lock an origin out of its own visitors.
- **`push_trusted_proxies` is a button on the host.** Applying a proxy change otherwise needed a full setup run, which reinstalls tooling this has nothing to do with. The job also re-anchors the config-drift indicator, but only when the proxy fields are the sole difference — it ships two documents where a full setup ships five, so claiming more would hide a real pending change.

### Fixed

- **A mistyped range is refused instead of silently dropped.** The read path skips entries it cannot parse, which is right on a request path but meant a typo narrowed who the panel believes with nothing said about it — the same silent narrowing this whole mechanism exists to prevent. Both the host field and the settings field now reject the write, naming the entries at fault. The docstring claiming the UI already validated has been corrected; it never did.

## [1.0.103] — 2026-09-03

### Fixed

- **The webhook allowlist no longer publishes against a posture the host is not running.** Two daily crons decide this between them — one learns the CDN's ranges, the other publishes the allowlist — and nothing ordered them. Whichever ran first won, and one of the two orderings is broken: an allowlist published before the host has been told about the CDN compares against the connecting address, which behind a CDN is always an edge and never GitHub, so it refuses every delivery. Silently, which is the whole reason this control needed a silence alarm in the first place. A host now records the proxy ranges its Traefik was actually given, and the allowlist is held back whenever that differs from what the panel intends — which also closes the narrower race where the ranges are known but the proxy has not restarted with them yet.

## [1.0.102] — 2026-09-02

### Added

- **Hosts can declare the proxies in front of them, and core acts on it.** A host now carries `trusted_proxy_ranges`, and a full setup renders them into `entryPoints.forwardedHeaders.trustedIPs` on both entrypoints. That one setting is what separates an `X-Forwarded-For` a client wrote from one the installation's own edge appended: Traefik discards the header on every connection that is not from a declared range. Everything that keys on client identity rests on it — measured against Traefik v2.11, an allowlist reading the forwarded chain without it matches nothing and rejects 100% of traffic. Empty by default, which leaves a host behaving exactly as before.
- **The per-source rate limit keys on the visitor, not the edge.** With trusted ranges declared, `ratelimit` gains `sourceCriterion.ipStrategy.depth: 1`, so a CDN edge stops being one bucket shared by everyone behind it. Deliberately not applied without them: measured, a forwarded-chain strategy on a host that answers its visitors directly reads an absent header and Traefik keys every visitor under the same empty value — one bucket for the whole internet, worse than the address-keyed limit it replaced.
- **`block_direct_access` — refuse traffic that skipped the edge.** Prepends an allowlist middleware to the https entrypoint chain, so a request that did not arrive through a trusted proxy is refused before HSTS, rate-limit accounting or a backend lookup. Off by default, and ignored while no trusted range is set, where it would lock out every visitor the host has. The http entrypoint is left alone: it only redirects, and a redirect leaks nothing.
- **`push_trusted_proxies` — apply that without a full setup run.** `trustedIPs` is static configuration: Traefik reads it once at start-up, so a change needs the proxy restarted rather than a file drop. The job ships both documents together because they are interlocked — a host whose entrypoint names a middleware its file provider does not define answers 500 on every router it serves.
- **`push_github_webhook_edge` and the routes it publishes.** A Traefik router per hostname of every deployed instance on the host, restricting the webhook path to GitHub's published source ranges. Verifying an HMAC signature means hashing the whole body, so inside the endpoint a forged signature can only be bounded, never made free; refused at the edge it costs nothing. The allowlist reads the forwarded chain on a host that declared a trusted proxy and the connecting address on one that did not, because an unproven forwarded header is written by whoever is calling. Removal runs first and always: a stale allowlist rejects deliveries silently, while no allowlist merely leaves them unfiltered.
- **`client_ip()` — one answer to "who is asking", used by every limit.** Odoo's `--proxy-mode` applies `ProxyFix(x_for=1)`, which takes the last entry of the forwarded chain. That is correct with one proxy in front and wrong with two, and a host behind a CDN always has two: the CDN, and the reverse proxy on the host. Confirmed against the shipped werkzeug 3.0.1 — a chain of `<client>, <edge>` resolves to the edge. The helper walks the chain from the right, discarding hops inside the declared ranges, and returns the connecting address when none are declared.

### Changed

- **The webhook allowlist is in core.** It is a capability of any installation that receives GitHub deliveries, so core owns the ranges, the refresh and the executor, and a layer on top keeps only the policy: which of its hosts get it and what sits in front of them. The migration re-points the external identifiers of a module that shipped them before, rather than letting core insert a second `cloud.job.type` with a code that module already holds — the unique index would have refused it, and core updates first.
- **The refresh republishes a host whose routes moved, not only one whose ranges did.** Each host's rendered document is compared against a digest of what it last received, so a host that gained or lost an instance is republished on the next daily run.

## [1.0.101] — 2026-09-02

### Added

- **An alert for GitHub webhook deliveries going quiet.** The webhook endpoint is about to sit behind a source-address allowlist at the edge, and that allowlist has a failure mode nothing else in the system can see: a range GitHub adds and the panel never mirrors stops deliveries with no error anywhere — pushes simply stop triggering rebuilds. An hourly cron now raises `github_webhook_silent` when no delivery has been recorded for `github_webhook_silence_hours` (default 48) while a GitHub App is configured, and resolves it as soon as traffic returns. Silence is measured against the newest event of any kind, so a run of processing failures still counts as "the edge is letting GitHub through" — those have their own alert. An install that never wired GitHub up stays quiet, matching `resolve_webhook_secret`, which returns before hashing anything when no App exists.
- **`github/meta.py` — the published webhook source ranges, read defensively.** `fetch_hook_ranges()` reads the `hooks` key of `api.github.com/meta` through the existing no-redirect opener, bounded to 1 MiB, and refuses the whole document unless every entry parses as a network and the list is a plausible length. Half-understanding that document would mean building an allowlist that silently drops deliveries, so partial trust is not an option: it either fully parses or it raises.

## [1.0.100] — 2026-09-01

### Fixed

- **The GitHub project import let any authorised user pin the workers indefinitely.** `/cloud/import_project` and `/cloud/fetch_odoojs_submodules` cloned the remote repository — plus every submodule — into a temporary directory and then ran `git show` over it, synchronously, inside the HTTP request. There was no per-user quota, no global concurrency limit and no ceiling on how much the operation could download or how long it could take, so anyone able to create a project could occupy every worker with repeated imports of a large repository. The import no longer runs Git at all: it reads trees and blobs through the GitHub API under a shared budget of 250 requests, 32 MiB and 120 seconds, with explicit ceilings on tree entries, repositories, submodules, `conf.d` files, domains, manifests and YAML nodes/depth. Manifests get their own 128 KiB cap and their AST is bounded to 10.000 nodes and depth 50 before `literal_eval`, because `ast.parse` cannot be interrupted once started. The deadline is re-checked around every HTTP call, during YAML walking and between ORM writes; a savepoint drops any partially created project if it expires mid-creation.
- **The App and PAT tokens travelled in the process command line.** Authenticating a clone means embedding the credential in the remote URL, which leaves it in `argv`, in `origin` and potentially in error output. Now both credentials only ever appear in an `Authorization` header, and no authenticated remote is created.

### Added

- **Configurable hourly quotas for the two GitHub operations.** `cloud.settings` gains `rate_limit_github_previews_per_hour` (default 10) and `rate_limit_github_imports_per_hour` (default 5), both editable from the Rates tab and validated as positive. Preview and import share a single non-blocking global advisory lock so only one costly GitHub operation runs per panel at a time; that limit stays fixed because raising it changes worker capacity directly. The quota hit is committed in a short independent cursor *after* the lock is granted, so an admitted attempt still counts even if the request later rolls back, and the row is never held during the remote access.
- **`try_advisory_xact_lock` in `models/_concurrency.py`.** A transaction-scoped, non-blocking advisory lock whose key is hashed together with the current database OID, since PostgreSQL advisory locks are cluster-global rather than database-local. Used here for the import lock, and available to other modules.

## [1.0.99] — 2026-08-31

### Fixed

- **The preferences modal was an id-to-name oracle over every project on the platform.** `/cloud/save_user_preferences` sanitised the personal mute list with `cloud.project.sudo().browse(ids).exists()` — sudo skips ACLs *and* record rules, so any id that existed anywhere was accepted — and `/cloud/get_user_preferences` then read the list back and returned each project's `name`. Write anything, read the name. The docstring's reasoning ("muting only restricts the caller's own notifications, so no visibility check is needed beyond existence") holds for the *effect* of a mute and fails for what the read side hands back. Reach was every internal user: `/cloud` renders on `_is_internal()` alone with no cloud role required, and because the whole path went through sudo it worked even for someone with no `ir.model.access` on `cloud.project` at all. Both ends now go through `_filtered_access('read')`, which returns the permitted subset instead of raising, so an id beyond the caller's record rules is dropped silently and a caller with no cloud role still saves the rest of the form. The read side also stops reading the m2m off `env.user`, which is sudoed by construction (`Environment.user` returns `self(su=True)`) — the explicit `.sudo()` there had been a no-op sitting on top of an implicit one.
- **`_capped_search` counted with sudo what it listed without it.** `total` came from `Model.sudo().search_count(dom)` while `records` came from a plain `search`, so `/cloud/get_projects` — which has no role gate — told a stakeholder scoped by `rule_project_member` to a single project how many projects exist on the whole platform, and computed `truncated` from that same number, firing the truncation banner on a list that was never clipped. The count now honours record rules, matching `cloud_get_config` and `cloud_get_dashboard`, which already did the same counts correctly a few dozen lines away. No behaviour change for the other three call sites: the backup-backend routes are manager-gated and `cloud.host` carries no `ir.rule`.

Metadata disclosure only — no record, credential or job was ever reachable through either path. Audit point BUG-004.

---

## [1.0.98] — 2026-08-30

### Fixed

- **The uniqueness of a metric rule's code never existed.** `cloud.metric.rule` declared it as `_sql_constraints`, an attribute Odoo 19 does not read: `_add_sql_constraints` applies `_table_objects`, which only `models.Constraint`/`models.Index` descriptors populate. Nothing failed and nothing warned, and the database agreed — no unique index on `code` in any environment. It matters because `code` is the alert's dedup key, as the field's own help says: `_cron_evaluate` raises and resolves through `raise_alert(rule.code, ...)` / `resolve_alert(rule.code, ...)`, and `cloud.alert` dedups on `(code, host, instance)`. Two active rules on one code therefore share a single alert row, and on every cron pass the rule that is not breaching resolves the alert the breaching one just raised — a genuine alert that switches itself off

### Changed

- The rule-code uniqueness is now a partial unique index over **active** rules. Only active rules are evaluated, so only among them can two rules fight over an alert; a plain `UNIQUE (code)` would also reserve an archived rule's code for ever, which nobody asked for. Same shape as `cloud_alert_active_target_uidx`
- Migration `1.0.98` **archives** — never deletes — any active rule whose code another active rule already holds, keeping the one that carries an xmlid (or the oldest). The rows are operator configuration: they stay visible and reversible, and re-enabling one without fixing its code now hits the constraint and is told why. The migration is there for the databases that cannot be surveyed before they upgrade

## [1.0.97] — 2026-08-30

### Fixed

- **A hostname could stop a host setup halfway through, with no attacker involved.** `_build_inverseproxy` splices `wildcard_domain` into the Traefik router rule through the *replacement* argument of `re.sub`, where Python interprets backslashes: a domain carrying `\1` spliced in a capture group and one carrying `\d` raised `re.error: bad escape`, killing `full_setup` mid-run. A backtick closed the rule early and turned the dashboard router into a catch-all. Both substitutions now pass a function, whose return value is used verbatim
- **`cloud.host.wildcard_domain` was `required=True` and nothing else** — no shape, no length, no character set — even though the value is read back by everything the platform generates for that machine. A constraint now refuses anything that is not an RFC 1123 hostname. Shape only, deliberately: that table also holds hosts an operator named, where `h.local` on an internal network is a fair name. Comparison is case-insensitive and the stored value is never rewritten, so a row recorded as `Pool1.example.com` stays writable and nobody's typing is silently canonicalised

### Changed

- Wildcard-domain rules move to `incubacloud/net/hostname.py` and are now the single source for every door that accepts one. The internal-address blocklist and the platform-reserved list are opt-in flags there, applied only where the value means "a public DNS name a user chose"

## [1.0.96] — 2026-08-30

### Fixed

- **A test could leave the process decrypting with the wrong key.** `test_password_utils` swaps `INCUBACLOUD_SECRET_KEY` for a throwaway one inside `patch.dict`, which restores the environment variable on exit but not the Fernet cached in the module global. Every later reader in that process then decrypted real ciphertext with a key that never wrote it, and the symptom — a CRITICAL naming a production row as unreadable — points at data corruption that is not there. The cached instance is now cleared after each of those tests, so the next reader re-initialises from the real environment; a test exercises the guarantee through `doCleanups` rather than assuming it

## [1.0.95] — 2026-08-30

### Fixed

- **One broken thing produced one alert per attempt.** `job_failed` alerts were created outright, skipping the dedup rule every other producer goes through, so a cron job that kept failing stacked a row *and* a mail/Telegram/webhook per attempt — fourteen identical criticals in six and a half hours, burying everything else in the panel. A repeat now refreshes the open alert and counts on it (`occurrences`), keyed by job type and target through a domain shared with the dismissal path so raising and clearing cannot drift apart. Failures of *different* job types on one host stay separate incidents
- **The same alert could be filed twice.** `raise_alert` searched and then created, and Odoo cursors are REPEATABLE READ: two producers firing together each read a snapshot without the other's row and both inserted — the host-metrics and instance-health probes did exactly that 142 ms apart on 2026-08-29. A partial unique index on `(code, host, instance) WHERE state='active'` now decides it, since a lock cannot: under snapshot isolation the loser still cannot see the winner's row. Losing that race is treated as success, inside a savepoint, so the job that lost it does not roll back over an alert that already exists
- The health probe's `_inst_alert`/`_resolve_inst_alert` were a second copy of that rule and the one that had drifted; both now delegate to the model. Resolutions of instance-scoped alerts consequently reach Telegram and webhooks too, which the hand-rolled dismissal never did
- **`instance_unresponsive` fired on instances that were merely starting, and outlived the container.** A running container whose Odoo is not listening yet is what every boot looks like from outside (`curl` exits 7 or 56), and an instance a layer puts to sleep boots several times a day. It now needs the same two consecutive failures CPU and memory already required. It is also resolved on every branch where the container is not running — asleep, down, missing or skipped — because a stopped container cannot be failing to answer: an alert raised just before an instance went back to sleep used to stay critical for as long as it slept, measured at ten hours and forty minutes
- **`host_unreachable` fired on jobs that then succeeded.** `execute` judged from the retry counter which attempt was the last and alerted there, but the runner can hand a job one more attempt than the budget — so every such alert in a day was followed by the same job connecting and finishing `done`. Deciding when to stop retrying is now queue_job's alone; the alert hangs off the terminal state, where "it gave up" is a fact
- **A lost row race became a failed job and an alert.** The SSH probe stamps `last_health_check` on `cloud_instance` while the metrics cron stamps `metrics_last_seen` on the same rows, and under snapshot isolation the second one was aborted outright even though the columns are disjoint. Both writers now run on their own READ COMMITTED cursor — the liveness cron's stamp, and the private cursor every executor's `on_success`/`on_failure` already used — so the loser waits for the row instead of losing it. The same hazard could have failed a deploy *after* it had succeeded, if the cron stamped the row while the deploy's outcome hook was open. Retrying would not have been enough: PostgreSQL's error reaches Odoo's SQL layer, which logs `bad query` at ERROR before any Python can catch it, and the instance log scraper reported those lines straight back as an `instance_error_logs` alert. Serialisation failures that still escape are handed to queue_job, which already retries them, instead of being wrapped as permanent failures

### Changed

- Connection-retry budget for the monitoring probes raised from 3 attempts to 10 (~90 s to ~10 min). Three was shorter than the routine maintenance of the hosts being probed: `unattended-upgrades` reboots them inside its 04:00 window, which took a production host out for two minutes, and a network blip on the same day took it out for four — both paged. A host that is genuinely gone is still critical ten minutes later, and `metrics_host_absent` covers the same ground meanwhile
- `docs/architecture.md` documents the 04:00 UTC host maintenance window and how the monitoring is sized around it

## [1.0.94] — 2026-08-28

### Fixed

- **A purge of an archived copy could delete a live instance's backups.** A backup prefix is derived from the instance name, so a new instance taking that name inherits the prefix — and the purge emptied everything under it. On the happy path the window was narrow, but the dangerous case was the retry: a purge that failed today and ran again next month would find a live instance in that prefix and empty it in silence, reporting success. The purge now carries `PURGE_BEFORE`, the instant the deletion was decided, and deletes only objects older than it. Whatever the successor writes is newer, so the purge is safe to land at any later time
- The cutoff is stamped once, on the first `delete_archived`, and reused by every retry — taking "now" on a retry would reproduce exactly the bug it prevents. It is cleared when an instance is revived, because a deletion that was decided and then reverted must not bound a future purge: that one would predate the chain written since, delete nothing, and still report success
- An unparseable `PURGE_BEFORE` is refused rather than ignored. Falling back to the unconditional purge is the one outcome the cutoff exists to prevent, so a bad value must never mean "carry on"

## [1.0.93] — 2026-08-27

### Added

- **"Keep in panel" now means archived, not abandoned.** It takes a fresh full backup at that moment and prunes everything older, so exactly one restorable copy survives instead of a chain nobody maintains — the retention prune ran inside the very container the teardown removes, so what used to be left behind aged forever. The copy's location is frozen onto the record (`custom_backup_dst`) because the computed path is derived from the project and the name, either of which can move afterwards: a project a layer detaches from its owner has its computed path silently fall back to a shared `.../default/` prefix. Everything that touches the copy from then on — verification, revival, deletion — reads the frozen value
- **An Archived tab on the project, with what each copy costs.** It appears only when the project has archived instances, and shows for each one where its copy is, how big it was at the last check and how long ago that check happened. The state comes from a nightly cron and never from the render: listing a bucket prefix per row would put a network call behind every page view, so the panel reports what it last saw and says how old that reading is rather than presenting a week-old check as current
- **Reviving an archived instance.** It deploys again on any host — the original may be gone, full, or have been the reason for archiving — and restores the frozen copy into it. The copy is re-checked live at that moment, not read from the cron's stamp: a deploy that succeeds followed by a restore that finds nothing leaves an empty instance where the operator expected their data
- **Deleting an archived instance takes its chain with it**, through a container created for that one command and thrown away. Reviving in order to delete would be a full deploy — domains, Traefik, minutes of host resources — with all of its own failure modes, to destroy something. The record is unlinked only once the prefix is verified empty, so an interruption leaves a record whose chain is gone (deletable again) rather than a chain with no record, which nothing could ever find. It asks for the instance name in writing, and refuses outright when there is no host left to run the container on
- **A daily check that an archived copy is still there.** "The backend answers" is not "the copy is there": a provider lifecycle rule or a manual delete empties the prefix while the credentials and the bucket stay perfectly healthy, and every reachability check still passes. `unreachable` is kept apart from `missing` on purpose — one is waited out, the other means the data is gone — so a network blip never raises a false alarm about data loss. A copy that is really gone raises a critical alert immediately, rather than being discovered when someone presses revive

### Changed

- **Where an archived copy lives is manager-only.** The frozen destination and the backend's name are the same bucket metadata every other endpoint has been gated on since SEC-009, and on a hosted panel that storage may be the host's rather than the user's. Everyone who can see the project still sees that a copy exists, how big it is and when it was last verified — none of which says where it is
- **An archived instance's name stays reserved in its project.** Creating a new instance with that name is refused with a message that names the archived one — otherwise the new instance would compute the same backup path and write into the copy the archived record was keeping
- The purge program that empties a prefix now lives in one place (`scripts/lib/purge_prefix.sh`), shared by the deletion purge and the archived purge. Two copies would have been two chances to fix a bug in one and not the other, and the drift would only ever show up as objects surviving a deletion

## [1.0.92] — 2026-08-27

### Changed

- **Deleting an instance now clears its backups first, or does not happen.** Nothing pruned them afterwards — the retention job runs inside the very container the teardown destroys — so every completely-deleted instance left a chain behind that no instance owned, and on a managed destination it kept consuming a quota the customer cannot reach from the panel. `Delete completely` now empties the instance's prefix before the containers come down, through the instance's own `backup` container: the only thing that holds the credentials, the endpoint and the destination, and the only moment anything can still reach the storage
- **If the purge fails the instance is not deleted.** Tearing down first would strand the objects with nothing left able to delete them, so the step carries `stop_on_failure` and there is deliberately no "delete anyway". The failure raises an alert that names its own fix — `backup_purge_service_missing`, `backup_purge_unauthorized` or `backup_purge_failed` — classified from the script's exit code, never by matching duplicity's or boto's wording, which would silently downgrade every diagnosis to the catch-all the day either changes phrasing. An already-empty prefix answers 10 and is not a failure: the invariant already holds
- **`Delete completely` asks for the instance name.** It is now irreversible in a way it was not, and the expensive mistake it guards against is deleting the wrong instance, not deleting by accident — so it asks for the name rather than a fixed word, matched exactly, with the name deliberately not selectable inside the dialog
- The purge is gated on `_owns_instance_lifecycle`, so the two move cleanups that reuse these teardown commands against a host the instance has left never reach it — their instance is alive elsewhere and those backups are still its own. Instances without backups are excluded by `_backup_enabled()`, the same gate that decides whether the container is deployed at all

## [1.0.91] — 2026-08-27

### Fixed

- **A completed instance deletion reported as failed.** Observed in production: the teardown removed the containers, images, volumes, logrotate config and directory, logged `✓ Instance 'prod' removed from host`, and then raised `AttributeError: 'DeleteInstanceExecutor' object has no attribute '_host'`. `_host()` was a convention — six host-scoped executors each define it as `return self.job.host_id` — that `AbstractSSHExecutor` never provided, and two instance-scoped callers assumed. It is now defined once on the base class
- **Every successful move between hosts ended with its cleanup job failed.** `MoveCleanupSourceExecutor.on_success` made the same call, but unconditionally rather than on a rare path, so the last step of a working move always raised. Besides the false failure, the source host's observability labels were never refreshed and kept advertising an instance that had left it
- Both success handlers now have tests. Neither had any: the suite referenced `move_cleanup_source` only as a job-type string in the gate and chain tests, so no `on_success` in either executor was ever executed and the `AttributeError` sat there undetected

## [1.0.90] — 2026-08-27

### Fixed

- **A backup uploaded from the browser could never reach the host it was meant to restore.** The upload is served by `odoo` and consumed by `odoo_runner`, and since the job runner was split out those are separate containers sharing exactly one mount — the data directory. The archive was staged in `/tmp`, which is private to each, so the path handed across in the job payload named a file the executor could not open. It answered *"Backup file not found on Odoo server. Please re-upload and try again."* — advice that reproduces the same failure forever. Uploads now stage under the data dir, which both halves can reach; a test pins that the writer and the validator agree on that location
- **Every one of those attempts also stranded up to 2 GiB.** The only `unlink` for a staged archive lives in the executor, so it ran in the container that could not see the file. Even within one container the deletion sat after the SFTP transfer with no `try/finally`, so any failed transfer — host down, disk full, connection dropped — left the archive behind. Both restore paths that stage locally (`browser` and `from_job`) now release the file in a `finally`, and a daily cron sweeps uploads whose job never ran at all, which no `finally` can cover
- **The terminal GC cron was deciding liveness from PIDs belonging to another container.** `os.kill(pid, 0)` is meaningful only where the process was spawned; terminals come from `odoo`, crons run in `odoo_runner`. An unrelated live process there answers "alive" for a dead route, and a working terminal looks dead because its PID does not exist — and since both containers start their processes in the same low range, collisions are the common case. The cron now reaps by `last_seen`, refreshed by the controller that does own the PID; the inline check on the web side is unchanged. Affects `cloud.terminal.route` and `cloud.host.terminal.route`

## [1.0.89] — 2026-08-26

### Fixed

- **The GitHub webhook let an unsigned request cost more than it should.** The endpoint is public, POST and CSRF-exempt because GitHub cannot authenticate any other way — trust comes from an HMAC over the body — and deciding a signature is false *requires* hashing the whole body, so a forged-signature flood always costs something. What was wrong was how much. Odoo's default cap is 128 MiB while GitHub itself never sends more than 25 MB, so the endpoint accepted five times any legitimate delivery; measured, that is 147 ms of CPU per request spent on HMAC alone before the signature can be known to be false. The route now declares `max_content_length` of 32 MiB, which werkzeug enforces from `Content-Length` — an oversized delivery is answered 413 without a byte being read
- **The body was read before anything decided from the headers.** A request missing the mandatory `X-GitHub-Delivery` still paid for a full read before being told it was malformed. Header checks now come first, and a `X-Hub-Signature-256` that is not shaped like one (`sha256=` plus 64 hex) is refused without reading the body. That is not the trust boundary — the HMAC still is — just the cheapest possible refusal
- The cap is declared per route on purpose, and a test pins the restore upload's 2 GiB next door: a global limit, or a `maxRequestBodyBytes` on the Traefik `buffering` middleware (which hangs off the router for the whole host), would have broken file-upload restores

---

## [1.0.88] — 2026-08-26

### Fixed

- **Deleting a backup backend only looked at one of the three ways an instance reaches it.** An instance resolves its destination as `instance.backup_backend_id or project.backup_backend_id or <global default>`, but the guard searched only for direct assignments — so a backend used as a project default, or as the **global default inherited by the entire fleet**, could be deleted without a single warning. That search also ran with the default `active_test`, which hides archived instances: exactly the records whose backup chains are still sitting in the bucket. The guard now asks `_deletion_blockers()`, which reports all three routes at once and counts archived instances, and the refusal names what it is protecting — marking archived ones as such — instead of only saying no
- The refusal no longer implies the backups themselves are at stake. Deleting the backend record never removes an object from the bucket; it removes the panel's only pointer to them, and the message now says exactly that

### Changed

- **A destructive confirmation dialog opens with Cancel focused, not Confirm.** `IcConfirmDialog` focused the confirm button on mount for muscle memory, which meant an Enter pressed before the dialog had been read confirmed the delete. `isDanger` dialogs now open on Cancel; ordinary confirmations keep Confirm, so the common flow stays one keypress and the extra Tab is spent only where it buys something
- **The "Remove instance" dialog says what it actually does.** It read "Containers will be stopped and the directory deleted" — true, and misleading by omission: the teardown runs `docker compose down -v`, so **both** options destroy the database and the filestore and they differ only in whether the record survives. Each option now carries its own line — "Keep in panel" preserves repos, dependencies, domains and resource limits as a redeployable draft that comes back empty; "Delete completely" also removes the record and its configuration — and the dialog states what becomes of the backups: kept, but no longer maintained, because the retention prune runs inside the very backup container the teardown destroys. An instance with no backup destination is told that nothing here is recoverable

---

## [1.0.87] — 2026-08-24

### Fixed

- **The metrics account sync verified itself with a password it was never given.** `MetricsAclSyncExecutor` overrides `get_extra_vars` without calling `super()` — deliberately, because the parent announces "Deploying the metrics central", which a sync must not claim — and the one thing that fell out of the child's dict was `ic_operator_plain`. The playbook's boundary probe then authenticated as `operator` with no password at all, vmauth answered **401**, and the job failed *after* having already written and reloaded the access-control list. Two consequences, both silent: `on_success` never ran, so the granted list was never recorded, so the cron saw drift forever and re-enqueued every 30 minutes; and the play died before provisioning the new account's Grafana organisation and rewriting `orgMapping`, leaving that account's user landing in the default organisation. The sync only runs when there is an account to grant, so the bug shipped on 14 Aug and lay dormant until the next account was added
- `tasks/vmauth_acl_apply.yml` no longer defaults the operator password to `omit`. That hedge is what turned a missing variable into a 401 indistinguishable from the frontier itself having broken; undefined now fails as undefined
- A structural test walks each observability playbook (following `include_tasks`), collects every `ic_*` name it reads and never defines, and asserts the executor's own `get_extra_vars` supplies it — so an override that drops a variable fails in CI rather than at a customer's gateway

- **A rebuild could fail because Odoo was writing its own log.** `instance_logs.sh` puts `logs/` *inside* the copier project directory, which is a git repository, and nothing excluded it. `rebuild.sh commit-dirty` staged and committed the live `odoo.log` — ~20k-line diffs, on every rebuild of every instance — but Odoo kept writing, so the tree went dirty again in the sub-second before `copier update` looked, and copier refuses to run on a dirty tree. The rebuild then ran its 26 remaining steps to completion and failed at the very end on the deferred error, leaving the instance rebuilt, restarted and healthy but flagged "Changes not deployed" and skipping the template update. Only the instance with live traffic hit it; its idle sibling, rebuilt in the same second, did not
- `ic_git_exclude_logs` (new, in `scripts/lib/common.sh`) writes `/logs/` to `.git/info/exclude` and untracks whatever earlier rebuilds committed, leaving every file on disk. It runs from `commit-dirty` **before** staging, so it takes effect in the same rebuild rather than the next one, and from `instance_logs.sh install`, so a freshly deployed instance is born clean. `.git/info/exclude` and not `.gitignore` because copier owns the latter, and editing it would provoke the very template conflict this avoids

---

## [1.0.86] — 2026-08-23

### Security

- **The role gates protected the buttons, not the actions.** `cloud.job.enqueue` and `enqueue_chain` are public model methods: any internal user with create access on `cloud.job` can call them over JSON-RPC, and the panel itself calls them directly for several actions. The capability checks that decide who may deploy, back up or delete lived in the HTTP controllers and in the SPA, so a direct call skipped them. The only server-side check covered ten host-level job types; everything else was open to whoever could reach the model
- Enqueueing is now authorised per job type against a minimum role, server-side, in `_check_job_type_allowed`: lifecycle and deploy jobs need Consultant; backups, restores and exports need Developer; host-level, observability and host-move jobs need Administrator. The gate covers every dispatch path — `enqueue`, `enqueue_chain`, **`retry_job` and `unblock_and_enqueue`** (a retry runs under the *retrying* user, so without it a Consultant could relaunch a failed higher-role job of their project). The map is **fail-closed** — an unmapped code demands Administrator — and a structural test forces every declared job type to be listed, so a new one cannot ship ungated by omission
- This closes real escalations, not hypothetical ones. A Developer could enqueue `move_cutover` (which rewrites an instance's host with no further check) or `move_cleanup_source` (which runs the delete teardown — `compose down` plus `rm -rf` — on a host and never reaches the guarded `unlink`), and could start the remote teardown of a **production** instance: the manager check lived in `unlink()`, which runs *after* the instance is already gone from the host. Deleting a production instance is now refused at enqueue, before any command is sent
- **Executors are trusted readers of the host connection material.** Jobs run under the environment of whoever enqueued them, while the SSH endpoint and credential are field-gated to Developer — so a Consultant-triggered deploy or rebuild failed with an access error inside the worker, and had done since those gates were introduced. The executor now reads the host record elevated when it opens the connection (constructor, transport, Ansible inventory), which is what the original hardening intended; everything else keeps the caller's environment, so ORM guards and audit attribution are unchanged. The elevation stays out of `ssh_connect_kwargs()`, which is public and returns the password

### Removed

- Dead job types `pause_instance` and `clone_data`: no executor, no caller, no jobs ever recorded. The `clone_to_staging` **feature** is untouched — it chains deploy, download and restore, and never used the `clone_data` type
- The `delete_project` executor, orphaned since project deletion stopped queueing remote cleanup. Its job type record stays: historical jobs still reference it

---

## [1.0.85] — 2026-08-23

### Security

- **ACLs granted read to the lowest cloud role without any record rule to scope it (SEC-009).** Every role inherits the Stakeholder (`group_cloud_user`) read grant, so a model whose only rule targeted a higher group left the low roles with unrestricted read: a stakeholder or consultant could `search([])` over the whole system by raw RPC and read infrastructure they were never meant to see — host SSH endpoints, backup-backend buckets/endpoints, GitHub webhook payloads (pusher identities, commit messages, contributor emails). Latent today (only Administrators exist) but the lower roles are part of the product
- `cloud.github.event` and `cloud.host.whitelist` lose the Stakeholder read ACL — both are now Administrator-only, closing the raw-RPC leak. The former's record rule only named the Project-Manager group, so it never restricted the lower roles it was written to protect; it is removed as dead
- `cloud.instance.pending.push` keeps the ACL (its "pending pushes" panel is legitimately read by a project member) but gains a member-scoped record rule + a PM+ all rule, mirroring the other instance-child models: a stakeholder now sees pending pushes only for instances of projects they belong to, not every project's commit metadata
- The host SSH endpoint (`ip_address`, `user`, `port`) is now field-gated at the ORM layer to **Developer** — the same gate the host credential (`password`/`key_file`) already carries, and the lowest role that opens SSH jobs: jobs run under the user who enqueued them, so a stricter gate would have failed every Developer-triggered deploy/rebuild/backup inside the executor. Roles below Developer get neither the endpoint nor the credential, by raw RPC or otherwise. `allowed_ssh_ips` (hardening configuration) and the backup-backend `s3_access_key_id` are gated to Administrator, like the S3 secret already was. The instance serializers (`get_instance`, `get_project_full`, `get_host_instances`, `get_project_instances`) redact the SSH endpoint below Developer (so a Developer's restore dialog still builds a working rsync command) and the bucket path below Administrator, as `get_hosts` already did
- The backup-backend list endpoint (`/cloud/get_backup_backends`) was the one backend route missing its capability gate; it now checks `manage_settings` like all its siblings

---

## [1.0.84] — 2026-08-23

### Added

- **The nftables connection-rate cap is now settable per host.** A new `http_conn_rate` field on the host (default 0 = off) is handed to the hardening playbook as `ic_http_conn_rate`, so the forward-hook meter added in 1.0.83 can be turned on for a single host from the panel (Host → Settings) and pushed with the next hardening run. It stays off until a real value is set; enable it only after rehearsing the change on a throwaway VPS (RB-18), since an unrehearsed drop on that hook can break Docker's published ports. A layer's own hardening executor inherits the wiring unchanged

---

## [1.0.83] — 2026-08-22

### Security

- **Nothing throttled HTTP traffic to the instances.** An instance domain that resolves straight to its host, not through a CDN, let a login flood reach Odoo's pbkdf2 hasher directly — the same asymmetric CPU-DoS as SEC-008 on the panel, but against the instances' sites, and unbounded: a single source could saturate a host's workers
- Traefik now carries a per-source-IP `rateLimit` middleware, attached as a default of the `https` entrypoint so it reaches every copier-generated instance router without editing a single generated compose file — the same mechanism the HSTS header already uses. Requests are throttled by client IP before they can reach a worker (default 300/min, burst 100 per IP: generous for a human, a wall for a flood)
- The threshold lives in the host's watched `config.yml`, so it is tunable live with no image rebuild. Existing hosts are retrofitted in place by `init_traefik_templates`: the middleware is added to the dynamic config and referenced on the entrypoint as an interlocked pair that fails closed, exactly like the HSTS retrofit — a reference to a middleware the file provider does not define would 500 the whole host, so the reference is only written once the middleware is, and only onto the entrypoint chain this project manages (the one HSTS marks), never an operator's own
- Direct traffic only by design: `sourceCriterion` defaults to the real remote IP, correct when a host is reached directly. A host placed behind a trusted CDN (the panel behind Cloudflare, or a customer fronting their own domain) must set `sourceCriterion.ipStrategy` so the limit keys on the real client and not a handful of edge IPs
- **Defence in depth at the host layer**: the hardening ruleset gained an optional per-source new-connection cap on 80/443, on the nftables **forward** hook — the path instance traffic actually takes, since it is DNAT'd to the Traefik container and never crosses the input chain. It ships **off** (`ic_http_conn_rate` unset renders the current byte-for-byte ruleset) and is enabled per host only after a throwaway-VPS rehearsal: an unrehearsed drop on that hook is what once took a whole fleet down. Both rendered states validate under `nft -c`, keep balanced braces and never use `flush ruleset`
- The layered model — provider L3/L4 anti-DDoS (assumed, e.g. Hetzner/OVH), host conn-rate, Traefik per-IP, app counters — is documented in `docs/architecture.md`, and the ops actions (tuning the Traefik limit live, the behind-CDN `ipStrategy`, rehearsing and enabling the nftables cap, the provider-without-anti-DDoS gap) in the new runbook RB-18

---

## [1.0.82] — 2026-08-22

### Security

- **The GitHub App's RSA private key was stored in clear**, in the same row whose `webhook_secret` was already encrypted — the `EncryptedChar` import sat four lines above the plaintext field. Verified against production: 1675 bytes of PEM readable with a plain `SELECT`. Anyone with a database dump, a filesystem backup or read access to the table could sign App JWTs and act as the installed App
- The key is now an `EncryptedText` field, encrypted at rest with the same Fernet chain as every other secret. A post-migrate encrypts the existing row and is idempotent, so re-running it is harmless
- **`EncryptedChar` and the new `EncryptedText` share one implementation** (`EncryptedFieldMixin`). A PEM is multi-line, so it needs a `text` column, but a separate class would have escaped `_rotate_all_secrets` — which discovers what to rotate by `isinstance` — leaving ciphertext that key rotation silently skips. Rotation now tests for the shared mixin, so any future shape is picked up automatically
- The GitHub settings panel reports `has_private_key` through `_has_encrypted` instead of a bare truthiness read, so a key the current `INCUBACLOUD_SECRET_KEY` cannot open no longer takes the whole settings screen down with it

---

## [1.0.81] — 2026-08-22

### Security

- **Notification webhooks are the one outbound URL a user chooses, and they were guarded by a `startswith('https://')` check at save time.** The senders then handed the URL to a bare `urllib.request.urlopen`, which follows redirects by default and permits the https → http downgrade, so a single 302 left the checked destination. Nothing ever looked at where the hostname resolved, and a check at save time says nothing about where the name points minutes later when the notification is sent
- Measured from a panel's container, with only a TCP connect and no request sent: `169.254.169.254:80` (cloud metadata), `127.0.0.1:8069` (its own Odoo) and `db:5432` (the internal Postgres) all answer. On a panel host that has not been through the hardening playbook there is no egress filter behind the application check
- Outbound webhook delivery now goes through a single guarded client (`net/outbound.py`): https only, no credentials in the URL, port 443, and every address the hostname resolves to must be public — loopback, RFC1918, link-local (which is where the metadata service lives), reserved, multicast, CGNAT and their IPv6 equivalents including IPv4-mapped forms are refused, and a name answering with a mix of public and private addresses is refused whole
- **The socket is pinned to the address that passed validation.** Resolving, approving and then letting the stack resolve again is a TOCTOU, and DNS rebinding exists to exploit exactly that window; the hostname is kept for SNI, the `Host` header and certificate validation, so pinning does not weaken TLS
- Redirects are refused outright rather than re-validated, matching what this codebase already did for its GitHub calls. The response is read up to a bound and discarded, so the channel stays blind: no body, no status, no error detail that could turn a webhook into a probe of the internal network
- The three Telegram calls moved to the existing no-redirect opener. The host is fixed, but the URL carries a user-supplied bot token and the bare opener followed redirects
- **`/cloud/save_user_preferences` now refuses share users.** The route is `auth='user'` with no group check, so a portal customer could store a webhook URL; it stayed inert only because both senders filter `share = False` in other files. The SPA that owns the form is internal-only, so no legitimate caller is affected — what this removes is a trap that any future widening of the notification audience would have sprung
- Fixed-host GitHub calls were already using the no-redirect opener. The user-chosen destination — the only one that carries real risk — was the one left on the bare call

---

## [1.0.80] — 2026-08-20

### Fixed

- **The daily prune no longer deletes the networks of stopped stacks.** `docker system prune` also prunes networks, and a project's network is the one resource here that can be neither protected nor safely swept. Labelling it changes its definition, so `docker compose` recreates it and tears a live stack down mid-command — that was tried and it left instances stopped. Leaving it unlabelled was justified by the claim that protecting the containers already protects the network, "because Docker keeps a network alive while any container, running or stopped, holds an endpoint on it". **That claim is false.** Stopping a container tears its sandbox down and releases the endpoint, so a stopped stack's network counts as unused and goes, however well its containers are labelled
- Measured: the prune deleted the `<project>_default` networks of two stacks kept stopped on purpose. Their containers survived, so the post-prune check reported `✓ All managed stacks intact` while neither stack could start. It surfaced hours later as a failed backup, on an instance whose backup path has to wake the stack
- The failure is not obvious from the outside because compose *recreates* containers whose definition changed and merely *starts* the rest — and a started container keeps the network id it was created with. `db` is precisely such a container when a stopped stack is woken, so the daemon answers `network <id> not found` and the whole `up` fails
- The play now runs `docker container prune`, `docker image prune` and `docker builder prune` instead of `docker system prune`. Networks occupy no disk, so never touching them reclaims exactly as much as before — the 12.86 GB of the run that caused this were images and build cache
- **The post-prune verification covers networks now.** It only ever inventoried containers, which is why it certified two broken stacks as intact. It checks `<project>_default` for every deployed instance, exempting those with an active job for the same reason the container check does: a rebuild's `compose down` legitimately takes the network with it
- The reclaimed-space line is a sum now. Three prunes print three totals, and reading the output backwards would have reported whatever the build cache freed and called it the total

---

## [1.0.79] — 2026-08-20

### Fixed

- **Two image builds on one host no longer collide.** Jobs serialise per *instance* — deliberately, so one instance's deploy does not block another's — but every build on a host runs `docker compose build` against the same daemon, and upstream doodba's Dockerfile mounts the apt cache with a BuildKit id shared across the machine. Two builds reaching `apt-get install` together produce `Could not get lock /var/cache/apt/archives/lock` and one of them dies. Measured in production: the release rollout enqueued two rebuilds in the same second on the same host, one built for 456 s and the other failed 75 s in. `RebuildInstanceExecutor` now carries a per-host advisory lock, so at most one build runs per host and the loser is **deferred 30 s, not failed**
- The distinction matters more than the collision. A failed rebuild is what the rollout's guard reads as "this release is not safe to spread", so a single unlucky pair of builds latched the whole fleet until an operator re-ran the job by hand. A deferral costs thirty seconds and the rollout never notices
- The lock sits on `RebuildInstanceExecutor` because that is the only class in the codebase that issues `docker compose build`; every rebuild variant, a layer's included, subclasses it, so one placement covers all of them. It shares its namespace with the build lock a layer may already hold — two lock families each holding their own would still collide

---

## [1.0.78] — 2026-08-20

### Security

- **The session cookie now carries `Secure` and `SameSite=Lax`.** Odoo emits it with `HttpOnly` and nothing else, which left two ways for it to escape: a user who types the bare domain makes one cleartext request before the redirect to HTTPS, and that request carries the cookie; and the cookie travels on cross-site requests. `Secure` is added only when the request itself is secure (`X-Forwarded-Proto` under `proxy_mode`), so a development instance over plain HTTP is untouched and keeps working. `SameSite=Lax` is added only to `session_id` — the livechat and website visitor cookies may legitimately need cross-site delivery, and `Lax` is safe for the OIDC flows here because the authorization endpoint is reached by a top-level GET navigation. Both wrappers only ever tighten: a caller that already asked for a value keeps it
- **Traefik actually sends `Strict-Transport-Security` now.** It did not, anywhere — not on the panel and not on a single instance. The `secure` middleware that carries the header is defined on every host, complete and correct, and referenced by no router: it hangs off the `doodba` chain, and the routers copier generates reference only their own per-project middlewares. Meanwhile the middleware that *is* applied to the panel set `forceSTSHeader` with `stsSeconds` at zero, and Traefik writes no header at all when the max-age is zero. Fixed in the only two places that reach everything: `stsSeconds` on the panel's own middleware, and a new minimal `hsts` middleware wired as the default middleware of the `https` entrypoint, which covers every router on a host — instances, the catch-all, the metrics gateway — without editing a single generated compose file
- **`secure` is deliberately *not* what the entrypoint defaults to.** It sets `frameDeny`, which would blank out the panel's two Grafana iframes and break Odoo's own website editor, and `stsIncludeSubdomains`/`stsPreload`, which would make an expired instance certificate unreachable with no click-through. The `hsts` middleware carries a one-year max-age and nothing else; the remaining headers are a separate decision, taken one at a time
- Both Traefik edits ship as idempotent retrofits on the stored per-host templates, in the same conservative style as the metrics and access-log ones: they add what is missing, they leave a hand-edited file alone, and they are a no-op on a shape they do not recognise. The two are **interlocked and fail closed**: the entrypoint reference is only written when the dynamic config actually defines the middleware, because a `hsts@file` that does not resolve is not a missing header — Traefik answers 500 on every router of that entrypoint, which is the whole host. The reverse order (a middleware nobody references) is harmless. `traefik_yml` is part of the config-drift snapshot, so touching it marks every host as needing a `full_setup` — which is accurate, because a static Traefik change only takes effect when the proxy restarts

---

## [1.0.77] — 2026-08-19

### Added

- **Odoo now writes its log to a file on the host, archived one file per day.** Docker keeps a container's output inside the container, and a rebuild recreates the container by design — so the log an operator wants three days after the fact had already been discarded, and the size cap added in 1.0.76 bounds that window further. Each instance gets `logs/odoo.log` bind-mounted from its own directory and a logrotate config on the host (`daily`, `dateext`, `delaycompress`, `nocreate`), which turns it into `odoo.log.<date>` and then `odoo.log.<date>.gz`, kept for `cloud.settings.odoo_log_archive_days` days (60 by default). The file outlives the container, so a rebuild no longer takes the history with it. Existing instances pick it up on their next rebuild
- **The flag rides on the compose `command`, not on `odoo.conf`.** The panel runs one-shot containers whose output has to keep reaching the job log — module installs and the `click-odoo-update` boot test — and `docker compose run` replaces the command, so those runs are untouched by construction; a `logfile` in the conf would have silently swallowed every one of them, including third-party tooling with no flag to opt out. The override replaces the whole command, so each environment's own arguments are reproduced with it: `prod.yaml` sets none (the image's CMD applies) and `test.yaml` pins `--workers=3 --max-cron-threads=1`, which staging would otherwise lose. A test asserts both, and RB-15 now says to re-check them when the template pin moves
- **The log viewer reads the archive**: a day picker, search executed on the host (a day of logs is megabytes and the answer is usually a handful of lines), and a download button that hands over a whole day gzipped. An instance not rebuilt yet keeps showing the container's output, so the viewer never goes blank during the transition. A plain day the host compressed after it was listed (a viewer left open over midnight) still opens and downloads from its `.gz` twin instead of coming back empty
- **The day picker filters as you type and can search every day at once.** Sixty days is not a dropdown: it is a list nobody can scan, and it only helps at all if you already know the date. The picker is the same type-to-filter widget the SPA uses for any set that grows (with ◀ ▶ to step day by day), and pressing Enter in the filter box sweeps the whole archive on the host — newest day first, bounded by a timeout — and marks the days that match with their hit counts, so "which day was it" is a question the panel answers instead of one the operator has to guess. The sweep emits a completion marker: one cut short by its timeout says so, because reporting it as "no matches" would be a lie in exactly the moment someone is hunting an incident
- **`instance_logs_unhealthy`** — the archive's two silent failures now have a watchdog. Odoo falling back to the container's output (the mount is not writable — Docker creates a missing bind-mount source as root, which is why the directory is created and chowned before the stack ever starts) and a live log that grew past 512 MB (nothing is rotating it). Both look perfectly healthy from every other reading the probe takes

### Security

- **Reading logs is rate-limited and audited.** The reads share one per-user cap (`rate_limit_logs_per_min`, 60/min — the viewer polls the live tail every 4 s, so a tighter cap would break one open viewer), counted in a bucket per endpoint family (`logs_tail_`, `logs_list_`, `logs_day_`): the viewer fires the listing and the tail the instant it opens, and one shared bucket made those two upserts contend for the same row on every open — a serialization failure Odoo retried silently while the server log filled with it. The cross-day sweep and the download share a much tighter cap (`rate_limit_log_search_per_min`, 6/min) because each one makes the customer's host decompress days of logs or ship a whole day through the panel. Both caps sit on the Rates tab with the caps that were already there. Opening the viewer, sweeping every day (with the term) and downloading a day now write an audit row, so "who read whose logs" has an answer; the live tail's polling deliberately writes none, or one row every four seconds would bury the rest
- **What a log read may cost is configurable instead of baked into a release**: `log_download_max_mb` (64), `log_search_max_files` (60) and `log_search_timeout_s` (30) on Settings → General → Instance Logs. The numbers guard someone else's host, which is exactly the kind of thing that should not need a deploy to change
- **Every command that touches the archive refuses anything that is not a regular file.** `logs/` belongs to the container's uid so Odoo can write it, the panel's terminal hands a Developer a shell *inside that container* on purpose (`terminal_session` never opens a host login shell), and the reader runs on the host as the SSH user — root on an unhardened one. A symlink planted from inside the container as `logs/odoo.log.<date>` would therefore have made the log viewer read any host file the SSH user can, which is precisely the escalation the confined terminal exists to prevent. The listing offers regular files only (`find -type f`), and the read, the download and the cross-day sweep each check `[ -f … ] && [ ! -L … ]` before opening anything. Verified by planting a symlink to `/etc/passwd` and watching all four paths ignore it — and kept verified: a test module now runs every one of those commands through `sh` against a temporary `logs/` holding real files and a planted link, so the guard is exercised rather than grepped for
- **A planted file cannot use up the sweep's budget, and the health probe does not follow links either.** Executing the sweep for real showed that its candidate list came from `ls -1t odoo.log*`, which ranks a freshly planted symlink (or any junk named `odoo.log.*`) as the newest entry: skipped, yes, but each still took one of the `log_search_max_files` slots, so sixty of them would have left the sweep with nothing real to read and an honest "no matches" to report. Candidates now come from `find -type f` restricted to logrotate's exact shapes, ordered by mtime. The same executed test on the health probe showed that its ERROR scrape picked "the newest archive" with the same `ls -1t` and that `tail`/`stat` on `logs/odoo.log` would follow a link put in the live file's place — a planted link would have carried host-file lines that look like Odoo errors into an alert. Both readings now take regular files only

### Changed

- **The health probe scrapes ERROR lines from the file instead of `docker compose logs --since`.** The file has no `--since`, so the window is applied by timestamp with an awk state machine that keeps the untimestamped lines following each header — the traceback belongs to the error above it, and filtering by timestamp alone would have thrown away exactly the context 1.0.75 added. It reads the newest archive alongside the live file because rotation happens at midnight and the window straddles it once a day, and falls back to the container's output for an instance that has not been rebuilt yet

## [1.0.76] — 2026-08-18

### Added

- **Every service in the compose override now carries a `json-file` log limit** (`max-size` × `max-file`, `10m` × 3 by default). doodba's Odoo logs to stdout, the copier template sets no logging driver, and Docker's default json-file log grows until something stops it — on a host set up by `full_setup` alone nothing does, since only `host_hardening` writes a `daemon.json` with `log-opts`. Left alone, a busy production instance fills its host's disk within months and takes Postgres and every other instance on the host down with it, silently. The override that deploy and rebuild already write for resource limits and the protect label is where the guarantee belongs: per service, so it holds on any host regardless of how it was set up; on hardened hosts it merely restates the daemon default. Existing instances pick it up on their next rebuild, which recreates the containers anyway — no retrofit job, no manual step
- **`cloud.settings.container_log_max_size` / `container_log_max_file`**, exposed on Settings → General as "Container Log Rotation". Read at render time and deliberately kept out of the per-instance config snapshot: bumping the retention must not light the fleet's "Changes not deployed" pill (that has happened twice for other renders), it simply lands on each instance's next rebuild. The size is validated against Docker's grammar in one canonical lower-case spelling so a typo fails at save time instead of at every stack's next `docker compose up`
- **A registry sweep in the tests asserts the limit on every deploy flavour**, in core and in any module that adds one — the same class of guard that the protect-label incident earned: a flavour whose override stops calling `super()` would ship stacks without the limit and nobody would notice until a disk filled

## [1.0.75] — 2026-08-18

### Added

- **The `instance_error_logs` alert now carries the traceback, not just the headline.** The probe greps the container log anchored to the Odoo log-level field, and a Python traceback's lines carry no level of their own, so the payload held a line saying `Exception during request handling` and nothing that could explain it. When three of those arrived on 2026-08-16, the rebuild an hour later recreated the container and took the only copy of the stack with it. The grep now keeps the lines following each header and files them under the fingerprint they belong to, one sample per group, bounded so a log stuck in a loop cannot inflate the serialized payload
- **The snapshot behind `applied_config_hash` is stored alongside it.** The hash answers whether the saved configuration is still what the last deploy shipped, and nothing else: when the whole fleet lit its "Changes not deployed" pill on 2026-08-18, finding out *what* had moved meant rebuilding the snapshot by hand and testing hypotheses against it. `_config_drift_diff()` now names the keys. Instances anchored before this field existed return nothing rather than guessing
- **A structural gate freezes the shape of the config snapshot.** Editing what `_render_copier_answers` emits moves every instance's hash at once, with nobody having touched a thing — it has happened twice, and both times the fleet stayed dirty until each instance was rebuilt or re-anchored by hand. The test fails on any key added or removed and says what the release owes the fleet. It asserts on keys rather than values, which differ legitimately between an empty CI database and a real one

## [1.0.74] — 2026-08-18

### Fixed

- **The removal of the default organisation's unfiltered datasource did not remove anything.** 1.0.73 dropped the provisioning file and deleted the row through Grafana's HTTP API; Grafana flags what it provisioned from a file as read-only and answers `403 Cannot delete read-only data source`, so the deployment failed at that task with production's datasource still in place — and dropping the file on its own leaves the row orphaned and serving, which reads as fixed and is not. `deleteDatasources` is not one way to remove a provisioned datasource, it is the only one: the file is written again, declaring the deletion instead of the datasource, and left in place so every run reasserts it. Grafana reads provisioning at startup and this deployment gives it no reason to restart, so the provisioning reload endpoint applies it in place

## [1.0.73] — 2026-08-17

### Fixed

- **Grafana's default organisation held a datasource with no account filter**, and the default organisation is where Grafana drops a login whose `groups` claim matches no entry in `orgMapping`. File provisioning carries no orgId, so the datasource the central deployment provisioned from a file could only ever land there — pointed at vmauth's operator path, which spans every account. The fallback was therefore the most privileged view in the system, reachable by a user whose account exists but is not yet in the map. The deployment now provisions none and deletes the one an earlier deployment left behind; the operator loses nothing, since OIDC puts them in their own account's organisation, filtered like everyone else's. A cross-account view, if ever wanted, belongs in that organisation as a datasource named for what it is
- **The account sync skipped the organisation map whenever it had no new accounts to grant** — the common case, and the one that mattered. The organisations are the only part that needs a new account; `orgMapping` names every one of them and is rewritten whole, so gating it left an account minted and unmapped until the next full deployment of the central, with its user landing in the default organisation for the entire window. A revocation-only sync also left the map naming an account that no longer existed. The include is no longer gated: every loop inside it is over the new-account list and does nothing when it is empty, so the cost of running it anyway is one request

## [1.0.72] — 2026-08-17

### Added

- **`deploy_instance_executor._repo_merge_ref(repo, branch)`** — the ref `repos.yaml` merges for a repo, split out of the renderer so a subclass can answer from somewhere other than the repo row. Same behaviour as before (`commit_sha or branch`); the seam exists because a pin written on a different transaction than the one rendering the file is invisible to it — Odoo opens its cursors `REPEATABLE READ`, so the render's snapshot predates the commit and no amount of cache invalidation reveals it

## [1.0.71] — 2026-08-16

### Fixed

- **The fleet's "Instances observed" counter reported one instance more than the fleet has**, on every panel with at least one host reporting containers — a panel with a single instance read 2, and an empty one would have read 1. `instance_id` is attached by the agent's relabelling and only to containers that belong to an instance, so the agents' own stack and the proxy never carry it. Grouping by a label the series do not have does not drop them: PromQL collects them all into one group keyed on the empty string, and that group counted as an instance. Filtered to the series that carry the label, with `or vector(0)` so an empty fleet still reads 0 instead of falling back to "No data" — until now the phantom group guaranteed a number was always drawn
- Third appearance of this same omission: the instance picker and the liveness cron already filter on `instance_id!=""`, and the fleet counter was the surface left out. Pinned by a test so the next reader of the trio finds all three agreeing

## [1.0.70] — 2026-08-16

### Added

- **`cloud.instance._periodic_maintenance_domain()`** — one extension point for the daily maintenance crons, so a layer that deploys instances they have no business touching says so once instead of reimplementing each cron. `cron_refresh_backup_list` and `cron_instance_health` AND it into their own domain; core adds nothing to it

### Fixed

- A layer that carried **copies** of both cron bodies to add a single filter stopped receiving the guards added here afterwards. The one that mattered was "only instances whose compose file declares a `backup` service": an instance without backups declares none, so this side skipped it and the copy did not, and each such instance got a daily `backup_list` shelling into a container that does not exist. `docker compose exec` answers `service "backup" is not running` for a service that is stopped **and** for one that was never declared — so it read as a sleeping stack rather than a missing service, and the diagnosis went the wrong way for a while. A failed job and a warning alert per instance per day, and the alert never resolved, since a clean run is what clears it
- The docstring of `cron_refresh_backup_list` now says which of the two `exec` answers it is guarding against, because they are indistinguishable from the message alone

## [1.0.69] — 2026-08-16

### Removed

- **Reverts the whole of 1.0.68**, because the failure it guarded against does not occur. The reasoning behind it had a gap: `frame-ancestors 'self'` only bites when the identity provider has to *render* its login page inside the frame, and it never does when the browser already carries a provider session — the authorize step answers with a redirect, and a redirect is not something a framing rule can block. Since every panel is entered through that same provider, having a session there is the normal state and not a lucky one, so the embed simply works. The link and its note were an answer to a question nobody was asking, and the note in particular told a reader looking at working charts that signing in was impossible
- What is left behind is the measurement itself, rewritten in `docs/observability-operations.md` as a statement of why the embed works and what it depends on, so nobody has to re-derive it from headers a year from now
- Also gone: the `kiosk` parameter that only the removed link passed, and the structural test that required every embed to offer a way out — that rule described a constraint this system does not have

## [1.0.68] — 2026-08-16

### Fixed

- **An embedded dashboard can no longer be a blank rectangle with nothing to click.** Grafana delegates its sign-in to the panel's own identity provider, and that provider answers with `content-security-policy: frame-ancestors 'self'` — so the browser refuses to paint the login page inside a frame served from any other subdomain. Both surfaces that embed a dashboard now carry a link that opens the same dashboard in a tab of its own, where the sign-in completes normally
- *(Superseded the same day by 1.0.69, which reverts all of this. Kept in the log because it was released and ran in production: see that entry for why the premise was wrong.)*

## [1.0.67] — 2026-08-15

### Changed

- **One definition of "observability is configured", replacing four.** The nav entry read the master switch; the Monitoring page read the switch *and* the Grafana URL; the Settings tab read who owns the settings; the instance Metrics tab read none of them. Four surfaces each deciding it for themselves is four ways to disagree, and the disagreement that shipped was a panel offering a Monitoring entry whose only possible message was an instruction to open a Settings tab that same panel hides — a dead end reachable by anyone whose settings are injected rather than owned. `cloud.settings._observability_capabilities()` is now the single answer, and it is three independent facts rather than one boolean pretending to be enough: `collect` (the data layer is on), `dashboards` (there is something to look at — collection *and* a Grafana), and `configure` (this panel's own operator edits these settings). Collapsing those three into two flat flags is what made the contradiction expressible in the first place
- Every UI surface consumes the descriptor and none re-derives it. The Monitoring entry now appears when there are dashboards to show, **or** when there are none and this operator is the one who could set them up — so a self-hosted panel that switched observability on without a Grafana URL keeps its actionable prompt, and a panel that can act on neither is offered neither. The instance Metrics tab drops the charts block entirely in that last case rather than printing an instruction to nowhere, and keeps Recent requests, which needs no Grafana at all
- `configure` is published as `True` and left for the layer above to contradict, exactly as the flag it replaces was: core has no notion of a panel whose settings arrive from elsewhere, and acquiring one to fix this would have been the wrong repair

## [1.0.66] — 2026-08-15

### Fixed

- **Instance liveness was querying the metrics central with no credentials at all.** `promql_query` attaches auth only when handed both halves — `auth=(user, token) if (token and user) else None` — so the liveness cron, which unpacked `user, token` and then forwarded only `token=`, queried anonymously. The central answered 401, the caller logged a warning and returned, and `running` silently stopped being refreshed from metrics: every instance kept whatever it last had, which feeds whatever a layer derives from it, such as an activity clock. One of three call sites; the other two always passed both. Beyond the fix, a structural test now reads the source and refuses any `promql_query` call missing either keyword, because a behavioural test would have to be written once per call site — which is exactly what had not happened, for any of them

## [1.0.65] — 2026-08-15

### Fixed

- **The backup listing no longer shells into stacks that are stopped.** `cron_refresh_backup_list` already skipped instances whose compose stack defines no `backup` service, but "defined" and "running" are not the same precondition: `docker compose exec` needs the container up, and a stopped stack answers `service "backup" is not running` with status 1 — a failed job and an alert, every day, for as long as the stack stays down. Instances a layer keeps stopped by design surfaced it: their own rebuild writes `backup` into `compose_services`, so the moment one was refreshed it became a permanent daily failure that listing its backups could never have made succeed. The cron now requires the instance to be running, which is the same condition the executor depends on and also covers an instance asleep behind a wake gate

## [1.0.64] — 2026-08-15

### Changed

- **Grafana's identity configuration left the compose file, so adding an account no longer recreates it.** The organisation map names every account, so it changed with every account added — and it lived in the container's environment, which means `up -d` recreated Grafana and dropped every open session for a change that concerned one account. That cost is what made automating the grant look unacceptable in the first place. Measured in a lab against Grafana 11.2.0: the SSO settings API writes to Grafana's own database, **overrides** the `GF_AUTH_GENERIC_OAUTH_*` environment (the provider's `source` flips from `system` to `database`), and applies live — the login page served the new configuration with the container's restart count still at zero. The deployment and the account sync now both push the provider through that API, and the compose file carries none of it: since the database wins, a copy in the environment would be a second source that silently loses. The PUT replaces the whole provider block rather than patching a field, so the complete configuration travels — including `orgAttributePath`, whose absence silently sends every login to the default organisation while the token carries the right claim, which this codebase has already paid for once
- `_grafana_oidc()` and `_grafana_org_mapping()` join the account list on `cloud.settings`. With that, neither central job needs a subclass in a layer: behaviour that depends on the accounts follows the data instead of being re-declared on each job

## [1.0.63] — 2026-08-14

### Changed

- **The access-control list is assembled by the settings model, not by the job that writes it.** Reconciliation has to compare the same set without being an executor, and two places assembling "who should have access" is exactly how the gateway and the database drifted apart in the first place. `cloud.settings._desired_metrics_accounts()` is now the single source; the deployment and the sync both read it. A layer that adds accounts — one per panel it hosts, say — extends the model instead of overriding every job that touches the list, so behaviour that depends on the accounts follows the data rather than having to be re-declared each time

## [1.0.62] — 2026-08-14

### Added

- **Granting a metrics account is now an act of its own, instead of a side effect of redeploying the central.** The access-control list vmauth enforces was only ever written by the full deployment. For a self-hosted panel that is airtight: there is one account, minted by that same deployment, so minting and granting are literally the same act and cannot drift. A shared central breaks the equivalence — accounts are minted whenever a panel is added, while the list stays a snapshot of whoever existed the last time somebody deployed, so everyone minted afterwards holds a credential the gateway has never heard of and their agents retry against a 401 forever. Re-running the full deployment would fix it and is the wrong tool: it rewrites the compose file, recreates whatever differs, and resets Grafana's admin password. The new `sync_metrics_accounts` job does only the part that grants — rewrite the document, reload the proxy, provision the organisations the new accounts need — from the same task files the deployment uses, so the two cannot disagree about what the boundary looks like. It never touches the compose file, so it cannot recreate VictoriaMetrics, Loki or Grafana: granting an account is a sub-second reload of one proxy, which is what makes automating it uncontroversial
- **The panel now records what the gateway was actually told, not what it intended.** `metrics_accounts_deployed` holds the accounts the last successful deployment or sync put in force, and `metrics_central_host_id` holds the host carrying the stack. Without the first, "the gateway has never heard of this account" and "we have not told this panel about it yet" are indistinguishable, and nothing can decide whether handing out a credential will authenticate or fail forever. Both are written from what the run was built from rather than from the database as it stands afterwards, so an account minted while the playbook is running is not recorded as granted — because on the gateway it is not

### Changed

- The vmauth document, its reload-and-verify, and the per-account Grafana provisioning moved into task files shared by the deployment and the sync. The dashboards fileglob moved with them, which changes what its relative path resolves against — an error that does not fail but silently provisions organisations with no dashboards at all, so the task file now asserts the glob is non-empty

## [1.0.61] — 2026-08-14

### Fixed

- **Hardening no longer takes Docker's packet rules down with it.** The nftables ruleset opened with `flush ruleset`, which deletes *every* nft table on the host — including the filter/nat chains Docker programs through iptables-nft. The daemon does not notice and never re-adds them, so every published port silently loses its DNAT: containers stay up and answer on localhost while the outside world gets nothing, and the host still passes an ICMP and SSH check. Re-running hardening on a live host therefore took every instance on it offline, and it had never surfaced before because hardening only ever ran on fresh hosts, ahead of `full_setup` installing Docker — the flush had nothing to destroy yet. The ruleset now replaces only its own table (`table inet filter` / `delete table inet filter`, the idiom that also works on a first run when the table does not exist), leaving every other table untouched. Verified against a live `nft`: a foreign table created between two passes survives the second one

## [1.0.60] — 2026-08-13

### Security

- **The fail2ban exemption list no longer inherits the firewall's catch-all.** 1.0.59 gave the sshd jail an `ignoreip` built straight from the SSH allowlist, on the reasoning that those addresses are already the only ones the firewall lets near the port. That reasoning only holds while the allowlist names addresses. It degrades to `0.0.0.0/0` whenever no operator IP is known — deliberately, since locking every operator out is worse than a reachable port — and a production host can sit in exactly that state, so the jail would have been written exempting every source on the internet. fail2ban would have stopped banning anyone while the config still read like a hardened one: no error, no log line, nothing to notice. The list is now built in its own fact that drops catch-alls and keeps named addresses, and the test asserts the filter rather than the mere presence of `ignoreip` — the weaker assertion is what let the flaw through in the first place

## [1.0.59] — 2026-08-13

### Fixed

- **The rebuild's boot test can no longer take the instance down with it.** Its throwaway Postgres was started *on* the project's compose network, so it held an endpoint there for the length of the test. Any reconciliation `docker compose` decided to do in that window could not remove the network to recreate it, the step died with `has active endpoints`, and `stop_on_failure` aborted the job with `db` and `smtp` already stopped under a live `odoo` — twice in one day, every instance on the host serving 502 until the containers were recreated by hand. The throwaway Postgres is now published on the project network's *gateway* instead of joining it: reachable from every container on that bridge, invisible to compose, which stays free to reconcile whatever it likes. If the gateway cannot be resolved the step fails outright rather than falling back to the old behaviour, so the rebuild stops with the instance still running its previous image
- **A failed boot test now puts the stack back.** The cleanup ran from the tail of the script, which `set -e` skips the moment anything upstream fails — precisely when the stack most needs restoring. It moved to an `EXIT` trap that restarts whatever was running before the test. Deliberately `docker compose start` and not `up`: `up` would re-evaluate the image and deploy the very build the boot test just rejected, and would reconcile networks, so the restore could trip over the same drift as the failure it is cleaning up after
- **Metric alerts stop chasing hosts that were decommissioned on purpose.** Retiring a host does not remove its series from the central, so `metrics_host_absent` kept measuring the silence of a machine the operator had destroyed and re-raising a critical alert that could never resolve. Host-scoped rules now skip archived hosts entirely — nothing measured on a machine that left the fleet is worth an alert — and archiving a host closes whatever it still had open, through `resolve_alert` so the external channels see the closure rather than an incident that stays open forever
- **Deleting several instances from one host no longer queues one observability install per deletion.** Five removals queued five identical playbook runs, and that stampede is what fed the serialization race of 13 August: the losing transactions were rolled back and re-run by queue_job, which then reported failures for teardowns that had already finished. A refresh now collapses onto an install that is queued but not yet started. Nothing is lost, because the playbook renders its label map when it runs rather than when it was queued; a job already `started` is deliberately not matched, since it rendered its map at launch and a change arriving mid-flight does need its own run

### Security

- **fail2ban no longer bans the panel.** The sshd jail shipped without `ignoreip`, so three failed authentications from the panel — one agent offering too many keys reaches that on its own — banned it for the full `bantime`. The panel is the only thing that manages a host, so every job against the banned host failed with `ConnectionRefusedError` for a solid hour, as happened to one host. Customers were unaffected throughout (80/443 are never involved); management and monitoring were not. The jail now exempts the same allowlist nftables enforces, the control IP included. Those addresses are already the only ones the firewall lets near the SSH port, so fail2ban was adding nothing against them while being able to lock the panel out

## [1.0.58] — 2026-08-13

### Fixed

- **A teardown that already finished no longer reports itself as failed.** `on_success` commits on its own cursor, so when the job's own transaction dies afterwards the remote work survives the rollback — and queue_job re-queues the job. That happens for real whenever several instances are deleted at once on one host: the sibling jobs finish within the same instant, their closing writes lose a serialization race, and every one of them runs a second time. The retry then asked a record that no longer existed for its remote directory and died with `Expected singleton`, so a clean removal showed up in the panel as a failure and raised an alert for a host that was already tidy. The teardown now recognises the case and finishes as the no-op it is. Only an *unlinked* instance takes that path: an archived one still exists and still has its directory on the host, so it is torn down exactly as before

## [1.0.57] — 2026-08-13

### Fixed

- **The override no longer labels the project's default network, which was tearing live stacks down.** 1.0.56 stamped the protect label on the network as well as on the services. Adding a label changes the network's definition, and `docker compose` answers that by *recreating* it — so on any instance whose network predates the label, the next compose command stopped the running containers to rebuild the network underneath them. It then failed either way: a rebuild's boot test died with `has active endpoints` (its temporary Postgres was still attached) and a plain `up` with `is not connected to the network`, leaving three instances stopped instead of started. Labelling the network was never necessary: Docker keeps a network alive while any container — running or stopped — still holds an endpoint on it, so containers that survive the prune keep their network alive with them. It only disappeared before because the containers disappeared first

## [1.0.56] — 2026-08-12

### Fixed

- **The prune protection now covers everything the panel deploys, on every path.** The `incubacloud.protect=1` label was stamped only by a layer's own *deploy* executor, and that layer's *rebuild* resolved to core's rebuild — a class that never passed through it. Every rebuild therefore rewrote `docker-compose.override.yml` without the label and handed the stopped stack back to the next nightly prune, which is why the backups that wake those stacks kept failing after 1.0.35 supposedly fixed them: measured on the live host, not one container or network carried the label. The label is now stamped by core's deploy override on every service and on the project's default network, unconditionally, so it cannot depend on which subclass rendered the file. The invariant is "the panel deployed it and it may legitimately sit stopped" — one a layer keeps stopped or asleep, one stopped by hand — and what the panel deploys is destroyed by `delete_instance`, never by the prune
- **A stopped `odoo` container and a missing one are no longer the same thing to the health probe.** It listed only running containers, so a pruned stack and a sleeping one both read as "not running" and got the same generic `instance_down`. The probe now lists stopped containers too and says which case it found; a *missing* container always alerts, while a merely stopped one can be declared expected by a layered module through the new `_odoo_stop_is_expected` hook (core has no notion of scheduled sleep and keeps treating it as an incident). Companion services are still graded while an instance sleeps, since only `odoo` is put to sleep

### Added

- **The prune now proves it did no harm.** The playbook inventories the containers that survived and the executor compares them against the `odoo`/`db` containers every deployed instance on that host must have, raising a critical `prune_swept_managed` alert naming what is missing and resolving it once the stack is whole again. Instances with a job in flight are exempt, since a rebuild legitimately leaves the stack containerless for a while. With the labelling fixed this should never fire — it exists because the silent version of this failure went unnoticed for two days, surfacing only as a failed backup
- The prune log now says what was deleted, counted by kind, and names the networks it removed instead of only reporting the reclaimed space

## [1.0.55] — 2026-08-12

### Fixed

- **"Disk used per instance" legended every series identically.** The per-instance disk collector labelled its samples `instance`, which is the one label a scraper owns: Prometheus sets it to the target address and renames the exposed one to `exported_instance`. Every series on a host therefore arrived as `node-exporter:9100` — distinct series, correct numbers, one repeated legend. Only visible once 1.0.54 made the metric exist at all, since the collector had never produced a sample. It now labels the name `instance_name`, which nothing else claims, and the panel legends by it. A test pins the panel's legend to a label the collector actually emits and refuses the reserved one. Re-running the observability agents on a host applies it.

## [1.0.54] — 2026-08-12

### Fixed

- **The fleet's staleness panel showed one host and hid the rest.** Every agent in the fleet scrapes on the same wall-clock tick — Prometheus derives its jitter offset from the job and target address, and those are identical on every host (`node` / `node-exporter:9100`); only the external labels differ. So the healthy hosts produce the same value at the same timestamp, bit for bit, and on a shared time axis the last series drawn covered all the others. The panel is now a state timeline: one lane per host, which cannot overlap. Its expression is untouched — it is deliberately the same one the host-down rule alerts on — and the green/red break is pinned to that rule's own threshold, so panel and alert cannot drift apart. A test now enforces both halves.
- **Monitoring's Hosts and Instances tabs rendered an unnamed subject.** The embed passed no `var-host`/`var-instance`, so Grafana fell back to the first value its variable query returned, alphabetically; kiosk mode then hid the picker that would have said which one. Every host and instance but one was unreachable from that tab, with nothing on screen indicating it. Both tabs now carry a searchable picker and pin the subject explicitly.
- **The instance picker offered a bucket that is not an instance.** cAdvisor labels any container the scrape config cannot attribute — the proxy, the whitelist, the metrics stack itself — with the raw target address, which is the same literal string on every host. It sorts before every real instance, so it was exactly what the dashboard opened on. The picker now lists only containers that carry an instance id, and the panels scope to a host as well: container names repeat across hosts and an instance name is only unique within its project, so filtering by instance alone could add up two machines without saying so.
- **Every chart now says what its numbers are.** No panel in any of the three dashboards declared a unit, so a memory reading rendered as `10000000000`. Units are set per panel (bytes, percent, seconds, requests/second), with an axis label where Grafana has no unit to offer; a test fails any new chart panel that omits both.
- **Traefik legends dropped the provider suffix.** Service names arrive as `name@docker`/`name@file`; the two Traefik panels now strip it in the query, so the legend reads as the service.
- **"Disk used per instance" was empty on every host, always.** The generated collector tested `[ -d "~/project/instance" ]` with the tilde still literal — bash never expands one that reaches it as data, inside quotes or through a variable — so `du` never ran and the metrics file held nothing but its two header lines, which reads as "no data" rather than as a fault. The path is now resolved while the collector is rendered, which is where the remote home directory is known. This is the same expansion the scripts that go through `run_script()` already do; this generator was the one that did not.
- **The proxy access log never reached a provisioned host.** `traefik.yml` gained a JSON access log, but the stored per-host copies are only ever filled when empty, so the change reached new hosts only and "Recent requests" stayed empty forever on every existing one. Upgrading now merges the block into each host's saved copy — a minimal merge, never a regeneration, so a host that customised its template keeps what it configured. Re-run full setup afterwards to ship it.
- **The `secure` middleware was missing its header set on hosts provisioned before it grew one.** The same one-way template problem: HSTS, clickjacking, MIME-sniffing and referrer headers are now merged into the saved `config.yml` of hosts still on the older version, leaving any value the host set itself alone.

### Added

- **Hosts report when their saved configuration is behind the shipped templates.** Config drift only ever answered "is what we saved what we shipped" — a host could be perfectly clean by that measure and still be running a template from before a feature existed, which is exactly how the access log went unnoticed. Host detail now also shows which specific settings the saved copy lacks. It reports missing settings only: a value the host set differently is its own configuration, not drift, and settings that are the same feature written another way (a file provider reading a directory rather than a single file) are recognised as equivalent rather than flagged forever.

## [1.0.53] — 2026-08-12

### Fixed

- **The instance and host dashboards render again.** Both carried their `$instance`/`$host` template variable's query in the legacy plain-string format Grafana 9 used, plus a datasource pinned to a UID (`victoriametrics`) that only ever existed by convention, never by actually matching what the API assigns on creation. Grafana 11 tries to auto-upgrade the legacy format on load and failed, surfacing "Templating: Failed to upgrade legacy queries" and leaving the fleet-wide Hosts/Instances tabs with no data — panels still rendered wherever a URL param handed them `$instance` directly (the per-instance embed), which is what made this easy to miss. The query now uses the structured object form real Grafana 11 dashboards ship with, and the variable drops its pinned datasource entirely — same as every panel already does, which is why panels never broke: falling back to whichever datasource is default in the org it loads into

## [1.0.52] — 2026-08-11

### Fixed

- **Every account's Grafana organisation now actually gets its own datasource — and its own dashboards.** Switching the admin session to an organisation and creating its datasource were two separate tasks, each looping over every account on its own; Grafana's "current org" is a persistent property of the authenticated admin, not a per-request header, so by the time the datasource task's loop started, the switch task had already finished its own loop and left every write landing in whichever org was switched to last. Measured in production: 9 accounts, 2 datasources ever created, one of them holding a DIFFERENT account's credential. Switch and write are now one block per account, so the org is never stale when written to; an existing-but-wrong datasource is corrected in place rather than silently left behind (a bare create only ever 409s on a name collision, it can't fix one)
- **Dashboards were never visible outside Grafana's default organisation.** The file-based dashboard provider carries no `orgId`, so the fleet/instance/host dashboards only ever loaded into org 1 — and every account lands in its own organisation instead, where the panel's embed showed "Dashboard not found" and a `dashboards:read` permission error. Each account's organisation now gets its own copy of the same three dashboards, created via the API right after its datasource; since none of their panels pin an explicit datasource, each copy binds to whichever one is default in the org it lands in

## [1.0.51] — 2026-08-11

### Fixed

- **The embedded Grafana now actually feels embedded.** The panel embeds it in an iframe on an already-authenticated session, but Grafana still opened on its own native login form and made the operator click "Sign in with IncubaCloud" by hand before the OIDC exchange even started — inside an iframe that reads as "asks me to log in again" even though nothing was actually wrong. `GF_AUTH_GENERIC_OAUTH_AUTO_LOGIN` skips straight to `/oauth/authorize`; with the panel session already live, the whole round-trip completes with no visible login screen. The basic-auth path used only for the server-to-server admin API swap is unaffected and still reachable directly if ever needed

## [1.0.50] — 2026-08-11

### Fixed

- **Grafana's org_mapping now actually reads the account claim.** 1.0.49 wired the account list into `GF_AUTH_GENERIC_OAUTH_GROUPS_ATTRIBUTE_PATH`, which feeds Grafana's team-sync feature — a different thing from org_mapping, which reads `org_attribute_path` instead. With no `org_attribute_path` set, org_mapping had nothing to match against and silently fell back to Grafana's default organisation, Viewer role, regardless of what the id_token or userinfo carried. Caught live: the id_token decoded to the correct `"groups":["acct_…"]`, `/oauth/userinfo` returned the same, and the operator still landed in "Main Org." — the claim was right, Grafana just wasn't reading it from where org_mapping looks. Renamed to `GF_AUTH_GENERIC_OAUTH_ORG_ATTRIBUTE_PATH`

## [1.0.49] — 2026-08-11

### Fixed

- **Grafana's OIDC login now actually completes.** The central's `oidc.client` requires PKCE by default, but the playbook never told Grafana to send a `code_challenge` — the provider's `/oauth/authorize` would have rejected every login attempt with a 400. It also never told Grafana where the account claim lives, so the per-account `org_mapping` had nothing to match against and everyone would have landed in Grafana's default organisation. Both are now wired into the OIDC branch: `GF_AUTH_GENERIC_OAUTH_USE_PKCE` and `GF_AUTH_GENERIC_OAUTH_GROUPS_ATTRIBUTE_PATH`. The claim itself, the `/oauth/*` endpoints and the account-keyed org mapping come from the layer that hands the accounts out

## [1.0.49] — 2026-08-09

<!-- TODO(prune-protect): renumber to the next free version before committing — 1.0.49 was taken above. -->

### Fixed

- **Every deploy flavour now stamps the prune-protect label — in the base executor, unconditionally.** The label that keeps `docker system prune -af --filter "label!=…"` away from panel-managed stacks lived in one subclass in a layer, so any rebuild whose class chain skipped it rewrote the compose override *without* the label — and the nightly prune then swept the stopped stack. Measured on a live host: zero labelled containers, zero labelled networks, stopped stacks deleted daily, two days of failed backups. The invariant is "deployed by the panel, may legitimately sit stopped" (one a layer keeps stopped or asleep, a manual stop), so the base deploy executor now labels every service and the default network for every flavour, and a structural test sweeps the executor registry so a future flavour cannot lose it again

### Added

- **The prune now proves what it did.** The maintenance playbook inventories the surviving containers after pruning, and the job compares that against the `odoo`/`db` containers every deployed instance must have — a swept managed stack raises a critical alert the minute it happens, instead of surfacing days later as a failed backup. The job log also gains a per-kind summary of what was deleted (networks by name — containers are printed by ID, which explains nothing once they are gone)
- **The health probe now tells "stopped" apart from "gone".** `docker compose ps` without `-a` shows neither, so a slept instance and a pruned one produced the same generic alert. The probe now sees stopped containers, says "missing (pruned, or never created)" when there is no container at all, and asks a hook whether a stopped-but-present `odoo` was scheduled — core always answers no; a layer that puts instances to sleep answers for them, so a sleeping instance reads healthy while its companion containers are still graded

## [1.0.48] — 2026-08-09

### Changed

- **Documentation caught up with the system it describes.** The operations guide still showed a stack with no gateway, told operators to press a per-host button that no longer exists and to deploy a central via a job name that is now one setting away; the user reference still said metrics were manager-only and listed configuration steps that are now done for you. Both were rewritten, including the account boundary and *why* it rejects rather than sanitises — that rule came from measurement, and a reader who does not know it will eventually "simplify" it back into a hole
- The metrics-backend runbook now diagnoses through the gateway (the only published port), tells the two credentials apart, and explains what a 400 on a query that used to work actually means
- **New runbook: a panel sees no metrics.** Five links in a chain, each failing differently, and only one of them that panel's problem — with the explicit note that "ask them to enable something" is never the answer, because enrolment converges on its own
- Spanish translations complete: 1564 entries, none untranslated, none fuzzy

## [1.0.47] — 2026-08-09

### Changed

- **Turning observability on is one decision and one button.** The tab presented six fields and no answer to the only question an operator actually has on that screen — is this working? It now asks the single thing that cannot be derived (which host runs the central), and the button deploys the stack and, when it comes up, fills in the endpoints, generates the credential and switches observability on. A single-host setup never has to open the rest, which is folded away under Advanced. What genuinely cannot be derived is left empty and said out loud: agents on hosts *other* than the central's need a public HTTPS endpoint, which requires DNS and a certificate — inventing one would leave a fleet quietly failing to push, and that is worse than an empty field
- The tab also states, in one line, whether metrics are being collected and whether charts are available — the question the six fields never answered

## [1.0.46] — 2026-08-09

### Added

- **Alerts can now describe an instance, not just the host it happens to sit on.** Every seeded rule was host-scoped, so "this instance is broken" had no way of being said and landed, at best, as a symptom on its host's page. Rules gained a scope: instance-scoped ones resolve through the label the agents attach and raise against the instance itself. Two ship with it — an instance that stopped reporting containers, and an instance returning sustained server errors. The second only became expressible now that HTTP samples are attributed per instance, and it catches what no container metric can: an Odoo that is up and failing looks perfectly healthy to cAdvisor
- **A suppression hook so sleeping instances do not page anybody.** An instance a layer puts to sleep stops every container on purpose; cAdvisor goes quiet, and a naive rule concludes it is down. Every such instance would then raise a critical alert nightly, and operators would learn to ignore the one that matters. Core has no notion of sleeping, so it asks the instance and the layer that puts it to sleep answers. Going to sleep also clears an alert already raised, rather than freezing it on screen

### Changed

- The guard that stops a seeded rule from aggregating away the label its alert is attributed by is now **scope-aware**: it previously demanded the host label on every rule, which an instance rule has no business carrying

## [1.0.45] — 2026-08-09

### Added

- **Access logs are collected centrally, with the same account boundary as metrics.** Reading them live over SSH answers "who is hitting this instance right now"; it cannot answer "was this happening yesterday", "which instances are seeing a spike in 401s", or alert on any of it. A collector on each host now ships the proxy's access lines to a log store beside VictoriaMetrics. The account is imposed exactly as it is for metrics — set from the authenticated user, never taken from what the sender claims — which was measured in a throwaway stack before being relied on: a collector pushing with a forged account header lands under its own account, and a reader forging the header still sees only its own lines
- Retention is **14 days and deliberate**: these lines carry the IP addresses of the instances' end users, so the store keeps what an incident needs and no more

### Changed

- The log endpoint is **derived from the metrics one** rather than configured separately — on the gateway the two sit side by side, and one fewer field to fill in is one fewer to get wrong. An endpoint that does not follow that shape (a self-hosted operator pointing at their own backend) disables collection instead of guessing a URL, because a collector retrying forever against something that does not exist looks like a fault rather than an absence

## [1.0.44] — 2026-08-09

### Fixed

- **Grafana had no route to a browser at all.** The previous release moved it behind the gateway and removed its host port, which is right, but nothing then proxied it: the embed could only ever have rendered a blank frame, with no error to explain why. The gateway now serves it under `/grafana/`, and Grafana is told to build its links with that prefix — without which every asset 404s and the result looks identical to the original bug

### Added

- **One Grafana organisation per account, each with a datasource it cannot widen.** Grafana refuses a datasource outside a user's organisation, so this is the ring behind the gateway's own account filter: even a fault in the query path cannot cross accounts. Verified in a throwaway Grafana rather than assumed — a datasource created in one organisation is invisible from another, and the create/lookup/switch/create sequence behaves as the playbook expects, including the 409 that re-running produces
- **An operator-only path to Grafana's admin API.** File provisioning cannot create organisations, so the API is the only way and it needs a credential. Rather than distributing Grafana's admin password, the gateway authenticates the caller with the operator credential and then swaps in Grafana's own before proxying — so the panel never holds it, and rotating it is a redeploy of the central instead of a change everywhere that talks to it

## [1.0.43] — 2026-08-09

### Changed

- **Observability settings can be hidden by the layer above.** A new `can_configure_observability` feature flag gates the Monitoring tab in Settings. Core leaves it on, so a self-hosted operator configures their own stack as before; a layer that injects the account, credential and endpoints at deploy time and re-pushes them afterwards turns it off. Showing an editable copy there would let the panel's operator point their agents elsewhere, or simply drift from what was pushed and then look broken to both sides

## [1.0.42] — 2026-08-09

### Added

- **A Metrics tab on the instance page.** When one instance is in trouble, the fleet view is the wrong place to look, and it was the only place there was. The tab embeds that instance's dashboard pinned to it, and underneath shows the requests the charts cannot describe: status-code breakdown, the client IPs hitting hardest, the paths they are hitting, and the most recent requests with IP, method, path and status. The dashboard variable in the URL is a view filter and never a boundary — what a viewer can see is decided by the gateway from the credentials the datasource authenticates with, so editing it by hand only moves within what that account could already read
- **A monitoring readout on the host page.** Whether a host is actually reporting was previously invisible, which is half of why manual enrolment was confusing: an unmonitored host looked exactly like a monitored one, and there was no way to tell whether the button still needed pressing. It now says whether the agents are installed, whether data is arriving, and whether an install keeps failing — a health readout, not a prompt, since enrolment converges on its own

### Changed

- **Fleet metrics moved out of the settings area and are hidden until they exist.** The entry hung at the bottom of the sidebar after Settings, as though it were something you configure rather than something you look at; it now sits with the other operational views, next to Hosts. It also disappears entirely while observability is off, instead of offering a section whose only possible message is "not configured"
- **Metrics and the access log are visible from the developer role upwards**, where they were manager-only. Whoever debugs a slow or attacked instance is exactly the person who needs them, and requiring the manager role turned every incident into a relay race

## [1.0.41] — 2026-08-09

### Added

- **The proxy access log, and a per-instance view of it.** Metrics say an instance is being hammered; they carry neither the client IP nor the requested path, so they cannot say what is being done to it or let you block it — the difference between an alert and an answer. Traefik's access log was never switched on; it now is, in JSON, with headers dropped so cookies and `Authorization` never land on disk. It goes to stdout rather than a file on purpose: a file needs rotation, an unrotated access log on a busy host fills the disk, and Docker's json-file driver already rotates every container's output at 10 MB fleet-wide. The panel reads a slice on demand over SSH and summarises it — request count, status-code breakdown, top client IPs, top paths — and stores nothing, because these lines contain the IP addresses of somebody else's end users. Attribution uses `RouterName`/`ServiceName`, which each line carries in full and which is the same key the metric relabelling joins on, so it survives custom domains, redirects and multi-domain instances rather than depending on a Host-header lookup

### Fixed

- **`cloud.instance.running` had two writers and no arbitration.** The SSH health probe and the metrics liveness cron both decide whether an instance is up, on their own schedules. They agree most of the time, and the state flapped whenever they briefly did not — a race that only appears once observability is enabled, which is to say it was waiting rather than absent. Metrics now own the flag while their readings are fresh (`cloud.instance.metrics_last_seen`, written only by the cron); the SSH probe keeps doing everything metrics cannot — HTTP probing, error-log scraping — and takes the flag back by itself the moment the readings go stale, so there is no window where nobody decides

## [1.0.40] — 2026-08-09

### Added

- **Metrics accounts, so one panel cannot read or poison another's data.** The central used to be protected by a single fleet-wide secret that was valid for both reading and writing and was copied onto every host — workable while one panel owned its own backend, untenable the moment several share one. Each panel now has an account (`metrics_account`) and a credential scoped to it, and the central derives the label it stamps on every series from whoever authenticated rather than from anything the agent claims. That distinction is the whole point: an agent runs on a machine its owner has root on, so its claim about who it is can never be trusted. A second, separate credential covers the operator's unfiltered view and is deliberately never written to a host. Measured against VictoriaMetrics 1.102 before being designed around: repeated `extra_filters[]` are ORed and the last `extra_label` wins, so a gateway that merely *appends* its own filter is bypassable in both directions — hence the write path discards the agent's query string outright and the read path rejects (400) anything carrying those parameters instead of trying to sanitise it
- **Per-instance HTTP attribution.** Traefik's request rates, status codes and latencies were already being scraped and thrown away unattributed, on the documented belief that its service names were copier-time literals unrelated to anything we control. They are not: the panel runs `copier copy` itself and feeds it `project_name = doodba_project_name`, the same string it forces into `COMPOSE_PROJECT_NAME`, so the names are derived and the scrape config now relabels them onto the owning instance. The earlier conclusion came from surveying doodba projects scaffolded by hand, which is not how any panel-deployed instance is built
- **Reconciliation of observability enrolment.** A cron every 15 minutes enrols any host that should be reporting and is not, with a per-host back-off and an alert once failures persist

### Changed

- **Observability is applied to every host instead of being installed by hand.** The button on the host page is gone. It had been justified as "how an operator refreshes the labels after adding instances", a task that became automatic when deploy/move/remove started refreshing them; what remained was a button whose only purpose was enrolment — and enrolment is not a decision, since there is no host to which observability does not apply. Leaving it there implied a step that could be forgotten, and forgetting it produced a silently unmonitored host. Enabling observability is now itself the instruction: the next reconciliation tick enrols everything pending. The chain from host setup stays, but only as an accelerator — if it fails, the cron picks the host up, which is precisely what used to be missing
- Grafana no longer runs anonymously and VictoriaMetrics no longer publishes a port; both sit behind the gateway, which is now the only way in

### Fixed

- **Two bugs that shared one cause: `last_probed` meant "somebody read this host's specs", and was being used to answer two questions it could not answer.** Both the metrics reader and the SSH telemetry job write it, so (a) the guard deciding whether a host had agents installed saw the entire fleet as enrolled — the opposite of what its comment claimed — and (b) the SSH fallback's self-disabling check read a field the SSH job itself had just written, so with observability on and agents not actually reporting it stood down on the strength of its own footprint, resuming only when the freshness window expired and then standing down again. That degraded the fallback from every five minutes to roughly every fifteen, oscillating, exactly when it was the only thing collecting. Enrolment is now recorded explicitly (`metrics_agents_state`) and metric freshness has its own field written by the reader alone (`metrics_last_seen`). Both bugs were latent: they could only bite once observability was switched on, which it never has been

## [1.0.39] — 2026-08-09

### Changed

- **The Traefik scrape job no longer claims its samples cannot be attributed to an instance.** The note there stated there was no sound join key between a Traefik service name and a `cloud.instance`, and concluded that per-instance HTTP had to wait for a later observability phase. That conclusion came from surveying doodba projects scaffolded by hand, where `project_name` is whatever their author typed — it does not describe instances the panel deploys, which is all of them: `cloud_instance._render_copier_answers()` passes `project_name = doodba_project_name`, the very string the deploy forces into `COMPOSE_PROJECT_NAME`, and nothing overrides that method. Service and router names are therefore derived rather than guessed (`<doodba_project_name>-<version, dots as dashes>-prod-<main|longpolling>@docker`), as confirmed against a running proxy. The comment now records the real rule and why the old survey misled; an instance a layer routes through a router of its own reports under that router's name, a mapping that belongs to that layer. No behaviour changes; the relabelling itself is still unwired. The point is that the next person to read it will not re-derive the wrong answer and shelve the feature again

## [1.0.38] — 2026-08-08

### Changed

- **Dependencies now travel with the code a rebuild ships.** A repo line without a pinned commit is aggregated at the tip of its branch, so every rebuild pulled whatever upstream had published since — while the pip list stayed frozen at whatever the repo's `requirements.txt` said the day the line was created (it was read on create, and never again). An OCA repo adding a module that needs a new library, or bumping the floor of one it already used, produced a build with the new code and without the library it imports. Deploy and rebuild now re-read the `requirements.txt` of every unpinned repo just before writing `pip.txt`, in the same job that pulls the code. Pinned repos are skipped — frozen code, frozen dependencies — and a failed fetch falls back to the stored list, because a blip at GitHub must not stop the fleet
- **Merging those requirements knows who wrote each line.** Re-reading upstream on every rebuild would have turned each routine version bump into a conflict marker and a blocking alert per instance, for a decision nobody actually has to make. Each package now records the repo that authored its spec (`pip_dependency_sources`, on projects and instances): a repo changing a line it wrote is applied and reported in the job log, while a spec that contradicts the operator — or another repo — still becomes the usual conflict marker, files a `pip_conflict` alert and stops the job until a human picks a side. Absence of an entry means the operator owns the line, so existing records need no migration: ownership is claimed lazily the first time a repo declares a spec that already matches. Editing the field by hand takes those lines back, resolving a conflict in the repo's favour hands the line to it, and nothing upstream removes is ever deleted — the job log just notes the package is gone from the manifest. `incubacloud.requirements_resync_enabled=0` restores the old frozen behaviour

## [1.0.37] — 2026-08-08

### Fixed

- **Ansible-backed jobs no longer die with an empty log on images that lack ansible-runner.** A new host's `host_hardening` failed with a blank log page: since the Phase 3 migration to Ansible, every host-state executor (hardening, probe, prune, delete, whitelist, observability) needs `ansible-core`/`ansible-runner` at runtime, but this repo's `requirements.txt` — the file whose contents deployments merge into their pip dependencies — never declared them, so any image built from it ran without them. The executor's early guard then raised before the first log line, the only failure path in the whole pipeline that leaves no trace in the job log (the reason surfaced only in `queue_job.exc_message` and the alert excerpt). Both halves are fixed: `requirements.txt` now pins `ansible-core==2.21.2` and `ansible-runner==2.4.3` (matching the panel image), and the two early guards — missing ansible-runner, undefined playbook — write their reason into the job log before raising, so a job that cannot even start still says why on its own log page

## [1.0.36] — 2026-08-07

### Fixed

- **Standalone dialogs now follow the light theme.** The shared modal chrome (`.ic-modal`, `.ic-frow`, `.ic-btn`, `.ic-alert`) painted itself with literal dark hex values, a leftover from when the `--rl-*` tokens did not resolve outside `.ic-app` — relay.scss has since defined them on `:root` for exactly that case, and both files ship in the same /cloud bundle. Under `data-ic-theme="light"` every dialog built on that chrome (nine of them) rendered as a hybrid: dark panel and header, paper-white theme-following inputs inside it, and a Cancel button whose ink-coloured label sat invisible on the dark footer. The chrome now uses the tokens, so it matches the active theme at any mount point; the overlay scrim and drop shadows stay literal darks in both themes, same convention as the slide-over scrim
- **The light theme now reaches every corner of the SPA.** A sweep of all stylesheets found the same disease the modal chrome had, in more places: the import-dialog styled with the pre-redesign dark palette in literal hex; the instance and host detail screens building elevation from white-alpha glazes (invisible over paper) and two custom properties that no longer exist anywhere (`--text-muted`, `--text-primary` — their dark fallbacks always won); toasts and code/log panels frozen dark by literal SCSS constants; Tailwind-era accent colors (`#ef4444`, `#34d399`, `#F87171`…) instead of the Relay state tokens; and light-pastel status pills and banners doing the reverse damage to the dark theme. 118 literals across 20 files now resolve through `--rl-*` tokens — toast and code-panel tints via `color-mix()` over the themed surface so they stay opaque — and the "info" hue, which existed as five slightly different literal cyans, is now a proper token family (`--rl-cyan` + dim/soft/fg/text, with a light variant) alongside a new `--rl-red-fg`. One real dark-theme bug fell out of the sweep: primary buttons in some screens used white-on-green at 1.9:1 contrast; they now use `--rl-green-fg` like every other green control
- **SearchSelect placeholders can finally be translated.** Every dropdown passed its placeholder as a quoted OWL expression (`placeholder="'Type to search hosts…'"`), a form the i18n extractor never sees — the strings were absent from the .pot, so no .po could ever cover them and Spanish users met English placeholders in an otherwise translated dialog. All such props now use the `.translate` attribute suffix (the standard the web client's own dialogs use), the component's built-in "Search…" / "— select —" defaults go through `_t()`, and two plain inputs in the header that had borrowed the quoted-expression syntax — rendering the quotes into the visible placeholder — lost their stray quotes. The regenerated .pot picked up 19 new terms, all translated in `es.po`

## [1.0.35] — 2026-08-07

### Fixed

- **The nightly Docker prune no longer wipes containers that are stopped on purpose.** `docker system prune -af` removes every stopped container, and the exclusion filter meant to spare them (`--filter "label!=incubacloud.protect=1"`) lived in a dependent module as a patch of `get_commands` — a method this executor stopped calling the day it moved to Ansible. Nothing failed, so the loss went unnoticed until a prune landed between the rebuild of an instance kept stopped and that night's backup and took the instance's containers with it, failing the backup for the whole host. The filter now belongs to the playbook itself, fed the label from the executor so Python and playbook cannot drift apart, and a structural test pins that the prune command carries it

## [1.0.34] — 2026-08-07

### Fixed

- **Every confirmation dialog outside the instance page works again.** Extracting the shared `confirmVia` helper rewired all five components to call it but added the import to only one of them, so the host, project, settings and backup-backend panels each reached for an undefined global: ten confirmations — "Delete Host" among them — died with `ReferenceError: confirmVia is not defined` the moment the button was pressed. Nothing caught it because the bundle parses fine and the reference is only resolved on the click
- A structural test now refuses any SPA file that calls a `utils/` export it never imported. This is the check a JavaScript `no-undef` would make, done in the Python suite so the pipeline needs no linter to catch the next omitted import

## [1.0.33] — 2026-08-06

### Fixed

- **A new template question no longer writes a placeholder secret into `backup.env`.** The doodba template v9.7.0 adds `backup_backend_password`, whose default is the literal string `example-backup-backend-password`; since deploys run copier with `--defaults`, any question we leave unanswered takes its default. The answers file now sets it empty, which keeps the line out of the rendered file entirely. Only password-capable duplicity backends read it — the panel's authenticate with AWS keys — so nothing was broken, but a fake credential in a live env file is not something to ship

- **The host list no longer flickers while a job runs.** Every background refresh replaced the rendered fleet with the loading skeleton for about 120 ms, so a running job — which notifies watchers twice a second — turned the page into a strobe. The skeleton now belongs to the first paint and to the Retry button; bus events and tab refocus take a silent path that leaves the cards in place. A failed background refresh no longer replaces good data with the error stub either

### Changed

- **Executors notify on output, not on the clock.** The run loop published a bus event every half-second for the whole life of a job, whether or not anything had been written. A job sitting in a silent multi-minute `docker compose build` therefore cost every open panel a full refetch twice a second for nothing. The tick now publishes only when it actually persisted log rows; the final drain still publishes unconditionally so watchers settle on the last state, and the standalone log page keeps its own fallback poll
- **Job events reach only the users allowed to read the job**, resolved through the same visibility helper the email and external notifiers use. A stakeholder no longer receives traffic for the projects they are not a member of
- **Job events now name their target** (`host_id`, `instance_id`, `project_id`), so a panel can ignore an event that does not concern it without asking the server what the event was about. The instance page used to spend one `load_jobs` call per event — for every job in the fleet, not just its own — purely to learn that. Naming the target is safe because the audience is ACL-filtered first; the host page and a layer's pages regain filters that had been dropped for costing an extra round-trip
- Refreshed collections are merged by id instead of replaced wholesale, so a refresh only re-renders the rows that changed. This also fixes an active host search being cleared by a background refresh whenever every host happened to match the query

## [1.0.32] — 2026-08-06

### Added

- Revoking a host's trusted SSH key on an endpoint change now raises a critical alert (`host_key_revoked`) through every notification channel, and re-running "Trust SSH Key" resolves it. The revocation used to leave only an audit row, so a host could sit unreachable — excluded from every cron, refusing to build SSH connections — with nothing telling the operator to re-verify the key
- **Machine-identity check on re-trust.** A revocation now records the fingerprint of the key it dropped, and the next capture is compared against it: an identical fingerprint proves the machine did not change and the endpoint edit was administrative, while a different one raises a critical `host_key_changed` alert naming both fingerprints. The verdict reaches the operator who clicked (toast), the panel (alert) and the audit log. This is the verification actually available — most providers expose no console output or metadata carrying host keys — so the alert text no longer asks for an out-of-band check the panel cannot support. Unlike the revocation alert, a change alert is not auto-resolved: it records a past event and waits for a human to dismiss it
- Host key fingerprints are visible in the panel (host overview, `SHA256:…` in the same form `ssh-keygen -lf` prints), alongside the fingerprint of a pending revocation

### Fixed

- **The SSH host key fingerprint was never logged.** The capture read the key *type* field instead of the key blob, so the base64 decode always failed and the log line fell back to a message with no fingerprint in it — the mechanism meant to let an operator verify a TOFU capture had never produced a single fingerprint since it was written

- **Hosts hardened by the panel became unreachable for Ansible-backed jobs.** Hardening rotates the SSH port without revoking the trusted key (same machine), but the stored `known_hosts` line kept the label captured on port 22. OpenSSH files a non-default port under `[ip]:port` and reads the bare form as port 22 only, so it treated those hosts as unknown and failed host-key verification; asyncssh accepts either form, which is why the SSH executors and the terminal never noticed. Hardening now re-files the verified key under the rotated endpoint, and a migration repairs existing hosts. Recurring Docker Prune was the only job affected in practice — it had been failing on every hardened host since the Ansible executors landed in 1.0.20

## [1.0.31] — 2026-08-04

### Added

- **Refresh from production**: replace a staging instance's database and filestore with a copy of its production's, keeping the staging's own code. Pick the latest backup snapshot or an on-demand live dump; the dialog falls back to the live dump when the production has no backup destination. Runs as the existing `backup_download` → `restore_instance` pair, so both instances keep their running-job protection and their timeline entries
- `backup_download` accepts `time='live'` on production for an on-demand dump, alongside the duplicity snapshot path
- `restore_instance` accepts `neutralize` and `reset_base_url` payload flags

### Changed

- **Chained restores no longer round-trip through the core.** Clone to staging, refresh from production and host moves used to relay the full backup ZIP (database + filestore) as a base64 `ir.attachment` — the whole archive in the core's RAM twice and in its database for up to 2 hours. The download step now stages the archive on the host (`handoff='host'`) and the restore consumes it in place when both run on the same machine, or streams it source host → core disk → target host (`mode='from_host'`) when they don't. Memory stays flat regardless of archive size, and a failed restore stays retryable because the staged copy is only removed on success. Operator-facing downloads (backup, neutralized dump, export) keep the attachment but now write it via `raw`, halving their peak memory
- **Staging clones are now neutralized.** `Clone to staging` (including GitHub PR previews) restores with `--neutralize`, so the copy comes up with scheduled actions and outgoing mail servers disabled and Odoo's test banner on. Previously a clone inherited production's live crons and mail servers, which could invoice, dun and email real customers from a test copy
- A restored copy's `web.base.url` is rewritten to the target instance's own domain, and any inherited `web.base.url.freeze` is dropped, instead of keeping the source's URL until the next admin login

### Fixed

- Cloning a production with no backup destination no longer fails mid-chain on the duplicity path (leaving the fresh staging deployed but empty): the clone falls back to an on-demand live dump, same as the refresh dialog
- The Developer requirement for cloning and refreshing is now enforced on the model methods themselves, closing the direct-RPC path that only the panel routes used to gate

## [1.0.9] — 2026-07-12

### Added

- Telegram and HMAC-signed webhook push notifications for job state changes, with per-channel configuration, chat-id auto-detect and test-message actions
- Unified alert notification pipeline: every alert (including job failures, routed through their `job_failed` alert) dispatches to email, Telegram and webhook from a single code path
- Instance activity timeline split into active and recent jobs with an overflow indicator
- Watchdog cron that recovers instances stranded by a cross-host move that failed before cutover

### Fixed

- Cross-host move pipeline: fresh backup before transfer, only the `odoo` service stopped on the source, chained `queue.job` cleanup on failure
- Traefik proxy restarted after Full Setup so it re-reads its static configuration
- Webhook payload timestamp serialized as a string for JSON compatibility

## [1.0.8] — 2026-07-07

### Added

- Server-side role gate on job enqueue: host-level job types require the Administrator role

### Fixed

- Backup/restore hardening and Traefik ACME storage persistence

## [1.0.7] — 2026-07-04

### Added

- Alerts & notifications overhaul: alert dedup and auto-resolve, dismissed-alert garbage collection, per-user notification preferences (level, muted projects), email channel with immediate or daily-digest mode

## [1.0.6] — 2026-06-28

### Added

- Server-side pagination for backups, audit logs and alert history
- Light theme and Relay UI redesign
- Placement hook a layer can narrow, and cross-host instance move
- Webhook auto-rebuild can be declined per instance; auto-rebuild guards for archived instances/hosts

### Fixed

- Backup container hostname capped at 64 bytes; S3 301 redirects reported as backend connection errors

## [1.0.5] — 2026-06-10

### Added

- Transient host-connection failures retried automatically before alerting
- Host-scoped job serialization (running-job guard per host)

### Fixed

- Cron-bot server actions authorized for the Odoo 19 cron pre-check

## [1.0.1 – 1.0.4] — April–May 2026

### Added

- Multi-VPS DNS support, executor consolidation and terminal UX improvements
- Instance filter and searchable host/instance selectors in the SPA
- User documentation site (mkdocs-material) at docs.incubacloud.io

## [1.0.0] — 2026-03-29

### Added

- Initial public release of IncubaCloud Core for Odoo 19.

<!-- Rebuild routing check: two pushes in a row (1/2). -->

<!-- Rebuild routing check: two pushes in a row (2/2). -->
