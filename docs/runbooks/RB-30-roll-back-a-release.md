# RB-30 — Roll back a release in place, keeping the data

**Severity:** critical · **When:** a release is deployed, the manager runs
it and has served customers since, and it must be left for the previous
image (`<image>:prod-prev`) without losing what was written after the
deploy. §3 onwards is the way back from release C (core 1.0.211,
`incubacloud_saas_manager` 1.187, `incubacloud_website` 19.0.1.44.0) to
release B (1.0.210, 1.186, 19.0.1.42.3), rehearsed on 4-nov-2026.

## 1. Restore or go back in place

[RB-13](RB-13-rollback-panel-deploy.md) §3 restores the backup that
`deploy-update` step 5 takes right before the window. It is exact, and it
loses everything written after it: the payments Stripe took (they stay
taken at Stripe, with no transaction here), the orders and invoices, the
tenants that signed up (their panels run on a host the restored database
does not know), the jobs that changed a host. It is acceptable only while
none of that exists. Elapsed time does not say it, the database does, with
`<end>` the time step 8 ended (UTC):

```sql
db$ SELECT 'payment', count(*) FROM payment_transaction WHERE create_date > '<end>'
    UNION ALL SELECT 'order', count(*) FROM sale_order WHERE create_date > '<end>'
    UNION ALL SELECT 'invoice', count(*) FROM account_move WHERE create_date > '<end>'
    UNION ALL SELECT 'tenant', count(*) FROM cloud_tenant WHERE create_date > '<end>'
    UNION ALL SELECT 'job not of a cron', count(*) FROM cloud_job
         WHERE create_date > '<end>' AND create_uid IS DISTINCT FROM
               (SELECT id FROM res_users WHERE login = '__incubacloud_cron__');
```

All zero (in practice: a failure seen in the window or in its first
minutes): restore, RB-13 §3. Anything else: go back in place, this runbook.

## 2. Going back in place is a downgrade

`click-odoo-update` with the older image updates every module whose code
changed, to the older code: it drops the columns and records the newer
release added, and runs no migration (Odoo has none downwards). Two things
it cannot do by itself, and the reason `deploy-update` refuses an older
image without `--downgrade` (step 0 with `--no-build`, 3d otherwise):

- **Records still in use.** A record the newer release's data created and
  the older one does not have is deleted at the end of the update. If a
  row points at it through a `RESTRICT` key, the update stops there,
  inside the window, with the modules updated so far committed. Step 4
  runs the same update on a clone first, so it shows up there, before
  anything stops (never `--skip-pretest` here).
- **`noupdate` records the newer release rewrote** in a migration: the
  older update leaves them as they are.

What is in each release's diff decides both: §3 is C to B.

## 3. Release C to B

