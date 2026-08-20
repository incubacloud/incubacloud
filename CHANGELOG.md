# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [1.0.79] — 2026-08-20

### Fixed

- **Two image builds on one host no longer collide.** Jobs serialise per *instance* — deliberately, so one tenant's deploy does not block another's — but every build on a host runs `docker compose build` against the same daemon, and upstream doodba's Dockerfile mounts the apt cache with a BuildKit id shared across the machine. Two builds reaching `apt-get install` together produce `Could not get lock /var/cache/apt/archives/lock` and one of them dies. Measured in production: the release rollout enqueued two rebuilds in the same second on the same host, one built for 456 s and the other failed 75 s in. `RebuildInstanceExecutor` now carries a per-host advisory lock, so at most one build runs per host and the loser is **deferred 30 s, not failed**
- The distinction matters more than the collision. A failed rebuild is what the rollout's guard reads as "this release is not safe to spread", so a single unlucky pair of builds latched the whole fleet — core and tenant module alike — until an operator re-ran the job by hand. A deferral costs thirty seconds and the rollout never notices
- The lock sits on `RebuildInstanceExecutor` because that is the only class in the codebase that issues `docker compose build`; every rebuild variant (manual, tenant, warm, apply-plan) subclasses it, so one placement covers all of them. It shares its namespace with the warm-pool lock that solved this for warm builds first — two lock families each holding their own would still collide — and `WarmHostLockMixin` is now an alias of it

---

## [1.0.78] — 2026-08-20

### Security

- **The session cookie now carries `Secure` and `SameSite=Lax`.** Odoo emits it with `HttpOnly` and nothing else, which left two ways for it to escape: a user who types the bare domain makes one cleartext request before the redirect to HTTPS, and that request carries the cookie; and the cookie travels on cross-site requests. `Secure` is added only when the request itself is secure (`X-Forwarded-Proto` under `proxy_mode`), so a development instance over plain HTTP is untouched and keeps working. `SameSite=Lax` is added only to `session_id` — the livechat and website visitor cookies may legitimately need cross-site delivery, and `Lax` is safe for the OIDC flows here because the authorization endpoint is reached by a top-level GET navigation. Both wrappers only ever tighten: a caller that already asked for a value keeps it
- **Traefik actually sends `Strict-Transport-Security` now.** It did not, anywhere — not on the panel and not on a single tenant. The `secure` middleware that carries the header is defined on every host, complete and correct, and referenced by no router: it hangs off the `doodba` chain, and the routers copier generates reference only their own per-project middlewares. Meanwhile the middleware that *is* applied to the panel set `forceSTSHeader` with `stsSeconds` at zero, and Traefik writes no header at all when the max-age is zero. Fixed in the only two places that reach everything: `stsSeconds` on the panel's own middleware, and a new minimal `hsts` middleware wired as the default middleware of the `https` entrypoint, which covers every router on a host — tenants, the catch-all, the metrics gateway — without editing a single generated compose file
- **`secure` is deliberately *not* what the entrypoint defaults to.** It sets `frameDeny`, which would blank out the panel's two Grafana iframes and break Odoo's own website editor, and `stsIncludeSubdomains`/`stsPreload`, which would make an expired tenant certificate unreachable with no click-through. The `hsts` middleware carries a one-year max-age and nothing else; the remaining headers are a separate decision, taken one at a time
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
- **A registry sweep in the tests asserts the limit on every deploy flavour**, in core and in the SaaS module — the same class of guard that the protect-label incident earned: a flavour whose override stops calling `super()` would ship stacks without the limit and nobody would notice until a disk filled

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

- **Grafana's default organisation held a datasource with no account filter**, and the default organisation is where Grafana drops a login whose `groups` claim matches no entry in `orgMapping`. File provisioning carries no orgId, so the datasource the central deployment provisioned from a file could only ever land there — pointed at vmauth's operator path, which spans every account. The fallback was therefore the most privileged view in the system, reachable by a tenant whose account exists but is not yet in the map. The deployment now provisions none and deletes the one an earlier deployment left behind; the operator loses nothing, since OIDC puts them in their own account's organisation, filtered like everyone else's. A cross-account view, if ever wanted, belongs in that organisation as a datasource named for what it is
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

