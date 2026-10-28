# RB-24: The direct path — down, a host not admitted, the DNS token, rolling back

**Severity:** high when `direct_path_down` is raised; planned otherwise.
**Typical trigger:** `direct_path_down`, `direct_allowlist_stale`,
`direct_certificate_expiring`, `direct_path_unreachable_from_host`; a host whose
metrics stopped while the direct path says _Working_; Grafana refusing logins; the
DNS token has to be rotated.
**Who runs it:** platform manager.

The direct path is the platform talking to itself without the CDN: hosts and rented
servers push metrics to `direct.metrics.…:9443`, tenant panels call `direct.www.…:9443`
for `/saas/*` and the OIDC back channel, Grafana exchanges its logins there. The public
path (`metrics.…`, `www`) is never touched and is always the way back: every failure
below ends with hosts, panels and Grafana on it.

## Symptoms

- **`direct_path_down` (critical).** Two probes in a row failed (one, if the name
  resolves elsewhere or has an AAAA record). Hosts and panels are back on the public
  path on their own; Grafana follows with its next account sync. Read the layer in the
  alert (`dns`, `connect`, `tls`, `route`).
- **`direct_allowlist_stale`.** A host's list has lagged for 30 minutes: new addresses
  use the public path until it is applied. Look at the host's last _Sync Direct Path_
  job.
- **`direct_certificate_expiring`.** Under 21 days left: Let's Encrypt renews at 30, so
  the renewal failed. Check the DNS token and Traefik's log.
- **`direct_path_unreachable_from_host`.** The host's own try failed twice: its agents
  use the public path; it is tried again every six hours. Usually the customer blocks
  outbound 9443 (`hosts/hire.md` tells them).
- **A host gone quiet while the path says _Working_.** The agents only try the path when
  they are installed. After 20 minutes without metrics the reconciliation re-applies
  them (at most hourly, only while the central answers), and they fall to the public
  path if the try fails. To force it now: tick _Use the public path_ on the host.
- **Grafana refuses logins.** Between the panel's name going down and the account sync
  that gives Grafana the public issuer back, Grafana has no fallback. Wait for the sync
  on the central's host, or run it.
- **429 on `/oauth/token`.** On Cloud1 every container reaches Traefik from the proxy
  network's gateway (172.19.0.1): mail, QA, Grafana and the light probe share its rate
  limits. A client failing many exchanges blocks the others for the window; the access
  log shows it.

## Diagnosis (on the host that publishes the name)

```bash
nft list set inet filter ic_direct_v4          # the firewall's list
nft list chain inet filter forward | grep ic-direct
cat ~/traefik/dynamic/direct.yml               # Traefik's list
curl -s 127.0.0.1:8080/api/http/routers | python3 -m json.tool | grep -A3 direct
echo | openssl s_client -connect <ip>:9443 -servername <name> 2>/dev/null \
  | openssl x509 -noout -issuer -enddate
dig +short A <name> @1.1.1.1; dig +short AAAA <name> @1.1.1.1
```

From a container (the way agents, Grafana and the manager come):

```bash
docker exec incubacloud-production-odoo-1 python3 -c "import requests; \
print(requests.get('https://<name>:9443/oauth/jwks', timeout=5).status_code)"
```

## Rotate the DNS token

1. Cloudflare → API Tokens: create a token with _Zone → Zone → Read_ and _Zone → DNS →
   Edit_ on the zone, client addresses filtered to the panel's.
2. Settings → Monitoring → Advanced → _DNS token for direct certificates_: paste it and
   save.
3. _Push Trusted Proxy Settings_ on every host with a direct port (it restarts Traefik
   there).
4. Revoke the old token in Cloudflare.

## Roll back

- One host: tick _Use the public path_ on it. Its agents and the panels on it go back at
  once.
- Everybody's metrics: empty _Central's direct name_. Everybody's API: empty the panel's
  direct name (SaaS tab). Grafana: empty its back channel.
- The port itself: direct port 0 and Full Setup on the host (Traefik restarts), then
  remove the provider's firewall rule and the two DNS records.
