# RB-21: Issue the origin certificate for the fleet

**Severity:** planned
**Typical trigger:** preparing hosts to be served through the CDN; the
certificate has to be replaced; a host reports it is serving the wrong
one.
**Who runs it:** platform manager.

Read this before touching anything: the certificate here is **not** the
one browsers see. It secures only the leg between the CDN and our
hosts. Cloudflare's edge trusts it; nothing else on the internet does,
and a host serving it to a direct visitor shows a browser warning. That
is why it is only handed to hosts marked *Reached through a CDN*, and
why a self-hosted host never receives it.

## What the panel does

**Settings → SaaS → DNS Routing → Origin Certificate → Issue with
Cloudflare** generates an EC P-256 key **inside the panel**, builds a
signing request for `<base domain>` and `*.<base domain>`, and asks
Cloudflare to sign it. Only the request travels; the private key is
stored encrypted here and never sent anywhere.

This differs from Cloudflare's dashboard flow, which generates the key
on their side and shows it in the page. Prefer the button.

One certificate covers the whole zone. Hosts behind the CDN are
interchangeable, and a certificate each would multiply issuance for no
gain.

## The one prerequisite

The API token already configured for DNS needs one more permission:

```
Zone → SSL and Certificates → Edit
```

Add it to the existing token in the Cloudflare dashboard (My Profile →
API Tokens → the token → Edit), or issue a new token with `Zone:Read` +
`DNS:Edit` + that, and paste it in Settings.

A token without it fails the request with a 403 whose message the panel
shows verbatim next to the button.

## After issuing

The certificate reaches hosts through the queue: pressing the button
enqueues a `push_trusted_proxies` on every host of ours that is behind
the CDN and already has a proxy. The button reports how many. Watch them
finish in the activity list before checking anything.

To confirm a host is serving it:

```
$ openssl s_client -connect <host ip>:443 \
      -servername <anything>.<base domain> </dev/null 2>/dev/null \
    | openssl x509 -noout -issuer -dates -ext subjectAltName
```

Expect `issuer=C = US, O = CloudFlare, Inc., CN = CloudFlare Origin
Certificate` and both names in the SAN list. An issuer of *Let's
Encrypt* means the host is still serving its own — either the push has
not run or the host is not marked as behind the CDN.

**Cloud 1 is a special case worth checking rather than assuming.** Its
current certificate expires 2026-11-22. Traefik serves the *default*
certificate only for requests no router matched with its own; if a
router there names a certificate explicitly, the default never applies
and the expiry stands. Check the router definition before treating
RB-21 as having covered it.

## Replacing one

Press the button again. The new certificate is stored and re-shipped;
the old one is **not** revoked, deliberately: hosts converge one at a
time through the queue, and revoking before they all have the new one
would leave some serving a revoked certificate. Revoke from the
Cloudflare dashboard once every host reports the new fingerprint, or
leave it — it expires on its own.

## Pasting one instead

*Paste a certificate instead* takes a purchased wildcard or one from
another CA. Both halves are stored together and a key that does not
match its certificate is refused, because a host given a mismatched
pair serves **no** valid certificate at all — every handshake, including
the CDN's, gets Traefik's throwaway one.

## Rollback

**Remove** empties the pair, switches *Serve tenants through the CDN*
off, and re-ships. The push renders an empty document, which is how the
file is removed from the host; Traefik then falls back to whatever the
routers obtain for themselves.

Removing it while tenant DNS records are proxied breaks those tenants:
the CDN has nothing valid to talk to. Move the records back to unproxied
first.

```
db$ SELECT id, name, origin_cert_id IS NOT NULL AS issued_here
      FROM cloud_settings;
db$ SELECT name, behind_cdn, traefik_deployed
      FROM cloud_host WHERE active AND behind_cdn;
```

## See also

- [RB-18: tune host edge protection](RB-18-tune-host-edge-protection.md)
  — the trusted ranges and direct-access block that go with this.
- [RB-12: a custom domain's certificate](RB-12-custom-domain-certificate.md)
  — the unrelated case of a tenant's own domain.
