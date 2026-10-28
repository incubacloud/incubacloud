# RB-18: Tune host edge protection

**Severity:** planned / warning
**Typical trigger:** legitimate users on an instance hit `429` from the
proxy; a host is being flooded and you want the connection-rate cap on;
or you are onboarding a provider without L3/L4 anti-DDoS.
**Who runs it:** ops.

A host serving tenant instances has historically been reached
**directly** — tenant domains resolve straight to the host, not through
a CDN — so it is protected by **rate**, on the box itself, in layers.
Whether that is still true of a given host is written on the host
itself (`behind_cdn`), and it changes two of the layers below: read
[architecture.md → Host edge protection](../architecture.md) before
tuning anything on a host that sits behind a CDN, where the per-source
cap becomes an allowlist and the rate limit keys on the forwarded
chain. See
[architecture.md → Host edge protection](../architecture.md) for the
full model. This runbook is how you tune each layer.

| Layer | Where | Default | Tunable without redeploy? |
|-------|-------|---------|---------------------------|
| Provider L3/L4 anti-DDoS | provider network | on (Hetzner/OVH) | n/a — assumed |
| Host conn-rate (nftables) | `host_hardening.yml`, forward hook | **50/s per source** (since 1.0.183) | no (host field + re-run hardening) |
| CDN allowlist (nftables) | `host_hardening.yml`, forward hook | **off** — needs `behind_cdn` + `block_direct_access` | ranges: yes (the proxy-ranges job refreshes the set); turning it on: no (re-run hardening) |
| Proxy per-IP limit (Traefik) | host `config.yml`, `ratelimit` middleware | 300/min, burst 100 per IP | **yes** (file is watched) |
| Login limit (Traefik) | host `config.yml`, `loginlimit` middleware, one login router per instance | 20/min, burst 10 per IP, POSTs to the login endpoints | **yes** (file is watched) |
| Web-login ban (fail2ban) | `host_hardening.yml`, jail `incubacloud-weblogin` | 10 failures in 10 min → 1 h, 80/443 only; off behind a CDN | no (re-run hardening) |
| CDN client certificate (Traefik) | `dynamic/tls-client-auth.yml` | off — needs `behind_cdn` + `block_direct_access` + an authority | yes (proxy-settings push) |
| Direct allowlist (nftables + Traefik) | `direct_allowlist.sh`, `dynamic/direct.yml` | only with `direct_port` | yes (`sync_direct_path`, no restart) — [RB-24](RB-24-direct-path.md) |
| App counters (`cloud.rate.limit`) | `cloud.settings` | see RB-04 | yes — [RB-04](RB-04-tune-rate-limits.md) |

---

## Layer 3 — the Traefik per-IP rate limit (the usual knob)

The proxy throttles every instance on the host by client IP before a
request can reach Odoo's pbkdf2 login. It is the principal control for
the SEC-008 login-DoS applied to the tenant sites.

### Tune it live

The middleware lives in the host's dynamic Traefik config, which the
proxy **watches** — a change is picked up with no restart and no image
rebuild.

```bash
$ ssh <operator>@<host>
$ sudoedit ~/traefik/config.yml     # the `ratelimit:` middleware
```

```yaml
    ratelimit:
      rateLimit:
        average: 300      # requests per `period`, per source IP
        period: 1m
        burst: 100        # allowance for a cold page load
```

- **Legitimate users hitting 429** (a cold Odoo web load can fire dozens
  of asset/XHR requests): raise `burst` first, then `average`.
- **Abuse slipping through**: lower `average`. A single IP is still
  bounded to `burst` instantly plus `average` sustained.
- Save; Traefik reloads within seconds. Confirm in the proxy log that it
  reloaded the dynamic config.

### Persist it across re-provisions

The running `config.yml` is a copy of `cloud.host.traefik_config_yml`.
`full_setup` rewrites the file from that stored field, so a live edit is
**reverted on the next provision** unless you also change the source.
Update the host record (Hosts → the host → Traefik config) with the same
values, or re-apply the live edit after any re-provision.

### Host behind a CDN (the panel, or a tenant's own CDN)

`sourceCriterion` defaults to the real remote IP — correct for direct
traffic. If a host sits behind a **trusted** CDN (the panel behind
Cloudflare, or a tenant fronting their own domain), Traefik sees a
handful of edge IPs and would bucket every visitor together. Set an
`ipStrategy` so the limit keys on the real client:

```yaml
    ratelimit:
      rateLimit:
        average: 300
        period: 1m
        burst: 100
        sourceCriterion:
          ipStrategy:
            depth: 1      # trust one proxy hop's X-Forwarded-For
```

