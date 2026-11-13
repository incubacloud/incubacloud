# RB-35: The central's single name — pending, down, its certificate, a server left out

**Severity:** high when `metrics_central_name_down` is raised; warning otherwise.
**Typical trigger:** `metrics_central_dns_pending`, `metrics_central_name_down`,
`metrics_central_cert_expiring`, `metrics_central_writer_outside`.
**Who runs it:** platform manager.

With **One path, without a CDN** on, the central answers under its public name only,
on its server's platform port (`https://metrics.example.com:9443`), with a
certificate the panel's own authority signs. There is no other path to fall back to:
while the name does not answer, the agents keep what they cannot send in their queues
(512 MB per URL for vmagent, 64 MB for Fluent Bit) and send it when it answers again.
vmagent keeps its queue in its volume, so a re-apply of the agents keeps it too; a new
URL starts a queue of its own.
See the operations guide, "The single path".

## Symptoms

- **`metrics_central_dns_pending` (warning; critical after 12 hours when a central was
  already serving).** The name does not answer from the central's server yet, so the
  panel and the agents keep their earlier URLs. The alert names the record to create
  and the layer that failed. **Critical at once when the name is in use**: its record
  no longer points at the central's server (a CDN's proxy turned back on, the record
  deleted, or left behind after a move), and no agent can write.
- **`metrics_central_name_down` (critical).** The name was in use and two checks in a
  row failed. The layer is in the alert: `dns`, `connect`, `tls` or `route`.
- **`metrics_central_cert_expiring` (critical).** The certificate under the name has
  less than 21 days left; the reconciliation has been trying to renew it since 60.
- **`metrics_central_writer_outside` (warning).** One server cannot write: the port
  refused it, or it has been silent 20 minutes while the others write. The alert gives
  the address it leaves with and the one the panel has for it, or says the panel could
  not learn it (the server's ask over IPv4 failed, or a CDN answered it). Silence alone
  raises nothing for a server the panel cannot reach either (`host_unreachable`), or
  whose own last check got 401 with the address the panel has.

## Diagnosis

From the central's server (replace the name, the port and the address):

```bash
getent ahostsv4 metrics.example.com      # the server's address, and only it
getent ahostsv6 metrics.example.com      # nothing
curl -sS -o /dev/null -w '%{http_code}\n' --cacert ~/observability/central-ca.pem \
  --resolve metrics.example.com:9443:203.0.113.9 \
  -X POST https://metrics.example.com:9443/w/api/v1/write   # 401
ls -l ~/traefik/certs/ic-central.* ~/traefik/dynamic/ic-central-tls.yml
docker logs --since 1h $(docker ps -qf name=proxy) 2>&1 | grep -i ic-central
```

`~/observability/central-ca.pem` exists once the agents use the name; before that,
take the authority from the panel (Settings › Monitoring shows the state; the
certificate is `metrics_ca_cert`). From a server that does not write, the same `curl`
without `--resolve` tells a refusal by the port (no answer, or 403) from a problem of
the name.

## Resolution

- **`dns`.** Create or fix the A record: the central's server's address, no CDN proxy,
  no AAAA. Then **Check now** under *Central's public name*, or wait for the
  observability cron (15 minutes). One check that passes moves the URLs of a name not
  in use yet and closes the alert; a name in use stays *Failing* until two good checks
  30 minutes apart, while the agents already write.
- **`connect`.** The direct port is not published or not admitted: run Full Setup on
  the central's server and *Sync Direct Path*; check the provider's firewall lets the
  port in.
- **`tls`.** The files above are missing or Traefik did not load them: deploy the
  central again (*Enable observability* on the same server). A proxy kept by hand must
  mount `~/traefik/certs` at `/etc/certs` and watch `dynamic/`.
- **`route`.** vmauth did not answer 401: look at the central's last deployment and
  `docker compose -p incubacloud-central ps` on its server.
- **Certificate expiring.** Read why the central's last deployments failed and deploy
  it again; the new certificate loads without a restart.
- **A server left out.** Correct the server's address in the panel if it is wrong
  (the address it leaves with is in the alert), and *Sync Direct Path* runs on its own.
  A server behind NAT, or whose address changes, needs *Who can write to the central*
  set to *Any address*, where the panel offers it.

## Before cutting the earlier path

Before the earlier path stops answering (the name's record taken out of a CDN, a
gateway on 443 removed), check that everything still writing through it moves:

- Every server's agents on the name: `metrics_agents_write_url` is the single
  name's write URL on every installed, active host.
- Where a module that extends the panel sends these URLs to other panels, each of
  them installs the authority; the module's own guide says how to check it.

## A server installed without the authority, or with another

The reconciliation re-applies a server whose agents' URL differs from the panel's;
it does not compare the authority. A server installed with the name's URL and no
authority, or with an authority since rotated, refuses the central's certificate.
No alert names it reliably: its silence is put down to its last 401, so it does not
raise `metrics_central_writer_outside`. Look for it by `metrics_last_seen` falling
behind, and the TLS error is in its vmagent's log. Re-apply its agents: on the panel, set the host's
`metrics_agents_revision` to 0 and the reconciliation re-applies it at its next pass
(the same lever the operations guide uses when the security logs change).

## Rotate the authority

When its key may have left the panel (a copy of the database not neutralized, a
leak), or as its ten years end. Every agent trusts the authority alone for the
central, so between the central's new certificate and each server's re-apply that
server's agents refuse the central and queue: do it at a quiet hour, all at once.

1. In an Odoo shell on the panel, forget the authority and the leaf's end, and
   commit:
   ```python
   s = env["cloud.settings"].sudo()._get_system()
   s.write({"metrics_ca_cert": False, "metrics_ca_key": False,
            "metrics_central_cert_expires": False})
   env.cr.commit()
   ```
2. Deploy the central again (*Enable observability* on its server). It mints a new
   authority and signs a certificate with it; Traefik loads it without a restart.
3. Re-apply every server's agents: set `metrics_agents_revision` to 0 on every
   installed, active host and commit. The reconciliation re-applies five per pass
   (every 15 minutes); `host._enqueue_observability("authority rotated")` in the same
   shell queues one at once.
4. Where a module that extends the panel sends the authority to other panels, it
   sends the new one with its next push; each of those panels must re-apply its own
   servers' agents the same way.
5. Check each host's `metrics_last_seen`: a host where it does not move after its
   re-apply still refuses the central, so re-apply it again (step 3). Go by
   `metrics_last_seen` alone: `metrics_central_writer_outside` does not name these
   servers, as their last 401 from the address the panel has explains their silence.
   The panel's own reads use a bundle named by the authority's digest, so they take
   the new one at once.

## Rollback

Turn **One path, without a CDN** off and save. The panel puts back the URLs it used
before the name (kept in `incubacloud.metrics_central_legacy_urls`) and the
reconciliation re-applies every server's agents to them. Those paths must still exist:
the public name on 443, a direct name, or a hand-written gateway.
