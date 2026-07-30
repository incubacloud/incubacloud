#!/usr/bin/env bash
# Build a secret-free tarball of an instance folder.
#
# Usage: export_sanitize.sh <instance_dir> <staging_dir> <archive>
#
# Copies the instance into <staging_dir>, strips every credential the
# project folder carries, tars the clean copy into <archive> and prints
# ``SIZE:<human size>`` for the job log.
#
# Two treatments, deliberately different:
#   * Files whose *shape* a re-deploy needs are rewritten with a
#     placeholder (odoo.env and the two postgres env files).
#   * Files the deploy regenerates are removed outright — backup.env and
#     incubacloud.env. The latter holds the instance's Fernet key and the
#     deploy's "Ensure incubacloud.env" step only creates it when absent,
#     so a placeholder would survive as an unusable key instead of being
#     regenerated.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 3 "$#" \
    "export_sanitize.sh <instance_dir> <staging_dir> <archive>"

inst_dir="$(ic_expand_home "$1")"
staging="$(ic_expand_home "$2")"
archive="$(ic_expand_home "$3")"

[ -d "$inst_dir" ] || ic_die "instance directory not found: $inst_dir"

# 1. Copy the project, leaving out VCS checkouts and data directories.
ic_log "copying $inst_dir to a staging area"
rm -rf "$staging"
mkdir -p "$staging"
rsync -a \
    --exclude='.git' \
    --exclude='odoo/auto' \
    --exclude='dumps' \
    --exclude='postgres' \
    --exclude='secrets' \
    --exclude='docker-compose.override.yml' \
    "$inst_dir/" "$staging/"

# 2. Strip access tokens from the repository URLs.
if [ -f "$staging/odoo/custom/src/repos.yaml" ]; then
    ic_log "stripping tokens from repos.yaml"
    sed -i 's|://x-access-token:[^@]*@|://|g' \
        "$staging/odoo/custom/src/repos.yaml"
fi

# 3. Remove SSH material.
rm -f "$staging/odoo/custom/ssh/id_rsa" \
      "$staging/odoo/custom/ssh/id_rsa.pub" \
      "$staging/odoo/custom/ssh/known_hosts"

# 4. Replace the docker env secrets with placeholders, drop the files a
#    deploy regenerates.
if [ -d "$staging/.docker" ]; then
    ic_log "replacing credentials in .docker with placeholders"
    echo 'ADMIN_PASSWORD=changeme' > "$staging/.docker/odoo.env"
    echo 'PGPASSWORD=changeme' > "$staging/.docker/db-access.env"
    echo 'POSTGRES_PASSWORD=changeme' > "$staging/.docker/db-creation.env"
    rm -f "$staging/.docker/backup.env" "$staging/.docker/incubacloud.env"
fi

# 5. Drop the backup destination from the copier answers.
if [ -f "$staging/.copier-answers.yml" ]; then
    sed -i '/^backup_dst:/d' "$staging/.copier-answers.yml"
fi

# 6. Tar the sanitized copy and report its size.
ic_log "building $archive"
tar -czf "$archive" -C "$staging" .
rm -rf "$staging"
echo "SIZE:$(du -sh "$archive" | cut -f1)"
