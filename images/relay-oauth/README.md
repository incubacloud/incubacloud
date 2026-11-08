# relay-oauth: the SMTP relay that signs in to Microsoft 365 with OAuth2

`ghcr.io/incubacloud/relay-oauth` is the `smtp` service of a production
instance whose relay authenticates with **OAuth2 (client credentials)**
instead of a password. Microsoft turns SMTP AUTH with passwords off by
default for Exchange Online at the end of December 2026 (MC786329); Odoo
does not change: it keeps sending to the local relay, and the relay signs
in to `smtp.office365.com:587` with XOAUTH2.

The image is docker-mailserver, the same release core's relays with a
password run (`SMTP_RELAY_IMAGE`), plus two things:

- **sasl-xoauth2** ([tarickb/sasl-xoauth2](https://github.com/tarickb/sasl-xoauth2),
  Apache-2.0), built from a tag checked against its commit. Postfix loads it
  to speak XOAUTH2. Debian 13 does not package it.
- **`ic-relay-oauth`**, a renewer (Python 3, standard library only) run by
  supervisord. sasl-xoauth2 only knows refresh tokens; the renewer asks Entra
  ID for an access token with the client credentials grant and rewrites the
  token file the plugin opens on every authentication.

Why a derived image rather than installing the plugin at start-up: the
start stays as fast as today (instances that sleep wake up and restart
`smtp`), it needs no network to start, it is reproducible by digest and it
carries no compiler. The image is never used for a relay with a password.

## How it fits together

```
Odoo ──► smtp (this image) ── Postfix ── sasl-xoauth2 ──► smtp.office365.com:587
                                              │ reads on every sign-in
                       ic-relay-oauth ──► /var/lib/ic-relay-oauth/token.json
                            │ client credentials
                            └──► login.microsoftonline.com/<tenant>/oauth2/v2.0/token
```

docker-mailserver writes `/etc/postfix/sasl_passwd` from `RELAY_USER` and
`RELAY_PASSWORD`; with `RELAY_PASSWORD=/var/lib/ic-relay-oauth/token.json`
that is exactly the line sasl-xoauth2 wants (the "password" is the token
file's path). `smtp` and `relay` run without chroot in docker-mailserver, so
the path needs no copy under `/var/spool/postfix`.

## Files

| Path in the image | What |
|---|---|
| `/run/secrets/ic-relay-oauth.json` | Credentials, bind-mounted read-only by the panel, root only. |
| `/var/lib/ic-relay-oauth/token.json` | The plugin's token file, `root:postfix 0640`, directory `0750`. In the container's own filesystem: tokens are not kept. |
| `/var/lib/ic-relay-oauth/status.json` | The renewer's state, read by the panel's probe. No secret, no token. |
| `/etc/sasl-xoauth2.conf` | The plugin's configuration (below). |
| `/etc/supervisor/conf.d/ic-relay-oauth.conf` | The renewer as a supervisord program. |
| `/etc/postfix/main.cf` | One line added: `smtp_sasl_mechanism_filter = xoauth2`. docker-mailserver edits the file in place at start-up, so the line survives. |

**Credentials** (a file, not environment variables: supervisord passes its
environment to every program and `docker inspect` shows it):

```json
{"provider": "microsoft", "tenant_id": "<GUID or domain>", "client_id": "<GUID>",
 "client_secret": "<secret>", "mailbox": "<sender mailbox>"}
```

**`/etc/sasl-xoauth2.conf`**: `client_id` and `client_secret` present and
empty (the plugin requires both keys), `token_endpoint` `http://127.0.0.1:9/`
(a closed port: the plugin can never refresh on its own nor send a secret
anywhere), `log_to_syslog_on_failure` `yes`, and
**`log_full_trace_on_failure` `no`**: with `yes` the plugin's trace writes
the access token to `mail.log`. A test fixes it to `no`.

## The token file contract

What sasl-xoauth2 `release-0.28` reads (`TokenStore::Read`), checked against
the real plugin in the integration test:

```json
{"refresh_token": "unused", "access_token": "<token>", "expiry": "1794178000", "user": "<mailbox>"}
```

- `refresh_token` is **mandatory** (without it: `missing refresh_token`, the
  sign-in fails); any value, since the plugin's refresh goes to the closed port.
- `expiry` is epoch seconds, read with `stoi(asString())`: a string (what the
  plugin itself writes, and what the renewer writes) or a number. Missing
  means 0, expired.
- `user` overrides the user in `sasl_passwd`.
- **Exactly these four keys.** `token_endpoint`, `client_id`,
  `client_secret`, `proxy`, `ca_*` and `refresh_window` in this file override
  `/etc/sasl-xoauth2.conf`: the renewer never writes them, and a test checks it.
- The plugin opens the file on every authentication, so an atomic rename is
  enough: Postfix needs no reload. When `now + 10 s >= expiry` it tries its
  own refresh (twice at most), which fails at once against the closed port.

Whatever goes wrong with the token (file missing, corrupt, expired), Postfix
**defers** the mail (4.7.0, `smtp_sasl_auth_soft_bounce = yes`): nothing is
lost, it is retried.

## The renewer

`ic-relay-oauth run` (supervisord) loops:

1. Reads the credentials. Missing, a directory (Docker creates one when the
   bind-mounted file did not exist on the host at `up`) or wrong:
   state `error` with `config_missing`, `config_is_directory` or
   `config_invalid`, and looks again every 60 s, without exiting.
2. `POST https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`,
   `grant_type=client_credentials`, scope
   `https://outlook.office365.com/.default`, 30 s timeout, TLS checked
   against the system's CAs, redirects refused. An answer without a
   token, or with an `expires_in` that is not positive or is over a day, is
   `bad_response`.
3. Writes the token file atomically (temporary file in the same directory,
   group `postfix`, `0640`, rename).
4. Sleeps until 20 min before `expires_in` ends (60 s at least). On a
   failure it keeps the previous token while it lasts and retries after 30 s,
   1, 2 and then every 5 min. `SIGUSR1` renews at once.
5. After the first token, and after a success that follows a failure,
   `postqueue -f`: the mail deferred meanwhile leaves now.
6. Never logs the secret or a token; errors go to supervisord's log
   (`/var/log/supervisor/ic-relay-oauth.log`) with their AADSTS code.

`ic-relay-oauth status` prints `status.json`:

| Key | |
|---|---|
| `state` | `starting`, `ok` or `error` |
| `last_success`, `expires_at` | epoch seconds of the last token; kept through errors, so an `error` with a token still valid is told apart |
| `error_code` | `AADSTS<n>`, `HTTP <status>`, `network`, `bad_response`, `token_write_failed` or a `config_*` code |
| `error_message` | Entra ID's `error_description` or the network error, one line, 300 characters at most |
| `updated_at` | epoch seconds |

A token `ok` does not mean mail leaves: Exchange can still refuse a good
token (`535 5.7.3`: service principal not registered or without rights on
the mailbox). Only a delivery proves the relay works.

`IC_RELAY_OAUTH_TOKEN_URL` replaces Microsoft's endpoint **for the tests
only**; the panel never writes it. It counts only for an `http(s)` URL whose
host resolves to loopback or a private network (not link-local); anything
else is ignored and Microsoft's endpoint is used.

## Build and test locally

```bash
docker buildx build --platform linux/amd64 --load -t relay-oauth:test images/relay-oauth
python3 -m pytest images/relay-oauth/tests          # the renewer and the fixed files
images/relay-oauth/tests/integration.sh relay-oauth:test
```

`integration.sh` starts the image with the environment
doodba-copier-template v10.0.8 gives `smtpreal`, next to
`tests/fake_m365.py` (run from the same image): a token endpoint and an SMTP
server with STARTTLS that demands XOAUTH2 and answers like Exchange Online.
It covers sending, the token file contract, the plugin never reaching the
token endpoint, deferral without loss, recovery with `postqueue -f`, a
`535` with a good token, the renewer keeping a working token through
errors, the credentials file problems, and that neither the secret nor any
token reaches the logs or `docker inspect`. The plugin's own `ctest` runs
inside the build.

## Releases

`.github/workflows/relay-oauth-image.yml` builds natively on amd64 and arm64
and runs all of the above on both, on every pull request and push that
touches this directory. A tag `relay-oauth-v<docker-mailserver>-<sasl-xoauth2>-<revision>`
(`relay-oauth-v16.0.1-0.28-1`) also publishes the tested images, signed with
cosign (keyless, the workflow's identity) and with a build provenance
attestation, as `ghcr.io/incubacloud/relay-oauth:16.0.1-0.28-1`. A published
tag never moves; the base digest it was built on is in its labels
(`org.opencontainers.image.base.digest`). Core pins the image by digest
(`RELAY_OAUTH_IMAGE`).

Verify a pull:

```bash
cosign verify ghcr.io/incubacloud/relay-oauth@sha256:<digest> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github.com/incubacloud/incubacloud/\.github/workflows/relay-oauth-image\.yml@refs/tags/relay-oauth-v'
gh attestation verify oci://ghcr.io/incubacloud/relay-oauth@sha256:<digest> --repo incubacloud/incubacloud
```

`.github/workflows/relay-oauth-versions.yml` looks every week for a newer
docker-mailserver in the same series, a newer sasl-xoauth2 tag, and the
docker-mailserver tag rebuilt upstream since the last image, and opens an
issue for each. Updating: `docs/runbooks/RB-31-bump-relay-oauth-image.md`.