- The SaaS layer had been carrying **copies** of both cron bodies to add a single filter, and the copies stopped receiving the guards added here afterwards. The one that mattered was "only instances whose compose file declares a `backup` service": a free-plan tenant declares none, so this side skipped it and the copy did not, and every free tenant got a daily `backup_list` shelling into a container that does not exist. `docker compose exec` answers `service "backup" is not running` for a service that is stopped **and** for one that was never declared — so it read as a sleeping stack rather than a missing service, and the diagnosis went the wrong way for a while. A failed job and a warning alert per free tenant per day, and the alert never resolved, since a clean run is what clears it
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

- **Instance liveness was querying the metrics central with no credentials at all.** `promql_query` attaches auth only when handed both halves — `auth=(user, token) if (token and user) else None` — so the liveness cron, which unpacked `user, token` and then forwarded only `token=`, queried anonymously. The central answered 401, the caller logged a warning and returned, and `running` silently stopped being refreshed from metrics: every instance kept whatever it last had, which feeds `sleeping`, then `last_activity_at`, then the auto-suspend clock. One of three call sites; the other two always passed both. Beyond the fix, a structural test now reads the source and refuses any `promql_query` call missing either keyword, because a behavioural test would have to be written once per call site — which is exactly what had not happened, for any of them

## [1.0.65] — 2026-08-15

### Fixed

- **The backup listing no longer shells into stacks that are stopped.** `cron_refresh_backup_list` already skipped instances whose compose stack defines no `backup` service, but "defined" and "running" are not the same precondition: `docker compose exec` needs the container up, and a stopped stack answers `service "backup" is not running` with status 1 — a failed job and an alert, every day, for as long as the stack stays down. Warm spares are what surfaced it: they ship stopped by design and hold no customer data at all, yet their own rebuild writes `backup` into `compose_services`, so the moment a spare was refreshed it became a permanent daily failure that listing its backups could never have made succeed. The cron now requires the instance to be running, which is the same condition the executor depends on and also covers a tenant asleep behind its wake gate

## [1.0.64] — 2026-08-15

### Changed

- **Grafana's identity configuration left the compose file, so adding an account no longer recreates it.** The organisation map names every account, so it changed with every tenant — and it lived in the container's environment, which means `up -d` recreated Grafana and dropped every open session for a change that concerned one account. That cost is what made automating the grant look unacceptable in the first place. Measured in a lab against Grafana 11.2.0: the SSO settings API writes to Grafana's own database, **overrides** the `GF_AUTH_GENERIC_OAUTH_*` environment (the provider's `source` flips from `system` to `database`), and applies live — the login page served the new configuration with the container's restart count still at zero. The deployment and the account sync now both push the provider through that API, and the compose file carries none of it: since the database wins, a copy in the environment would be a second source that silently loses. The PUT replaces the whole provider block rather than patching a field, so the complete configuration travels — including `orgAttributePath`, whose absence silently sends every login to the default organisation while the token carries the right claim, which this codebase has already paid for once
- `_grafana_oidc()` and `_grafana_org_mapping()` join the account list on `cloud.settings`. With that, neither central job needs a SaaS subclass at all: the tenant-aware behaviour follows the data instead of being re-declared on each job, and `observability_central_ext` has nothing left to declare

## [1.0.63] — 2026-08-14

### Changed

- **The access-control list is assembled by the settings model, not by the job that writes it.** Reconciliation has to compare the same set without being an executor, and two places assembling "who should have access" is exactly how the gateway and the database drifted apart in the first place. `cloud.settings._desired_metrics_accounts()` is now the single source; the deployment and the sync both read it. A layer that adds accounts — the SaaS manager adds one per tenant — extends the model instead of overriding every job that touches the list, so the tenant-aware behaviour follows the data rather than having to be re-declared each time

## [1.0.62] — 2026-08-14

### Added

