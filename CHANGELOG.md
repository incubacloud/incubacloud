# Changelog

All notable changes to this project will be documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

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
