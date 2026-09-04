# RB-20: Restore a copy of production without arming it

**Severity:** planned — but a mistake here reaches production
**Typical trigger:** developing against real data; reproducing a bug that
only happens with the live dataset; rehearsing a migration.
**Who runs it:** anyone restoring a production dump anywhere.

A restored dump is production. It holds the SSH keys that open every
host, the API tokens that create and destroy servers, and the passwords
to customer databases — and its jobs and crons will use them, from a
laptop, against the live fleet. Neutralization is what removes that
capability, and it is not optional.

## The procedure

```
$ docker compose stop odoo odoo_runner
db$ DROP DATABASE devel;  CREATE DATABASE devel OWNER odoo;
$ pg_restore ... | psql -U odoo -d devel          # or click-odoo-copydb
$ docker compose run --rm odoo odoo neutralize -d devel
$ docker compose up -d odoo odoo_runner
```

`odoo neutralize` runs Odoo's own script plus `data/neutralize.sql` from
every installed module. Ours clear SSH credentials and API tokens,
deactivate every host and instance, and drop terminal sessions, DNS
records and reservations. Odoo's own disables crons, mail servers and
webhooks.

**Do not start `odoo_runner` before neutralizing.** The queue in a fresh
dump still holds production's pending jobs, and the runner will execute
them against production hosts.

## Verify it, do not assume it

The whole thing runs as **one transaction**. A single line naming a
column that no longer exists rolls back everything — Odoo's part
included — and the database stays fully armed. The failure is loud in
the log and silent everywhere else:

```
CRITICAL odoo.cli.neutralize: An error occurred during the
neutralization. THE DATABASE IS NOT NEUTRALIZED!
```

That is exactly how a development copy once kept live credentials for
weeks. So check the result, every time:

```
db$ SELECT
      (SELECT value FROM ir_config_parameter
        WHERE key = 'database.is_neutralized')          AS neutralized,
      (SELECT count(*) FROM ir_cron WHERE active)       AS crons,
      (SELECT count(*) FROM cloud_host WHERE active)    AS hosts,
      (SELECT count(*) FROM cloud_host
        WHERE coalesce(key_file, '') <> ''
           OR coalesce(password, '') <> '')             AS creds;
```

Expected: `neutralized = true`, `crons = 1` (Odoo's autovacuum), and
`hosts = 0`, `creds = 0`.

## Updating a module re-arms the crons

A module update reloads its data files, and cron records are data: every
cron the update touches comes back **active**, on a database that was
neutralized minutes earlier. Hosts stay deactivated and the credentials
stay gone, so the blast radius is small — but crons that talk to
external services do not need a host to run.

Re-run the neutralization after any `-u`, or after `invoke install`. It
is idempotent and takes a second:

```
$ docker compose run --rm odoo odoo neutralize -d devel
```

## `web.base.url` is emptied on purpose

Neutralization blanks it so a copy cannot build links back to
production. Anything rendering a mail template then fails on the empty
URL, which makes a test run look broken for a reason that has nothing to
do with the code. Point it at the local panel before running tests:

```
db$ UPDATE ir_config_parameter SET value = 'http://localhost:50069'
     WHERE key = 'web.base.url';
```

## When it fails

The error names the offending table and column. It means a field was
renamed or removed and the script was not updated. Fix the script — do
not comment the statement out and move on, because the same script is
what protects the next copy.

`TestNeutralize` in `incubacloud` catches both halves of this before it
ships: it reads the registry for every encrypted field and requires the
scripts to clear it, and it refuses any script that names a column the
database no longer has.

## Rollback

There is nothing to roll back — neutralization only removes capability.
If a copy was already started un-neutralized, treat it as an incident:
check `cloud_job` for anything that ran against a production host, and
`queue_job` for anything still pending, before neutralizing.

## See also

- [RB-02: restore the master database](RB-02-restore-master-db.md)
- [RB-05: triage a failed job](RB-05-triage-failed-job.md)
