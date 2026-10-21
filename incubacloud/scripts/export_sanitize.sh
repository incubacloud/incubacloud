#!/usr/bin/env bash
# Build a development copy of an instance folder.
#
# Usage: export_sanitize.sh <instance_dir> <staging_dir> <archive>
#
# Copies the instance into <staging_dir>, keeps what a developer needs to
# rebuild the project locally (compose files, copier answers, repos.yaml,
# addons.yaml, pip/apt dependencies, the Dockerfile) and leaves out every
# credential and production-only piece, then tars the clean copy into
# <archive> and prints ``SIZE:<human size>`` for the job log.
#
# The copy is for development only. devel.yaml and common.yaml read no
# env file — the development passwords are written in devel.yaml — so no
# env file is exported at all, not even as a placeholder: only prod.yaml
# and test.yaml need them, and a developer who wants to run those
# elsewhere writes their own (DEVELOPMENT.md says which).
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 3 "$#" \
    "export_sanitize.sh <instance_dir> <staging_dir> <archive>"

inst_dir="$(ic_expand_home "$1")"
staging="$(ic_expand_home "$2")"
archive="$(ic_expand_home "$3")"

[ -d "$inst_dir" ] || ic_die "instance directory not found: $inst_dir"

# 1. Copy the project, leaving out VCS checkouts, data directories, every
#    env file (``.docker/`` holds nothing else), the instance's logs, the
#    override that carries the platform's resource limits and the
#    continuous recovery agent's ``.walg/`` (wal-g, the host's libraries it
#    runs with, the logs of past restores).
ic_log "copying $inst_dir to a staging area"
rm -rf "$staging"
mkdir -p "$staging"
rsync -a \
    --exclude='.git' \
    --exclude='odoo/auto' \
    --exclude='dumps' \
    --exclude='postgres' \
    --exclude='secrets' \
    --exclude='/.docker' \
    --exclude='/logs' \
    --exclude='/.walg' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='*.env' \
    --exclude='*.env.*' \
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

# 4. Drop credentials and production-only options from the Odoo conf:
#    any option whose name speaks of a password, secret, token or key, and
#    the real-IP options, which name the proxies of the production host.
if [ -d "$staging/odoo/custom/conf.d" ]; then
    ic_log "removing credentials from odoo/custom/conf.d"
    find "$staging/odoo/custom/conf.d" -type f -name '*.conf' -exec \
        sed -i -E \
            -e '/^[[:space:]]*[A-Za-z0-9_]*(pass|secret|token|key)[A-Za-z0-9_]*[[:space:]]*[=:]/Id' \
            -e '/^[[:space:]]*x_real_ip_(header|trusted)[[:space:]]*[=:]/d' \
            {} +
fi

# 5. Drop the production backup and SMTP relay settings from the copier
#    answers; development uses neither.
if [ -f "$staging/.copier-answers.yml" ]; then
    sed -i -E '/^(backup_|smtp_relay_)[A-Za-z0-9_]*:/d' \
        "$staging/.copier-answers.yml"
fi

# 6. Point docker-compose.yml at the development stack.
if [ -f "$staging/devel.yaml" ]; then
    rm -f "$staging/docker-compose.yml"
    ln -s devel.yaml "$staging/docker-compose.yml"
fi

# 7. Tell the developer how to start it.
cat > "$staging/DEVELOPMENT.md" <<'MD'
# Development copy

This folder is a development export of the instance's Doodba project. It
carries the project's configuration — compose files, copier answers,
`repos.yaml`, `addons.yaml`, the pip and apt dependencies and the
Dockerfile — and none of its credentials: no env files, no SSH keys, no
repository tokens, no logs. The platform's resource limits
(`docker-compose.override.yml`) are not included either.

## Start it

You need the usual Doodba toolchain: Docker with Compose, `invoke`,
`copier` and `pre-commit`.

```sh
invoke develop          # git init, docker-compose.yml -> devel.yaml
invoke git-aggregate    # clone the repositories listed in repos.yaml
invoke img-build --pull
invoke resetdb          # a fresh `devel` database; or load the instance's data, below
invoke start
```

Odoo answers on `http://localhost:<Odoo major version>069`, 19069 for
Odoo 19. `invoke resetdb` stops Odoo and leaves a cache database next to
`devel`, so run `invoke start` after it and pick `devel` in the database
selector.

Private repositories: `repos.yaml` no longer carries an access token.
Either put an SSH key with access to them in `odoo/custom/ssh/` and
switch those remotes to SSH, or add a token of your own to the URL.

## Data

The export carries no database. Instead of `invoke resetdb`, you can
load the instance's data:

1. Download a neutralized backup of the instance from the panel
   ("Download Neutralized Backup") into this folder as `backup.zip`.
2. Restore it over `devel`:

```sh
docker compose stop odoo
MD
# The load command is the instance's series' (IC_DB_TOOL): the developer
# runs it in the image this export was made from.
ic_db_load_args devel /tmp/backup.zip 1
cat >> "$staging/DEVELOPMENT.md" <<MD
docker compose run --rm -v "\$PWD/backup.zip:/tmp/backup.zip:ro" odoo \\
    ${IC_DB_ARGS[*]}
MD
cat >> "$staging/DEVELOPMENT.md" <<'MD'
docker compose start odoo
```

## Running it elsewhere

`prod.yaml` and `test.yaml` are included. They read their credentials
from `.docker/odoo.env`, `.docker/db-access.env` and
`.docker/db-creation.env` (plus `.docker/smtp.env` and
`.docker/backup.env` when those services are present), which the export
leaves out on purpose: write your own, as described in the Doodba copier
template's documentation, before linking `docker-compose.yml` to one of
them.
MD

# 8. Tar the clean copy and report its size.
ic_log "building $archive"
tar -czf "$archive" -C "$staging" .
rm -rf "$staging"
echo "SIZE:$(du -sh "$archive" | cut -f1)"
