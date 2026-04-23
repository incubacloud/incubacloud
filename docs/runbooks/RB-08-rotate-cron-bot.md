# RB-08: Rotate the cron bot user

**Severity:** critical
**Typical trigger:** cron bot password / API key believed leaked,
or periodic rotation after personnel change.
**Who runs it:** ops + security.

Every `ir.cron` from the `incubacloud*` modules runs as the cron bot
user (login `__incubacloud_cron__`, created by the post-init hook).
If that user's credentials leak, everything the bot can do — which
is most of the write paths in the control plane — is reachable.

Rotating the bot means: creating a fresh bot user, reassigning every
cron to the new user, disabling the old one. Group membership is
rebuilt from scratch via the existing helpers, so no manual ACL
work is needed.

## Symptoms / triggers

- Alert from secret scanner matching the bot's API key / password.
- Suspected insider threat requiring credential rotation.
- Scheduled rotation (annual).

## Diagnosis

Inventory current cron ownership:

```sql
db$ SELECT c.id, c.cron_name, c.user_id, u.login, c.active
    FROM ir_cron c
    JOIN res_users u ON u.id = c.user_id
    WHERE c.cron_name ILIKE '%incubacloud%'
       OR c.user_id IN (SELECT id FROM res_users
                        WHERE login = '__incubacloud_cron__');
```

You should see exactly one `user_id` across all rows. If you see
`uid=1` (OdooBot), the post-init hook did not run for a module —
note which module and run the hook manually (below).

## Resolution

1. **Disable the old cron bot** first, so new crons don't start
   under it during rotation:

   ```sql
   db$ UPDATE res_users SET active = false
       WHERE login = '__incubacloud_cron__';
   ```

   No running crons are interrupted by this; only future ticks fail
   to acquire the lock. That's the behavior we want for the brief
   rotation window.

2. **Trigger the provisioning hook** from `odoo shell` to create a
   fresh bot (it renames the old login-collision aside if needed)
   and reassign every cron:

   ```python
   env['res.users']._incubacloud_ensure_cron_bot()
   for module in ('incubacloud',
                  'incubacloud_saas_manager',
                  'incubacloud_tenant'):
       env['res.users']._incubacloud_assign_cron_user_id(
           module_name=module,
       )
   ```

   `_incubacloud_ensure_cron_bot` is idempotent: if a bot with the
   known login already exists and is active, it is reused; if
   inactive, it is re-activated and its password reset via
   `password_crypt`.

3. **Verify every cron now points at the new bot**:

   ```sql
   db$ SELECT c.cron_name, u.login
       FROM ir_cron c
       JOIN res_users u ON u.id = c.user_id
       WHERE c.cron_name ILIKE '%incubacloud%';
   ```

   All rows must show `__incubacloud_cron__` with `active = true`.

4. **Smoke-test one cron** to confirm it runs under the new user:

   ```python
   env.ref('incubacloud.cron_terminal_route_gc').method_direct_trigger()
   ```

   Then check `ir_cron.log` for a successful run entry.

5. **Purge the old bot** only if step 2 created a *new* user. Usually
   the same login is reused and the password is reset in-place —
   there is nothing to purge. If a previous rotation left an
   `__incubacloud_cron__old` row behind, drop it:

   ```sql
   db$ DELETE FROM res_users WHERE login = '__incubacloud_cron__old';
   ```

## Rollback

If the new bot can't run crons (likely cause: a new module forgot
to grant its groups to the bot via the provisioning hook), re-
enable the old bot and investigate offline:

```sql
db$ UPDATE res_users SET active = true
    WHERE login = '__incubacloud_cron__';
```

Crons point at the same `user_id` either way (same login), so
re-enabling is sufficient — no cron rewrites needed.

## References

- [`models/res_users_ext.py`](../../incubacloud/models/res_users_ext.py)
  — `_incubacloud_ensure_cron_bot` + `_incubacloud_assign_cron_user_id`.
- [`migrations/1.0.2/post-migrate.py`](../../incubacloud/migrations/1.0.2/post-migrate.py)
  — upgrade-path counterpart of the post-init hook.
- [RB-06](RB-06-multi-worker-checklist.md) — §4 audits cron
  ownership for multi-worker readiness.