Only do this where the front is trusted — otherwise a client can forge
`X-Forwarded-For` and dodge the limit. Direct-traffic hosts need none of
this.

---

## Layer 2 — the nftables connection-rate cap

A per-source new-connection limit on 80/443, in the hardening ruleset.
It sits on the **forward** hook, because tenant traffic is DNAT'd to the
Traefik container and never crosses the input chain. **On by default
since 1.0.183** at 50 per second (`cloud.host.http_conn_rate`; hosts at 0
were moved to 50), applied on each host's next hardening run. It shipped
off from 2026-08-14, when an unrehearsed drop on the forward hook took the
whole fleet down, until it was rehearsed.

To change it, edit the host's rate (0 turns it off) and re-run hardening.

### Rehearse a change on a throwaway VPS

Any change to these rules, before it reaches a live host:

1. Spin up a disposable VPS on the same provider/OS.
2. Run hardening with the cap set, e.g. extra-vars
   `ic_http_conn_rate=50` (and optionally `ic_http_conn_burst`).
3. Bring up two instances. From a third IP, simulate a flood and confirm
   the **excess new connections are dropped** while a normal user from
   another IP is unaffected, and two instances on the host do not
   interfere.
4. **Confirm Docker's DNAT survived** — the 2026-08-14 check:
   ```bash
   $ sudo iptables -t nat -S | grep -c DOCKER      # must be non-zero
   ```
5. Only then enable it on a live host by passing the same extra-var to
   that host's hardening run.

### What it renders to

With a rate of 0 the forward chain is the plain `policy accept`. With a
rate it adds, per family, the same guards as the CDN allowlist below --
only the first packet of a connection redirected from the host's 80/443,
so the containers' own outbound traffic is never counted (matched on
`tcp dport` alone, it was):

```
meta l4proto tcp ct state new ct status dnat ct original proto-dst { 80, 443 } \
    meter ic_http_conn4 { ip saddr limit rate over 50/second burst 50 packets } drop
meta l4proto tcp ct state new ct status dnat ct original proto-dst { 80, 443 } \
    meter ic_http_conn6 { ip6 saddr limit rate over 50/second burst 50 packets } drop
```

Only **new** connections over the rate are dropped; established and
under-rate traffic falls through to Docker's own forward rules. The
ruleset still uses declare-then-delete of our own table, never
`flush ruleset`.

The play renders the ruleset to `/etc/nftables.conf.new`, parses it
there with `nft -c`, and only then installs and applies it: a ruleset
this host's nft cannot load fails with the parser's message and leaves
the live file alone (the staging file stays for inspection). CI parses
every variant the template can render with the nft of each OS the
preflight admits, so a rule one release refuses is caught before it
ships.

### Behind a CDN: the allowlist replaces the cap

On a host with `behind_cdn` the hardening run renders no cap at all,
whether or not `block_direct_access` is set — behind a proxy every new
connection comes from one of the CDN's edge addresses, so a per-source
cap counts edges, not visitors, and throttles the CDN itself. A host
still open at the origin (between moving its names and closing its
firewall) is therefore uncapped for as long as it stays open. With
`block_direct_access` too, the forward chain accepts 80/443 only from
the CDN's published ranges:

```
meta l4proto tcp ct state new ct status dnat ct original proto-dst { 80, 443 } ip saddr @ic_cdn_v4 accept
meta l4proto tcp ct state new ct status dnat ct original proto-dst { 80, 443 } ip6 saddr @ic_cdn_v6 accept
meta l4proto tcp ct state new ct status dnat ct original proto-dst { 80, 443 } drop
```

It lives in **forward** for the same reason the cap does: instance
traffic is DNAT'd to the Traefik container and never crosses input. The
three guards are what keep it narrow — only the first packet
(`ct state new`: replies come from the container and would otherwise be
judged too), only connections that were redirected from the host's own
ports (`ct status dnat`: the containers' outbound 80/443 stays out of
it), and by the port the visitor dialled (`ct original proto-dst`).
Other published ports are untouched. `meta l4proto tcp` has to come
first: nftables 1.0.2 (Ubuntu 22.04) cannot type `ct original
proto-dst` until the transport protocol is stated, and refuses the
whole ruleset otherwise — check any change to these lines against that
release, not only against a newer one.

The ranges are named sets, refreshed in place by the proxy-ranges job
each time the CDN publishes a change; only turning the list on or off
needs a hardening run.

Consequences to know before enabling it:

- A direct request to the origin **times out** instead of getting the
  proxy's `403`; check with a hostname the host serves, through the CDN
  (200) and against the bare address (no answer).
