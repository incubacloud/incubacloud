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
