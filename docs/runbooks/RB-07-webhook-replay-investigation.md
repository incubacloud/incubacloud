# RB-07: Investigate a webhook replay

**Severity:** info
**Typical trigger:** `Duplicate delivery — already processed` log
line, or an alert from an external SIEM flagging repeated
`X-GitHub-Delivery` headers.
**Who runs it:** ops, with security escalation if the replay traces
back to a non-GitHub source IP.

The webhook endpoint rejects duplicate `X-GitHub-Delivery` UUIDs via
a UNIQUE index on `cloud.github.event.delivery_id` and returns 200
so GitHub stops retrying. A rejected delivery is logged at INFO
level — most entries are boring (GitHub's own retry after a
transient 500), but an occasional one is a replay attempt.

## Symptoms / triggers

- Multiple log lines with the same `delivery_id`, minutes or hours
  apart.
- A SIEM alert on "duplicate webhook" patterns.
- Suspicion that our webhook secret leaked.

## Diagnosis

1. **Pull the duplicate log entries** with full context:

   ```bash
   odoo$ grep 'Duplicate delivery' /var/log/odoo/odoo.log | tail -50
   ```

   Each line includes the `delivery_id` and the source IP.

2. **Look up the original delivery** in `cloud.github.event`:

   ```sql
   db$ SELECT id, event_type, create_date, repo, ref, source_ip
       FROM cloud_github_event
       WHERE delivery_id = '<uuid>';
   ```

   There is exactly one row (that's the point of the UNIQUE index).
   The `create_date` is when we first accepted it.

3. **Classify the replay**:
   - Source IP in GitHub's published hook ranges
     (`https://api.github.com/meta` → `.hooks`) → benign retry.
   - Source IP not in GitHub's ranges, same HMAC valid → **possible
     leaked secret**, escalate to security.
   - Same IP range as GitHub but time gap > 24h → likely a bad
     proxy retrying a queued payload; safe to ignore after noting.

## Resolution

### Benign retry (GitHub IP, recent)

No action. The log line is there precisely for audit; we already
swallowed the replay. Close the ticket.

### Leaked secret suspected

1. **Rotate the GitHub webhook secret** in the target repository /
   GitHub App. Update `cloud.github.app.webhook_secret` accordingly.
2. **Restart the stack** so cached secret is re-read:

   ```bash
   $ invoke restart
   ```

3. **Audit recent accepted deliveries** from the same source IP:

   ```sql
   db$ SELECT delivery_id, event_type, create_date, ref
       FROM cloud_github_event
       WHERE source_ip = '<ip>'
         AND create_date > NOW() - INTERVAL '7 days'
       ORDER BY create_date;
   ```

   Anything non-GitHub is suspicious — pull the payload for each and
   confirm with the repo owner whether they actually triggered that
   event.

4. **File a security incident**. The webhook can trigger deploys
   and rebuilds; a leaked secret is a privilege escalation path
   (the attacker can push a malicious commit to a repo we watch and
   have it auto-deployed).

### Stale proxy

Note the event in the ticket, close. No action needed.

## Rollback

Rotating the webhook secret is the only destructive action here,
and it is not really reversible — the new secret is what matters.
If you rotated in error, rotate again to a fresh value and update
GitHub. There is no "undo" to the old secret.

## References

- [`controllers/github_webhook.py`](../../incubacloud/controllers/github_webhook.py)
  — HMAC check + anti-replay logic.
- [`models/cloud_github_event.py`](../../incubacloud/models/cloud_github_event.py)
  — event model + `delivery_id` UNIQUE index.
- [`tests/test_cloud_github_event.py`](../../incubacloud/tests/test_cloud_github_event.py)
  — `TestDeliveryIdAntiReplay`.
