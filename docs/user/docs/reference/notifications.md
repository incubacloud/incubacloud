# Notifications

Stay on top of what your platform is doing without watching the screen. Every
user configures their own channels from the bell menu in the header
(**Notifications** modal).

## What triggers a notification

Job outcomes — a deploy, rebuild, backup or any other operation finishing
(successfully or not) — and infrastructure alerts (host unreachable, disk
critical, instance down…). Failed deploys and rebuilds are treated as **severe**
and always delivered immediately.

## Your preferences

- **Level** — `All` (every job outcome), `Failures only` (default), or `None`.
- **Muted projects** — silence everything from projects you don't care about.
- Notifications only cover what you can already see: project visibility rules
  apply to every channel.

## Channels

### In-app (always on)

Toasts for finished jobs and critical alerts, plus the alert badge in the
header. Real-time — no configuration needed.

### Email

Toggle it on and pick a mode:

- **Immediate** — one email per job outcome.
- **Daily digest** — a single summary email every morning. Severe failures
  (deploy/rebuild) still arrive immediately.

### Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and paste the bot token.
2. Send any message to your bot, then click **Detect chat ID** — the platform
   finds your chat automatically.
3. **Send test** to verify. Done: job outcomes arrive as Telegram messages.

### Webhook

Point the platform at any HTTPS endpoint to integrate with Slack bridges,
on-call tools or your own automation. Each job state change POSTs a JSON
payload:

```json
{
  "event": "job_state_change",
  "job_id": 123,
  "job_name": "Deploy Instance",
  "state": "failed",
  "severe": true,
  "host": "host-1",
  "instance": "production",
  "log_url": "https://…/cloud/log/123",
  "timestamp": "2026-07-12 10:00:00"
}
```

If you set a **secret**, every request carries an
`X-IncubaCloud-Signature: sha256=<hex>` header — the HMAC-SHA256 of the raw
body. Verify it with a constant-time comparison before trusting the payload.

## Backup usage alerts

Separately from job notifications, each backup backend can email a warning when
bucket usage crosses a threshold (default 80%). Configure it on the backend
detail page.
