#!/usr/bin/env bash
# The non-trivial host-side steps of an instance rebuild.
#
# Usage:
#   rebuild.sh commit-dirty     <dir> <timestamp>
#   rebuild.sh copier-update    <dir> <answers_file>
#   rebuild.sh resolve-conflicts <dir>
#   rebuild.sh boot-test        <dir> <inst_id> <project> <pg_user> <pg_version> <dbname> [<cap_s>]
#
# Rebuild subclasses deploy and reuses deploy.sh / strip_compose_service.sh
# for the steps they share (ensure/inject secret env, cap hostname, strip
# service, set system params). This script carries what is specific to a
# copier *update* plus the safe boot test.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

#: The boot test's measures (``boot-test``, #91, plan of 6-nov-2026).
#: What it leaves free on Docker's disk besides its copy, in KiB: the 5 GiB
#: a restore leaves to the server's other instances
#: (``restore_space.RESERVE_BYTES``).
IC_BOOT_RESERVE_KB=$(( 5 * 1024 * 1024 ))
#: The speed its estimate assumes, in MB/s: pg_basebackup into a volume ran
#: at 285-300 MB/s on a Hetzner CX with local disk (spike of 6-nov-2026),
#: halved for slower disks. The log gives the real speed after each copy.
IC_BOOT_ESTIMATE_MBPS=150
#: Its time budget, in seconds: an hour to boot the new image and update
#: the copy, plus the copy at IC_BOOT_BUDGET_MBPS, slower than the estimate
#: on purpose. An hour is what the whole step had before.
IC_BOOT_BUDGET_BASE_S=3600
IC_BOOT_BUDGET_MBPS=100
#: What its trap needs once the budget ends, in seconds: a budget that
#: leaves less than this of the step's cap is not started.
IC_BOOT_TRAP_MARGIN_S=300

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
        # Before staging, not after, so that this very commit goes
        # without them (see ic_git_exclude). The live Odoo log and
        # continuous recovery's spool would dirty the tree again between
        # the commit and copier's own cleanliness check. ``.walg`` as a
        # whole: wal-g, the host's libraries it runs with and a restore's
        # working files are no project's either.
        ic_git_exclude_logs "$dir"
        ic_git_exclude "$dir" .walg/
        # Secrets, which a commit keeps for good: the SSH directory
        # doodba copies into the image as root's ~/.ssh (where a deploy
        # key goes), repos.yaml with the repository tokens the panel
        # writes into its URLs, and the Odoo conf and pip.txt, whose
        # contents are the user's (a password, a token in a git URL).
        # All of them stay on disk for the build; copier renders ssh/,
        # repos.yaml and pip.txt only when missing (the template's
        # ``_skip_if_exists``), and never the conf.
        secrets=(
            odoo/custom/ssh/
            odoo/custom/src/repos.yaml
            'odoo/custom/conf.d/*.conf'
            odoo/custom/dependencies/pip.txt
        )
        for path in "${secrets[@]}"; do
            ic_git_exclude "$dir" "$path"
        done
        # And the repository trees fetched before the build (deploy.sh
        # fetch-repos, every series that does: 8.0 to 10.0 since
        # 8-nov-2026, all but 19.0 since 10-nov-2026, 19.0 too from core
        # 1.0.224): every directory of
        # odoo/custom/src but private, their temporary trees and the
        # build's Dockerfile.dockerignore. fetch-repos excludes them
        # itself before fetching; this is the second lock: hundreds of MB
        # of odoo/odoo committed never leave the history (8-nov-2026).
        trees=(
            ':(glob)odoo/custom/src/*/**'
            ':(exclude,glob)odoo/custom/src/private/**'
            odoo/Dockerfile.dockerignore
        )
        exclude="$dir/.git/info/exclude"
        mkdir -p "$dir/.git/info" 2>/dev/null || true
        for line in '/odoo/custom/src/*/' '!/odoo/custom/src/private/' \
            /odoo/Dockerfile.dockerignore; do
            if ! grep -qxF -- "$line" "$exclude" 2>/dev/null; then
                if [ -s "$exclude" ] && [ -n "$(tail -c 1 "$exclude")" ]; then
                    printf '\n' >> "$exclude" 2>/dev/null || true
                fi
                printf '%s\n' "$line" >> "$exclude" 2>/dev/null \
                    || ic_warn "could not write $exclude: $line"
            fi
        done
        if [ -n "$(git ls-files -- "${trees[@]}")" ]; then
            git rm -r --cached -f --quiet -- "${trees[@]}" \
                || ic_warn "could not untrack the fetched repository trees in $dir"
        fi
        git add -A
        # Fail closed: an exclusion that could not be written, or an
        # untracking git refused, only warns, and the secret is staged
        # again by now. A rebuild stopped here can be retried; a commit
        # stays in the history.
        staged="$(git ls-files -- "${secrets[@]}" | tr '\n' ' ')"
        if [ -n "$staged" ]; then
            ic_die "secrets are still in the repository's index after excluding them, so nothing was committed: ${staged% } (check $dir/.git/info/exclude and the warnings above)"
        fi
        staged="$(git ls-files -- "${trees[@]}" | sed -n "1,5p" | tr '\n' ' ')"
        if [ -n "$staged" ]; then
            ic_die "fetched repository trees are still in the repository's index after excluding them, so nothing was committed: ${staged% } (check $dir/.git/info/exclude and the warnings above)"
        fi
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
        # commit-dirty has just committed everything but what it keeps out
        # of the repository, the product's own writes among it. What is
        # dirty now changed since, and copier would refuse it saying only
        # that the tree is dirty: say what changed.
        dirty="$(git status --porcelain --untracked-files=all 2>/dev/null || true)"
        if [ -n "$dirty" ]; then
            changed="$(sed -n '1,10{s/^...//;p}' <<<"$dirty" | tr '\n' ' ')"
            ic_die "the project changed after the rebuild's commit, and copier refuses a dirty tree: ${changed% }"
        fi
        # Unpinned, ``copier update`` moves the project to the template's
        # latest revision — and rebuild runs on every push, so upstream
        # changes land continuously and invisibly. Log the effective ref
        # either way.
        if [ -n "$vcs_ref" ]; then
            ic_log "running copier update in $dir (template @ $vcs_ref)"
            ic_copier update --defaults --trust \
                --vcs-ref "$vcs_ref" --data-file "$answers" "$dir"
        else
            ic_log "running copier update in $dir (template @ latest — unpinned)"
            ic_copier update --defaults --trust \
                --data-file "$answers" "$dir"
        fi
        ;;

    resolve-conflicts)
        # copier update can leave conflict markers; keep the new version
        # (everything after ``=======``). Files default to
        # prod/test/common; a layer's own build may pass its subset.
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
            "rebuild.sh boot-test <dir> <inst_id> <project> <pg_user> <pg_version> <dbname> [<cap_s>]"
        # <project> is accepted and no longer read: the boot test stopped
        # going through the project network when it got a network of its
        # own. Kept so the callers' argv does not have to change.
        # <cap_s> is the step's cap, the panel's ``timeout``: a test whose
        # budget (below) would not end inside it stops before it copies.
        inst_id="$1"; pg_user="$3"; pg_version="$4"; dbname="$5"
        cap_s="${6:-}"
        # Boot the new image against a throwaway physical copy of the
        # production DB before touching the live stack. Every name is
        # suffixed by the instance id so concurrent rebuilds never overlap.
        ic_log "safe boot test for instance $inst_id"
        boot_volume="ic_boot_${inst_id}"
        copy_container="ic_boot_copy_${inst_id}"
        pg_container="ic_boot_pg_${inst_id}"
        boot_net="ic_boot_${inst_id}"
        odoo_container="ic_boot_odoo_${inst_id}"
        # postgres-autoconf (not vanilla postgres:*-alpine) bundles
        # pgvector, which the copied Odoo 19 cluster needs to open its
        # ``ai`` embedding index. The copy and the volume's preparation
        # run in it too: one image, with the database's own pg_basebackup.
        pg_image="ghcr.io/tecnativa/postgres-autoconf:${pg_version}-alpine"
        # The rule that lets the copy in: replication for the database's
        # own user only, marked so its removal takes this line and no
        # other. ``hba_added`` says it may be in the file, from just before
        # it is written until it is taken out right after the copy.
        hba_rule="host replication ${pg_user} 127.0.0.1/32 trust # incubacloud boot test"
        hba_file=""
        hba_added=""

        # Whether the test needs the gatekeeper (below) is read from the
        # compose, and a compose that cannot be read says nothing about
        # it. Taken for a stack without one, it put the throwaway PG on a
        # network the boot container, inside the gatekeeper's namespace,
        # could not join, and the test failed with nothing to say why
        # (QA, the pull request preview walk 6f6, 2026-10-22). So the
        # rebuild stops instead, and here, before it has copied anything.
        if ! services="$(ic_compose_services)"; then
            ic_die "could not read the compose to know whether the boot test needs the gatekeeper; stopping rather than assume there is none"
        fi

        # Whatever this test disturbs has to come back up, so the restore
        # runs from a trap rather than from the tail of the block: with
        # ``set -e`` any unexpected failure in between (a copy that breaks,
        # a container that will not start) would jump straight past a
        # cleanup written at the end and leave the stack down. That is
        # how a failed boot test used to take the instance with it.
        #
        # ``start`` and not ``up``, deliberately, twice over: it neither
        # recreates containers nor re-evaluates the image, so a failed
        # boot test can never deploy the very image it just rejected;
        # and it does not reconcile networks, so the restore cannot trip
        # over the same drift as the failure it is cleaning up after. On
        # services that never stopped it is a no-op.
        running_before="$(ic_running_services 2>/dev/null | tr '\n' ' ')"
        # shellcheck disable=SC2317  # reached through the EXIT trap below
        ic_boot_restore() {
            ic_boot_rc=$?
            # First of all: a stop gives the trap 60 s before its KILL
            # (``CANCEL_TERM_WAIT_S``), and removing a volume the size of
            # the database may take longer than that. Normally gone already,
            # right after the copy; this is for a test cut during it.
            if [ -n "$hba_added" ]; then
                if ic_boot_hba_drop; then
                    hba_added=""
                else
                    ic_boot_hba_warn
                fi
            fi
            docker rm -f "$copy_container" 2>/dev/null || true
            docker rm -f "$odoo_container" 2>/dev/null || true
            docker rm -f "$pg_container" 2>/dev/null || true
            docker network rm "$boot_net" 2>/dev/null || true
            # After the containers that mount it.
            docker volume rm "$boot_volume" >/dev/null 2>&1 || true
            if [ -n "${gatekeeper_started:-}" ] && [ "$ic_boot_rc" -ne 0 ]; then
                # A first boot under the gatekeeper that failed leaves the
                # stack as it found it: the old egress model, no gatekeeper.
                docker compose rm -sf gatekeeper 2>/dev/null || true
            fi
            if [ -n "${running_before// /}" ]; then
                # Word splitting is the point: a space-separated service list.
                # shellcheck disable=SC2086
                docker compose start $running_before 2>/dev/null || true
            fi
        }
        trap ic_boot_restore EXIT

        # ic_boot_stop MESSAGE: end the test with MESSAGE, on stdout, where
        # the panel reads it to say why the rebuild stopped
        # (``RebuildInstanceExecutor.parse_results``). The trap puts back
        # whatever the test had touched.
        ic_boot_stop() {
            printf '[incubacloud] ERROR: boot test stopped: %s\n' "$*"
            exit 1
        }
        # ic_boot_psql SQL: one answer of the live database, as text.
        ic_boot_psql() {
            docker compose exec -T db psql -U "$pg_user" -d "$dbname" -tAc "$1"
        }
        # ic_boot_gib KIB: a size in KiB as the messages say it.
        ic_boot_gib() {
            awk -v k="$1" 'BEGIN { printf "%.1f GiB", k / 1048576 }'
        }
        # ic_boot_hba_drop: take the rule ``hba_rule`` out of the live
        # database's pg_hba.conf, in place (the file keeps its owner and
        # mode), and reload. Each step only after the one before it
        # worked: a temporary copy that could not be written, or came out
        # empty (a full disk, the very case this test risks), never
        # replaces the file, which PostgreSQL would then fail to reload
        # while it kept the rule in memory. Fails when the rule is still
        # in the file or the reload failed.
        # shellcheck disable=SC2317  # reached through the EXIT trap too
        ic_boot_hba_drop() {
            # shellcheck disable=SC2016  # expanded by the container's sh
            docker compose exec -T db sh -c \
                'grep -vxF "$1" "$2" > /tmp/ic_boot_hba && [ -s /tmp/ic_boot_hba ] && cat /tmp/ic_boot_hba > "$2" && ! grep -qxF "$1" "$2"; rc=$?; rm -f /tmp/ic_boot_hba; exit "$rc"' \
                sh "$hba_rule" "$hba_file" \
                && ic_boot_psql 'SELECT pg_reload_conf()' >/dev/null
        }
        # ic_boot_hba_warn: say, in the job's log, that the rule could not
        # be taken out, and how it goes.
        # shellcheck disable=SC2317  # reached through the EXIT trap too
        ic_boot_hba_warn() {
            ic_warn "could not take the boot test's replication rule out of the database's $hba_file: it lets the database's own user copy it over the loopback of its container without a password until the line '$hba_rule' is removed and the configuration reloaded, or the database container restarts"
        }

        # What a run killed before its trap left (a job whose worker died,
        # a server that rebooted): its containers, then its volume, which
        # pg_basebackup would refuse as a target that is not empty; and
        # the copy releases before 1.0.228 made inside the database's own
        # container, the size of the database.
        docker rm -f "$copy_container" "$odoo_container" "$pg_container" 2>/dev/null || true
        docker network rm "$boot_net" 2>/dev/null || true
        docker volume rm "$boot_volume" >/dev/null 2>&1 || true
        docker compose exec -T db rm -rf "/tmp/ic_boot_backup_${inst_id}" 2>/dev/null || true

        # Measure before copying (#91, plan of 6-nov-2026): the database's
        # size, as ``du`` counts its directory (what pg_basebackup copies,
        # its WAL included), and the free space of the disk the copy goes
        # to, read on the volume itself. A test that does not fit stops
        # here, with the instance as it was.
        db_id="$(docker compose ps -q db | head -n 1)"
        [ -n "$db_id" ] \
            || ic_boot_stop "The database of the instance is not running: there is nothing to copy. Nothing was changed. Start the instance and rebuild it again."
        data_dir="$(ic_boot_psql 'SHOW data_directory' | tr -d '[:space:]')" \
            || ic_die "could not ask the database where its data lives"
        size_kb="$(docker compose exec -T db du -sk "$data_dir" | awk 'NR == 1 { print $1 }')" \
            || size_kb=""
        case "$size_kb" in
            ''|*[!0-9]*) ic_die "could not measure the database at $data_dir" ;;
        esac
        docker volume create "$boot_volume" >/dev/null
        # PostgreSQL refuses a data directory in the 755 Docker creates, and
        # the SSH user may not chown: the volume is made postgres's, mode
        # 700, from a container, which reads its free space too.
        free_kb="$(docker run --rm -v "$boot_volume:/out" --user root --entrypoint sh "$pg_image" \
            -c 'chown postgres:postgres /out && chmod 700 /out && df -Pk /out | awk "NR == 2 { print \$4 }"')" \
            || free_kb=""
        case "$free_kb" in
            ''|*[!0-9]*) ic_die "could not read the free space of Docker's disk" ;;
        esac
        # The copy needs the database's size and a tenth more, for the WAL
        # of the test and what the update writes, and leaves free the 5 GiB
        # a restore leaves to the server's other instances
        # (``restore_space.RESERVE_BYTES``): 6-nov-2026, plan §2.3.
        need_kb=$(( size_kb * 11 / 10 + IC_BOOT_RESERVE_KB ))
        size_bytes=$(( size_kb * 1024 ))
        copy_s=$(( size_bytes / (IC_BOOT_ESTIMATE_MBPS * 1000000) ))
        budget_s=$(( IC_BOOT_BUDGET_BASE_S + size_bytes / (IC_BOOT_BUDGET_MBPS * 1000000) ))
        ic_log "boot test: the database takes $(ic_boot_gib "$size_kb"), Docker's disk has $(ic_boot_gib "$free_kb") free and the test needs $(ic_boot_gib "$need_kb"); the copy should take about $(( (copy_s + 59) / 60 )) min, and the test has $(( (budget_s + 59) / 60 )) min"
        if [ "$free_kb" -lt "$need_kb" ]; then
            ic_boot_stop "No room on the server for the boot test: the database takes $(ic_boot_gib "$size_kb"), Docker's disk has $(ic_boot_gib "$free_kb") free and the test needs $(ic_boot_gib "$need_kb") (the database, a tenth more for what the update writes, and $(ic_boot_gib "$IC_BOOT_RESERVE_KB") left to the server's other instances). Nothing was changed. Free space on the server, enlarge its disk, or turn off «Test the new image before applying it» on the instance."
        fi
        if [ -n "$cap_s" ] && [ "$budget_s" -gt $(( cap_s - IC_BOOT_TRAP_MARGIN_S )) ]; then
            ic_boot_stop "The boot test would need about $(( (budget_s + 1800) / 3600 )) h for a database of $(ic_boot_gib "$size_kb") (the copy at ${IC_BOOT_BUDGET_MBPS} MB/s and an hour to boot and update it), more than the $(( (cap_s + 1800) / 3600 )) h a step may run. Nothing was changed. Turn off «Test the new image before applying it» on the instance to rebuild without it."
        fi
        deadline=$(( SECONDS + budget_s ))

        # postgres-autoconf writes /etc/postgres/pg_hba.conf on every start
        # with no rule for a replication connection on the loopback (the
        # word ``all`` does not cover one; spike of 6-nov-2026): the test
        # adds its own, reloads, and its trap takes it out again.
        hba_file="$(ic_boot_psql 'SHOW hba_file' | tr -d '[:space:]')"
        [ -n "$hba_file" ] || ic_die "could not ask the database for its pg_hba.conf"
        hba_added=1
        # shellcheck disable=SC2016  # expanded by the container's sh
        docker compose exec -T db sh -c \
            'grep -qxF "$1" "$2" || printf "%s\n" "$1" >> "$2"' \
            sh "$hba_rule" "$hba_file"
        ic_boot_psql 'SELECT pg_reload_conf()' >/dev/null

        # One physical copy (no exclusive lock → zero downtime), straight
        # into the volume: pg_basebackup in a container of its own on the
        # database's network namespace, which reaches it on 127.0.0.1. The
        # copy keeps its backup_label and the WAL streamed while it ran, so
        # the throwaway PG recovers from the backup rather than as after a
        # crash. Under ``timeout`` with the test's budget; the shell itself
        # says every minute that it still copies, so a test orphaned by a
        # request cut short dies at its next line rather than at the end.
        # Without ``--rm``: a copy killed with 137 is read afterwards to
        # tell the kernel's out of memory from the budget, and removed then.
        ic_log "copying the database into the volume $boot_volume"
        copy_started=$SECONDS
        timeout --kill-after=60 "$budget_s" docker run --name "$copy_container" \
            --network "container:$db_id" -v "$boot_volume:/out" \
            --user postgres --entrypoint pg_basebackup "$pg_image" \
            -h 127.0.0.1 -U "$pg_user" -D /out -Fp -X stream --checkpoint=fast --no-sync &
        copy_pid=$!
        said=$SECONDS
        while kill -0 "$copy_pid" 2>/dev/null; do
            sleep 1
            if [ $(( SECONDS - said )) -ge 60 ]; then
                said=$SECONDS
                ic_log "still copying the database: $(( (SECONDS - copy_started) / 60 )) min so far"
            fi
        done
        copy_rc=0
        wait "$copy_pid" || copy_rc=$?
        copied_s=$(( SECONDS - copy_started ))
        # The rule is needed for the copy alone (Fable's review of #91,
        # 13-nov-2026): out now rather than when the test ends, hours
        # later. Not taken out, the trap tries again.
        if ic_boot_hba_drop; then
            hba_added=""
        else
            ic_boot_hba_warn
        fi
        copy_oom=""
        if [ "$copy_rc" -eq 137 ]; then
            copy_oom="$(docker inspect -f '{{.State.OOMKilled}}' "$copy_container" 2>/dev/null || true)"
        fi
        docker rm -f "$copy_container" >/dev/null 2>&1 || true
        # 137 is a KILL: timeout's own once the budget and its minute more
        # ran out, the kernel's out of memory, or someone removing the
        # container. Docker says the second; the time spent, the first.
        if [ "$copy_rc" -eq 124 ] \
            || { [ "$copy_rc" -eq 137 ] && [ "$copied_s" -ge "$budget_s" ]; }; then
            ic_boot_stop "The boot test ran past its time budget of $(( (budget_s + 59) / 60 )) min while it copied the database. Nothing was changed."
        fi
        if [ "$copy_rc" -eq 137 ] && [ "$copy_oom" = "true" ]; then
            ic_boot_stop "The copy of the database ran out of memory: the server's kernel killed it after $(( (copied_s + 59) / 60 )) min. Nothing was changed. Free memory on the server, or turn off «Test the new image before applying it» on the instance."
        fi
        if [ "$copy_rc" -eq 137 ]; then
            ic_boot_stop "The copy of the database was killed after $(( (copied_s + 59) / 60 )) min, before its time budget of $(( (budget_s + 59) / 60 )) min ran out: the server ran out of memory, or something removed its container. Nothing was changed."
        fi
        [ "$copy_rc" -eq 0 ] || ic_die "the copy of the database failed (exit $copy_rc)"
        ic_log "copied in ${copied_s} s, about $(( size_bytes / 1000000 / (copied_s > 0 ? copied_s : 1) )) MB/s"

        # The throwaway PG lives on a network of its own, never on the
        # project's. A container holding an endpoint on the project
        # network blocks any reconciliation docker compose decides to do
        # mid-test: compose cannot remove the network to recreate it, so
        # the step dies with 'has active endpoints' and leaves the stack
        # half stopped — twice on 2026-08-13, taking every instance on the
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
        # A staging rendered by doodba template v10 is different again:
        # its ``odoo`` runs inside the network namespace of the
        # template's ``gatekeeper`` (``network_mode: service:gatekeeper``),
        # and Docker refuses to connect a container that shares a
        # namespace to any other network — the first pull request
        # preview under v10 died on exactly that (QA, 2026-10-21). There
        # the throwaway PG joins the gatekeeper's namespace instead, and
        # the boot container finds it on 127.0.0.1: the gatekeeper's
        # rules accept the loopback and the private ranges, so the
        # staging's egress stays filtered while the test runs.
        #
        # A staging rendered before v10 has no gatekeeper until its first
        # rebuild under v10 — this one — brings the stack up: copier has
        # already rewritten the files, but the old odoo, proxy_general
        # and odoo_net_setup are still the ones running (acme-staging,
        # the first staging rebuilt under v10, 2026-10-22). The test
        # starts that one service, alone, and the trap removes it if the
        # test fails. Docker Compose will not let the boot container
        # share the namespace of a one-off gatekeeper ("container
        # missing"), so it has to be the service's own.
        gatekeeper=""
        gatekeeper_started=""
        if printf '%s\n' "$services" | grep -qx gatekeeper; then
            gatekeeper="$(docker compose ps -q gatekeeper 2>/dev/null | head -n 1)"
            if [ -z "$gatekeeper" ]; then
                ic_log "no gatekeeper yet (first rebuild under template v10): starting it for the test"
                gatekeeper_started=1
                docker compose up -d --no-deps --wait --wait-timeout 120 gatekeeper \
                    || ic_die "the gatekeeper did not start: nothing to boot the test in"
                gatekeeper="$(docker compose ps -q gatekeeper 2>/dev/null | head -n 1)"
            fi
            [ -n "$gatekeeper" ] \
                || ic_die "the gatekeeper is not running: nothing to boot the test in"
        fi
        if [ -n "$gatekeeper" ]; then
            pg_host="127.0.0.1"
            pg_network="container:$gatekeeper"
        else
            pg_host="$pg_container"
            pg_network="$boot_net"
            docker network create "$boot_net"
        fi
        docker run -d --name "$pg_container" --network "$pg_network" \
            -v "$boot_volume:/var/lib/postgresql/data" \
            --user postgres --entrypoint postgres "$pg_image" \
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
        # network right after — unless it already shares the gatekeeper's
        # namespace, where the throwaway PG waits on the loopback. Doodba's
        # own wait for Postgres has no deadline — an unreachable database
        # hung the job for good — so it is replaced by a bounded one; and
        # the unaccent hook, which would query before the network is
        # joined, is skipped: the copy already carries the extension.
        # ``--no-deps``: the services it depends on are running, and
        # without it Compose recreates any whose definition the template
        # changed — under v10 the live ``db``, restarted mid-test. What is
        # left of the budget bounds the boot as it bounded the copy.
        ic_boot_join() {
            [ -n "$gatekeeper" ] \
                || docker network connect "$boot_net" "$odoo_container"
        }
        remaining=$(( deadline - SECONDS ))
        [ "$remaining" -gt 0 ] \
            || ic_boot_stop "The boot test ran past its time budget of $(( (budget_s + 59) / 60 )) min before it booted the new image. Nothing was changed."
        set +e
        # shellcheck disable=SC2016  # expanded by the container's bash
        ic_compose_run -d --no-deps --name "$odoo_container" \
            -e "PGHOST=$pg_host" -e PGPORT=5432 \
            -e WAIT_DB=false -e UNACCENT=false odoo \
            bash -c 'for _ in $(seq 120); do
                         psql -l >/dev/null 2>&1 \
                             && exec click-odoo-update --database "$0"
                         sleep 1
                     done
                     echo "boot test: the throwaway postgres never answered" >&2
                     exit 1' "$dbname" \
            && ic_boot_join \
            && timeout "$remaining" docker logs -f "$odoo_container"
        ic_test_exit=$?
        if [ "$ic_test_exit" -eq 0 ]; then
            ic_test_exit="$(docker wait "$odoo_container")"
        fi
        set -e
        if [ "$ic_test_exit" -eq 124 ]; then
            ic_boot_stop "The boot test ran past its time budget of $(( (budget_s + 59) / 60 )) min while it booted the new image. Nothing was changed."
        fi
        # Cleanup and stack restore both run from the EXIT trap.
        exit "$ic_test_exit"
        ;;

    *)
        ic_die "unknown operation: $op"
        ;;
esac