- **Granting a metrics account is now an act of its own, instead of a side effect of redeploying the central.** The access-control list vmauth enforces was only ever written by the full deployment. For a self-hosted panel that is airtight: there is one account, minted by that same deployment, so minting and granting are literally the same act and cannot drift. A shared central breaks the equivalence — accounts are minted whenever a panel is added, while the list stays a snapshot of whoever existed the last time somebody deployed, so everyone minted afterwards holds a credential the gateway has never heard of and their agents retry against a 401 forever. Re-running the full deployment would fix it and is the wrong tool: it rewrites the compose file, recreates whatever differs, and resets Grafana's admin password. The new `sync_metrics_accounts` job does only the part that grants — rewrite the document, reload the proxy, provision the organisations the new accounts need — from the same task files the deployment uses, so the two cannot disagree about what the boundary looks like. It never touches the compose file, so it cannot recreate VictoriaMetrics, Loki or Grafana: granting an account is a sub-second reload of one proxy, which is what makes automating it uncontroversial
- **The panel now records what the gateway was actually told, not what it intended.** `metrics_accounts_deployed` holds the accounts the last successful deployment or sync put in force, and `metrics_central_host_id` holds the host carrying the stack. Without the first, "the gateway has never heard of this account" and "we have not told this panel about it yet" are indistinguishable, and nothing can decide whether handing out a credential will authenticate or fail forever. Both are written from what the run was built from rather than from the database as it stands afterwards, so an account minted while the playbook is running is not recorded as granted — because on the gateway it is not

### Changed

- The vmauth document, its reload-and-verify, and the per-account Grafana provisioning moved into task files shared by the deployment and the sync. The dashboards fileglob moved with them, which changes what its relative path resolves against — an error that does not fail but silently provisions organisations with no dashboards at all, so the task file now asserts the glob is non-empty

## [1.0.61] — 2026-08-14

### Fixed

- **Hardening no longer takes Docker's packet rules down with it.** The nftables ruleset opened with `flush ruleset`, which deletes *every* nft table on the host — including the filter/nat chains Docker programs through iptables-nft. The daemon does not notice and never re-adds them, so every published port silently loses its DNAT: containers stay up and answer on localhost while the outside world gets nothing, and the host still passes an ICMP and SSH check. Re-running hardening on a live host therefore took an entire tenant fleet offline, and it had never surfaced before because hardening only ever ran on fresh hosts, ahead of `full_setup` installing Docker — the flush had nothing to destroy yet. The ruleset now replaces only its own table (`table inet filter` / `delete table inet filter`, the idiom that also works on a first run when the table does not exist), leaving every other table untouched. Verified against a live `nft`: a foreign table created between two passes survives the second one

## [1.0.60] — 2026-08-13

### Security

- **The fail2ban exemption list no longer inherits the firewall's catch-all.** 1.0.59 gave the sshd jail an `ignoreip` built straight from the SSH allowlist, on the reasoning that those addresses are already the only ones the firewall lets near the port. That reasoning only holds while the allowlist names addresses. It degrades to `0.0.0.0/0` whenever no operator IP is known — deliberately, since locking every operator out is worse than a reachable port — and both production hosts sit in exactly that state, so the jail would have been written exempting every source on the internet. fail2ban would have stopped banning anyone while the config still read like a hardened one: no error, no log line, nothing to notice. The list is now built in its own fact that drops catch-alls and keeps named addresses, and the test asserts the filter rather than the mere presence of `ignoreip` — the weaker assertion is what let the flaw through in the first place

## [1.0.59] — 2026-08-13

### Fixed

