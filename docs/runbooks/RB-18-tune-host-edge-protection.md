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
| Host conn-rate (nftables) | `host_hardening.yml`, forward hook | **off** | no (extra-var + re-run) |
| Proxy per-IP limit (Traefik) | host `config.yml`, `ratelimit` middleware | 300/min, burst 100 per IP | **yes** (file is watched) |
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

## Layer 2 — enable the nftables connection-rate cap (rehearse first)

An optional per-source new-connection limit on 80/443, in the hardening
ruleset. It sits on the **forward** hook, because tenant traffic is
DNAT'd to the Traefik container and never crosses the input chain. It is
**off by default** and must stay that way until rehearsed:

> An unrehearsed drop on the forward hook is exactly what took the whole
> fleet down on 2026-08-14. Do not enable it on a live host you have not
> rehearsed the change on.

### Rehearse on a throwaway VPS

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

With `ic_http_conn_rate` unset the forward chain is the plain
`policy accept` every host already runs — the ruleset is byte-for-byte
the current one. With a rate set it adds, per family:

```
meta nfproto ipv4 tcp dport { 80, 443 } ct state new \
    meter ic_http_conn4 { ip saddr limit rate over 50/second burst 50 packets } drop
meta nfproto ipv6 tcp dport { 80, 443 } ct state new \
    meter ic_http_conn6 { ip6 saddr limit rate over 50/second burst 50 packets } drop
```

Only **new** connections over the rate are dropped; established and
under-rate traffic falls through to Docker's own forward rules. The
ruleset still uses declare-then-delete of our own table, never
`flush ruleset`.

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
- **nftables cap**: unset `ic_http_conn_rate` and re-run hardening; the
  meter block disappears and the forward chain returns to `policy
  accept`.

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
- [RB-04](RB-04-tune-rate-limits.md) — the application-level counters.
