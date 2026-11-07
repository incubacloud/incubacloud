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

Go to `/cloud/hosts`. Click **Add Host**.

### 2. Fill in connection details

- **Name** — whatever helps you remember it (e.g. `customer-foo-prod`).
- **IP Address** — the public IP works fine.
- **Port** — the SSH port, usually `22`.
- **User** — `root` is fine; a user with passwordless `sudo` is also fine.
- **Login Type** — **SSH Key** (recommended) or **Password**. Hardening
  needs a key: it copies the user's authorized keys to the account it creates.
- **Wildcard Domain** — the domain your instances get their names under
  (`example.com` or `*.example.com`): each instance answers at a name such as
  `myproject-prod.example.com`. Point a wildcard DNS record (`*.example.com`)
  at the host's IP before you deploy.

### 3. SSH key (recommended path)

On the **Connection** tab, **Generate a key for me** is selected: the panel
creates a key pair for this host and shows its **public key**, with a button to
copy it. The private half stays in the panel, stored encrypted.

Put the public key on the VPS, as the user you entered:

- if you have not created the server yet, paste it in your provider's SSH key
  field when you create it, then come back and fill in its IP address;
- if the server already exists, run the command the panel shows under the key,
  which appends it to that user's `~/.ssh/authorized_keys`:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo "ssh-ed25519 AAAA... incubacloud" >> ~/.ssh/authorized_keys
```

Keep the form open while you do it: the key is saved with the host.

To use a key of your own instead, choose **Upload my own private key** and
upload the private half. It must have no passphrase (the panel has no field for
one).

The host's **Connection** tab keeps showing the public key, so you can copy it
again later.

Create the host, then click **Trust SSH Key** in its action bar. Green = we
got in, and the panel keeps the host's key to check it on every connection.

### 4. Run `Check`

This probes the host: kernel, distro, free disk, RAM, Docker presence.
Anything missing shows up so you can decide whether to install it manually
or let us run **Full Setup**.

### 5. Run `Full Setup`

We install Docker (if missing), drop in Traefik, wire up cert auto-renewal,
and run a final probe. Watch the log to follow each step.

When it ends in green, the host is ready to receive instances.

## Hardening (optional)

The first time you trust a new host's SSH key, the panel offers to harden it.
You can also do it at any time with **Apply Hardening** in the host's action
bar; a host that has not been hardened shows **Not hardened** next to its
name.

Hardening applies a security recipe on the host:

- a non-root operator user when you connect as `root`, and SSH on a rotated
  port, key-only, with root login disabled at the end;
- an nftables firewall that accepts only SSH, HTTP and HTTPS, with a cap of
  new connections per visitor's address (50 a second by default, set on the
  host's Advanced tab);
- fail2ban: an address that fails SSH three times, or the Odoo login form ten
  times in ten minutes, is banned for an hour (the login ban is off on a host
  behind a CDN);
- kernel `sysctl` hardening, automatic security updates (with a reboot at
  04:00 when a kernel update lands), a hardened Docker daemon, journald limits
  and auditd rules, with the audit log kept for weeks.

Every instance also gets a stricter limit on its login endpoints (20 attempts
a minute per address) on top of the general one, from its next deploy or
rebuild. The proxy's access log is kept 30 days on the host.

Before you apply it:

- It needs Ubuntu 22.04+ or Debian 12+, and `root` or a user with passwordless
  `sudo`.
- **Services installed directly on the server, outside Docker, stop being
  reachable from outside**: the firewall only lets SSH, HTTP and HTTPS in.
- The first run restarts Docker, a 5–10 second interruption for the instances
  already running there.
- If the host has never been set up and you connect as `root`, choose
  **Hardening + Full Setup**: Full Setup then installs its tools in the new
  user's home instead of root's.
- **Who can open SSH afterwards**: the dialog says it. A panel that knows its
  own address allows only itself and the host's allowed SSH IPs; tick
  **Also allow my IP** to add yours. A panel that does not know its address
  leaves SSH open to any address, key-only.

Afterwards the panel reaches the host as the operator user on the new port.
Re-running it is safe: every step checks the host before it changes anything.

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