- Anything that reaches the host's 80/443 without going through the CDN
  stops working: a name that resolves straight to the host, and with it
  any ACME challenge for that name. Every name such a host serves has
  to go through the CDN.
- A container dialling its own host's public address on 80/443 is
  refused as well (its source is not a CDN range). Nothing in the
  platform does this — the health probe uses loopback — but a custom
  add-on might.

### The panel checks that it is closed, and that the hardening is current

Ticking `block_direct_access` changes the proxy at the next Full Setup
or proxy push, but the firewall only at the next hardening run, and
nothing starts one by itself. The hourly cron *Alert When a Host Is Not
as Closed or as Hardened as Set* (`cron_check_host_posture`,
`models/cloud_host_posture.py`) says when the two disagree:

- **`host_origin_open`** (warning, after two runs in a row): a host with
  `behind_cdn` and `block_direct_access`, its proxy deployed, let a
  direct connection from the panel in — the panel's own
  `curl --resolve <name>:443:<ip>`, then `:80`, for the primary domain
  of one of its instances (its address when it serves none). The
  message names the port and what answered; Traefik's `403` counts, it
  means the firewall half is missing. Fix: check that the host's
  **Applied to this host** ranges are there, **Re-apply Hardening**,
  then **Push Trusted Proxy Settings**. It clears on the first run whose
  connections are left unanswered until the wait runs out while the
  same name answers through the CDN on 443 — the control that keeps a
  panel unable to reach anything directly from reading every host as
  closed. A refused connection (the proxy restarting, or down), an
  unreachable address, a drop while the CDN is out of reach from the
  panel, or a drop at a host that serves no name yet decides nothing
  that run.
- **`host_hardening_outdated`** (warning, at once): a host hardened from
  the panel whose last successful run (`hardening_recipe`,
  `hardening_cdn_only`) disagrees with what is wanted now. The message
  names the case: hardened before the panel recorded it (every host
  hardened before this check existed), set closed after its hardening
  (a host joining a pool after it, or the box ticked by hand), opened
  after it, or an older recipe — a change to the playbook other than in
  its comment lines. Fix: **Re-apply Hardening**; its success clears it.

Neither re-applies anything: re-running hardening touches a live host,
and that stays a decision. To check by hand, from outside the CDN:

```bash
$ curl -sk -o /dev/null -w '%{http_code}\n' --connect-timeout 10 \
    --resolve <name>:443:<ip> https://<name>/        # closed: 000
$ curl -s -o /dev/null -w '%{http_code}\n' --connect-timeout 10 \
    --resolve <name>:80:<ip> http://<name>/          # closed: 000
$ curl -s -o /dev/null -w '%{http_code}\n' --connect-timeout 10 \
    https://<name>/                                  # through the CDN: answers
```

A closed origin prints `000` only after the full ten seconds; an
immediate `000` (refused, unreachable) says nothing either way.

**What the probe costs.** A closed host makes the panel wait out its
timeout: 5 s on 443, 5 s on 80, then the control through the CDN —
about 10.5 s per closed host when the CDN answers at once, up to 15 s
when it does not. Hosts are probed one after another, in a single cron
run, and Odoo kills a cron that outlives its real-time limit and rolls
back what it recorded, so the check never completes. With multiple
workers, the default `limit_time_real_cron = -1` gives the cron
`limit_time_real` (120 s by default), so from about a dozen closed
hosts the run is killed every hour. Set `limit_time_real_cron = 0` (no
limit) there. With
`workers = 0`, `0` falls back to `limit_time_real`: give
`limit_time_real_cron` a positive value of at least 15 s per host
behind the CDN refusing direct traffic, plus a margin.

**A host the control cannot vouch for.** A drop is read as closed only
once the same name answers through the CDN, so a host that serves no
name yet (no instance with a domain), or whose name the panel cannot
reach through the CDN, gets no reading from it: an alert raised while
it was open stays up after it is closed. Check it by hand with the
three commands above and, once it is closed, dismiss the alert: the
**Alerts** icon in the panel header, then **Dismiss** on the alert's
card (Consultant or above; it is dismissed for every user), or a
JSON-RPC call to `/cloud/dismiss_alert` with its `alert_id`. It comes
back only after two new sightings in a row, like a new one.

**What the probe assumes.** That the panel reaches the hosts' public
addresses by the same way its lookups of the instances' names reach
the CDN. A panel with split DNS for the instances' zone, or whose
egress admits only the CDN's ranges, has the control answer while its
own network drops the direct connections, and reads every host as
closed. Run the panel with plain egress to the internet, or check the
hosts by hand from outside.

