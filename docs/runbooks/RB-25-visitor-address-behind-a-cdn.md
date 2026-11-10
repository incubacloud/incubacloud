# RB-25: The visitor's address behind a CDN

**Severity:** planned
**Typical trigger:** putting a panel behind a CDN; sessions, the device
list or failed sign-ins show the CDN's addresses instead of the
visitors'; Odoo shares its Docker network with other containers; the log
says `[real-ip] the proxy … does not resolve`.
**Who runs it:** whoever runs the panel.

Odoo records the last entry of `X-Forwarded-For` as the visitor. With a
CDN and a reverse proxy in front, that entry is the CDN's edge: every
session, every device and Odoo's own lock-out after failed sign-ins key
on a handful of the CDN's addresses. Nothing fails, so nothing says so.

`incubacloud` corrects it from the header the CDN states the visitor in,
when its options are set in the Odoo configuration file:

| Option | Value | Without it |
|---|---|---|
| `x_real_ip_header` | the CDN's header, e.g. `CF-Connecting-IP` | nothing is corrected |
| `x_real_ip_trusted` | the CDN's published ranges, commas or spaces | nothing is corrected |
| `x_real_ip_proxy` | the reverse proxy's container name or address | see below |

The header is believed only when the reverse proxy connected to Odoo and
the last entry of the chain — the one the proxy appends from the
connection it accepted — is one of the CDN's addresses (or when the CDN
connects to Odoo itself). Whoever else connects to Odoo has its chain
replaced by its own address, so it cannot choose one.

**The CDN's ranges must be public.** A private network in
`x_real_ip_trusted` is ignored, with a warning at start-up, and an
address is taken for the CDN's only if it is public itself: otherwise
anything on Odoo's own network could speak as the CDN. A host's own
*Trusted Proxy Ranges* win over the CDN's list, so this is where a
private range typed there would end up. A public range that spans
private addresses, such as `0.0.0.0/0`, is kept but pointed out: none of
its private addresses counts as the CDN's.

## Who counts as the proxy

- **With `x_real_ip_proxy`**, only that container or address. A name is
  resolved through Docker's own resolver, so Odoo has to share a network
  with it; the answer is reused for 30 seconds and asked again after 5
  when a connection comes from an address it did not include. End the
  name with a dot (`inverseproxy-proxy-1.`) to make it absolute: the
  resolver then never tries it under a search domain, where it could be
  somebody else's.
- **Without it**, any connection from loopback or a private network
  (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`). That is the proxy,
  and also **every other container on a network Odoo shares**: any of
  them can set the address it is recorded as, as Odoo's `proxy_mode`
  already lets it with `X-Forwarded-For` alone.

**If Odoo shares its network with anything but its proxy, set
`x_real_ip_proxy`.** That is the case wherever several panels, or other
services, share `inverseproxy_shared` on the same host.

### When the name does not resolve

The name is looked up once as the server starts; if it does not
resolve, a **warning** is logged there and then.

- Once it has resolved, a failure — whatever the resolver answers —
  keeps the last address that resolved, for five minutes at most. Which
  error a missing container gets depends on Docker's version, so it says
  nothing about whether the proxy is gone. During those five minutes the
  kept address counts as the proxy even if Docker has already given it
  to another container on the network; that is one address, still
  narrower than the fallback below, where every private address counts.
- A name that never resolved, or one still unresolved after those five
  minutes, falls back to **without it** above: any private address
  counts as the proxy. Visitors are still recorded as themselves; what is
  lost is the guarantee that no neighbour on the network can choose the
  address it is recorded as. While a request comes in, the name is asked
  again every five seconds.
- When a request arrives and finds the fallback in use, an **error** is
  logged, at most every ten minutes. That is the one to act on: this
  server is recording visitors under the weaker guarantee.
- The name counts again as soon as it resolves.

It does not fall back to believing nobody: every visitor would then be
recorded as the proxy's own address, and every limit keyed on the
visitor — sign-up, OIDC, the routes of modules on top, Odoo's lock-out
after failed sign-ins — would be one bucket that a single caller could
fill for everybody.

A warning at start-up with no error after it is expected wherever the
image runs without Traefik and serves no proxied request:

- a separate **job runner** that loads the same configuration and is
  kept off the proxy's network on purpose — it warns on every start;
- stacks built from the same image without Traefik in front, such as a
  restore drill;
- a development copy that loads the production configuration and has
  no such container.

A CI that does not load that configuration installs nothing: without
the options, nothing is corrected.

## What the platform sets

- **The panel itself**: set the three options in its Odoo
  configuration file (with doodba, a file under `odoo/custom/conf.d/`):
  the header, the CDN's ranges and, behind doodba's Traefik,
  `x_real_ip_proxy=inverseproxy-proxy-1.`, the name `docker compose -p
  inverseproxy` gives Traefik's container.
- **A panel the panel deploys on a host behind the CDN**: the deploy writes all three
  into the panel's configuration (`DeployInstanceExecutor._conf_content`),
  the proxy being the host's Traefik container, `inverseproxy-proxy-1.`.
  A host that is not behind a CDN gets none of them. A panel deployed
  before the option existed runs **without it** until its next deploy
  rewrites its configuration.
- **Any other instance** gets the options too, but nothing in it reads
  them: the correction is part of `incubacloud`, which such an instance
  does not run. Its Odoo keeps recording the CDN's edges.

The container name rather than the service alias `proxy` avoids picking
the wrong container by accident. It is not what makes the guarantee
hold: any compose project on the network could declare the same name as
an alias. What does is that only the panel writes compose files onto
`inverseproxy_shared`.

## Check it

On the panel, after a restart, from a `psql` session on its database
(`db$`, see the conventions in the index):

```
db$ SELECT ip_address, count(*) FROM res_device_log
      WHERE last_activity > now() - interval '1 hour'
      GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

Or, in developer mode, **Settings → Technical → Security → User
Devices** (`/odoo/user-device`). Visitors'
addresses, not `162.158.*`, `172.64-71.*`, `104.16-27.*` and the rest of
Cloudflare's list. The server logs one line at start-up:

```
[real-ip] honouring CF-Connecting-IP from 22 trusted network(s), through inverseproxy-proxy-1.
```

An **error** `[real-ip] the proxy inverseproxy-proxy-1. does not
resolve` means a server that does serve visitors does not share a
network with that container, or it is not running: it is recording them
under the weaker guarantee until the name resolves. The same text as a
**warning** at start-up, with no error after it, is the job runner or
another server that serves no proxied request; see above.

## Rollback

Remove `x_real_ip_proxy` from the configuration and restart: any
private address counts as the proxy again, with the limit described
above. Remove all three options to stop correcting altogether; Odoo then
records the CDN's edges, as it did before.

On a panel the configuration is the deploy's: an edit by hand on the
host lasts until the next deploy writes it again. Change
the host instead (its *Real Client IP Header* and *Trusted Proxy Ranges*,
or whether it is reached through a CDN) and deploy.

## See also

- The CDN's origin certificate, the one hosts behind the CDN serve: it
  goes in each host's default certificate store.
- `incubacloud/net/real_ip.py` — the rule, and why each part of it is
  needed.
