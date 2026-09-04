# Hosts

A **host** is a server (VPS or physical) where instances run. You can connect
your own VPS or order one through the platform.

## Two paths

- **Connect your own** — for any VPS you already have. Hetzner, OVH, DigitalOcean,
  AWS EC2, on-premises — anything that exposes SSH.
  [:octicons-arrow-right-24: Walkthrough](connect.md)
- **Order one through the platform** *(SaaS, paid plans)* — we provision a Hetzner
  VPS, lock down SSH, configure the proxy and register it ready to use. Billed by
  the hour with a monthly cap, minus your plan's discount.
  [:octicons-arrow-right-24: Walkthrough](hire.md)

## What we install

Once a host is registered and you run **Full Setup**, we deploy:

- **Docker + docker-compose** (if missing).
- **Traefik** as the network proxy. It terminates HTTPS and routes traffic to
  the right instance based on the domain.
- **Let's Encrypt cert manager**. HTTPS is automatic when you add a custom domain.

The host stays under your control — root access is yours. VPS ordered through
the platform are billed on your IncubaCloud subscription invoice.

## Hosts behind a CDN

A host can be reached by its visitors directly, or through a CDN or reverse
proxy that answers for it. The **General** tab has a switch for this, and it
decides three things:

- **Who counts as a visitor.** With the switch on, the rate limit counts the
  client the CDN forwards; with it off, the address that opened the connection.
  Setting it on a host the world reaches directly is the mistake that costs
  most: a direct request carries no forwarded chain, so every visitor is
  counted as the same one and they all share a single limit.
- **Whether the origin may be reached at all.** With trusted proxy ranges set,
  *Only accept traffic from the proxies applied above* refuses anything that
  did not come through them, so the host's address cannot be used to go around
  the edge.
- **Where the certificate comes from.** A CDN terminates TLS, so the challenge
  a host uses to obtain its own certificate never reaches it. Such a host is
  given a certificate instead — an origin certificate covering the whole
  domain, which only ever has to satisfy the CDN.

!!! warning "Order matters"
    Give the host its certificate and its trusted ranges *before* pointing the
    domain at the CDN, and refuse direct traffic only once traffic is actually
    arriving through it. The other order leaves the host answering nobody.

## Common tasks

- [Connect your own VPS](connect.md)
- [Order a VPS through the platform](hire.md)
- Check host health (Host detail → Check)
- Whitelist external domains that may proxy through (Host detail → Whitelist)
- Audit who did what on the host (Host detail → Audit Logs)
- Import an instance that was already running on the host
  (Host detail → Import Instance)

!!! note "Reference page is in progress"
    A full per-screen reference for the Host detail UI is coming soon.
