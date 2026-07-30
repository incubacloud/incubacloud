# RB-12 — Serve a domain with an existing (non-Let's Encrypt) certificate

**Trigger:** a customer needs a domain served with a certificate you
already hold — a wildcard, an EV cert, or one issued by their own CA —
instead of the per-domain Let's Encrypt certificate the panel issues by
default.

**What it means:** on the instance's domain row you set **Certificate**
to *Existing certificate (Traefik store)*. That tells the tenant's router
to enable TLS **without naming an ACME resolver**, so Traefik serves
whatever certificate its own store already holds for that hostname.

!!! warning "Selecting the option is only half the job"
    The setting does not upload anything. If the host's Traefik has no
    matching certificate loaded, Traefik falls back to its **self-signed**
    default and every visitor gets a browser warning — with no alert
    anywhere in the panel. Load the certificate on the host *first*, then
    switch the domain over.

## 1. Decide whether you actually need this

| Situation | Use |
|---|---|
| Ordinary customer domain, public DNS points at the host | **Let's Encrypt** (default). Nothing to do. |
| One certificate covering many subdomains (`*.example.com`) | *Existing certificate* + this runbook |
| Certificate issued by the customer's own CA | *Existing certificate* + this runbook |
| Domain behind another TLS terminator, panel edge should not do TLS | *No TLS* — but read step 5 first, it does not mean what it sounds like |

The panel's own ACME resolver is `letsencrypt`, using the TLS-ALPN-01
challenge. **It cannot issue wildcards** — that requires DNS-01. So a
wildcard has to come from outside and be loaded by hand, which is exactly
what this runbook covers.

## 2. Put the certificate on the host

The host's Traefik lives in `~/traefik` (home of the host's SSH user,
`cloud.host.user`), and its compose file mounts `./certs` — that is
`~/traefik/certs` — at **`/etc/certs`** inside the container.

`full_setup` does not create that directory. If it does not exist when
the container starts, Docker creates it as an empty **root-owned**
directory, which is why an `scp` into it can fail with a permission
error that has nothing to do with your SSH user.

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

Use the **full chain** (leaf + intermediates), not just the leaf, or
some clients will reject it while browsers accept it — a failure mode
that looks like "works for me".

## 3. Declare it in the host's Traefik file provider

In the panel: **Hosts → &lt;host&gt; → Traefik → `config.yml`**. This is the
dynamic-configuration file the host's Traefik watches; the panel owns its
content, so edit it here and not on the host, or the next `full_setup`
will overwrite your change.

Add (or extend) the `tls` section:

```yaml
tls:
  certificates:
    - certFile: /etc/certs/fullchain.pem
      keyFile: /etc/certs/privkey.pem
```

Those are **container** paths (`/etc/certs`), not the host paths you
copied to in step 2 (`~/traefik/certs`). Getting this backwards is the
most common mistake here: Traefik logs a missing-file warning and keeps
serving its self-signed default, so the site stays up and looks broken
only in the browser.

Traefik picks the certificate whose SAN matches the requested hostname,
so no router-side wiring is needed — loading it is enough.

Apply it:

```bash
# on the host
cd ~/traefik && docker compose -p inverseproxy -f inverseproxy.yaml up -d
```

The file provider is watched, so a change to `config.yml` is normally
picked up without a restart; the `up -d` is there for the first time,
when the `certs` mount itself is new.

## 4. Switch the domain over and redeploy

1. **Instances → &lt;instance&gt; → Domains**, set **Certificate** to
   *Existing certificate*.
2. **Save.**
3. **Rebuild the instance.** Editing a domain does not regenerate the
   tenant's compose files on its own — the labels are written by
   `copier`, which only runs on deploy/rebuild. Until you rebuild,
   nothing changes on the host.

## 5. Verify — do not trust the panel here

```bash
# $ — check what is actually being served
openssl s_client -connect <domain>:443 -servername <domain> </dev/null 2>/dev/null \
  | openssl x509 -noout -issuer -subject -dates
```

- Issuer is your CA and the dates match → done.
- Issuer is `TRAEFIK DEFAULT CERT` → the store has no matching
  certificate. Traefik is serving self-signed and **nothing will tell
  you**. Go back to step 3 and check the SAN really covers this hostname.
- Issuer is Let's Encrypt → the domain row still says *Let's Encrypt*,
  or the instance was not rebuilt after the change.

About **No TLS**: it removes the TLS configuration from *that router*,
but the host's Traefik still redirects HTTP to HTTPS at the entrypoint
level for every site on the box. A "No TLS" domain therefore still lands
on port 443 and gets the self-signed default. It is faithful to doodba's
semantics, but under our edge it behaves as "self-signed", not as "plain
HTTP". Do not change the host-wide redirect to work around this — it
affects every tenant on that host.

## 6. Renewal

Certificates loaded this way are **not** renewed by anything. Let's
Encrypt domains renew themselves; these do not. Put the expiry date in
your calendar and repeat steps 2–3 (no rebuild needed for a
same-path replacement — Traefik reloads the file provider on change).

## Rollback

Switch **Certificate** back to *Let's Encrypt* and rebuild the instance.
The panel re-issues a per-domain certificate over TLS-ALPN-01 within a
few seconds of the router coming up, provided public DNS for the domain
still points at the host. Leaving the custom certificate in the Traefik
store is harmless — the router asks for ACME explicitly, so the stored
one stops being used.
