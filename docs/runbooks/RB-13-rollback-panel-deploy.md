# RB-13 — Roll back a bad panel deploy

**Severity:** critical · **When:** `deploy-update` finished but the
panel is broken, or `click-odoo-update` failed inside the downtime
window.

The deploy pipeline leaves you two safety nets, created automatically:

1. **The previous image** — `img-release` rotates the live
   `<image>:prod` into `<image>:prod-prev` on the server before
   shipping the new one (exactly two images are kept).
2. **A fresh restorable backup** — `deploy-update` step 5.5 runs the
   backup container's daily job chain (dump + duplicity incremental to
   the usual destination) right before the downtime window opens.

Which net you need depends on whether the database was migrated.

## 1. Decide the rollback axis

```text
Did step 7 (click-odoo-update on the real DB) run?
├── NO  → code-only rollback (§2). The DB is untouched.
└── YES → code + DB rollback (§3). The old image cannot boot a
          migrated DB reliably — never retag alone and hope.
```

`deploy-update` prints numbered steps; the last `[N]` line in your
terminal tells you where it stopped.

## 2. Code-only rollback (DB untouched)

On the prod host (`<image>` is the `image:` value in `prod.yaml`):

```bash
prod$ docker image tag <image>:prod <image>:prod-bad   # keep for forensics
prod$ docker image tag <image>:prod-prev <image>:prod
prod$ docker compose -f prod.yaml up -d odoo
prod$ curl -fsS http://127.0.0.1:8069/web/health
```

Then re-activate the crons if the aborted run did not reach its
cleanup step (step 9 normally does this even on abort — verify):

```bash
odoo$ click-odoo -d <db> --rollback -c "print(env['ir.cron'].search_count([('active','=',False)]))"
```

If crons owned by our modules are still paused, re-run
`invoke deploy-update --dry-run` to see the enumeration, or flip them
in Settings → Technical → Scheduled Actions.

## 3. Code + DB rollback (migrations already ran)

1. Retag the image as in §2 (do **not** start Odoo yet).
2. Restore the pre-update backup taken in step 5.5 — it is the most
   recent set on the usual duplicity destination. Follow
   [RB-02](RB-02-restore-master-db.md) using `latest` as the restore
   time; the step-5.5 chain guarantees "latest" is minutes old, not
   yesterday.
3. Start Odoo and check health as in §2.

## 4. Aftermath

- The bad image stays tagged `prod-bad` — inspect at leisure, then
  `docker image rm <image>:prod-bad`.
- The failed migration is now an `upgrade`-job red in CI? If it was
  not, add the failing scenario to a migration test before re-shipping.
- Re-run `invoke img-release` only after the fix; the rotation will
  overwrite `prod-prev` with the (bad) current image — that is why
  §2 keeps `prod-bad` aside first.

## Rollback of this runbook

None — this runbook *is* the rollback.
