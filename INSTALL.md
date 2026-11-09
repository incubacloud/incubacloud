# Installing and updating the panel

The IncubaCloud panel is a set of Odoo 18.0 modules. It runs inside an Odoo
18.0 server you operate, new or one you already run; Community is enough. This
guide assumes you administer Odoo. If you need to install Odoo first, follow
Odoo's own [installation guide](https://www.odoo.com/documentation/18.0/administration/on_premise.html)
and come back here.

What follows is only what the panel adds to a working Odoo: its dependencies,
the server settings its jobs need, what the proxy in front must pass, and how to
update it without losing the work in progress.

Using the panel once it runs (hosts, projects, instances, backups, agents) is
covered in the [user documentation](https://docs.incubacloud.io/). A panel that
only your office network reaches has a page of its own there:
[Panel on an internal network](https://docs.incubacloud.io/hosts/internal-network/).

- [What the panel needs](#what-the-panel-needs)
- [Install the modules](#install-the-modules)
- [The Odoo settings that matter](#the-odoo-settings-that-matter)
- [The proxy](#the-proxy)
- [Recommended configuration](#recommended-configuration)
- [Update the panel](#update-the-panel)

## What the panel needs

| Piece | Why |
|---|---|
| Odoo 18.0 | The panel is Odoo modules. |
| OCA [`queue_job`](https://github.com/OCA/queue), release 18.0.4.1.2 (commit `4ccc50b153fdfd4ea48292978070043cda2e9aee` of branch 18.0, [`oca_dependencies.txt`](oca_dependencies.txt)) | Runs every job: deploys, backups, restores, host setup. The panel is tested against this commit; moving it is a release of its own. |
| The Python packages of [`requirements.txt`](requirements.txt) | `asyncssh` (SSH to the hosts), `ansible-core` and `ansible-runner` (host playbooks), `boto3` (backups), `cryptography` and `pyOpenSSL` (stored secrets; they must move together), `bcrypt`, `PyYAML`, `joserfc` (agents), `urllib3` and `requests` (current releases: the ones Odoo 18 pins on Python 3.10 break with this `cryptography`). |
| The Python packages of OCA/queue's `requirements.txt` | `requests`, which `queue_job` declares in its manifest: without it `queue_job` does not install. |
| The Ansible collections of [`incubacloud/ansible/requirements.yml`](incubacloud/ansible/requirements.yml) | `ansible.posix` and `community.general`, used by the host playbooks. |
| OpenSSH's client (`ssh`), and `sshpass` | Ansible connects with `ssh`. `sshpass` only matters for a host registered with a password instead of a key. |
| A UTF-8 locale for the Odoo process | Ansible refuses to start without one (`Ansible could not initialize the preferred locale`). |
| `INCUBACLOUD_SECRET_KEY` in Odoo's environment | The key every stored secret is encrypted with: see [The encryption key](#the-encryption-key). |

### The modules

| Module | Install it on the panel? | Adds |
|---|---|---|
| `incubacloud` | Yes | The panel: hosts, projects, instances, jobs, GitHub, backups, terminals. |
| `incubacloud_mcp` | Recommended | The MCP server for AI agents. |
| `incubacloud_pitr` | Optional | Restore to any minute (continuous recovery). |
| `incubacloud_mcp_pitr` | With both above | Continuous recovery for agents. |
| `incubacloud_assistant` | Optional | The assistant inside the panel, with your own AI key (needs `incubacloud_mcp`). |
| `auth_oauth_github` | Optional | Sign-in with GitHub. |
| `incubacloud_connect` | No | The panel installs it on the instances it deploys (sign-in from the panel). |

### Network

- **Out:** SSH to your hosts, and HTTPS to GitHub, to your backup storage and,
  with the assistant, to your AI provider.
- **In:** people reach the panel through your proxy. GitHub's push
  notifications (`/cloud/github/webhook`) and AI agents that call from their
  provider's servers (`/mcp`) need it reachable from the internet over HTTPS;
  everything else works on an internal network.

### Resources

For a panel and its PostgreSQL on one server: **2 vCPU and 4 GB of RAM**, and a
few GB of free disk in Odoo's data directory beyond what your Odoo already uses.

- **Memory.** Each Odoo process with the panel loaded uses about 150 to 250 MB
  (measured on a running panel), in line with Odoo's own estimate for a light
  worker. The configuration below runs about nine of them (five workers, the
  cron worker, the job runner, the live-channel process and the main process),
  close to 2 GB. A job on a host also starts an Ansible process beside its
  worker, which we have not measured, and PostgreSQL for the panel's database
  ran in under 1 GB on the same panel. 4 GB leaves room for both; in 2 GB,
  Odoo and PostgreSQL alone fill the server.
- **CPU.** Jobs mostly wait on SSH: a panel running about 2,400 jobs in eight
  hours used under 2% of one core on average. The 2 vCPU are for module
  updates and bursts, and they fit Odoo's own rule of thumb of
  `2 × CPU + 1` workers for the five workers below.
- **Disk.** A backup restored through the browser is staged in Odoo's data
  directory until the job sends it to the host: up to 2 GiB per upload by
  default.

## Install the modules

1. **Get the code** and add both directories to `addons_path`:

   ```bash
   git clone --branch 18.0 https://github.com/incubacloud/incubacloud.git
   git clone --branch 18.0 https://github.com/OCA/queue.git
   git -C queue checkout 4ccc50b153fdfd4ea48292978070043cda2e9aee
   ```

2. **Install the dependencies** in the environment Odoo runs in:

   ```bash
   pip install -r incubacloud/requirements.txt -r queue/requirements.txt
   ansible-galaxy collection install \
       -r incubacloud/incubacloud/ansible/requirements.yml \
       -p /usr/share/ansible/collections
   apt-get install --no-install-recommends openssh-client sshpass
   ```

   Install the collections where the Ansible of the user running Odoo looks for
   them: `/usr/share/ansible/collections` or that user's
   `~/.ansible/collections`. On the official `odoo:18.0` image, add
   `--break-system-packages --ignore-installed` to `pip`: the image ships some
   of these packages from Ubuntu, which `pip` cannot uninstall, and the pinned
   `cryptography` and `pyOpenSSL` must take precedence over Ubuntu's.

3. **Set the encryption key** in Odoo's environment ([below](#the-encryption-key)).

4. **Apply [the Odoo settings](#the-odoo-settings-that-matter)** and restart Odoo.

5. **Install the modules:**

   ```bash
   odoo -c <your odoo.conf> -d <panel database> -i incubacloud,incubacloud_mcp --stop-after-init
   ```

   Then start Odoo as usual and open `/cloud`. The `admin` user is an
   IncubaCloud Administrator from the start. Add the optional modules the same
   way.

### The encryption key

`INCUBACLOUD_SECRET_KEY` holds a Fernet key. Generate one with the
`cryptography` you just installed:

```bash
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Give it to every Odoo process that loads the panel, the job runner included.

> **Keep a copy of `INCUBACLOUD_SECRET_KEY` somewhere else.** Every secret the
> panel stores is encrypted with it: the hosts' SSH keys and passwords, GitHub
> tokens, the backup destinations' keys and passphrases. A copy of the panel's
> database without this key opens none of them. To change it later, follow
> [RB-01](docs/runbooks/RB-01-rotate-secret-key.md).

### Verify it worked

- Odoo's log shows `queue job runner ready for db <panel database>`, and no line
  saying `INCUBACLOUD_SECRET_KEY is missing or invalid` or naming a `limit_`
  setting.
- `/cloud` opens on the empty projects list.

Next: [connect a host](https://docs.incubacloud.io/hosts/connect/).

## The Odoo settings that matter

A job (a deploy, a backup, a restore, a host's setup) runs inside an Odoo
request for as long as its SSH commands run, and one command may last up to an
hour. `queue_job`'s runner does not run jobs itself: it calls
`/queue_job/runjob` on its own Odoo server, so each running job holds one of
that server's workers. Most of these settings follow from that.

A setup that works, for an Odoo running people's requests and jobs together:

```ini
[options]
server_wide_modules = base,web,queue_job
workers = 5
max_cron_threads = 1
limit_time_real = 3600
limit_time_cpu = 600
proxy_mode = True
db_name = <panel database>

[queue_job]
channels = root:3,root.user:2,root.bg:1
```

| Setting | Value | Why |
|---|---|---|
| `server_wide_modules` | Odoo's default plus `queue_job` | `queue_job` here is what starts the job runner. The setting replaces Odoo's default list (`base,web`) instead of adding to it. |
| `workers` | More than the `root` channel's capacity | Each running job holds a worker for its whole run. With `root:3`, three jobs at once hold three workers; `workers = 5` leaves two for the people using the panel. Equal numbers freeze the panel while jobs run. |
| `[queue_job] channels` | `root:3,root.user:2,root.bg:1` | `root` is how many jobs run at once; without the setting `queue_job` runs one at a time. The panel sends what people ask for (deploys, rebuilds, backups) to `root.user`, production first, and its own background work (health checks, metrics, clean-up) to `root.bg`, so background jobs never hold up a deploy. With `incubacloud_assistant`, add `root.assistant:1` or more. |
| `limit_time_real` | `3600` | Odoo's default, 120 seconds, kills any job longer than two minutes. The job is retried from the start, and after its third death it fails for good (`JobFoundDead`). The limit counts the whole job: 3600 lets its longest command finish. |
| `limit_time_cpu` | `600` | Odoo's default is 60 seconds of CPU per request, enforced on each worker. Jobs mostly wait on SSH; 600 is a margin for the long ones, not a measured need. |
| `limit_memory_soft`, `limit_memory_hard` | Odoo's defaults, with workers | They are per worker, and a panel worker stays far below them. With `workers = 0` they mean something else: [below](#with-workers--0). |
| `limit_time_real_cron` | Default, or `0` | The panel's scheduled checks visit every host in one run ([RB-18](docs/runbooks/RB-18-tune-host-edge-protection.md)). Its default follows `limit_time_real`, which is enough at 3600; `0` removes the limit. |
| `max_cron_threads` | At least `1` | The panel's scheduled actions start its background work (health checks, metrics), start any job queue left waiting (every 2 minutes) and close the jobs a lost worker left (every 5). |
| `proxy_mode` | `True` | Behind a proxy, Odoo then believes its `X-Forwarded-*` headers for the scheme, the host name and the visitor's address. |
| `db_name` or `dbfilter` | The panel's database, by name | Without a session, Odoo picks a database only when exactly one matches, and the agents' endpoint (`/mcp`) answers 404 otherwise. Filter by name (`db_name`, or `dbfilter = ^<name>$`), not by host name: the job runner calls its own Odoo on `http_interface` and `http_port` (`localhost:8069` by default) with the database in a header, and Odoo checks that name against `dbfilter` with that address as the host, so a filter built on `%h` or `%d` rejects it and no job runs. |

### With `workers = 0`

The panel also runs on Odoo's threaded server: the runner starts as a thread
and jobs run as request threads of the same process. `queue_job` supports it
but calls it unfit for production, and the limits change meaning:

- **`limit_time_real`** still applies to each request, but past it Odoo reloads
  the **whole server**, and every job running in it dies with it. With the
  default 120 seconds, every deploy longer than two minutes takes the panel
  down and is retried until it fails `JobFoundDead`.
- **`limit_memory_soft`** and **`limit_memory_hard`** apply to the whole process
  instead of a worker: past the soft one it reloads itself with every job
  running in it, and past the hard one its allocations fail.
- **`limit_time_real_cron = 0`** falls back to `limit_time_real`.

Set `limit_time_real = 3600` and both memory limits to `0` (or to what the whole
server may use). It suits trying the panel; for daily use, run with workers.

### The check at startup

The panel checks these limits when Odoo starts. A server started with
`odoo server` that runs the job runner (`queue_job` among its
`server_wide_modules`) logs a warning per setting and raises the alert
`job_server_limits_too_low` when `limit_time_real` is below 3600 seconds or,
with `workers = 0`, when a memory limit is at Odoo's per-worker default or
lower. The alert closes at the next start with values that fit. A web-only
server (`--load=web`) beside a dedicated runner is not judged.

## The proxy

The panel needs nothing from the proxy beyond what Odoo itself needs, as
Odoo's [deployment guide](https://www.odoo.com/documentation/18.0/administration/on_premise/deploy.html)
describes it:

- **HTTPS**, with `X-Forwarded-Proto`, `X-Forwarded-Host` (or the original
  `Host`) and `X-Forwarded-For` for `proxy_mode`. Connecting the GitHub App
  refuses a panel served over plain HTTP, and the session cookie is marked
  `Secure` only when the request arrived over HTTPS.
- **`/websocket`**, which carries the panel's live updates (job logs, states).
  With workers, Odoo serves it on `gevent_port` (8072 by default): send that
  path there, with the `Upgrade` and `Connection` headers. With `workers = 0`
  it is served on the main port.
- **Request bodies up to 40 MiB.** A backup restored through the browser is
  sent in pieces of 32 MiB; a proxy with a smaller limit answers 413.

Every other path goes to Odoo as it is. A panel behind a CDN also needs the
visitor's address corrected:
[RB-25](docs/runbooks/RB-25-visitor-address-behind-a-cdn.md).

## Recommended configuration

### A dedicated job runner

A single Odoo, configured as above, runs the panel. With more jobs or more
people, we recommend a second Odoo server for the jobs: the same code, the same
database, the same data directory and the same `INCUBACLOUD_SECRET_KEY`, with
different command-line options.

- **The web server**, for people: `--load=web --max-cron-threads=0`, so it runs
  neither the job runner nor the scheduled actions. It keeps a shorter
  `limit_time_real` (we use 1200).
- **The runner**, for jobs and scheduled actions: it loads `queue_job` from
  `server_wide_modules` and starts with `--limit-time-real=3600` and enough
  workers for its channels (`root` at most `workers − 1`). The proxy does not
  route to it.

What the split gives:

- A burst of jobs never takes a worker from the people using the panel, and
  the web workers do not need the one-hour limit the jobs do.
- Restarting the web server leaves running jobs alone, and restarting the
  runner leaves the pages up. Live job logs still reach the browser: they
  travel through PostgreSQL, whichever server writes them.
- The runner can grow, or move to another machine, without touching the web
  side.

Run only one job runner per database: `queue_job` elects one through a
PostgreSQL lock and the others wait. The web side, on the other hand, stays on
one machine: each in-browser terminal is a process listening on the loopback of
the server that opened it
([RB-06](docs/runbooks/RB-06-multi-worker-checklist.md)).

Never give channels sized for a dedicated runner to a panel without one: jobs
would take every web worker.

### Back up the panel

The instances' backups go to the destinations you register in the panel. The
panel's own database and data directory need theirs too, with
`INCUBACLOUD_SECRET_KEY` kept apart from those copies: without it the restored
panel opens none of its secrets. Restoring the panel:
[RB-02](docs/runbooks/RB-02-restore-master-db.md).

## Update the panel

An update is new code for the modules, sometimes new Python packages, and a
module update that runs the migrations. Do it when no job is running.

**1. See that nothing is running.** **Job History** (in the panel's header)
shows what runs; or ask the database:

```sql
SELECT state, count(*) FROM queue_job WHERE state IN ('enqueued', 'started') GROUP BY state;
```

No rows means nothing is running. Jobs that are only queued are safe: they are
rows in the database and start after the restart.

**2. Back up the panel:** its database, its data directory, and the key.

**3. Fetch the code, install new packages, stop Odoo, run the migrations,
start it again:**

```bash
git -C incubacloud pull
pip install -r incubacloud/requirements.txt
# stop every Odoo server of the panel (the runner too)
odoo -c <your odoo.conf> -d <panel database> -u incubacloud,incubacloud_mcp --stop-after-init
# start them again
```

The panel is stopped while the migrations run: a server still running the old
code would go on serving pages and running jobs against a database that changes
under it. List in `-u` every IncubaCloud module you installed.
[CHANGELOG.md](CHANGELOG.md) says what each release changes.

### What happens to a job running during the restart

The restart ends the worker that runs it, and with it the job's SSH session.

1. `queue_job` sees the job's worker gone and puts the job back in the queue:
   once the panel is back, **the job runs again from the start**. A job whose
   worker dies a third time fails for good (`JobFoundDead`).
2. Every 5 minutes the panel closes the jobs a lost worker left behind (still
   shown running while `queue_job` has failed or removed them) and undoes what
   they leave: a deploy that never finished puts its instance back to draft
   with an error, for instance, and the job's log says what happened.
3. Jobs waiting in an instance's or a host's queue keep their place; a sweep
   every 2 minutes starts any queue left waiting with nothing running.

Running a step twice is not always harmless (a restore half way through starts
over), which is why step 1 comes first. How jobs are queued, run and recovered:
[architecture](docs/architecture.md#the-job-queue).

---

**Rather not run it yourself?** The [hosted platform](https://www.incubacloud.io/?utm_source=github&utm_medium=install)
runs the same panel for you.
