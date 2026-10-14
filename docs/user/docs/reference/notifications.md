# Notifications

Stay on top of what your platform is doing without watching the screen. Every
user configures their own channels from the bell menu in the header
(**Notifications** modal).

## What triggers a notification

Job outcomes — a deploy, rebuild, backup or any other operation finishing
(successfully or not) — and alerts. Alerts come in two levels: **critical**
(host unreachable, instance down, disk critical…) and **warning** (a container
of an instance down, errors in an instance's log, a declined subscription
payment…). Failed deploys and rebuilds are treated as **severe** and always
delivered immediately.

An open warning that turns critical is announced again, on every channel, to
everyone whose level covers critical alerts.

## Your preferences

- **Level** — decides both which job outcomes and which alerts reach you:

    | Level | Jobs | Alerts |
    | --- | --- | --- |
    | `All` | every outcome | critical and warning |
    | `Failures and warnings` | failed only | critical and warning |
    | `Failures only` (default) | failed only | critical only |
    | `None` | none | none |

    The daily digest follows the same level.
- **Muted projects** — silence everything from projects you don't care about.
- Notifications only cover what you can already see: project visibility rules
  apply to every channel.

## Channels

### In-app (always on)

Toasts for finished jobs and critical alerts, plus the alert badge in the
header. Real-time — no configuration needed.

### Email

Toggle it on and pick a mode:

- **Immediate** — one email per job outcome or alert.
- **Daily digest** — a single summary email every morning. Severe failures
  (deploy/rebuild) and critical alerts still arrive immediately.

### Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) and paste the bot token.
2. Send any message to your bot, then click **Detect chat ID** — the platform
   finds your chat automatically.
3. **Send test** to verify. Done: job outcomes and alerts arrive as Telegram
   messages, following your level.

### Webhook

Point the platform at any HTTPS endpoint to integrate with Slack bridges,
on-call tools or your own automation. Two event types arrive as JSON POSTs —
`job_state_change` for successful job completions, and `alert` for failures
and infrastructure alerts:

```json
{
  "event": "job_state_change",
  "job_id": 123,
  "job_name": "Deploy Instance",
  "state": "done",
  "severe": false,
  "host": "host-1",
  "instance": "production",
  "log_url": "https://…/cloud/log/123",
  "timestamp": "2026-07-12 10:00:00"
}
```

```json
{
  "event": "alert",
  "alert_id": 45,
  "code": "job_failed",
  "level": "critical",
  "message": "Deploy Instance on production failed",
  "host": "host-1",
  "instance": "production",
  "job_id": 123,
  "log_url": "https://…/cloud/log/123",
  "timestamp": "2026-07-12 10:00:00"
}
```

When an alert clears **automatically** (the monitor that raised it
observes recovery), the same payload shape is sent again with
`"event": "alert_resolved"` — consumers can close the incident they
opened on the `alert` event. Dismissing an alert by hand in the panel
does *not* send this event: the operator silencing it is already
looking at it.

If you set a **secret**, every request carries an
`X-IncubaCloud-Signature: sha256=<hex>` header — the HMAC-SHA256 of the raw
body. Verify it with a constant-time comparison before trusting the payload.

## Backup usage alerts

Separately from these notifications, each backup backend can email a warning when
bucket usage crosses a threshold (default 80%). Configure it on the backend
detail page.
