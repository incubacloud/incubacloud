#!/usr/bin/env bash
# A build step of the instance's image (doodba's ``odoo/custom/build.d``),
# not a host script: point apt at Freexian's Extended LTS on Debian 8
# (jessie), the release doodba's images of 8.0 to 10.0 are built on.
#
# Usage: debian_archive.sh   (run by doodba's build, as root, no arguments)
#
# deb.debian.org and security.debian.org no longer serve jessie: ``apt
# update`` fails there with 404s, and with it the template's own
# ``20-update-pg-repos`` (rendered below 11.0), so the image did not build
# at all. archive.debian.org has the suites, but signed with keys that have
# expired: apt reads their index with a warning and then refuses to install
# any package from it, those of an ``apt.txt`` included (QA of the series,
# 7-nov-2026). Freexian's Extended LTS still publishes jessie, with
# security updates and a key that is valid, so apt checks what it installs
# as on a supported release. Its keyring is fetched over verified TLS and
# installed only if it is the one whose sha256 is written here.
# Any other release is left untouched.
set -euo pipefail

[ "$(cut -d. -f1 /etc/debian_version 2>/dev/null)" = 8 ] || exit 0

keyring_url="https://deb.freexian.com/extended-lts/pool/main/f/freexian-archive-keyring/freexian-archive-keyring_2022.06.08_all.deb"
keyring_sha256="a8160d1aa1a40aa9988bf0b389b650550c7460ec3b4ec1d847778fe44b9c4dbc"

keyring="$(mktemp -d)/freexian-archive-keyring.deb"
curl -fsS --retry 3 -o "$keyring" "$keyring_url"
echo "$keyring_sha256  $keyring" | sha256sum -c --quiet -
dpkg -i "$keyring"
rm -rf "$(dirname "$keyring")"

# jessie's own suites go, Freexian's takes their place.
for list in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
    [ -f "$list" ] || continue
    sed -i -E \
        -e "\\#^deb(-src)? https?://(deb|httpredir|ftp|security)\\.debian\\.org/#d" \
        "$list"
done
echo "deb http://deb.freexian.com/extended-lts jessie main contrib non-free" \
    > /etc/apt/sources.list.d/incubacloud-freexian-elts.list
echo "[incubacloud] apt reads Freexian's Extended LTS of jessie"
