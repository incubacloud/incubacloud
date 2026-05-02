# Connect your own VPS

Bring any VPS — Hetzner, OVH, on-premises — over SSH. We deploy the proxy
and request HTTPS automatically.

## Prerequisites

- A VPS with **Ubuntu 22.04** or newer (Debian 12 also works).
- SSH access for a sudo-capable user. The user can be `root`.
- Outbound HTTPS allowed from the VPS (we pull container images from Docker Hub
  and the OCA mirror).

## Walkthrough

### 1. Open the hosts page

Go to `/cloud/ui/hosts`. Click **New Host**.

### 2. Fill in connection details

- **Name** — whatever helps you remember it (e.g. `customer-foo-prod`).
- **IP address or hostname** — the public IP works fine.
- **SSH port** — usually `22`.
- **Username** — `root` is fine; a sudo-capable user is also fine.
- **Auth method** — **SSH key** (recommended) or password.

### 3. SSH key (recommended path)

We generate an Ed25519 keypair for you. Copy the displayed public key.

On the VPS, append it to the chosen user's `~/.ssh/authorized_keys`:

```bash
echo "ssh-ed25519 AAAAC3..." >> ~/.ssh/authorized_keys
```

Click **Test connection**. Green = we got in.

### 4. Run `Check`

This probes the host: kernel, distro, free disk, RAM, Docker presence.
Anything missing shows up so you can decide whether to install it manually
or let us run **Full Setup**.

### 5. Run `Full Setup`

We install Docker (if missing), drop in Traefik, wire up cert auto-renewal,
and run a final probe. Watch the log to follow each step.

When it ends in green, the host is ready to receive instances.

## DNS for HTTPS

For Let's Encrypt to issue a cert when you deploy a domain on this host,
the domain must already point (A or AAAA) to the VPS IP **at the moment of
deployment**. Set DNS first, then deploy.

## Troubleshooting

??? note "Connection refused"
    - Check the VPS firewall (`ufw` is most common): `sudo ufw status`.
    - Check the SSH service: `sudo systemctl status ssh`.
    - Confirm the public key is in **that user's** `authorized_keys`, not just
      `root`'s. Mismatched users is the most frequent cause.

??? note "Docker install failed"
    Most often a kernel too old for the modern Docker engine. Upgrade Ubuntu
    to 22.04+ or Debian to 12+. We don't support older kernels.

??? note "Full Setup gets stuck on Traefik"
    Check that ports 80 and 443 are free on the host (no nginx, apache, or
    other webserver running): `sudo ss -tulpn | grep -E ':80|:443'`.
    If something is bound, stop it first.

## See also

- [Hire a Hetzner VPS](hire.md) if you don't want to bring your own.
- [Hosts overview](index.md).