---

## Login limit and web-login ban

The general limit is the same for every path. Two controls narrow it to
the endpoints that check a password:

- **`loginlimit`** (Traefik): every instance's override adds a login
  router beside each of its routers, matching POSTs to `/web/login`,
  `/web/login/totp`, `/web/session/authenticate`, `/web/reset_password`
  and `/xmlrpc/2/common`. Page views are not counted, so an office behind
  one address opening the form spends nothing. Legitimate users hitting
  `429` on login: raise `burst` first. It applies to an instance from its
  next deploy or rebuild.
- **`incubacloud-weblogin`** (fail2ban): reads `/var/log/traefik/access.log`
  and bans for an hour an address with ten POSTs to `/web/login` (or its
  `totp` step) answered `200` in ten minutes — a failed login; a good one
  is redirected. The ban lives in fail2ban's own nftables table on the
  forward hook, 80/443 only, so a banned address keeps SSH and nothing
  outbound is touched. The addresses the SSH jail exempts are exempt
  here too. Off on a host behind a CDN, where the address is the edge's.
  Check what it holds with `sudo fail2ban-client status incubacloud-weblogin`.

## CDN client certificate (Authenticated Origin Pulls)

The CDN's addresses are shared by all of its customers, so the allowlist
above admits anybody's zone pointed at our origin. A host behind the CDN
with direct access refused can be given the authority the CDN's client
certificate is signed by (Host → CDN client certificate authority, or the
zone's from Settings); the proxy then requires it on every TLS
connection. Turning it on: the CDN must already be presenting the
certificate, or the host refuses the CDN itself — for the zone's own,
Settings does it in two steps and the second waits for Cloudflare to
report the certificate deployed. Check with `curl` against the origin:
without the client certificate the handshake fails.

---

## Layer 1 — the provider's L3/L4 anti-DDoS (a dependency, not code)

Volumetric and protocol floods are absorbed upstream, in the provider's
network. Hetzner and OVH include this free and automatically; it is
**assumed**, not something we run.

- **Adding a provider without it** removes the volumetric layer. Do not
  front public instances on such a provider, or add an equivalent
  (e.g. Cloudflare Spectrum) in front of it. Record the decision — a
  silent gap here is invisible until the flood arrives.

---

## Rollback

- **Traefik limit**: set the previous `average`/`burst` back in
  `~/traefik/config.yml` (and the host record); it reloads live. To
  remove the limit entirely you would drop `ratelimit@file` from the
  https entrypoint in `~/traefik/traefik.yml` — rarely what you want,
  since it is a fleet-wide default.
- **nftables cap**: set the host's rate to 0 and re-run hardening; the
  meter block disappears and the forward chain returns to `policy
  accept`.
- **Login limit**: raise `average`/`burst` of `loginlimit` in
  `~/traefik/config.yml` (and the host record); it reloads live. A login
  router whose middleware is missing is dropped by Traefik on its own, and
  the login falls back to the instance's ordinary routers.
- **Web-login ban**: `sudo fail2ban-client set incubacloud-weblogin unbanip <ip>`
  lifts one ban; `enabled = false` in
  `/etc/fail2ban/jail.d/incubacloud-weblogin.local` and
  `sudo systemctl reload fail2ban` stops the jail until the next hardening
  run.
- **CDN client certificate**: clear the host's authority (or stop it from
  Settings, for the zone's) and push the proxy settings; the document is
  removed and the proxy accepts any connection the firewall lets in.
- **CDN allowlist**: turn off `block_direct_access` (or `behind_cdn`)
  on the host and re-run hardening; the forward chain returns to
  `policy accept`, or to the cap if a rate is set.

## References

- [`docs/architecture.md`](../architecture.md) — Host edge protection
  (the layered model).
- [`data/traefik/config.yml`](../../incubacloud/data/traefik/config.yml)
  — the `ratelimit` middleware; `traefik.yml` references it on the https
  entrypoint.
- [`models/cloud_host.py`](../../incubacloud/models/cloud_host.py) —
  `_add_traefik_ratelimit_middleware`, `_add_traefik_entrypoint_ratelimit`
  (the idempotent retrofit onto existing hosts).
- [`ansible/playbooks/host_hardening.yml`](../../incubacloud/ansible/playbooks/host_hardening.yml)
  — the gated nftables conn-rate meter.
- [`models/cloud_host_posture.py`](../../incubacloud/models/cloud_host_posture.py)
  — the hourly check behind `host_origin_open` and
  `host_hardening_outdated`.
- [RB-04](RB-04-tune-rate-limits.md) — the application-level counters.
