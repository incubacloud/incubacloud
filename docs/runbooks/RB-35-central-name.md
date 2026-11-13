# RB-35: The central's single name — pending, down, its certificate, a server left out

**Severity:** high when `metrics_central_name_down` is raised; warning otherwise.
**Typical trigger:** `metrics_central_dns_pending`, `metrics_central_name_down`,
`metrics_central_cert_expiring`, `metrics_central_writer_outside`,
`metrics_central_acme_same_name`.
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
  deleted, or left behind after a move), or the central's server failed to resolve it
  for a moment. Whether the agents still write depends on where the record points:
  during a move (RB-36) they go on writing to the server the central leaves. While the
  alert is open, the observability cron checks the name again every 15 minutes, so a
  passing failure of the resolver closes it within minutes.
- **`metrics_central_name_down` (critical).** The name was in use and two checks in a
  row failed. The layer is in the alert: `dns`, `connect`, `tls` or `route`.
- **`metrics_central_cert_expiring` (critical).** The certificate under the name has
  less than 21 days left; the reconciliation has been trying to renew it since 60.
- **`metrics_central_writer_outside` (warning).** One server cannot write: the port
  refused it, or it has been silent 20 minutes while the others write. The alert gives
  the address it leaves with and the one the panel has for it, or says the panel could
  not learn it (the server's ask over IPv4 failed, or a CDN answered it). Silence alone
  raises nothing for a server the panel cannot reach either (`host_unreachable`), or
  whose own last check got 401 with the address the panel has. When the server's check
  refused the certificate it was answered with, the port let it in: the alert says so
  instead of naming addresses, and the cause is usually the next symptom.
- **`metrics_central_acme_same_name` (warning).** The central's deployment found a
  certificate for the name in the ACME store of its server's proxy, obtained when the
  name was published on 443 with a certificate of its own. Traefik then serves that
  certificate or the panel's after each reload, and the agents refuse the first: the
  checks fail at `tls` by turns. The deployment says it in its log too, and says when
  it could not read the store.

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
  mount `~/traefik/certs` at `/etc/certs` and watch `dynamic/`. If it answers with
  another certificate of the name now and then, see the next item.
- **A certificate of the same name in the proxy's store.** The panel does not edit the
  store: it holds every name's key and belongs to the proxy. First make sure nothing
  asks a certificate resolver for the name any more (a route on 443 kept by hand, the
  labels of an older deployment), or the proxy obtains it again. Then, on the central's
  server, in `~/traefik` (replace `NAME`):
  ```bash
  P="docker compose -p inverseproxy -f inverseproxy.yaml"
  STORE=/etc/traefik/acme/acme.json
  umask 077
  $P exec -T proxy cat $STORE > acme.json.bak     # every key the proxy holds
  python3 -c '
  import json, sys
  name, store = sys.argv[1], json.load(open(sys.argv[2]))
  for resolver in store.values():
      if isinstance(resolver, dict) and resolver.get("Certificates"):
          resolver["Certificates"] = [
              c for c in resolver["Certificates"]
              if name not in [c["domain"].get("main")] + (c["domain"].get("sans") or [])
          ]
  json.dump(store, sys.stdout, indent=2)
  ' NAME acme.json.bak > acme.json.new
  $P exec -T proxy sh -c "cat > $STORE" < acme.json.new   # keeps the file's mode
  $P restart proxy
  $P exec -T proxy grep -c -F '"NAME"' $STORE             # 0
  rm -f acme.json.bak acme.json.new
  ```
  Traefik reads the store only when it starts and does not write it back when it
  stops, so the restart is what makes it forget the certificate; it cuts whatever else
  the proxy serves for a few seconds. Deploy the central again: its log no longer
  mentions the store, and the alert closes. Where the restart is not acceptable, a
  name the proxy never obtained a certificate for avoids the problem.
- **`route`.** vmauth did not answer 401: look at the central's last deployment and
  `docker compose -p incubacloud-central ps` on its server.
- **Certificate expiring.** Read why the central's last deployments failed and deploy
  it again; the new certificate loads without a restart.
- **A server left out.** When the alert says the server refused the central's
  certificate, see the item above. Otherwise, correct the server's address in the
  panel if it is wrong (the address it leaves with is in the alert), and *Sync Direct
  Path* runs on its own.
  A server behind NAT, or whose address changes, needs *Who can write to the central*
  set to *Any address*, where the panel offers it.

## Before cutting the earlier path

Before the earlier path stops answering (the name's record taken out of a CDN, a
gateway on 443 removed), check that everything still writing through it moves:

- Every server's agents on the name: `metrics_agents_write_url` is the single
  name's write URL on every installed, active host.
- Where a module that extends the panel sends these URLs to other panels, each of
  them installs the authority; the module's own guide says how to check it.
- The proxy of the central's server holds no certificate of the name in its ACME
  store (`grep -c -F '"NAME"'` as above gives 0).

Where the name goes through a CDN today, the cut cannot wait for the switch: from the
deployment that turns it on, the proxy answers the name with the panel's certificate
on every entrypoint, 443 included, and the CDN refuses it (Cloudflare answers 526).
So change the record first and turn the switch on after:

1. **The record.** The name's A record to the central's server, without the CDN's
   proxy and without an AAAA record. What went through the CDN stops answering here.
2. **Wait for the central's server to resolve it.** Its resolvers can answer the old
   and the new address by turns for a while (from half a minute to two minutes in a
   rehearsal):
   ```bash
   for ns in $(awk '/^nameserver/{print $2}' /etc/resolv.conf); do
     dig +short @"$ns" NAME
   done
   ```
   Every line is the central's address.
3. **The switch.** Set the public name, turn **One path, without a CDN** on and save;
   deploy the central. The direct path's job that follows the deployment checks the
   name and moves the URLs (25 seconds in the rehearsal), the reconciliation re-applies
   the agents, and a module that sends the URLs to other panels sends them. In the
   rehearsal every agent wrote under the name about two minutes after the record
   changed. **Check now** is needed only when that check failed on DNS.

What writes through the CDN has no path from step 1 until its agents are re-applied,
and their queue for the earlier URL is not carried to the new one.

## A server installed without the authority, or with another

Each server records the SHA-256 fingerprint of the authority its agents were
installed with (`metrics_agents_ca_fingerprint` on the host; empty for none), and
the reconciliation re-applies a server whose fingerprint is not the one the panel
installs now, as it does one whose URL is not ("central authority changed" in the
log). A server installed with the name's URL before the authority reached its
panel, or with an authority since rotated, is re-applied at the next pass (every
15 minutes) without anyone's hand. Without an authority in force nothing is
re-applied for it: with the switch off the agents get none, and while rotating,
between forgetting the authority and the deployment that mints the next one, the
servers stay as they are.

A server whose re-apply did not take still refuses the central's certificate. No
alert names it reliably: its silence is put down to its last 401, so it does not
raise `metrics_central_writer_outside`. Look for it by `metrics_last_seen` falling
behind; the TLS error is in its vmagent's log. Only then, as a fallback (a panel on
code older than the fingerprint, or a re-apply that failed), re-apply it by hand:
on the panel, set the host's `metrics_agents_revision` to 0 and the reconciliation
re-applies it at its next pass (the same lever the operations guide uses when the
security logs change).

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
3. The reconciliation re-applies every server's agents at its next pass (every 15
   minutes): their recorded fingerprint is the old authority's. To queue them at
   once, run `env["cloud.host"]._reconcile_direct_agents()` in an Odoo shell on
   the panel and commit.
