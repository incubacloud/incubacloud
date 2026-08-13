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
        inst_id="$1"; project="$2"; pg_user="$3"; pg_version="$4"; dbname="$5"
        # Boot the new image against a throwaway physical clone of the
        # production DB before touching the live stack. Every path is
        # suffixed by the instance id so concurrent rebuilds never overlap.
        ic_log "safe boot test for instance $inst_id"
        backup_in_db="/tmp/ic_boot_backup_${inst_id}"
        clone_on_host="/tmp/ic_boot_${inst_id}"
        pg_container="ic_boot_pg_${inst_id}"

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
            docker rm -f "$pg_container" 2>/dev/null || true
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

        # The throwaway PG is reached *through* the project network's
        # gateway, never from inside it. A container holding an endpoint
        # on that network blocks any reconciliation docker compose
        # decides to do mid-test: compose cannot remove the network to
        # recreate it, so the step dies with 'has active endpoints' and
        # leaves the stack half stopped — twice on 2026-08-13, taking
        # every tenant on the host down with it. Published on the gateway
        # address alone, the PG is reachable from every container on that
        # bridge and from nowhere else, while compose stays free to
        # reconcile whatever it likes.
        #
        # postgres-autoconf (not vanilla postgres:*-alpine) bundles
        # pgvector, which the cloned Odoo 19 cluster needs to open its
        # ``ai`` embedding index.
        gw="$(docker network inspect "${project}_default" \
            -f '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
        # Failing here is safe and deliberate: the caller marks the step
        # stop_on_failure, so the rebuild aborts with the instance still
        # running its previous image. Falling back to the old on-network
        # behaviour would reintroduce the outage silently.
        [ -n "$gw" ] || ic_die "cannot resolve the gateway of ${project}_default"

        docker rm -f "$pg_container" 2>/dev/null || true
        docker run -d --name "$pg_container" \
            -p "$gw::5432" \
            -v "$clone_on_host:/var/lib/postgresql/data" \
            --user postgres --entrypoint postgres \
            "ghcr.io/tecnativa/postgres-autoconf:${pg_version}-alpine" \
            -D /var/lib/postgresql/data
        sleep 5

        # Docker picked the host port; ask it which one.
        pg_port="$(docker port "$pg_container" 5432 | head -1 | sed 's/.*://')"
        [ -n "$pg_port" ] || ic_die "throwaway postgres published no port"

        # The boot test itself: click-odoo-update against the throwaway PG
        # verifies the new image opens the DB. ``set +e`` so a boot
        # failure is captured and reported rather than aborting before
        # cleanup — the whole point is to fail *without* touching the live
        # stack. Doodba reads PGHOST/PGPORT through libpq, so pointing at
        # the gateway needs no config change inside the image.
        set +e
        docker compose run --rm -e "PGHOST=$gw" -e "PGPORT=$pg_port" odoo \
            click-odoo-update --database "$dbname"
        ic_test_exit=$?
        set -e
        # Cleanup and stack restore both run from the EXIT trap.
        exit "$ic_test_exit"
        ;;

    *)
        ic_die "unknown operation: $op"
        ;;
esac
