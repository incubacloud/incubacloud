# RB-01: Rotate `INCUBACLOUD_SECRET_KEY` (MultiFernet)

**Severity:** planned
**Typical trigger:** scheduled rotation (quarterly), or suspected key leak.
**Who runs it:** ops + security.

Encrypted secrets (`s3_secret_access_key`, `passphrase`, SSH
credentials, SMTP passwords, …) are encrypted with Fernet. The
`INCUBACLOUD_SECRET_KEY` env var is a comma-separated list of Fernet
keys; the first entry is the **primary** (used for encryption), the
rest are legacy keys still accepted for decryption. Rotation moves
every ciphertext from the old primary to a new primary and then
drops the old key from the env var.

## Symptoms / triggers

- Scheduled 90-day rotation calendar event.
- Incident response after a leak: the old key may have been read.
- Before re-homing a deployment to a new secret store.

## Diagnosis

Check which keys are active and how many rows are still encrypted
with a legacy key:

```sql
db$ SELECT
       CASE WHEN s3_secret_access_key LIKE 'enc:%' THEN 'encrypted'
            ELSE 'other' END AS state,
       COUNT(*)
    FROM cloud_backup_backend
    GROUP BY 1;
```

`odoo log` will show `INCUBACLOUD_SECRET_KEY has N keys loaded (rotation
in progress)` at boot when more than one key is configured.

## Resolution

1. **Generate the new primary key** (on your workstation, not on
   production):

   ```bash
   $ python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

2. **Prepend the new key** to `INCUBACLOUD_SECRET_KEY` in the
   production env file. Keep the current primary as the second
   entry so existing ciphertext keeps decrypting:

   ```bash
   # BEFORE:  INCUBACLOUD_SECRET_KEY=OLD
   # AFTER:   INCUBACLOUD_SECRET_KEY=NEW,OLD
   ```

3. **Restart the stack** so the new env is read:

   ```bash
   $ invoke restart
   ```

4. **Enable the rotation cron** (disabled by default). In Odoo UI:
   Settings → Technical → Scheduled Actions → "Rotate Encrypted
   Secrets (MultiFernet)" → set Active = True. It runs hourly and
   re-encrypts one batch per tick so nothing moves all at once.

5. **Monitor progress** until every row is re-encrypted. A simple
   way: grep the Odoo log for `rotate_value` calls, or count
   ciphertext rows that still start with the old key's selector
   (Fernet tokens encode the key id in the first bytes — any
   row still decrypting under OLD will be rotated to NEW on next
   write).

6. **Disable the rotation cron** once every encrypted row has been
   rewritten.

7. **Remove the old key** from `INCUBACLOUD_SECRET_KEY`:

   ```bash
   # INCUBACLOUD_SECRET_KEY=NEW
   ```

   Restart once more. From this moment the old key cannot decrypt
   anything anymore — the window is closed.

## Rollback

If step 3 fails (service won't start), restore the previous
`INCUBACLOUD_SECRET_KEY` value and restart. No ciphertext has been
touched yet.

If step 5 is in-flight and a row fails to re-encrypt, keep both
keys in the env var. The row stays decryptable under OLD and will
be retried next tick. Do **not** remove OLD until the rotation cron
has completed a full pass with zero errors.

## References

- [`models/password_utils.py`](../../incubacloud/models/password_utils.py) — MultiFernet loader & `rotate_value()`.
- [`data/rotate_secrets_cron.xml`](../../incubacloud/data/rotate_secrets_cron.xml) — the rotation cron definition.
- `tests/test_password_utils.py::TestMultiFernetRotation` — integration tests of the mechanism.
