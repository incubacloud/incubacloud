# Memory and resources

**Instance detail → Resources** sets how much memory and CPU each container of
the instance may use: Odoo, the database, the backups and the mail relay. A
container that reaches its memory limit has a process killed by the system, so
the panel also sizes Odoo and PostgreSQL to stay inside those limits.

## What the panel sets for you

When you deploy or rebuild an instance, the panel derives these settings from
its memory limits:

| Setting | From | Value |
| --- | --- | --- |
| Odoo `limit_memory_soft` | Odoo's memory limit | 60 %, between 640 MiB and 2048 MiB |
| Odoo `limit_memory_hard` | Odoo's memory limit | 150 %, between 1536 MiB and 2560 MiB |
| PostgreSQL `shared_buffers` | the database's memory limit | a quarter, at least 16 MB |
| PostgreSQL `work_mem` | the database's memory limit | a sixteenth, at least 4 MB |
| PostgreSQL `effective_cache_size` | the database's memory limit | half, at least 32 MB |

For example, with the defaults (Odoo 2g, database 1g): each Odoo process is
recycled past 1228 MiB and stopped at 2560 MiB, and PostgreSQL runs with
`shared_buffers = 256MB`, `work_mem = 64MB` and `effective_cache_size = 512MB`.
An instance with no memory limit keeps Odoo's and PostgreSQL's own defaults.

**Why.** Odoo's own defaults (2 GiB soft and 2.5 GiB hard per worker) are more
than a whole container usually has, so no worker was ever recycled: each one
grew a little every day, mostly during the nightly jobs, and the instance sat
near its limit until a restart. With `limit_memory_soft`, a worker that grows
past it finishes the request it is serving and is replaced by a fresh one; you
do not see it. The floors exist because Odoo measures the soft limit against a
process's virtual size, which is larger than the memory it really uses, and
because PDF reports run inside the hard limit. The PostgreSQL settings replace a
`work_mem` of 512 MB, which let a single large query take more memory than the
database container has.

## When it applies

On the next **Deploy** or **Rebuild**. Saving a new memory limit changes nothing
on the server until then. The rebuild restarts Odoo, and the database too when
its settings change: a few seconds, after which Odoo reconnects by itself.

## Using your own values

- **Odoo:** write `limit_memory_soft` or `limit_memory_hard`, in bytes, in
  **Instance detail → Config**. If either one is there, the panel sets neither
  and yours apply as written. `0` turns a limit off.
- **PostgreSQL:** run `ALTER SYSTEM SET work_mem = '128MB';` and then
  `SELECT pg_reload_conf();` in the instance's database (`shared_buffers` needs
  a restart of the instance). It is kept in the database's own data and survives
  rebuilds. `ALTER SYSTEM RESET work_mem;` goes back to the panel's value.

## Alerts about memory

- **High memory:** Odoo's container above 95 % of its limit for 30 minutes.
- **Out of memory:** the system killed a process in the Odoo, database or mail
  container for lack of memory. This one is critical: something was stopped
  mid-way. Raise the container's limit in **Resources**, or look for the
  operation that needed that much.

The database has no alert by percentage: most of what it uses is cache, which
is released when needed, so it always reads close to its limit.

See [Monitoring](../reference/monitoring.md) for where alerts arrive.
