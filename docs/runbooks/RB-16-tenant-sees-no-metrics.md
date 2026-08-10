# RB-16 — A tenant sees no metrics

**Trigger:** a tenant reports empty charts, or their Monitoring section is
missing entirely.

**What it means:** somewhere along a chain of five links, one is broken.
The chain is worth knowing before touching anything, because each link
fails differently and only one of them is the tenant's problem.

```
account exists → settings injected → agents enrolled → data arriving → charts visible
```

!!! note "What is never the answer"
    There is no button for the tenant to press and none for you. Enrolment
    converges on its own. If the fix looks like "ask them to enable
    something", the diagnosis is wrong.

## 1. Does the tenant have an account?

On the manager, open the tenant. `metrics_account` and `metrics_token`
are created the first time they are needed, so a tenant created before
observability was switched on will have neither until the central is
deployed again.

**Fix:** press **Enable observability** in Settings. The account list is
rebuilt from every active tenant, so this both creates the account and
authorises it.

## 2. Were the settings injected into their panel?

They are pushed at deploy time and re-pushed afterwards. A tenant
deployed while our central was unconfigured got nothing — deliberately:
pushing a URL that answers nothing would have every one of their hosts
retrying forever against a dead endpoint, which looks like their fault
and is not.

**Check:** in the tenant's database, `cloud.settings` should have
`metrics_enabled`, `metrics_account`, the credential and both URLs.

**Fix:** re-run the tenant ICP push. Do **not** hand-edit their settings:
the next push would overwrite it, and the drift in between is exactly the
state this design avoids.

## 3. Are their hosts enrolled?

In the tenant's own panel, each host page states it: reporting, installed
but no data yet, install failed and retrying, or not enrolled.

- **Not enrolled** and staying that way → the host is not a target. Most
  often it has no stored SSH host key, meaning the panel never reached
  it; a host that has not completed setup cannot take agents.
- **Install failed** → the reconciliation cron is already retrying with a
  growing delay, and raises an alert once failures persist. Read the job
  log before intervening.

## 4. Is data arriving?

From the central host, with the **operator** credential (not the
tenant's):

```bash
curl -sS -u operator:<operator credential> -G \
  http://172.17.0.1:8428/admin-r/api/v1/query \
  --data-urlencode 'query=count by (ic_account) (up)'
```

That lists which accounts are writing at all. A tenant absent from it is
not pushing; a tenant present but with charts empty is a read-side or
Grafana problem, so continue to step 5.

If their agents report auth failures, their credential and the central's
user list disagree — re-deploy the central, which rebuilds the list
from current state.

## 5. Can they read it back?

Two things must both be true, and they fail differently:

- **vmauth must accept their credential** on `/r/`. A 401 here has
  the same cause and fix as above.
- **Grafana must place them in their own organisation**, whose datasource
  is scoped to their account. If the organisation is missing, re-deploying
  the central creates it — organisations are provisioned from the account
  list in the same run.

A tenant who lands in the wrong organisation sees an empty dashboard
rather than somebody else's data: the datasource of an organisation can
only read that account. That is the boundary working, not a leak.

## 6. If they see nothing at all

The Monitoring entry is hidden while observability is off for that panel,
and the whole Metrics tab requires the `developer` role. A tenant user
with a lower role sees no charts anywhere — which is a permissions
answer, not an infrastructure one.

## Related

- `docs/observability-operations.md` — the account boundary and how
  vmauth imposes it
- RB-11 — the central itself is unreachable