- **The rebuild's boot test can no longer take the tenant down with it.** Its throwaway Postgres was started *on* the project's compose network, so it held an endpoint there for the length of the test. Any reconciliation `docker compose` decided to do in that window could not remove the network to recreate it, the step died with `has active endpoints`, and `stop_on_failure` aborted the job with `db` and `smtp` already stopped under a live `odoo` — twice on 13 August, every tenant on the host serving 502 until the containers were recreated by hand. The throwaway Postgres is now published on the project network's *gateway* instead of joining it: reachable from every container on that bridge, invisible to compose, which stays free to reconcile whatever it likes. If the gateway cannot be resolved the step fails outright rather than falling back to the old behaviour, so the rebuild stops with the instance still running its previous image
- **A failed boot test now puts the stack back.** The cleanup ran from the tail of the script, which `set -e` skips the moment anything upstream fails — precisely when the stack most needs restoring. It moved to an `EXIT` trap that restarts whatever was running before the test. Deliberately `docker compose start` and not `up`: `up` would re-evaluate the image and deploy the very build the boot test just rejected, and would reconcile networks, so the restore could trip over the same drift as the failure it is cleaning up after
- **Metric alerts stop chasing hosts that were decommissioned on purpose.** Retiring a host does not remove its series from the central, so `metrics_host_absent` kept measuring the silence of a machine the operator had destroyed and re-raising a critical alert that could never resolve. Host-scoped rules now skip archived hosts entirely — nothing measured on a machine that left the fleet is worth an alert — and archiving a host closes whatever it still had open, through `resolve_alert` so the external channels see the closure rather than an incident that stays open forever
- **Deleting several instances from one host no longer queues one observability install per deletion.** Five removals queued five identical playbook runs, and that stampede is what fed the serialization race of 13 August: the losing transactions were rolled back and re-run by queue_job, which then reported failures for teardowns that had already finished. A refresh now collapses onto an install that is queued but not yet started. Nothing is lost, because the playbook renders its label map when it runs rather than when it was queued; a job already `started` is deliberately not matched, since it rendered its map at launch and a change arriving mid-flight does need its own run

### Security

- **fail2ban no longer bans the panel.** The sshd jail shipped without `ignoreip`, so three failed authentications from the panel — one agent offering too many keys reaches that on its own — banned it for the full `bantime`. The panel is the only thing that manages a host, so every job against the banned host failed with `ConnectionRefusedError` for a solid hour, as happened to a tenant host on 13 August. Customers were unaffected throughout (80/443 are never involved); management and monitoring were not. The jail now exempts the same allowlist nftables enforces, the control IP included. Those addresses are already the only ones the firewall lets near the SSH port, so fail2ban was adding nothing against them while being able to lock us out

## [1.0.58] — 2026-08-13

### Fixed

- **A teardown that already finished no longer reports itself as failed.** `on_success` commits on its own cursor, so when the job's own transaction dies afterwards the remote work survives the rollback — and queue_job re-queues the job. That happens for real whenever several instances are deleted at once on one host: the sibling jobs finish within the same instant, their closing writes lose a serialization race, and every one of them runs a second time. The retry then asked a record that no longer existed for its remote directory and died with `Expected singleton`, so a clean removal showed up in the panel as a failure and raised an alert for a host that was already tidy. The teardown now recognises the case and finishes as the no-op it is. Only an *unlinked* instance takes that path: an archived one still exists and still has its directory on the host, so it is torn down exactly as before

## [1.0.57] — 2026-08-13

### Fixed

- **The override no longer labels the project's default network, which was tearing live stacks down.** 1.0.56 stamped the protect label on the network as well as on the services. Adding a label changes the network's definition, and `docker compose` answers that by *recreating* it — so on any instance whose network predates the label, the next compose command stopped the running containers to rebuild the network underneath them. It then failed either way: a rebuild's boot test died with `has active endpoints` (its temporary Postgres was still attached) and a plain `up` with `is not connected to the network`, leaving three tenants stopped instead of started. Labelling the network was never necessary: Docker keeps a network alive while any container — running or stopped — still holds an endpoint on it, so containers that survive the prune keep their network alive with them. It only disappeared before because the containers disappeared first

## [1.0.56] — 2026-08-12

### Fixed

- **The prune protection now covers everything the panel deploys, on every path.** The `incubacloud.protect=1` label was stamped only by the SaaS warm-pool *deploy* executor, and a warm *rebuild* resolves to core's rebuild — a class that never passed through it. Every rebuild therefore rewrote `docker-compose.override.yml` without the label and handed the stopped stack back to the next nightly prune, which is why the free-host backups kept failing after 1.0.35 supposedly fixed them: measured on the live host, not one container or network carried the label. The label is now stamped by core's deploy override on every service and on the project's default network, unconditionally, so it cannot depend on which subclass rendered the file. The invariant is "the panel deployed it and it may legitimately sit stopped" — a warm spare, a Sablier-slept free instance, a manually stopped one — and what the panel deploys is destroyed by `delete_instance`, never by the prune
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

