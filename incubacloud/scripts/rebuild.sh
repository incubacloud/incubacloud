#!/usr/bin/env bash
# The non-trivial host-side steps of an instance rebuild.
#
# Usage:
#   rebuild.sh commit-dirty     <dir> <timestamp>
#   rebuild.sh copier-update    <dir> <answers_file>
#   rebuild.sh resolve-conflicts <dir>
#   rebuild.sh boot-test        <dir> <inst_id> <project> <pg_user> <pg_version> <dbname>
#
# Rebuild subclasses deploy and reuses deploy.sh / strip_compose_service.sh
# for the steps they share (ensure/inject secret env, cap hostname, strip
# service, set system params). This script carries what is specific to a
# copier *update* plus the safe boot test.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 2 "$#" "rebuild.sh <operation> <dir> ..."

op="$1"
dir="$(ic_expand_home "$2")"
shift 2

[ -d "$dir" ] || ic_die "instance directory not found: $dir"
cd "$dir"

case "$op" in
    commit-dirty)
        ic_require_args 1 "$#" "rebuild.sh commit-dirty <dir> <timestamp>"
        ts="$1"
        # copier update needs a clean working tree. Commit anything dirty
        # with inline identity — the host may have no git user configured.
        ic_log "committing any dirty files before copier update"
        # Before staging, not after: the live Odoo log lives inside this
        # repo and would dirty the tree again between the commit and
        # copier's own cleanliness check.
        ic_git_exclude_logs "$dir"
        git add -A
        git diff --cached --quiet || git \
            -c user.email='system@incubacloud' \
            -c user.name='IncubaCloud' \
            commit -m "IncubaCloud rebuild $ts" --no-verify || true
        ;;

    copier-update)
        ic_require_args 1 "$#" \
            "rebuild.sh copier-update <dir> <answers_file> [vcs_ref]"
        answers="$1"; vcs_ref="${2:-}"
        export PATH="$HOME/.local/bin:$PATH"
        git config --global --get init.defaultBranch >/dev/null 2>&1 \
            || git config --global init.defaultBranch master
        # Unpinned, ``copier update`` moves the project to the template's
        # latest revision — and rebuild runs on every push, so upstream
        # changes land continuously and invisibly. Log the effective ref
        # either way.
        if [ -n "$vcs_ref" ]; then
            ic_log "running copier update in $dir (template @ $vcs_ref)"
            "$HOME/.local/bin/copier" update --defaults --trust \
                --vcs-ref "$vcs_ref" --data-file "$answers" "$dir"
        else
            ic_log "running copier update in $dir (template @ latest — unpinned)"
            "$HOME/.local/bin/copier" update --defaults --trust \
                --data-file "$answers" "$dir"
        fi
        ;;

    resolve-conflicts)
        # copier update can leave conflict markers; keep the new version
        # (everything after ``=======``). Files default to
        # prod/test/common; warm claim passes its own subset.
        set -- "$@"
        [ "$#" -gt 0 ] || set -- prod.yaml test.yaml common.yaml
        ic_log "resolving copier merge conflicts"
        for f in "$@"; do
            [ -f "$f" ] || continue
            if grep -q '<<<<<<' "$f" 2>/dev/null; then
                sed -i '/^<<<<<<< /,/^=======/d;/^>>>>>>> /d' "$f"
            fi
        done
        ;;

    boot-test)
        ic_require_args 5 "$#" \
            "rebuild.sh boot-test <dir> <inst_id> <project> <pg_user> <pg_version> <dbname>"
        # <project> is accepted and no longer read: the boot test stopped
        # going through the project network when it got a network of its
        # own. Kept so the callers' argv does not have to change.
        inst_id="$1"; pg_user="$3"; pg_version="$4"; dbname="$5"
        # Boot the new image against a throwaway physical clone of the
        # production DB before touching the live stack. Every path is
        # suffixed by the instance id so concurrent rebuilds never overlap.
        ic_log "safe boot test for instance $inst_id"
        backup_in_db="/tmp/ic_boot_backup_${inst_id}"
        clone_on_host="/tmp/ic_boot_${inst_id}"
        pg_container="ic_boot_pg_${inst_id}"
        boot_net="ic_boot_${inst_id}"
        odoo_container="ic_boot_odoo_${inst_id}"

        # Whatever this test disturbs has to come back up, so the restore
        # runs from a trap rather than from the tail of the block: with
        # ``set -e`` any unexpected failure in between (a cp that breaks,
        # a container that will not start) would jump straight past a
        # cleanup written at the end and leave the stack down. That is
        # how a failed boot test used to take the tenant with it.
        #
        # ``start`` and not ``up``, deliberately, twice over: it neither
        # recreates containers nor re-evaluates the image, so a failed
        # boot test can never deploy the very image it just rejected;
        # and it does not reconcile networks, so the restore cannot trip
        # over the same drift as the failure it is cleaning up after. On
        # services that never stopped it is a no-op.
        running_before="$(docker compose ps --services --status running \
            2>/dev/null | tr '\n' ' ')"
        # shellcheck disable=SC2317  # reached through the EXIT trap below
        ic_boot_restore() {
            docker rm -f "$odoo_container" 2>/dev/null || true
            docker rm -f "$pg_container" 2>/dev/null || true
            docker network rm "$boot_net" 2>/dev/null || true
            docker run --rm -v /tmp:/host_tmp alpine \
                rm -rf "/host_tmp/ic_boot_${inst_id}" 2>/dev/null || true
            if [ -n "${running_before// /}" ]; then
                # Word splitting is the point: a space-separated service list.
                # shellcheck disable=SC2086
                docker compose start $running_before 2>/dev/null || true
            fi
            docker compose exec -T db rm -rf "$backup_in_db" 2>/dev/null || true
        }
        trap ic_boot_restore EXIT

        # Defensive pre-cleanup: pg_basebackup refuses a non-empty target,
        # and an interrupted run may have left a UID-70 dir on the host.
        docker compose exec -T db rm -rf "$backup_in_db"
        docker run --rm -v /tmp:/host_tmp alpine rm -rf "/host_tmp/ic_boot_${inst_id}"

        # Physical copy of the PG cluster (no exclusive lock → zero
        # downtime). Drop backup_label inside the container before the
        # host sees the files: postgres would otherwise try to replay WAL
        # from a backup, and once the mount is chowned to UID 70 the host
        # user can no longer remove anything in there.
        docker compose exec -T db pg_basebackup -U "$pg_user" \
            -D "$backup_in_db" --checkpoint=fast --no-sync -X fetch
        docker compose exec -T db rm -f "$backup_in_db/backup_label"
        docker compose cp "db:$backup_in_db" "$clone_on_host"
        docker compose exec -T db rm -rf "$backup_in_db"

        # Flip ownership to the postgres UID via an ephemeral root
        # container (the host user lacks CAP_CHOWN).
        docker run --rm -v "$clone_on_host:/data" alpine chown -R 70:70 /data

        # The throwaway PG lives on a network of its own, never on the
        # project's. A container holding an endpoint on the project
        # network blocks any reconciliation docker compose decides to do
        # mid-test: compose cannot remove the network to recreate it, so
        # the step dies with 'has active endpoints' and leaves the stack
        # half stopped — twice on 2026-08-13, taking every tenant on the
        # host down with it.
        #
        # It used to be published on the project network's gateway
        # instead. That cannot work on a staging: test.yaml declares
        # that network ``internal``, Docker drops whatever leaves an
        # internal bridge, and the published port is unreachable from
        # it. The first staging rebuild in production waited for it
        # forever (2026-10-03). The boot container joins this network
        # instead, which works whatever the project's networks are.
        #
        # postgres-autoconf (not vanilla postgres:*-alpine) bundles
        # pgvector, which the cloned Odoo 19 cluster needs to open its
        # ``ai`` embedding index.
        docker rm -f "$odoo_container" "$pg_container" 2>/dev/null || true
        docker network rm "$boot_net" 2>/dev/null || true
        docker network create "$boot_net"
        docker run -d --name "$pg_container" --network "$boot_net" \
            -v "$clone_on_host:/var/lib/postgresql/data" \
            --user postgres --entrypoint postgres \
            "ghcr.io/tecnativa/postgres-autoconf:${pg_version}-alpine" \
            -D /var/lib/postgresql/data

        # The boot test itself: click-odoo-update against the throwaway PG
        # verifies the new image opens the DB. ``set +e`` so a boot
        # failure is captured and reported rather than aborting before
        # cleanup — the whole point is to fail *without* touching the live
        # stack. Doodba reads PGHOST/PGPORT through libpq, so pointing at
        # the throwaway needs no config change inside the image.
        #
        # ``docker compose run`` cannot attach a container to a network
        # outside the project, so it starts detached and joins the boot
        # network right after. Doodba's own wait for Postgres has no
        # deadline — an unreachable database hung the job for good — so
        # it is replaced by a bounded one; and the unaccent hook, which
        # would query before the network is joined, is skipped: the
        # clone already carries the extension.
        set +e
        # shellcheck disable=SC2016  # expanded by the container's bash
        docker compose run -d --name "$odoo_container" \
            -e "PGHOST=$pg_container" -e PGPORT=5432 \
            -e WAIT_DB=false -e UNACCENT=false odoo \
            bash -c 'for _ in $(seq 120); do
                         psql -l >/dev/null 2>&1 \
                             && exec click-odoo-update --database "$0"
                         sleep 1
                     done
                     echo "boot test: the throwaway postgres never answered" >&2
                     exit 1' "$dbname" \
            && docker network connect "$boot_net" "$odoo_container" \
            && docker logs -f "$odoo_container"
        ic_test_exit=$?
        if [ "$ic_test_exit" -eq 0 ]; then
            ic_test_exit="$(docker wait "$odoo_container")"
        fi
        set -e
        # Cleanup and stack restore both run from the EXIT trap.
        exit "$ic_test_exit"
        ;;

    *)
        ic_die "unknown operation: $op"
        ;;
esac
