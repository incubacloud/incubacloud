# Connect your own VPS

Bring any VPS — Hetzner, OVH, on-premises — over SSH. We deploy the proxy
and request HTTPS automatically.

## Prerequisites

- A VPS with **Ubuntu 22.04** or newer (Debian 12 also works).
- SSH access for a user with passwordless `sudo` that can use Docker: `root`,
  or a user prepared as in [The user the panel connects as](#the-user-the-panel-connects-as).
- Ports **80 and 443 free**: Full Setup puts the proxy there
  ([what it changes](#what-full-setup-changes-on-the-host)).
- Outbound HTTPS from the VPS to Docker Hub, `ghcr.io`, GitHub and PyPI, and
  during Full Setup to Docker's and the distribution's package servers:
  [the full list](internal-network.md#what-needs-the-internet).

!!! info "A server only your network reaches"
    For a host and a panel without public access — an office VM — see also
    [Panel on an internal network](internal-network.md): names, certificates,
    and what needs the internet.

## The user the panel connects as

The panel needs a user that can:

- **run `sudo` without a password** — Full Setup installs packages and writes
  under `/etc`, and hardening checks `sudo -n` before it starts;
- **use Docker without `sudo`** — deploys run `docker compose` directly, so the
  user must be in the `docker` group.

`root` meets both. For a dedicated user, run this on the VPS, with the public
key the panel shows (step 3 below) in place of `ssh-ed25519 AAAA...`:

```bash
sudo useradd --create-home --shell /bin/bash incubacloud
echo 'incubacloud ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/incubacloud >/dev/null
sudo chmod 440 /etc/sudoers.d/incubacloud
sudo visudo -cf /etc/sudoers.d/incubacloud

# Created before Docker is: a Docker that Full Setup installs later
# already counts this user in.
sudo groupadd -f docker
sudo usermod -aG docker incubacloud

sudo install -d -m 700 -o incubacloud -g incubacloud /home/incubacloud/.ssh
echo 'ssh-ed25519 AAAA... incubacloud' | sudo tee -a /home/incubacloud/.ssh/authorized_keys >/dev/null
sudo chown incubacloud:incubacloud /home/incubacloud/.ssh/authorized_keys
sudo chmod 600 /home/incubacloud/.ssh/authorized_keys
```

Full Setup also adds the user to the `docker` group, but a group counts from
the user's next login: add it beforehand, as above.

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
mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo 'ssh-ed25519 AAAA... incubacloud' >> ~/.ssh/authorized_keys
```

Keep this page open until you create the host: the key is saved with it. Once
you have copied the key, the panel asks before you leave the page.

To use a key of your own instead, choose **Upload my own private key** and
upload the private half in **SSH Private Key**. It must have no passphrase (the
panel has no field for one).

The host's **Connection** tab keeps showing the public key, so you can copy it
again later.

Create the host, then click **Trust SSH Key** in its action bar. Green = we
got in, and the panel keeps the host's key to check it on every connection.

### 4. Run `Check`

This probes the host: kernel, distro, free disk, RAM, Docker presence.
Anything missing shows up so you can decide whether to install it manually
or let us run **Full Setup**.

The Check ends with a verdict on the host's status:

| Status | When | What it means |
|---|---|---|
| `Compatible` | Linux, 10 GB free or more, every tool in place. | Ready for instances. |
| `Degraded` | Under 10 GB free, or a tool missing. An alert says which. | The instances already there keep running. With little disk, no new one is placed on it and a build may run out of space: free space. For a missing tool, run **Full Setup**. |
| `Unsupported` | Not Linux, or the Check could not reach or read the host. | Nothing is deployed there. |

The job fails only when the Check could not measure the host. Once 11 GB are
free, the low-disk alert closes by itself within minutes; a Check closes it
from 10 GB. **Full Setup** on a disk under 10 GB fails and leaves the host
`Degraded` with the same alert.

### 5. Run `Full Setup`

We install Docker (if missing), drop in Traefik, wire up cert auto-renewal,
and run a final probe. Watch the log to follow each step.

When it ends in green, the host is ready to receive instances.

## What Full Setup changes on the host

Every step checks before it changes anything, so running Full Setup again is a
repair, not a reinstall.

| Where | What |
|---|---|
| Packages | Installs git, Docker CE (through `get.docker.com`), the Compose plugin, `python3-pip`, `python3-venv`, pipx, zip and logrotate, when missing. |
| The user | pipx installs copier (pinned), invoke and pre-commit in `~/.local/bin`, added to `PATH` in `~/.bashrc`; `git config --global init.defaultBranch master` if unset; the user joins the `docker` group. |
| `/etc/docker/daemon.json` | Only while no container exists: Docker's bridge at 172.17.0.1/16 and an address pool of 10.200.0.0/14 in /24 networks, so the host can hold many instances (each takes a network). Docker restarts. With containers running, it is left alone. |
| `~/traefik/` | The proxy: Traefik and a read-only proxy of the Docker socket, Compose project `inverseproxy`, publishing **ports 80 and 443** (and 8082 on the loopback, for metrics). Its settings are the host's **Traefik** tab. |
| `/var/log/traefik/` | The proxy's access log, rotated daily and kept 30 days. |
| `~/globalwhitelist/` | The proxies of the host's **Whitelist** tab (Compose project `globalwhitelist`): the names stagings may reach. |

Each instance you deploy later lives in `~/<project folder>/<instance name>/`
of the same user.

!!! warning "A web server already on 80 or 443"
    The proxy must own ports 80 and 443, and the panel has no setting to move
    it. With nginx, Apache or anything else bound there, *Start Traefik* fails.
    See what holds them with
    `sudo ss -tlpn | grep -E ':(80|443)\s'`, and move that service to other
    ports or to another machine first.

!!! warning "Addresses your network already uses"
    Docker's networks on the host come from 172.17.0.0/16 and 10.200.0.0/14,
    and the host routes those ranges to its containers. If machines of your
    network have addresses there, the host cannot reach them. Check before you
    run Full Setup: the panel has no setting for these ranges.

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
deployment**. Set DNS first, then deploy. A host the internet cannot reach never
gets one this way: see [Panel on an internal network](internal-network.md#certificates).

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

??? note "Full Setup fails on Docker with *permission denied* on its socket"
    The user is not in the `docker` group for this connection yet: add it as
    in [The user the panel connects as](#the-user-the-panel-connects-as), and
    run Full Setup again.

??? note "Hardening refuses to start: sudo asks for a password"
    The user needs `NOPASSWD` in its sudoers line.

## See also

- [Hire a Hetzner VPS](hire.md) if you don't want to bring your own.
- [Hosts overview](index.md).