- **Grafana's OIDC login now actually completes.** The central's `oidc.client` requires PKCE by default, but the playbook never told Grafana to send a `code_challenge` — the provider's `/oauth/authorize` would have rejected every login attempt with a 400. It also never told Grafana where the account claim lives, so the per-account `org_mapping` had nothing to match against and everyone would have landed in Grafana's default organisation. Both are now wired into the OIDC branch: `GF_AUTH_GENERIC_OAUTH_USE_PKCE` and `GF_AUTH_GENERIC_OAUTH_GROUPS_ATTRIBUTE_PATH`. The claim itself, the corrected `/oauth/*` endpoints and the account-keyed org mapping live in the SaaS layer, which is the only one that knows tenants — and their accounts — exist

## [1.0.49] — 2026-08-09

<!-- TODO(prune-protect): renumber to the next free version before committing — 1.0.49 was taken above. -->

### Fixed

- **Every deploy flavour now stamps the prune-protect label — in the base executor, unconditionally.** The label that keeps `docker system prune -af --filter "label!=…"` away from panel-managed stacks lived in one SaaS subclass, so any rebuild whose class chain skipped it rewrote the compose override *without* the label — and the nightly prune then swept the stopped stack. Measured in production: zero labelled containers, zero labelled networks, the warm pool deleted daily, two days of failed free-host backups. The invariant is "deployed by the panel, may legitimately sit stopped" (a warm spare, a plan-slept instance, a manual stop), so the base deploy executor now labels every service and the default network for every flavour, and a structural test sweeps the executor registry so a future flavour cannot lose it again

### Added

- **The prune now proves what it did.** The maintenance playbook inventories the surviving containers after pruning, and the job compares that against the `odoo`/`db` containers every deployed instance must have — a swept managed stack raises a critical alert the minute it happens, instead of surfacing days later as a failed backup. The job log also gains a per-kind summary of what was deleted (networks by name — containers are printed by ID, which explains nothing once they are gone)
- **The health probe now tells "stopped" apart from "gone".** `docker compose ps` without `-a` shows neither, so a slept instance and a pruned one produced the same generic alert. The probe now sees stopped containers, says "missing (pruned, or never created)" when there is no container at all, and asks a hook whether a stopped-but-present `odoo` was scheduled — core always answers no; the SaaS layer answers from the tenant's plan, so a sleeping Free instance reads healthy while its companion containers are still graded

## [1.0.48] — 2026-08-09

### Changed

- **Documentation caught up with the system it describes.** The operations guide still showed a stack with no gateway, told operators to press a per-host button that no longer exists and to deploy a central via a job name that is now one setting away; the user reference still said metrics were manager-only and listed configuration steps that are now done for you. Both were rewritten, including the account boundary and *why* it rejects rather than sanitises — that rule came from measurement, and a reader who does not know it will eventually "simplify" it back into a hole
- The metrics-backend runbook now diagnoses through the gateway (the only published port), tells the two credentials apart, and explains what a 400 on a query that used to work actually means
- **New runbook: a tenant sees no metrics.** Five links in a chain, each failing differently, and only one of them the tenant's problem — with the explicit note that "ask them to enable something" is never the answer, because enrolment converges on its own
- Spanish translations complete: 1564 entries, none untranslated, none fuzzy

## [1.0.47] — 2026-08-09

### Changed

- **Turning observability on is one decision and one button.** The tab presented six fields and no answer to the only question an operator actually has on that screen — is this working? It now asks the single thing that cannot be derived (which host runs the central), and the button deploys the stack and, when it comes up, fills in the endpoints, generates the credential and switches observability on. A single-host setup never has to open the rest, which is folded away under Advanced. What genuinely cannot be derived is left empty and said out loud: agents on hosts *other* than the central's need a public HTTPS endpoint, which requires DNS and a certificate — inventing one would leave a fleet quietly failing to push, and that is worse than an empty field
- The tab also states, in one line, whether metrics are being collected and whether charts are available — the question the six fields never answered

## [1.0.46] — 2026-08-09

### Added

