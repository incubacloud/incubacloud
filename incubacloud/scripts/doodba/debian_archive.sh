#!/usr/bin/env bash
# A build step of the instance's image (doodba's ``odoo/custom/build.d``),
# not a host script: point apt at archive.debian.org on a Debian release
# that has left the mirrors.
#
# Usage: debian_archive.sh   (run by doodba's build, as root, no arguments)
#
# doodba's images of the oldest Odoo series are built on Debian 8
# (jessie), whose suites deb.debian.org and security.debian.org no longer
# serve: ``apt update`` fails there with 404s, and with it the template's
# own ``20-update-pg-repos`` (rendered for 7.0 to 10.0), so their image did
# not build at all (QA of the series, 7-nov-2026). Debian 9 (stretch) is
# in the same archive. The archive's signatures are past their keys'
# expiry, so its index is read without its validity date; packages from it
# still need a valid signature to install, which this does not change.
# Any other release is left untouched.
set -euo pipefail

case "$(cut -d. -f1 /etc/debian_version 2>/dev/null)" in
    8) codename=jessie ;;
    9) codename=stretch ;;
    *) exit 0 ;;
esac

for list in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
    [ -f "$list" ] || continue
    sed -i -E \
        -e "s#https?://(deb|httpredir|ftp)\.debian\.org/debian/? #http://archive.debian.org/debian #" \
        -e "s#https?://security\.debian\.org(/debian-security)?/? #http://archive.debian.org/debian-security #" \
        -e "/ ${codename}-updates /s/^deb/# deb/" \
        -e "s#https?://apt\.postgresql\.org/pub/repos/apt/? ${codename}-pgdg #https://apt-archive.postgresql.org/pub/repos/apt ${codename}-pgdg-archive #" \
        "$list"
done
echo 'Acquire::Check-Valid-Until "false";' \
    > /etc/apt/apt.conf.d/99-incubacloud-debian-archive
echo "[incubacloud] apt points at archive.debian.org ($codename)"
