# IncubaCloud Webhook Notifications

When you configure an external webhook URL, IncubaCloud will POST a JSON
payload to that endpoint every time a job reaches a terminal state
(`done` / `failed`). Cancelled jobs and silent background jobs
(`host_metrics`, `instance_health`, `docker_prune`) are never posted.

## Payload

`Content-Type: application/json`

```json
{
  "event": "job_state_change",
  "job_id": 123,
  "job_name": "Deploy instance",
  "state": "failed",
  "severe": true,
  "host": "prod-host-1",
  "instance": "my-project / prod-instance",
  "log_url": "https://app.incubacloud.io/cloud/log/123",
  "timestamp": "2026-07-10T17:42:00"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `event` | string | Always `"job_state_change"` |
| `job_id` | integer | The job's record id in the IncubaCloud database |
| `job_name` | string | Display name of the job |
| `state` | string | `"done"` or `"failed"` |
| `severe` | boolean | `true` for production deploy/rebuild failures |
| `host` | string | Host name the job ran on |
| `instance` | string or null | Instance name (with project prefix) or `null` for host-level jobs |
| `log_url` | string | Full URL to the job log in the IncubaCloud console |
| `timestamp` | string | ISO-8601 UTC timestamp of the notification |

## Signature verification

When a signing secret is configured, every POST includes an
`X-IncubaCloud-Signature` header:

```
X-IncubaCloud-Signature: sha256=<hex-digest>
```

The signature is computed as `HMAC-SHA256(secret, raw_request_body)`.
Your endpoint should **verify this signature** to make sure the request
genuinely came from IncubaCloud and was not tampered with.

### Python (Flask)

```python
import hmac, hashlib

SECRET = b"your-signing-secret"

@app.route("/webhooks/incubacloud", methods=["POST"])
def incubacloud_webhook():
    expected = "sha256=" + hmac.new(
        SECRET, request.data, hashlib.sha256
    ).hexdigest()
    header = request.headers.get("X-IncubaCloud-Signature", "")
    if not hmac.compare_digest(expected, header):
        return "Invalid signature", 403
    # Process the payload
    payload = request.get_json()
    print(f"Job {payload['job_name']} {payload['state']}")
    return "", 200
```

### Node.js (Express)

```javascript
const crypto = require("crypto");

const SECRET = "your-signing-secret";

app.post("/webhooks/incubacloud", (req, res) => {
    const expected = "sha256=" + crypto
        .createHmac("sha256", SECRET)
        .update(JSON.stringify(req.body))
        .digest("hex");
    const header = req.get("X-IncubaCloud-Signature") || "";
    if (!crypto.timingSafeEqual(
        Buffer.from(expected), Buffer.from(header)
    )) {
        return res.status(403).send("Invalid signature");
    }
    console.log(`Job ${req.body.job_name} ${req.body.state}`);
    res.sendStatus(200);
});
```

### No signature

If you leave the signing secret empty, IncubaCloud will still send the
POST but without the `X-IncubaCloud-Signature` header. Use this when your
endpoint does not support signature verification (e.g. Discord / Slack
incoming webhooks, Zapier, Make.com).

## Delivery guarantees

- **At most once** — if the POST fails (timeout, 4xx/5xx, network error)
  the notification is lost. There are no retries.
- **Timeout** — 10 seconds. If your endpoint takes longer to respond the
  connection is closed and the notification is discarded.
- **Ordering** — notifications are dispatched in the order job state
  transitions occur, but delivery order is not guaranteed.

## Filtering

Webhook notifications follow the same rules as email and Telegram:

- You only receive events for jobs you can see in the console (record rules
  are the single source of truth).
- If your delivery mode is *Daily digest*, only severe failures reach the
  webhook immediately; regular events arrive in the daily email digest.
- Muted projects never generate webhook calls.

See the **General settings** section in the Notification Preferences modal.