- **Alerts can now describe an instance, not just the host it happens to sit on.** Every seeded rule was host-scoped, so "this instance is broken" had no way of being said and landed, at best, as a symptom on its host's page. Rules gained a scope: instance-scoped ones resolve through the label the agents attach and raise against the instance itself. Two ship with it — an instance that stopped reporting containers, and an instance returning sustained server errors. The second only became expressible now that HTTP samples are attributed per instance, and it catches what no container metric can: an Odoo that is up and failing looks perfectly healthy to cAdvisor
- **A suppression hook so sleeping instances do not page anybody.** An instance a plan puts to sleep stops every container on purpose; cAdvisor goes quiet, and a naive rule concludes it is down. Every Free instance would then raise a critical alert nightly, and operators would learn to ignore the one that matters. Core has no notion of sleeping — that is a SaaS feature — so it asks the instance and the SaaS layer answers. Going to sleep also clears an alert already raised, rather than freezing it on screen

### Changed

- The guard that stops a seeded rule from aggregating away the label its alert is attributed by is now **scope-aware**: it previously demanded the host label on every rule, which an instance rule has no business carrying

## [1.0.45] — 2026-08-09

### Added

- **Access logs are collected centrally, with the same account boundary as metrics.** Reading them live over SSH answers "who is hitting this instance right now"; it cannot answer "was this happening yesterday", "which instances are seeing a spike in 401s", or alert on any of it. A collector on each host now ships the proxy's access lines to a log store beside VictoriaMetrics. The account is imposed exactly as it is for metrics — set from the authenticated user, never taken from what the sender claims — which was measured in a throwaway stack before being relied on: a collector pushing with a forged account header lands under its own account, and a reader forging the header still sees only its own lines
- Retention is **14 days and deliberate**: these lines carry the IP addresses of our tenants' end users, so the store keeps what an incident needs and no more

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

- **Observability settings can be hidden by the layer above.** A new `can_configure_observability` feature flag gates the Monitoring tab in Settings. Core leaves it on, so a self-hosted operator configures their own stack as before; SaaS tenant panels turn it off, because there the account, credential and endpoints are injected at deploy time and re-pushed afterwards. Showing an editable copy would let a tenant point their agents elsewhere, or simply drift from what was pushed and then look broken to both sides

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

- **The Traefik scrape job no longer claims its samples cannot be attributed to an instance.** The note there stated there was no sound join key between a Traefik service name and a `cloud.instance`, and concluded that per-instance HTTP had to wait for a later observability phase. That conclusion came from surveying doodba projects scaffolded by hand, where `project_name` is whatever their author typed — it does not describe instances the panel deploys, which is all of them: `cloud_instance._render_copier_answers()` passes `project_name = doodba_project_name`, the very string the deploy forces into `COMPOSE_PROJECT_NAME`, and nothing overrides that method in any of the three modes. Service and router names are therefore derived rather than guessed (`<doodba_project_name>-<version, dots as dashes>-prod-<main|longpolling>@docker`), as confirmed against a running proxy. The comment now records the real rule, why the old survey misled, and the one extra path SaaS adds — tenant panels sit behind a Sablier router and report under `<tenant.slug>-svc@file`, a mapping that belongs to the manager layer. No behaviour changes; the relabelling itself is still unwired. The point is that the next person to read it will not re-derive the wrong answer and shelve the feature again

## [1.0.38] — 2026-08-08

### Changed

- **Dependencies now travel with the code a rebuild ships.** A repo line without a pinned commit is aggregated at the tip of its branch, so every rebuild pulled whatever upstream had published since — while the pip list stayed frozen at whatever the repo's `requirements.txt` said the day the line was created (it was read on create, and never again). An OCA repo adding a module that needs a new library, or bumping the floor of one it already used, produced a build with the new code and without the library it imports. Deploy and rebuild now re-read the `requirements.txt` of every unpinned repo just before writing `pip.txt`, in the same job that pulls the code. Pinned repos are skipped — frozen code, frozen dependencies — and a failed fetch falls back to the stored list, because a blip at GitHub must not stop the fleet
- **Merging those requirements knows who wrote each line.** Re-reading upstream on every rebuild would have turned each routine version bump into a conflict marker and a blocking alert per instance, for a decision nobody actually has to make. Each package now records the repo that authored its spec (`pip_dependency_sources`, on projects and instances): a repo changing a line it wrote is applied and reported in the job log, while a spec that contradicts the operator — or another repo — still becomes the usual conflict marker, files a `pip_conflict` alert and stops the job until a human picks a side. Absence of an entry means the operator owns the line, so existing records need no migration: ownership is claimed lazily the first time a repo declares a spec that already matches. Editing the field by hand takes those lines back, resolving a conflict in the repo's favour hands the line to it, and nothing upstream removes is ever deleted — the job log just notes the package is gone from the manifest. `incubacloud.requirements_resync_enabled=0` restores the old frozen behaviour