4. Where a module that extends the panel sends the authority to other panels, it
   sends the new one with its next push, and each of those panels re-applies its own
   servers the same way.
5. Check each host's `metrics_agents_ca_fingerprint` (the new one once its re-apply
   ended) and `metrics_last_seen`: a host where it does not move after its re-apply
   still refuses the central. Go by `metrics_last_seen`:
   `metrics_central_writer_outside` does not name these servers, as their last 401
   from the address the panel has explains their silence. A host left behind is
   re-applied by hand as in the section above. The panel's own reads use a bundle
   named by the authority's digest, so they take the new one at once.

## Rollback

Turn **One path, without a CDN** off and save. The panel puts back the URLs it used
before the name (kept in `incubacloud.metrics_central_legacy_urls`), the
reconciliation re-applies every server's agents to them, and a deployment of the
central is queued at once. That deployment matters: until it removes the name's
certificate and its TLS document from the proxy, the proxy answers the name with
the panel's certificate on 443 too, and a CDN in front refuses it. Then:

- Where the name went through a CDN, put the CDN's proxy back on its record.
- Where the public name was empty before the switch (the central reached through a
  route on 443 kept by hand), empty it in the same save. With a name and the switch
  off, the deployment publishes the central on 443 under it, and may obtain an ACME
  certificate for it: the certificate that gets in the way when the switch is turned
  on again (above).

Only the save that turns the switch off queues that deployment. A name emptied in
a later save, with the switch already off, queues nothing: the certificate stays on
443 until the central is deployed again, so deploy it by hand then.

The earlier paths must still exist: the public name on 443, a direct name, or a
route kept by hand. In a rehearsal the way back took about three minutes.
