# Installing and updating the panel

The IncubaCloud panel is a set of Odoo 19.0 modules with OCA's `queue_job`.
This guide installs it on a server of your own with Docker Compose, explains
the Odoo settings it depends on, gives the configuration we run in production
and why, and shows how to update it without losing the work in progress.

Using the panel once it runs — hosts, projects, instances, backups, agents — is
covered in the [user documentation](https://docs.incubacloud.io/). A panel that
only your office network reaches has a page of its own there:
[Panel on an internal network](https://docs.incubacloud.io/hosts/internal-network/).

- [What the panel needs](#what-the-panel-needs)
- [Install with Docker Compose](#install-with-docker-compose)
- [The Odoo settings that matter](#the-odoo-settings-that-matter)
- [Recommended configuration](#recommended-configuration)
- [Update the panel](#update-the-panel)

## What the panel needs

| Piece | Why |
|---|---|
| Odoo 19.0 (Community is enough) | The panel is Odoo modules. |
| OCA [`queue_job`](https://github.com/OCA/queue), branch 19.0 ([`oca_dependencies.txt`](oca_dependencies.txt)) | Runs every job: deploys, backups, restores, host setup. |
| The Python packages of [`requirements.txt`](requirements.txt) | `asyncssh` (SSH to the hosts), `ansible-core` and `ansible-runner` (host playbooks), `boto3` (backups), `cryptography` and `pyOpenSSL` (stored secrets), `bcrypt`, `PyYAML`, `joserfc` (agents). |
| The Python packages of OCA/queue's `requirements.txt` | `openupgradelib` and `requests`, which `queue_job` declares in its manifest: without them it does not install. |
| The Ansible collections of [`incubacloud/ansible/requirements.yml`](incubacloud/ansible/requirements.yml) | `ansible.posix` and `community.general`, used by the host playbooks. |
| OpenSSH's client, and `sshpass` | Ansible connects with `ssh`; `sshpass` only matters for a host registered with a password instead of a key. |
| A UTF-8 locale | Ansible refuses to start without one: `ERROR: Ansible could not initialize the preferred locale: unsupported locale setting`. |
| `INCUBACLOUD_SECRET_KEY` in the environment | The key every stored secret is encrypted with: SSH keys, passwords, tokens, backup passphrases. |

The modules of this repository:

| Module | Install it on the panel? | Adds |
|---|---|---|
| `incubacloud` | Yes | The panel: hosts, projects, instances, jobs, GitHub, backups, terminals. |
| `incubacloud_mcp` | Recommended | The MCP server for AI agents. |
| `incubacloud_pitr` | Optional | Restore to any minute (continuous recovery). |
| `incubacloud_mcp_pitr` | With both above | Continuous recovery for agents. |
| `incubacloud_assistant` | Optional | The assistant inside the panel, with your own AI key (needs `incubacloud_mcp`). |
| `auth_oauth_github` | Optional | Sign-in with GitHub. |
| `incubacloud_connect` | No | The panel installs it on the instances it deploys (sign-in from the panel). |

## Install with Docker Compose

A reference setup: the panel, PostgreSQL, and a small nginx in front. Every
file below is meant to be copied as it is.

**Prerequisites:** a Linux server with Docker and the Compose plugin, `git`, and
network access from that server to your hosts over SSH and to GitHub over HTTPS.

### 1. Get the code

```bash
mkdir incubacloud-panel && cd incubacloud-panel
git clone --branch 19.0 https://github.com/incubacloud/incubacloud.git addons/incubacloud
git clone --branch 19.0 https://github.com/OCA/queue.git addons/queue
# The queue_job release the panel's CI and production run (19.0.2.1.6):
git -C addons/queue checkout da2664795eb525698f93dfdd63f6ba5c4c9fccba
```

### 2. The image

Save as `Dockerfile`. It starts from the official Odoo 19.0 image and adds what
the table above lists.

```dockerfile
FROM odoo:19.0

USER root

# Ansible refuses to start without a UTF-8 locale. The base image sets
# LANG=en_US.UTF-8 without generating it; C.UTF-8 is always there.
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

# OpenSSH's client: Ansible reaches your hosts with it.
# sshpass: only for hosts registered with a password instead of a key.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client sshpass \
    && rm -rf /var/lib/apt/lists/*

# The panel's Python packages, and OCA/queue's (openupgradelib, requests).
COPY addons/incubacloud/requirements.txt /tmp/requirements-incubacloud.txt
COPY addons/queue/requirements.txt /tmp/requirements-queue.txt
RUN pip3 install --no-cache-dir --break-system-packages --ignore-installed \
        -r /tmp/requirements-incubacloud.txt -r /tmp/requirements-queue.txt

# The Ansible collections the host playbooks use, pinned by the module.
COPY addons/incubacloud/incubacloud/ansible/requirements.yml /tmp/ansible-collections.yml
RUN ansible-galaxy collection install \
        --requirements-file /tmp/ansible-collections.yml \
        --collections-path /usr/share/ansible/collections

USER odoo
```

`--ignore-installed` is there because the base image ships some of these
packages from Ubuntu, which `pip` cannot uninstall (*Cannot uninstall PyYAML …,
RECORD file not found*). With it, the versions the panel pins — `cryptography`
and `pyOpenSSL`, which must move together, among them — are installed beside
Ubuntu's and take precedence.

### 3. Odoo's configuration

Save as `odoo.conf`. [The Odoo settings that matter](#the-odoo-settings-that-matter)
explains each value.

```ini
[options]
addons_path = /mnt/extra-addons/incubacloud,/mnt/extra-addons/queue
data_dir = /var/lib/odoo
db_host = db
db_port = 5432
db_user = odoo
db_name = incubacloud
dbfilter = ^incubacloud$
list_db = False
proxy_mode = True

server_wide_modules = base,rpc,web,queue_job
workers = 5
max_cron_threads = 1
gevent_port = 8072

limit_time_real = 3600
limit_time_cpu = 600
limit_time_real_cron = 0
limit_memory_soft = 1610612736
limit_memory_hard = 2147483648

[queue_job]
channels = root:3,root.user:2,root.bg:1
```

With `incubacloud_assistant`, add `root.assistant:1` (or more) to `channels`.

### 4. The proxy

With workers, Odoo serves its live channel (`/websocket`) on a second port,
8072, and the panel's live updates need it. Save as `nginx.conf`:

```nginx
server {
    listen 80;
    client_max_body_size 64m;
    proxy_read_timeout 720s;

    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Host $http_host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;

    location /websocket {
        proxy_pass http://odoo:8072;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    location / {
        proxy_pass http://odoo:8069;
    }
}
```

`client_max_body_size` leaves room for the 32 MB pieces a backup is uploaded in
when it is restored through the browser.

### 5. The compose file

Save as `compose.yaml`. Only the proxy publishes a port: 8090 on every address
of the server.

```yaml
services:
  db:
    image: postgres:18
    shm_size: 1gb
    environment:
      POSTGRES_USER: odoo
      POSTGRES_PASSWORD: ${DB_PASSWORD:?set DB_PASSWORD in .env}
      POSTGRES_DB: postgres
    volumes:
      - db-data:/var/lib/postgresql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U odoo"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  odoo:
    build: .
    depends_on:
      db:
        condition: service_healthy
    environment:
      PASSWORD: ${DB_PASSWORD}
      INCUBACLOUD_SECRET_KEY: ${INCUBACLOUD_SECRET_KEY:?set INCUBACLOUD_SECRET_KEY in .env}
    volumes:
      - ./odoo.conf:/etc/odoo/odoo.conf:ro
      - ./addons:/mnt/extra-addons:ro
      - odoo-data:/var/lib/odoo
    restart: unless-stopped

  proxy:
    image: nginx:1.27-alpine
    depends_on:
      - odoo
    ports:
      - "8090:80"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
    restart: unless-stopped

volumes:
  db-data:
  odoo-data:
```

`shm_size`: Docker gives a container 64 MB of shared memory, and PostgreSQL's
parallel queries fail past it (*could not resize shared memory segment*).

### 6. The secrets

Create `.env` next to `compose.yaml`, with a database password and the
encryption key:

```bash
{
  echo "DB_PASSWORD=$(docker run --rm --entrypoint openssl odoo:19.0 rand -hex 24)"
  echo "INCUBACLOUD_SECRET_KEY=$(docker run --rm --entrypoint python3 odoo:19.0 \
      -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
} > .env
chmod 600 .env
```

> **Keep a copy of `INCUBACLOUD_SECRET_KEY` somewhere else.** Every secret the
> panel stores is encrypted with it: the hosts' SSH keys and passwords, GitHub
> tokens, the backup destinations' keys and passphrases. A copy of the panel's
> database without this key opens none of them. To change it later, follow
> [RB-01](docs/runbooks/RB-01-rotate-secret-key.md).

### 7. Build, create the database, start

```bash
docker compose build
docker compose up -d db
docker compose run --rm odoo odoo -d incubacloud -i incubacloud,incubacloud_mcp --stop-after-init
docker compose up -d
```

Open `http://<server address>:8090/web/login` and sign in as `admin` with the
password `admin`, which a database created this way starts with. Change it at
once (Preferences → Security), then open `/cloud`. The `admin` user is an
IncubaCloud Administrator from the start.

Install the optional modules the same way, with `-i <module>` and
`--stop-after-init`, then `docker compose up -d`.

### Verify it worked

- `docker compose logs odoo` shows `queue job runner ready for db incubacloud`,
  and no line saying `INCUBACLOUD_SECRET_KEY is missing or invalid` or naming a
  `limit_` setting.
- `/cloud` opens on the empty projects list.

Next: [connect a host](https://docs.incubacloud.io/hosts/connect/).

## The Odoo settings that matter

Every job — a deploy, a backup, a restore, a host's setup — runs inside an Odoo
request on the server that runs `queue_job`'s job runner, for as long as its SSH
commands run. One command may last up to an hour. That is what most of these
settings are about.

| Setting | Value | Why |
|---|---|---|
| `server_wide_modules` | `base,rpc,web,queue_job` | `queue_job` here is what starts the job runner. The setting replaces Odoo's default list instead of adding to it: Odoo 19's default is `base,rpc,web`, kept. |
| `workers` | More than the `root` channel's capacity | A running job holds one worker for its whole run. With `root:3`, three jobs at once hold three workers; `workers = 5` leaves two for the people using the panel. Equal numbers freeze the panel while jobs run. |
| `limit_time_real` | `3600` | Odoo's default, 120 seconds, kills any job longer than two minutes. The job is retried from the start, and after its third death it fails for good (`JobFoundDead`). The limit counts the whole job: 3600 lets its longest command finish, and a job whose commands add up to more is cut all the same. Production's runner runs with 3600. |
| `limit_memory_soft`, `limit_memory_hard` | 1.5 GiB and 2 GiB per worker | Odoo recycles a worker past the soft limit once its request ends, and refuses its allocations past the hard one. The hard limit caps the address space of the worker and of what it starts, wkhtmltopdf among them: below 1.5 GiB a PDF report fails, so the hard limit never goes lower. |
| `limit_time_real_cron` | `0` | No time limit for the panel's scheduled checks, which visit every host in one run ([RB-18](docs/runbooks/RB-18-tune-host-edge-protection.md)). |
| `proxy_mode` | `True` | The panel sits behind a proxy: Odoo then believes its `X-Forwarded-*` headers for the scheme, the host name and the visitor's address. |
| `dbfilter`, `db_name` | One database | The agents' endpoint (`/mcp`) answers 404 when Odoo cannot tell which database a request is for. |
| `[queue_job] channels` | `root:3,root.user:2,root.bg:1` | `root` is how many jobs run at once. The panel sends what people ask for (deploys, rebuilds, backups) to `root.user`, production first, and its own background work (health checks, metrics, Docker clean-up) to `root.bg`, so background jobs never hold up a deploy. |

### With `workers = 0`

Odoo then runs as one process with threads, and the limits change meaning:

- **`limit_time_real`** still applies to each request, but past it Odoo reloads
  the **whole server**, and every job running in it dies with it. With the
  default 120 seconds, every deploy longer than two minutes takes the panel
  down and is retried until it fails `JobFoundDead`.
- **`limit_memory_soft`** and **`limit_memory_hard`** apply to the whole process
  instead of a worker. Measured as virtual size, a threaded server running jobs
  can pass Odoo's per-worker defaults with the load: past the soft one it
  reloads itself, with every job running in it, and past the hard one its
  allocations fail.

Set `limit_time_real = 3600` and both memory limits to `0` (or to what the whole
server may use), or run with workers.

## Recommended configuration

What we run in production, and why. A small panel works with the reference
setup above; these are the choices that held up as the panel grew.

**A dedicated job runner.** Two Odoo services from the same image and the same
`odoo.conf`, which then holds the web service's values:

```ini
workers = 6
limit_time_real = 1200

[queue_job]
channels = root:8,root.user:5,root.bg:2
```

- **The web service**, for people: started with `--load=web
  --max-cron-threads=0`, so it runs neither the job runner nor the scheduled
  actions. `workers = 6`: a PDF rendered during a request holds a worker, and
  wkhtmltopdf then fetches the report's stylesheets and fonts from the same
  server in parallel; with 4, a single invoice left no worker free. It keeps
  the `limit_time_real = 1200` of `odoo.conf`.
- **The runner**, for jobs and scheduled actions: `--workers=9
  --max-cron-threads=1 --limit-time-real=3600`. Its command line raises
  `limit_time_real` to 3600 for the jobs, and its `--workers` overrides the
  file's. It loads `queue_job` from the file's `server_wide_modules`, and
  publishes no port. Its channels follow the rule `root ≤ workers − 1`, as
  each running job holds one of its workers.

With the split, a burst of jobs never slows the panel down for people, and a
restart of the runner leaves the panel's pages up. A compose service for the
runner, beside the `odoo` service above (whose command then becomes
`odoo --load=web --max-cron-threads=0`):

```yaml
  odoo_runner:
    build: .
    depends_on:
      db:
        condition: service_healthy
    environment:
      PASSWORD: ${DB_PASSWORD}
      INCUBACLOUD_SECRET_KEY: ${INCUBACLOUD_SECRET_KEY}
    volumes:
      - ./odoo.conf:/etc/odoo/odoo.conf:ro
      - ./addons:/mnt/extra-addons:ro
      - odoo-data:/var/lib/odoo
    command: ["odoo", "--workers=9", "--max-cron-threads=1", "--limit-time-real=3600"]
    restart: unless-stopped
```

With the assistant, add `root.assistant:3` to the channels. Never give a panel
without a runner these channels: jobs would take every web worker.

| Setting | Our value | Why |
|---|---|---|
| `limit_memory_soft` / `limit_memory_hard` | 1.5 GiB / 2 GiB per worker | As above; the container's memory limit is the real ceiling. |
| `limit_time_cpu` | 600 | Odoo's default is 60 seconds of CPU per request. |
| `limit_time_real_cron` | 0 | The scheduled checks visit every host in one run. |
| `proxy_mode` | `True` | Behind Traefik. A panel behind a CDN also needs the visitor's address corrected: [RB-25](docs/runbooks/RB-25-visitor-address-behind-a-cdn.md). |
| PostgreSQL | 18, `shm_size` 4 GB | The newest major the panel offers for Odoo 19.0, and the one its CI tests on. |

**PostgreSQL for the instances.** The panel offers, per Odoo series, the
PostgreSQL range Odoo and doodba's template recommend for it, and picks a
default inside it: 14 to 18 for 16.0 to 19.0 (default 17), 16 to 18 for 20.0.
The table is in
[`incubacloud/models/_odoo_versions.py`](incubacloud/models/_odoo_versions.py),
with the reason for any departure written beside its series.

**Backups of the panel itself.** The instances' backups go to the destinations
you register in the panel. The panel's own database and data directory need
theirs too:

- a nightly encrypted copy of the database and the data directory to
  S3-compatible storage, a full copy every week, and one month kept. We use
  [docker-duplicity-postgres-s3](https://github.com/Tecnativa/docker-duplicity)
  with the same retention syntax the panel uses for instances (`remove-older-than 1M`);
- `incubacloud_pitr` for any minute of the recent past, which is why one month
  of nightly copies is enough;
- an outside check that alarms when a backup does not run, and mail only when
  one fails;
- `INCUBACLOUD_SECRET_KEY` kept apart from those copies: without it the
  restored panel opens none of its secrets. Restoring the panel:
  [RB-02](docs/runbooks/RB-02-restore-master-db.md).

**`queue_job` pinned.** The panel runs the `queue_job` release its CI tests
(the commit in [Get the code](#1-get-the-code)); moving it is a release of its
own, tested like one.

## Update the panel

An update is new code for the modules, sometimes new Python packages, and a
module update that runs the migrations. Do it when no job is running.

**1. See that nothing is running.** **Job History** (in the panel's header)
shows what runs; or ask the database:

```bash
docker compose exec db psql -U odoo -d incubacloud -c \
  "SELECT state, count(*) FROM queue_job WHERE state IN ('enqueued', 'started') GROUP BY state;"
```

No rows means nothing is running. Jobs that are only queued are safe: they are
rows in the database and start after the restart.

**2. Back up the panel:** its database, its data directory, and `.env`.

```bash
docker compose exec -T db pg_dump -U odoo --format=custom incubacloud > panel-$(date +%F).dump
docker compose run --rm --no-deps -T --entrypoint tar odoo czf - -C /var/lib/odoo . > panel-data-$(date +%F).tgz
```

**3. Fetch the code, rebuild, stop the panel, run the migrations, restart:**

```bash
git -C addons/incubacloud pull
docker compose build
docker compose stop odoo        # and odoo_runner, with a dedicated runner
docker compose run --rm odoo odoo -d incubacloud -u incubacloud,incubacloud_mcp --stop-after-init
docker compose up -d
```

The panel is stopped while the migrations run, as production does it: a server
still running the old code would go on serving pages and running jobs against
a database that changes under it. List in `-u` every IncubaCloud module you
installed. [CHANGELOG.md](CHANGELOG.md) says what each release changes.

### What happens to a job running during the restart

The restart ends the worker that runs it, and with it the job's SSH session.

1. `queue_job` sees the job's worker gone and puts the job back in the queue:
   once the panel is back, **the job runs again from the start**. A job whose
   worker dies a third time fails for good (`JobFoundDead`).
2. Every 5 minutes the panel closes the jobs a lost worker left behind — still
   shown running while `queue_job` has failed or removed them — and undoes what
   they leave: a deploy that never finished puts its instance back to draft
   with an error, for instance, and the job's log says what happened.
3. Jobs waiting in an instance's or a host's queue keep their place; a sweep
   every 2 minutes starts any queue left waiting with nothing running.

Running a step twice is not always harmless — a restore half way through starts
over — which is why step 1 comes first. How jobs are queued, run and recovered:
[architecture](docs/architecture.md#the-job-queue).
