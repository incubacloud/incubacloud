# Panel on an internal network

A self-hosted panel and its hosts can live on a network the internet does not
reach: a VM in your office, a server behind a VPN. Most of the panel works the
same there. This page covers what does not: names and certificates for the
instances, AI agents, and the features that need a way in from the internet.

Installing the panel itself is in the repository's
[INSTALL.md](https://github.com/incubacloud/incubacloud/blob/19.0/INSTALL.md);
preparing a host, in [Connect your own VPS](connect.md).

## What needs the internet

| Feature | On an internal network |
|---|---|
| **Let's Encrypt certificates** | The host's proxy proves each name to Let's Encrypt through a connection to its port 443, which never arrives. Use a certificate you obtain yourself: [Certificates](#certificates). |
| **Rebuild on push, pull request previews** | They start from GitHub's notifications, which cannot reach a private address: **Auto-rebuild** never fires. Deploy and **Rebuild** by hand after a push; both work. |
| **Custom connectors in Claude** (claude.ai and the Claude apps) | Claude calls them from Anthropic's servers, which need a public HTTPS address. Connect a client that runs on your computer instead: [AI agents](#ai-agents). |
| **Restore from a link** | The host only downloads from public addresses. Restore through the browser or over SSH. |

The hosts still need **outbound** HTTPS:

- container images from Docker Hub (Traefik and its socket proxy) and from
  `ghcr.io` (Tecnativa's Odoo, PostgreSQL, backup and egress images), whose
  layers come from `pkg-containers.githubusercontent.com`;
- code from GitHub (your repositories, OCA's and the doodba template) and
  Python packages from PyPI;
- during **Full Setup**, also `get.docker.com` and `download.docker.com`
  (Docker's install script and packages) and the distribution's apt mirrors;
- with host monitoring, its agents' images from Docker Hub, `quay.io` and
  `gcr.io`.

## Names: the wildcard domain

**Host → Traefik → Wildcard domain** is the domain the host's instances are
named under when you give them no name of your own. Write it with or without
the leading `*.`: `*.odoo.example.com` and `odoo.example.com` mean the same.

| Name | For |
|---|---|
| `<project folder>-<instance name>.odoo.example.com` | An instance created without a domain: the panel adds this row to its **Networking → Domains**. Without a project folder, `<instance name>.odoo.example.com`. |
| `traefik.odoo.example.com` | The proxy's dashboard, behind the host's **Panel password**. |

The panel creates no DNS records. On your network's DNS server, point the whole
domain at the host — one wildcard record, `*.odoo.example.com → 192.0.2.20`, or
with dnsmasq:

```text
address=/odoo.example.com/192.0.2.20
```

An instance can also answer to names outside the wildcard domain (**Networking
→ Domains**); each needs its own DNS record, and a certificate that covers it.

## Certificates

Each domain row has a **Certificate** choice
([what each one does](../instances/index.md#custom-domains-and-certificates)).
On a host the internet cannot reach:

| Certificate | On an internal network |
|---|---|
| **Automatic** (default) | Resolves to Let's Encrypt, as the host is not behind a CDN, and gets no certificate. |
| **Let's Encrypt** | Gets no certificate. |
| **Existing certificate** | Serves the certificate the host holds, its [default certificate](index.md#the-hosts-default-certificate). **The one to use.** |
| **No TLS** | Not plain HTTP: the proxy still redirects to HTTPS, and visitors get a self-signed certificate. |

### When the certificate does not cover a name

The deploy log says, per domain, what it got:

- `TLS for app.odoo.example.com: host's certificate` — covered.
- `⚠ app.odoo.example.com: the host's certificate does not cover this name;
  browsers will warn.` — the deploy goes on, and the host serves its
  certificate for a name it does not cover: browsers show a warning.
- *These domains ask for the host's certificate and the host holds none* — the
  deploy stops before touching the host. Load a certificate first.

A wildcard certificate for `*.odoo.example.com` covers every name one level
under it — all the names the panel generates — but not `odoo.example.com`
itself, nor `a.b.odoo.example.com`.

### A wildcard certificate by DNS, with Cloudflare

Let's Encrypt can check that a domain is yours through a TXT record in its
public DNS instead of a connection to your server (the DNS-01 challenge), and
it issues **wildcards** only that way. The host's proxy does not do it for you:
you obtain the certificate on any machine with Docker and internet access, and
load it on the host. The domain must be a zone in your Cloudflare account; the
records your network uses can stay on your internal DNS.

**1. A Cloudflare API token.** In Cloudflare, **My Profile → API Tokens →
Create Token**, with the permission **Zone → DNS → Edit**, for that zone only.

**2. Ask for the certificate.** On that machine:

```bash
mkdir -p ~/certbot
printf 'dns_cloudflare_api_token = %s\n' '<your token>' > ~/certbot/cloudflare.ini
chmod 600 ~/certbot/cloudflare.ini

docker run --rm -v ~/certbot:/etc/letsencrypt certbot/dns-cloudflare:v5.9.0 \
  certonly --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/cloudflare.ini \
  -d 'odoo.example.com' -d '*.odoo.example.com' \
  -m admin@example.com --agree-tos --non-interactive
```

The chain and its key land in `~/certbot/live/odoo.example.com/`:
`fullchain.pem` and `privkey.pem`.

**3. Load it on the host.** **Host → Traefik**: paste `fullchain.pem` under
**Default certificate** and `privkey.pem` under **Default certificate key**,
and save. The tab then shows the names it covers and when it expires. **Push
Trusted Proxy Settings** (or **Full Setup**) ships it to the host.

**4. Switch the domains.** On each instance, **Networking → Domains**, set
**Certificate** to *Existing certificate* — the row the panel generated
included — and rebuild or deploy the instance.

!!! warning "Nothing renews it"
    A certificate loaded this way is renewed by neither the panel nor the
    host. Before the expiry date the Traefik tab shows, run the same
    container with `renew` in place of `certonly …`, paste the new pair and
    push it again; the instances need no rebuild.

The same steps load a certificate from your own certificate authority or one
you bought: on an internal network, any certificate your users' browsers trust
will do. Several certificates on one host are loaded by hand:
[RB-12](https://github.com/incubacloud/incubacloud/blob/19.0/docs/runbooks/RB-12-custom-domain-certificate.md).

## AI agents

An agent works on the panel through its MCP address, as you, with your role
([Agents](../agents/index.md)). On an internal network, connect it from a
client that runs on your own computer, with an API key.

**1. The MCP address.** **Settings → Agents (MCP) → MCP address** is the address
the *Connect an agent* page puts in its commands. Left empty, it is the panel's
own address (`web.base.url`) followed by `/mcp`, and Odoo rewrites that address
each time a user with access to Odoo's Settings signs in: it becomes the one in
that user's browser, `http://localhost:8069` after a sign-in through an SSH
tunnel, for instance. Set the address your computers use, for example
`http://192.0.2.10/mcp`; or set `web.base.url` to the panel's address and
add the system parameter `web.base.url.freeze` with the value `True`, which
stops the rewrite. Plain `http` is accepted for the MCP. Connecting the GitHub App needs the panel served over HTTPS, with any certificate your browsers trust; over plain HTTP the panel refuses it.

**2. An API key**, created in your Odoo preferences:
[Self-hosted panel without OAuth](../agents/connect.md#self-hosted-panel-without-oauth).

**3. Claude Desktop through `mcp-remote`.** Claude Desktop starts local MCP
servers from its configuration file, and
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) is one that relays to a
remote address with a header on every request. It needs Node.js (`npx`) on the
computer. In Claude Desktop, **Settings → Developer → Edit Config** opens
`claude_desktop_config.json`; add:

```json
{
  "mcpServers": {
    "incubacloud": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "http://192.0.2.10/mcp",
        "--allow-http",
        "--header",
        "Authorization:${AUTH}"
      ],
      "env": {
        "AUTH": "Bearer <API_KEY>"
      }
    }
  }
}
```

- `--allow-http`: without it `mcp-remote` refuses plain `http` to anything but
  your own machine. The key then travels unencrypted: use it only on a network
  you trust, and drop the flag once the panel is behind HTTPS.
- `Authorization:${AUTH}` has no space after the colon on purpose; `mcp-remote`
  fills in `${AUTH}` from `env`, where the space of `Bearer <API_KEY>` is safe.
  `--header-file` reads the header from a file instead.

Restart Claude Desktop. Other clients that run on your computer — Claude Code,
Cursor, VS Code, Codex — connect to the address directly and send the key as a
header: the commands are on the *Connect an agent* page.

**4. Approvals.** Every command an agent wants to run on a server or on a
production instance, and every deletion or restore, waits for you, showing the
exact command. You answer in the client, or open the link the agent gives you
in a browser signed in to the panel. See [Approvals](../agents/confirmations.md).

## Verify it worked

```bash
openssl s_client -connect app.odoo.example.com:443 -servername app.odoo.example.com </dev/null 2>/dev/null \
  | openssl x509 -noout -issuer -subject -dates
```

- [x] The issuer is Let's Encrypt (or your authority) and the dates are the ones
  the Traefik tab shows; `TRAEFIK DEFAULT CERT` means no certificate on the host
  covers the name.
- [x] Asked to list your projects, the agent answers with them.

## Troubleshooting

| What you see | Why, and what to do |
|---|---|
| The agent gets `401` | The key is wrong, revoked or expired, or `Bearer ` is missing in `AUTH`. |
| `404` on `/mcp` | Odoo cannot tell which database the request is for: select the panel's database by name, with `db_name` or `dbfilter = ^<name>$` ([INSTALL.md](https://github.com/incubacloud/incubacloud/blob/19.0/INSTALL.md#the-odoo-settings-that-matter)). |
| `mcp-remote` refuses the `http://` address | Add `--allow-http`. |
| The commands on *Connect an agent* name `localhost:8069` | Set the MCP address (step 1). |
| A push does not rebuild the instance | Expected without public access: rebuild by hand. |
