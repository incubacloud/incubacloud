# RB-12 — Serve a domain with an existing (non-Let's Encrypt) certificate

**Trigger:** a customer needs a domain served with a certificate you
already hold — a wildcard, an EV cert, or one issued by their own CA —
instead of the per-domain Let's Encrypt certificate the panel issues by
default.

**What it means:** on the instance's domain row you set **Certificate**
to *Existing certificate*. That tells the tenant's router to enable TLS
**without naming an ACME resolver**, so Traefik serves a certificate its
own store already holds for that hostname — the host's default
certificate, or one loaded by hand.

!!! warning "Selecting the option is only half the job"
    The option does not upload anything. Load the certificate on the
    host *first*, then switch the domain over. Every deploy, rebuild and
    warm claim checks this before touching the host: if the host holds
    no certificate at all it **stops** and names the domain; if the
    host's default certificate does not cover the name it logs a
    warning. A certificate loaded by hand (step 2B) cannot be read from
    the panel, so for that one the log only says it was trusted.

## 1. Decide whether you actually need this

| Situation | Use |
|---|---|
| Ordinary customer domain | **Automatic** (default). Nothing to do: the host asks Let's Encrypt, or serves its own certificate when a CDN answers for the name and that certificate covers it. |
| One certificate covering many subdomains (`*.example.com`) | *Existing certificate* + this runbook |
| Certificate issued by the customer's own CA | *Existing certificate* + this runbook |
| Domain behind another TLS terminator, panel edge should not do TLS | *No TLS* — but read step 5 first, it does not mean what it sounds like |

The panel's own ACME resolver is `letsencrypt`, using the TLS-ALPN-01
challenge. **It cannot issue wildcards** — that requires DNS-01. So a
wildcard has to come from outside, which is exactly what this runbook
covers.

## 2. Put the certificate on the host

There are two ways. Use **A** unless the host has to serve more than one
such certificate.

### 2A. As the host's default certificate (panel, one per host)

1. **Hosts → &lt;host&gt; → Traefik.** Paste the **full chain** (leaf +
   intermediates) in *Default certificate* and the private key in
   *Default certificate key*.
2. **Save.** A pair that cannot serve is refused with the reason: half
   of one, an unreadable PEM, a key with a passphrase, or a key from
   another certificate. Once stored, the tab shows which names the
   certificate covers and when it expires. The key is never shown again.
3. The host now shows *Changes not deployed*. Run **Push Trusted Proxy
   Settings** (or *Full Setup*): it writes the pair to
   `~/traefik/certs/default.{crt,key}` and the file
   `~/traefik/dynamic/tls-default.yml` that names it.

This is the same certificate a CDN in front of the host connects to, so
on a host behind a CDN it is normally already set — check what it covers
before replacing it.

### 2B. Loaded by hand and declared in `config.yml` (several per host)

The host's Traefik lives in `~/traefik` (home of the host's SSH user,
`cloud.host.user`), and its compose file mounts `./certs` — that is
`~/traefik/certs` — at **`/etc/certs`** inside the container.

```bash
# $ — from the operator workstation
scp fullchain.pem privkey.pem <user>@<host-ip>:/tmp/
```

```bash
# on the host, as the host's SSH user
mkdir -p ~/traefik/certs                      # may already exist, root-owned
sudo chown "$USER" ~/traefik/certs            # only if Docker created it
mv /tmp/fullchain.pem /tmp/privkey.pem ~/traefik/certs/
chmod 600 ~/traefik/certs/privkey.pem
chmod 644 ~/traefik/certs/fullchain.pem
```

Do not name them `default.crt` / `default.key`: those are written (and
removed) by the panel for 2A.

Then declare them in **Hosts → &lt;host&gt; → Traefik → `config.yml`**.
This is the dynamic-configuration file the host's Traefik watches; the
panel owns its content, so edit it here and not on the host, or the next
`full_setup` will overwrite your change.

```yaml
tls:
  certificates:
    - certFile: /etc/certs/fullchain.pem
      keyFile: /etc/certs/privkey.pem
```

Those are **container** paths (`/etc/certs`), not the host paths you
copied to (`~/traefik/certs`). Getting this backwards is the most common
mistake here: Traefik logs a missing-file warning and keeps serving its
self-signed default, so the site stays up and looks broken only in the
browser.

Traefik picks the certificate whose SAN matches the requested hostname,
so no router-side wiring is needed — loading it is enough. Ship
`config.yml` with *Full Setup*; the file provider is watched, so later
changes are picked up without a restart.

## 3. Switch the domain over and rebuild

1. **Instances → &lt;instance&gt; → Networking**, set **Certificate** to
   *Existing certificate*.
2. **Save.**
3. **Rebuild the instance.** Editing a domain does not regenerate the
   tenant's compose files on its own — the labels are written by
   `copier`, which only runs on deploy/rebuild.
4. Read the job log. Each domain gets one line:
   - `TLS for <domain>: host's certificate` — 2A, and it covers the name.
   - `⚠ <domain>: the host's certificate does not cover this name` — 2A
     loaded, wrong certificate. Browsers will warn.
   - `TLS for <domain>: a certificate declared in the host's config.yml
     (not checked)` — 2B; verify in step 4.
   - The job stops with *the host holds none* — nothing is loaded; go
     back to step 2.

## 4. Verify — do not trust the panel here

```bash
# $ — check what is actually being served
openssl s_client -connect <domain>:443 -servername <domain> </dev/null 2>/dev/null \
  | openssl x509 -noout -issuer -subject -dates
```

- Issuer is your CA and the dates match → done.
- Issuer is `TRAEFIK DEFAULT CERT` → no loaded certificate covers this
  name. Traefik is serving self-signed. Check the SAN (2A: the Traefik
  tab lists it; 2B: `openssl x509 -noout -ext subjectAltName`).
- Issuer is Let's Encrypt → the domain row is not on *Existing
  certificate* (or *Automatic* resolved to Let's Encrypt — the job log
  says which), or the instance was not rebuilt after the change.

## 5. About *No TLS*

It removes the TLS configuration from *that router*, but the host's
Traefik still redirects HTTP to HTTPS at the entrypoint level for every
site on the box. A "No TLS" domain therefore still lands on port 443 and
gets the self-signed default. It is faithful to doodba's semantics, but
under our edge it behaves as "self-signed", not as "plain HTTP". Do not
change the host-wide redirect to work around this — it affects every
tenant on that host.

## 6. Renewal

Certificates loaded this way are **not** renewed by anything. Let's
Encrypt domains renew themselves; these do not. Put the expiry date in
your calendar (2A: the Traefik tab shows it).

- **2A:** paste the new pair on the Traefik tab, save, *Push Trusted
  Proxy Settings*. No rebuild needed.
- **2B:** replace the files at the same paths; Traefik reloads the file
  provider on change. No rebuild needed.

## Rollback

Switch **Certificate** back to *Automatic* and rebuild the instance. On
a host reached directly the panel re-issues a per-domain certificate
over TLS-ALPN-01 within a few seconds of the router coming up, provided
public DNS for the domain still points at the host. Leaving the custom
certificate on the host is harmless for that domain; for 2A remember it
is also what a CDN in front connects to before clearing it.
