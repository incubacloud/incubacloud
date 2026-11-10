# RB-31 — Bump the relay-oauth image

**Severity:** planned maintenance · **When:** the weekly
`relay-oauth versions` workflow opened an issue, a security advisory names
curl, jsoncpp, Cyrus SASL or sasl-xoauth2, or core adopts another
docker-mailserver release.

`ghcr.io/incubacloud/relay-oauth` is the relay of the instances whose relay
signs in to Microsoft 365 with OAuth2: docker-mailserver plus sasl-xoauth2
and the `ic-relay-oauth` renewer (`images/relay-oauth/README.md`). Core pins
it by digest, so nothing changes in the fleet until a core release moves the
pin, and each instance takes it on its next deploy or rebuild.

> **One docker-mailserver for both relays.** The image is built on core's
> `SMTP_RELAY_IMAGE`, the docker-mailserver the relays with a password run.
> `RELAY_OAUTH_IMAGE` and `SMTP_RELAY_IMAGE` move in the same core release,
> never one without the other; a test in `images/relay-oauth/tests` fails
> the build when the Dockerfile and `SMTP_RELAY_IMAGE` disagree.

Image tags read `<docker-mailserver>-<sasl-xoauth2>-<revision>`, for
example `16.0.1-0.28-1`. A published tag is never moved: a rebuild of the
same versions is the next revision.

## 1. What changes

| Trigger | Dockerfile | Tag |
|---|---|---|
| docker-mailserver rebuilt its tag upstream (Debian security updates), or an advisory for a Debian library in the image | nothing | same versions, revision + 1 |
| A newer docker-mailserver in the series (`16.0.x`) | `DMS_VERSION`, and `SMTP_RELAY_IMAGE` in the same PR | new versions, revision 1 |
| A newer sasl-xoauth2 tag | `SX_TAG` and `SX_COMMIT` | new versions, revision 1 |
| A new docker-mailserver series (17.x) | as above, after reading its breaking changes | new versions, revision 1 |

For a new sasl-xoauth2, read its changes against the token file contract in
the image's README (`TokenStore::Read`, the keys that override
`/etc/sasl-xoauth2.conf`, the logging options) and take the commit the tag
names:

```bash
git ls-remote --tags https://github.com/tarickb/sasl-xoauth2 'release-*' | tail -3
```

For a new docker-mailserver series, check what the image relies on before
anything else: `target/scripts/helpers/relay.sh` still writes `sasl_passwd`
from `RELAY_USER`/`RELAY_PASSWORD`, `main.cf` is still copied in and edited
in place, supervisord still includes `/etc/supervisor/conf.d/*.conf`,
`smtp` still runs without chroot, and the base is still the Debian the
build stage uses (`BUILD_BASE`; `libjsoncpp26` is that Debian's package).
The integration test covers each of these, so a red build names the one
that moved.

## 2. Pull request

Change the Dockerfile (and `SMTP_RELAY_IMAGE` if docker-mailserver moves).
`relay-oauth-image.yml` builds on amd64 and arm64 and runs, on each, the
plugin's `ctest`, the renewer's pytest and `tests/integration.sh`. A revision
with no Dockerfile change needs no PR: go to step 3 on the current commit.

## 3. Tag and publish

On the merged commit of `19.0`:

```bash
git tag relay-oauth-v16.0.1-0.28-2
git push origin relay-oauth-v16.0.1-0.28-2
```

The workflow refuses a tag that does not match the Dockerfile's versions,
that points to a commit that is not on `19.0`, or that is already published.
It builds and tests again, pushes the tested
images, signs the multi-arch index with cosign (keyless) and attaches the
build provenance. The job summary ends with the line for core:

```
RELAY_OAUTH_IMAGE = "ghcr.io/incubacloud/relay-oauth:16.0.1-0.28-2@sha256:…"
```

Check the signature and the provenance before using the digest:

```bash
cosign verify ghcr.io/incubacloud/relay-oauth@sha256:<digest> \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github.com/incubacloud/incubacloud/\.github/workflows/relay-oauth-image\.yml@refs/tags/relay-oauth-v'
gh attestation verify oci://ghcr.io/incubacloud/relay-oauth@sha256:<digest> --repo incubacloud/incubacloud
```

**The provenance of `16.0.1-0.28-1` names another commit.** That revision
was built and attested from commit `92131e40`, before the repository's
history was rewritten; the rewrite gave the same content new hashes, and the
tag `relay-oauth-v16.0.1-0.28-1` now points to `76974566`, its rewritten
counterpart. `92131e40` is no longer in the branch's history, so the
attestation's source commit cannot be checked out from it. What the image
was built from did not change: `images/relay-oauth` and
`.github/workflows/relay-oauth-image.yml` are the same trees in both commits
(`8ba0496e` and `565b9849`), compared on 2026-11-10 with
`git rev-parse <commit>:images/relay-oauth` in a clone that still had
`92131e40`. The digest stays as it is; no
revision is cut for this. The next revision is attested from a commit of the
current history.

**If the signing step fails** (cosign, the attestation), the images and the
index are already pushed, so running the `publish` job again cannot work: it
stops at "already exists". Do not delete or move the tag. Cut the next
revision (`…-3` after `…-2`) on the same commit and tag it; the unsigned
revision is left unused and no core release names it.

**Owner steps in the repository's settings** (not in the code, so they are
done once by hand, and checked again if the tag name changes):

- A tag ruleset for `relay-oauth-v*`: only maintainers may create it, and
  nobody may update or delete it. The workflow publishes whatever tag
  matches, so who can push one decides who can publish.
- Optional: in Settings → Environments, `relay-oauth-publish` (the `publish`
  job's environment) with required reviewers, so that a publication waits
  for a person even after a tag is pushed.

**First publication only:** the package is created private. An organisation
owner makes it public once (GitHub → the organisation → Packages →
`relay-oauth` → Package settings → Change visibility), and checks that it is
linked to `incubacloud/incubacloud` with Actions access for that repository.

## 4. Core release

In `deploy_instance_executor.py`, move `RELAY_OAUTH_IMAGE` to the new digest
(and `SMTP_RELAY_IMAGE` if docker-mailserver moved), in a normal release
with its QA. On a QA instance with an OAuth2 relay, after its rebuild:

```bash
docker compose exec smtp ic-relay-oauth status      # state ok
docker compose exec smtp postconf -h smtp_sasl_mechanism_filter   # xoauth2
```

and send the relay's test mail from the panel: it must come back delivered.

## Rollback

The previous tags are never deleted. Move `RELAY_OAUTH_IMAGE` back to the
previous digest in a core release (with `SMTP_RELAY_IMAGE` too if the two
moved together) and rebuild the instances that already took the new one.