Rehearsed on a copy of `devel`: updated to C with `click-odoo-update`
(C's image, without `website_sale_require_legal`), a job of a C type
added, then B's image. What does **not** work:

| Way | Result |
|---|---|
| `prod-prev` with no update (RB-13 §2) | B's `incubacloud_website` depends on `website_sale_require_legal`, which C uninstalled: Odoo skips the module («some depends are not loaded»), `/web/login`, `/` and `/pricing` answer 500. C's `scheduling_priority` (`NOT NULL`) on `cloud_instance` and `cloud_plan` makes B's creates fail. Never. |
| `deploy-update` with `prod-prev`, as for a release | B's update reinstalls `website_sale_require_legal` from the image and the site works, but it deletes the job types only C has (`move_quiet_check`, `move_fingerprint`, `move_freeze_source`): with one C move done, `cloud_job_job_type_id_fkey` (`RESTRICT`) stops it halfway. Without one, it ends with the planner's cron calling `_cron_placements()`, which B has not, every 5 minutes. |

What **is lost** going back in place: the columns C added, which B's update
drops. On `cloud_tenant`: `placement_state`, `placement_pinned`,
`move_window_tz` (the zone of the window the customer chose),
`move_postponed_count`, `move_postponed_reason`, `move_requested_now_at`,
`move_last_cut_seconds`. On `cloud_instance`: `scheduling_priority`,
`move_frozen_source_host_id`, `move_frozen_until`, `move_cut_started_at`,
`move_last_cut_seconds`. On `cloud_plan`: `scheduling_priority`. On
`cloud_settings`: `move_idle_minutes`, `move_window_local_hour`,
`move_window_hours`, `move_min_notice_hours`, `move_default_tz`,
`move_max_concurrent`, `move_frozen_source_hours`, `move_cut_alert_seconds`.
C's migrations work the states and priorities out again when C comes back
(§4); the pins, the settings an operator changed and the frozen copies they
do not: step 1 writes them down.

### Step 1 — With C serving: what B will forget

```sql
db$ -- frozen copies of moves done under C: B does not know them
    SELECT i.id, i.name, h.name AS host, i.move_frozen_until
      FROM cloud_instance i JOIN cloud_host h ON h.id = i.move_frozen_source_host_id;
db$ -- pins, and the moves owed or booked with the window chosen
    SELECT id, slug, placement_state, placement_pinned, move_scheduled_at, move_window_tz
      FROM cloud_tenant WHERE placement_state <> 'settled' OR placement_pinned;
db$ SELECT move_idle_minutes, move_window_local_hour, move_window_hours,
           move_min_notice_hours, move_default_tz, move_max_concurrent,
           move_frozen_source_hours, move_cut_alert_seconds
      FROM cloud_settings;
```

Each frozen copy: if its move went well, **Clean frozen copy now** on the
instance's page while C still knows it; otherwise take its dump
([RB-29](RB-29-tenant-placement-and-moves.md) §6) and remove it by hand
once B runs. `deploy-update` refuses to start while a move runs.

### Step 2 — With C serving: keep the job types C's jobs point at

Harmless to C: it only stops an update from rewriting or deleting these
three records.

```sql
db$ UPDATE ir_model_data SET noupdate = true
     WHERE module = 'incubacloud' AND model = 'cloud.job.type'
       AND name IN ('move_quiet_check', 'move_fingerprint', 'move_freeze_source');
```

### Step 3 — The SQL B's update cannot do

In the doodba checkout on the prod host, `rb30-after-update.sql`:

```sql
UPDATE ir_act_server s
   SET code = 'model._cron_owed_moves()'
  FROM ir_cron c JOIN ir_model_data d
    ON d.model = 'ir.cron' AND d.res_id = c.id
   AND d.module = 'incubacloud_saas_manager' AND d.name = 'cron_owed_moves'
 WHERE s.id = c.ir_actions_server_id;
UPDATE ir_cron c
   SET cron_name = 'SaaS: Move tenants to their plan''s pool at night',
       interval_number = 15, interval_type = 'minutes'
  FROM ir_model_data d
 WHERE d.model = 'ir.cron' AND d.res_id = c.id
   AND d.module = 'incubacloud_saas_manager' AND d.name = 'cron_owed_moves';
```

### Step 4 — Deploy B

```bash
prod$ docker image tag <image>:prod <image>:prod-bad     # C, to come back to
prod$ docker image tag <image>:prod-prev <image>:prod
prod$ invoke deploy-update --db prod --no-build --skip-tests --no-publish-core \
          --downgrade --after-update-sql rb30-after-update.sql
```

Step 0 lists the three modules going back; step 4 runs B's update on a
clone (a job type the inventory missed stops it there); 7a runs the file
right after the update. `--no-publish-core` leaves the tenants' panels on
the core release C published: putting them back too is another decision,
not rehearsed.

### Step 5 — Check

```sql
db$ SELECT name, state, latest_version FROM ir_module_module
     WHERE name IN ('incubacloud', 'incubacloud_saas_manager',
                    'incubacloud_website', 'website_sale_require_legal');
    -- 19.0.1.0.210, 19.0.1.186, 19.0.1.42.3, and the OCA module installed
db$ SELECT c.interval_number, s.code FROM ir_cron c
      JOIN ir_act_server s ON s.id = c.ir_actions_server_id
      JOIN ir_model_data d ON d.model = 'ir.cron' AND d.res_id = c.id
     WHERE d.name = 'cron_owed_moves';
    -- 15 | model._cron_owed_moves()
```

`/web/login`, `/` and `/pricing` answer 200 (rehearsal: 200, 200, 200, and
`/shop` 301, `/my/incubacloud` 303 to the login).

## 4. Rollback of this runbook: C again

C over the database B left, rehearsed after the steps above: it updates as
any release (it is newer, no `--downgrade`), uninstalls
`website_sale_require_legal` again, gives every tenant its placement state
and every plan and instance its priority, and the site answers as before.

```bash
prod$ docker image tag <image>:prod-bad <image>:prod
prod$ invoke deploy-update --db prod --no-build --skip-tests
```

Then put back what step 1 wrote down (pins, settings), and give the three
job types back to the data:

```sql
db$ UPDATE ir_model_data SET noupdate = false
     WHERE module = 'incubacloud' AND model = 'cloud.job.type'
       AND name IN ('move_quiet_check', 'move_fingerprint', 'move_freeze_source');
```