## [1.0.37] — 2026-08-08

### Fixed

- **Ansible-backed jobs no longer die with an empty log on images that lack ansible-runner.** A tenant ordering an on-demand VPS watched `host_hardening` fail with a blank log page: since the Phase 3 migration to Ansible, every host-state executor (hardening, probe, prune, delete, whitelist, observability) needs `ansible-core`/`ansible-runner` at runtime, but this repo's `requirements.txt` — the file whose contents deployments merge into their pip dependencies — never declared them, so any image built from it ran without them. The executor's early guard then raised before the first log line, the only failure path in the whole pipeline that leaves no trace in the job log (the reason surfaced only in `queue_job.exc_message` and the alert excerpt). Both halves are fixed: `requirements.txt` now pins `ansible-core==2.21.2` and `ansible-runner==2.4.3` (matching the panel image), and the two early guards — missing ansible-runner, undefined playbook — write their reason into the job log before raising, so a job that cannot even start still says why on its own log page

## [1.0.36] — 2026-08-07

### Fixed

- **Standalone dialogs now follow the light theme.** The shared modal chrome (`.ic-modal`, `.ic-frow`, `.ic-btn`, `.ic-alert`) painted itself with literal dark hex values, a leftover from when the `--rl-*` tokens did not resolve outside `.ic-app` — relay.scss has since defined them on `:root` for exactly that case, and both files ship in the same /cloud bundle. Under `data-ic-theme="light"` every dialog built on that chrome (nine across core, saas manager and tenant — the tenant's "Order new VPS" among them) rendered as a hybrid: dark panel and header, paper-white theme-following inputs inside it, and a Cancel button whose ink-coloured label sat invisible on the dark footer. The chrome now uses the tokens, so it matches the active theme at any mount point; the overlay scrim and drop shadows stay literal darks in both themes, same convention as the slide-over scrim
- **The light theme now reaches every corner of the SPA.** A sweep of all stylesheets found the same disease the modal chrome had, in more places: the import-dialog styled with the pre-redesign dark palette in literal hex; the instance and host detail screens building elevation from white-alpha glazes (invisible over paper) and two custom properties that no longer exist anywhere (`--text-muted`, `--text-primary` — their dark fallbacks always won); toasts and code/log panels frozen dark by literal SCSS constants; Tailwind-era accent colors (`#ef4444`, `#34d399`, `#F87171`…) instead of the Relay state tokens; and light-pastel status pills and banners doing the reverse damage to the dark theme. 118 literals across 20 files now resolve through `--rl-*` tokens — toast and code-panel tints via `color-mix()` over the themed surface so they stay opaque — and the "info" hue, which existed as five slightly different literal cyans, is now a proper token family (`--rl-cyan` + dim/soft/fg/text, with a light variant) alongside a new `--rl-red-fg`. One real dark-theme bug fell out of the sweep: primary buttons in the saas screens used white-on-green at 1.9:1 contrast; they now use `--rl-green-fg` like every other green control
- **SearchSelect placeholders can finally be translated.** Every dropdown passed its placeholder as a quoted OWL expression (`placeholder="'Type to search hosts…'"`), a form the i18n extractor never sees — the strings were absent from the .pot, so no .po could ever cover them and Spanish tenants met English placeholders in an otherwise translated dialog. All 34 such props across core, saas manager and tenant now use the `.translate` attribute suffix (the standard the web client's own dialogs use), the component's built-in "Search…" / "— select —" defaults go through `_t()`, and two plain inputs in the header that had borrowed the quoted-expression syntax — rendering the quotes into the visible placeholder — lost their stray quotes. The regenerated .pot picked up 19 new terms, all translated in `es.po`

## [1.0.35] — 2026-08-07

### Fixed

- **The nightly Docker prune no longer wipes containers that are stopped on purpose.** `docker system prune -af` removes every stopped container, and the exclusion filter meant to spare them (`--filter "label!=incubacloud.protect=1"`) lived in a dependent module as a patch of `get_commands` — a method this executor stopped calling the day it moved to Ansible. Nothing failed, so the loss went unnoticed until a prune landed between a warm instance's rebuild and that night's backup and took the instance's containers with it, failing the backup for the whole host. The filter now belongs to the playbook itself, fed the label from the executor so Python and playbook cannot drift apart, and a structural test pins that the prune command carries it

## [1.0.34] — 2026-08-07

### Fixed

- **Every confirmation dialog outside the instance page works again.** Extracting the shared `confirmVia` helper rewired all five components to call it but added the import to only one of them, so the host, project, settings and backup-backend panels each reached for an undefined global: ten confirmations — "Delete Host" and the SaaS "Open Host Shell" among them — died with `ReferenceError: confirmVia is not defined` the moment the button was pressed. Nothing caught it because the bundle parses fine and the reference is only resolved on the click
- A structural test now refuses any SPA file that calls a `utils/` export it never imported. This is the check a JavaScript `no-undef` would make, done in the Python suite so the pipeline needs no linter to catch the next omitted import

## [1.0.33] — 2026-08-06

### Fixed

- **A new template question no longer writes a placeholder secret into `backup.env`.** The doodba template v9.7.0 adds `backup_backend_password`, whose default is the literal string `example-backup-backend-password`; since deploys run copier with `--defaults`, any question we leave unanswered takes its default. The answers file now sets it empty, which keeps the line out of the rendered file entirely. Only password-capable duplicity backends read it — ours authenticate with AWS keys — so nothing was broken, but a fake credential in a live env file is not something to ship

- **The host list no longer flickers while a job runs.** Every background refresh replaced the rendered fleet with the loading skeleton for about 120 ms, so a running job — which notifies watchers twice a second — turned the page into a strobe. The skeleton now belongs to the first paint and to the Retry button; bus events and tab refocus take a silent path that leaves the cards in place. A failed background refresh no longer replaces good data with the error stub either

### Changed

- **Executors notify on output, not on the clock.** The run loop published a bus event every half-second for the whole life of a job, whether or not anything had been written. A job sitting in a silent multi-minute `docker compose build` therefore cost every open panel a full refetch twice a second for nothing. The tick now publishes only when it actually persisted log rows; the final drain still publishes unconditionally so watchers settle on the last state, and the standalone log page keeps its own fallback poll
- **Job events reach only the users allowed to read the job**, resolved through the same visibility helper the email and external notifiers use. A stakeholder no longer receives traffic for the projects they are not a member of
- **Job events now name their target** (`host_id`, `instance_id`, `project_id`), so a panel can ignore an event that does not concern it without asking the server what the event was about. The instance page used to spend one `load_jobs` call per event — for every job in the fleet, not just its own — purely to learn that. Naming the target is safe because the audience is ACL-filtered first; the host page and the tenant page regain filters that had been dropped for costing an extra round-trip
- Refreshed collections are merged by id instead of replaced wholesale, so a refresh only re-renders the rows that changed. This also fixes an active host search being cleared by a background refresh whenever every host happened to match the query

## [1.0.32] — 2026-08-06

### Added

- Revoking a host's trusted SSH key on an endpoint change now raises a critical alert (`host_key_revoked`) through every notification channel, and re-running "Trust SSH Key" resolves it. The revocation used to leave only an audit row, so a host could sit unreachable — excluded from every cron, refusing to build SSH connections — with nothing telling the operator to re-verify the key
- **Machine-identity check on re-trust.** A revocation now records the fingerprint of the key it dropped, and the next capture is compared against it: an identical fingerprint proves the machine did not change and the endpoint edit was administrative, while a different one raises a critical `host_key_changed` alert naming both fingerprints. The verdict reaches the operator who clicked (toast), the panel (alert) and the audit log. This is the verification actually available to us — our provider exposes no console output or metadata carrying host keys — so the alert text no longer asks for an out-of-band check the platform cannot support. Unlike the revocation alert, a change alert is not auto-resolved: it records a past event and waits for a human to dismiss it
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
- Host pools-aware placement hook and cross-host instance move
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
