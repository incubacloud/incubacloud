# RB-34: The central's single name — pending, down, its certificate, a server left out

**Severity:** high when `metrics_central_name_down` is raised; warning otherwise.
**Typical trigger:** `metrics_central_dns_pending`, `metrics_central_name_down`,
`metrics_central_cert_expiring`, `metrics_central_writer_outside`.
**Who runs it:** platform manager.

With **One path, without a CDN** on, the central answers under its public name only,
on its server's platform port (`https://metrics.example.com:9443`), with a
certificate the panel's own authority signs. There is no other path to fall back to:
while the name does not answer, the agents keep what they cannot send in their queues
(512 MB per URL for vmagent, 64 MB for Fluent Bit) and send it when it answers again.
See the operations guide, "The single path".

## Symptoms

- **`metrics_central_dns_pending` (warning; critical after 12 hours when a central was
  already serving).** The name does not answer from the central's server yet, so the
  panel and the agents keep their earlier URLs. The alert names the record to create
  and the layer that failed.
- **`metrics_central_name_down` (critical).** The name was in use and two checks in a
  row failed. The layer is in the alert: `dns`, `connect`, `tls` or `route`.
- **`metrics_central_cert_expiring` (critical).** The certificate under the name has
  less than 21 days left; the reconciliation has been trying to renew it since 60.
- **`metrics_central_writer_outside` (warning).** One server cannot write: the port
  refused it, or it has been silent 20 minutes while the others write. The alert gives
  the address it leaves with and the one the panel has for it.

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
  observability cron (15 minutes).
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

## Rollback

Turn **One path, without a CDN** off and save. The panel puts back the URLs it used
before the name (kept in `incubacloud.metrics_central_legacy_urls`) and the
reconciliation re-applies every server's agents to them. Those paths must still exist:
the public name on 443, a direct name, or a hand-written gateway.
