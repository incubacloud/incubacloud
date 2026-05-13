"""
Rebuild executor — updates a running doodba instance via ``copier update``.

Flow:
  1. Upload the same copier answer files as a fresh deploy (answers.yml,
     repos.yaml, addons.yaml, backup.env, odoo.conf, pip/apt, ic_connect).
  2. Commit any dirty files in the instance git repo so copier finds a clean
     working tree (required by copier update).
  3. Run ``copier update`` — regenerates every config file from the current
     template version and the new answers.  This is the single source of
     truth for all configuration: SMTP, domain, backup vars, etc.
  4. Post-copier fixups identical to the deploy executor:
     - Fix docker-compose.yml symlink
     - Overwrite backup.env (adds AWS_ENDPOINT_URL which copier doesn't know)
     - Overwrite repos.yaml / addons.yaml / odoo.conf / pip / apt
     - Copy incubacloud_connect
  5. Rebuild the Odoo Docker image and restart.
"""
from datetime import datetime, timezone

from .abstract_executor import sql_escape_literal
from .deploy_instance_executor import DeployInstanceExecutor


class RebuildInstanceExecutor(DeployInstanceExecutor):
    """Rebuild a running doodba instance using copier update."""

    _job_type = "rebuild_instance"

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    async def before_execute(self, transport):
        inst = self._inst()
        if not inst:
            raise ValueError("rebuild_instance job has no instance_id")
        await self._preflight_addon_check()
        self._sys(f"Preparing rebuild for '{inst.name}'...")
        await self._upload_copier_files(transport)

    def get_commands(self):
        inst = self._inst()
        name = inst.doodba_project_name
        d = self._inst_dir(inst)
        src = f"{d}/odoo/custom/src"
        confd = f"{d}/odoo/custom/conf.d"

        tmp_answers = self._tmp("answers.yml")
        tmp_addons = self._tmp("addons.yaml")
        tmp_repos = self._tmp("repos.yaml")
        tmp_conf = self._tmp("odoo.conf")
        tmp_pip = self._tmp("pip.txt")
        tmp_apt = self._tmp("apt.txt")

        # Read-first guard: full_setup seeds init.defaultBranch once per
        # host; the fallback write here covers legacy hosts only and
        # avoids racing siblings for the ~/.gitconfig lock.
        git_cfg = (
            '(git config --global --get init.defaultBranch >/dev/null 2>&1'
            ' || git config --global init.defaultBranch master)'
        )
        path_prefix = (
            'export PATH="$HOME/.local/bin:$PATH"'
            f' && {git_cfg}'
        )
        copier_bin = "$HOME/.local/bin/copier"

        compose_target = (
            "prod.yaml" if inst.environment == "production" else "test.yaml"
        )
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        has_smtp = bool(inst.smtp_relay_host)
        needs_full_rebuild = (
            inst.rebuild_fingerprint != inst.last_rebuild_fingerprint
        )
        if needs_full_rebuild:
            self._sys(
                "Full rebuild: configuration changed since last deploy."
            )
        else:
            self._sys(
                "Fast rebuild: only code changes detected, using Docker cache."
            )

        cmds = [
            # 1. Commit any dirty files so copier update finds a clean repo.
            #    Pass user config inline — the server may not have git configured.
            (
                "Commit dirty files",
                f"cd {d}"
                f" && git add -A"
                f" ; git diff --cached --quiet"
                f" || git"
                f" -c user.email='system@incubacloud'"
                f" -c user.name='IncubaCloud'"
                f" commit -m 'IncubaCloud rebuild {ts}' --no-verify"
                f" || true",
            ),
            # 2. Run copier update — regenerates all config files.
            (
                "Update with copier",
                f"{path_prefix} && "
                f"{copier_bin} update --defaults --trust "
                f"--data-file {tmp_answers} {d}",
            ),
            # 2b. Resolve copier merge conflicts: keep new version (after =======)
            (
                "Resolve merge conflicts",
                f"cd {d} && for f in prod.yaml test.yaml common.yaml;"
                f" do if grep -q '<<<<<<' \"$f\" 2>/dev/null; then"
                f" sed -i '/^<<<<<<< /,/^=======/d;/^>>>>>>> /d' \"$f\";"
                f" fi; done || true",
            ),
            # 3. Fix docker-compose.yml symlink (copier points to devel.yaml).
            (
                "Fix docker-compose symlink",
                f"rm -f {d}/docker-compose.yml"
                f" && ln -s {compose_target} {d}/docker-compose.yml",
            ),
        ]

        # 3b. Strip the smtp service from prod.yaml when no SMTP relay
        #     is configured (same as deploy executor).
        if not has_smtp and inst.environment == 'production':
            cmds.append((
                "Strip smtp service (not configured)",
                f"awk '/^  smtp:/ {{skip=1; next}}"
                f" skip && /^  [a-z]/ {{skip=0}}"
                f" skip && /^[^ ]/ {{skip=0}}"
                f" !skip' {d}/prod.yaml > {d}/prod.yaml.tmp"
                f" && mv {d}/prod.yaml.tmp {d}/prod.yaml",
            ))

        if not self._backup_enabled() and inst.environment == 'production':
            cmds.append((
                "Strip backup service (not configured)",
                f"cd {d} && for f in prod.yaml common.yaml; do"
                f" [ -f \"$f\" ] || continue;"
                f" awk '/^  backup:/ {{skip=1; next}}"
                f" skip && /^  [a-z]/ {{skip=0}}"
                f" skip && /^[^ ]/ {{skip=0}}"
                f" !skip' \"$f\" > \"$f.tmp\""
                f" && mv \"$f.tmp\" \"$f\";"
                f" done",
            ))

        cmds += [
            # 4. Overwrite backup.env with ours (adds AWS_ENDPOINT_URL).
            (
                "Write backup.env",
                f"f={self._tmp('backup.env')};"
                f" [ -f \"$f\" ] && mv \"$f\" {d}/.docker/backup.env || true",
            ),
            # 4b. Ensure .docker/incubacloud.env exists (generate once,
            #     never overwrite — survives rebuilds).
            (
                "Ensure incubacloud.env",
                f'[ -f {d}/.docker/incubacloud.env ] || '
                f'python3 -c "'
                f"from cryptography.fernet import Fernet; "
                f"print(f'INCUBACLOUD_SECRET_KEY={{Fernet.generate_key().decode()}}')"
                f'" > {d}/.docker/incubacloud.env',
            ),
            # 4c. Re-inject incubacloud.env in prod.yaml and test.yaml.
            #     The env_file block lives in those files (not common.yaml).
            #     Copier update regenerates them, stripping our addition.
            (
                "Inject incubacloud.env in prod.yaml and test.yaml",
                f"cd {d} && "
                f"for f in prod.yaml test.yaml; do "
                f"  [ -f \"$f\" ] || continue; "
                f"  grep -q 'incubacloud.env' \"$f\" || "
                f"  sed -i '/\\.docker\\/odoo\\.env/a\\      - .docker/incubacloud.env'"
                f" \"$f\"; "
                f"done",
            ),
            # 4d. Write docker-compose.override.yml with resource limits.
            (
                "Write resource limits",
                f"f={self._tmp('override.yml')};"
                f" [ -f \"$f\" ] &&"
                f" mv \"$f\" {d}/docker-compose.override.yml || true",
            ),
            # 5. Overwrite repos.yaml and addons.yaml.
            ("Write addons.yaml", f"mv {tmp_addons} {src}/addons.yaml"),
            ("Write repos.yaml", f"mv {tmp_repos} {src}/repos.yaml"),
            # 6. Copy incubacloud_connect.
            (
                "Install incubacloud_connect",
                f"mkdir -p {src}/private"
                f" && cp -r /tmp/.ic-modules-{name}/incubacloud_connect"
                f" {src}/private/"
                f" && rm -rf /tmp/.ic-modules-{name}",
            ),
            # 7. Write Odoo conf, pip.txt, apt.txt.
            (
                "Write Odoo conf",
                f"mkdir -p {confd} && mv {tmp_conf} {confd}/{name}.conf",
            ),
            (
                "Write pip.txt",
                f"mkdir -p {d}/odoo/custom/dependencies"
                f" && mv {tmp_pip} {d}/odoo/custom/dependencies/pip.txt",
            ),
            (
                "Write apt.txt",
                f"mv {tmp_apt} {d}/odoo/custom/dependencies/apt.txt",
            ),
            # 8. Remove the temp answers file.
            ("Remove temp files", f"rm -f {tmp_answers}"),
            # 9. Capture services list for on_success (parsed from stdout).
            ("List services", f"cd {d} && docker compose config --services"),
            # 10. Rebuild Odoo image.
            #     Smart rebuild: full (--pull --no-cache) when config that
            #     affects the image has changed; cached otherwise.
            (
                "Rebuild Odoo image",
                (
                    f"cd {d} && docker compose build"
                    f" --pull --no-cache odoo"
                ) if needs_full_rebuild else (
                    f"cd {d} && docker compose build odoo"
                ),
                {"stop_on_failure": True},
            ),
        ]
        # 11 + 12: safe boot test and actual module update both rely
        # on ``click-odoo-update``. When ``auto_update`` is disabled
        # the operator manages module state manually, so skipping both
        # avoids surprising DB migrations — at the cost of losing the
        # boot validation. ``inst.auto_update`` defaults to True for
        # all pre-existing instances, preserving current behavior.
        if inst.auto_update:
            cmds += [
            # 11. Safe boot test: clone the production DB, boot Odoo
            #     with the new image against the clone, and clean up.
            #     If the boot fails, stop_on_failure prevents up -d
            #     so the instance keeps running with the old image.
            (
                "Test new image (safe boot check)",
                (
                    # Per-instance suffix on every path we touch on
                    # both sides of the boundary so the safe-boot step
                    # is fully scoped: the in-container basebackup dir,
                    # the host bind mount and the temporary postgres
                    # container all share the same instance id. Two
                    # rebuilds for different tenants on the same host
                    # never overlap (already true via separate compose
                    # projects, but the symmetry makes the contract
                    # readable); two retries against the same instance
                    # share the path *by design* — pre-cleanup steps
                    # below take care of leftovers from interrupted
                    # earlier runs.
                    f"cd {d}"
                    # Defensive pre-cleanup. ``pg_basebackup`` refuses
                    # to write into a non-empty target, and the host
                    # bind mount may carry a UID-70 directory left by
                    # a chown step from an interrupted previous run.
                    # Both rms are idempotent — they are no-ops when
                    # the paths are absent — and both go through the
                    # right privilege boundary: the in-container path
                    # via ``docker compose exec`` (root in the db
                    # container), the host path via an ephemeral
                    # ``alpine`` container (root over the bind mount).
                    f" && docker compose exec -T db"
                    f" rm -rf /tmp/ic_boot_backup_{inst.id}"
                    f" && docker run --rm -v /tmp:/host_tmp alpine"
                    f" rm -rf /host_tmp/ic_boot_{inst.id}"
                    # pg_basebackup: physical copy of the PG cluster
                    # without exclusive lock — zero downtime.
                    f" && docker compose exec -T db"
                    f" pg_basebackup"
                    f" -U {inst.postgres_username or 'odoo'}"
                    f" -D /tmp/ic_boot_backup_{inst.id}"
                    f" --checkpoint=fast --no-sync -X fetch"
                    # Drop ``backup_label`` *inside* the db container,
                    # before the host ever sees the files. Postgres
                    # treats its presence as "starting from a backup,
                    # replay WAL"; we already fetched WAL in-band so we
                    # want a direct startup. Doing this inside the
                    # container avoids a host-side rm later — once the
                    # bind mount is chowned to UID 70 the host user
                    # cannot remove anything in there anymore.
                    f" && docker compose exec -T db"
                    f" rm -f /tmp/ic_boot_backup_{inst.id}/backup_label"
                    # Copy to host and clean up inside container.
                    f" && docker compose cp"
                    f" db:/tmp/ic_boot_backup_{inst.id}"
                    f" /tmp/ic_boot_{inst.id}"
                    f" && docker compose exec -T db"
                    f" rm -rf /tmp/ic_boot_backup_{inst.id}"
                    # Flip ownership to alpine's postgres UID via an
                    # ephemeral root container. The host user lacks
                    # CAP_CHOWN, so a host-side ``chown 70:70`` would
                    # fail per file with "Operation not permitted".
                    f" && docker run --rm"
                    f" -v /tmp/ic_boot_{inst.id}:/data"
                    f" alpine chown -R 70:70 /data"
                    # Best-effort cleanup of any leftover container from
                    # a previously interrupted run. Wrap in parens so a
                    # failure here doesn't escape — the bare ``; true``
                    # we used before split the entire ``&&`` chain in
                    # two and let ``docker run`` proceed even when the
                    # earlier chown step had failed.
                    f" && (docker rm -f ic_boot_pg_{inst.id} 2>/dev/null || true)"
                    # Start a temporary PG on the compose network
                    f" && docker run -d"
                    f" --name ic_boot_pg_{inst.id}"
                    f" --network {name}_default"
                    f" -v /tmp/ic_boot_{inst.id}:"
                    f"/var/lib/postgresql/data"
                    f" --user postgres"
                    f" --entrypoint postgres"
                    f" postgres:"
                    f"{inst.postgres_version or '17'}-alpine"
                    f" -D /var/lib/postgresql/data"
                    f" && sleep 5"
                    # Boot test: click-odoo-update against the
                    # temporary PG to verify the new image works.
                    f" && docker compose run --rm"
                    f" -e PGHOST=ic_boot_pg_{inst.id}"
                    f" odoo click-odoo-update"
                    f" --database {inst.postgres_dbname or 'prod'}"
                    # Always clean up, then propagate the test exit
                    # code. The host bind mount is owned by UID 70
                    # after the chown above, so the final unlink runs
                    # through another ephemeral container — same
                    # CAP_CHOWN gap in reverse. The in-container
                    # basebackup dir is wiped here too in case any
                    # earlier step short-circuited the &&-chain
                    # before the mid-chain in-container rm ran.
                    f" ; IC_TEST_EXIT=$?"
                    f" ; docker rm -f ic_boot_pg_{inst.id} 2>/dev/null"
                    f" ; docker compose exec -T db"
                    f" rm -rf /tmp/ic_boot_backup_{inst.id} 2>/dev/null"
                    f" ; docker run --rm -v /tmp:/host_tmp alpine"
                    f" rm -rf /host_tmp/ic_boot_{inst.id}"
                    f" ; exit $IC_TEST_EXIT"
                ),
                {"stop_on_failure": True},
            ),
            # 12. Update changed modules in the real DB.
            #     click-odoo-update compares checksums stored in the DB
            #     against the code in the new image and updates only
            #     modules that actually changed.
            (
                "Update changed modules",
                f"cd {d} && docker compose run --rm odoo"
                f" click-odoo-update"
                f" --database {inst.postgres_dbname or 'prod'}",
                {"stop_on_failure": True},
            ),
            ]
        cmds += [
            # 12b. Ensure incubacloud_connect is installed (restored DBs
            #      may not have it; click-odoo-update only updates, not installs).
            (
                "Ensure incubacloud_connect",
                f"cd {d} && docker compose run --rm odoo"
                f" odoo -d {inst.postgres_dbname or 'prod'}"
                f" -i incubacloud_connect"
                f" --stop-after-init --no-http",
            ),
            # 13. Restart all services with the new image.
            #     --remove-orphans drops containers no longer in the compose
            #     file (e.g. backup or smtp when the operator removes the
            #     corresponding service from the instance config) so they
            #     don't linger.
            (
                "Restart instance",
                f"cd {d} && docker compose up -d --remove-orphans",
            ),
            # 13. Set web.base.url and report.url in ir.config_parameter.
            #     Wrapped in a DO block: succeeds silently if the table does
            #     not exist yet (e.g. DB not yet initialised).
            #     ``base_url`` is sql-escaped as a defense-in-depth layer
            #     on top of the @api.constrains regex on
            #     cloud.instance.domain.hostname.
            (
                "Set system parameters",
                f"cd {d} && docker compose exec -T db"
                f" psql -U {inst.postgres_username or 'odoo'}"
                f" -d {inst.postgres_dbname or 'prod'}"
                f" -c \"DO \\$\\$ BEGIN"
                f" IF EXISTS (SELECT FROM information_schema.tables"
                f" WHERE table_schema='public'"
                f" AND table_name='ir_config_parameter') THEN"
                f" INSERT INTO ir_config_parameter (key,value) VALUES"
                f" ('web.base.url','{sql_escape_literal(self._base_url())}'),"
                f" ('report.url','http://localhost:8069')"
                f" ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value;"
                f" END IF; END \\$\\$;\"",
            ),
        ]
        return cmds

    def parse_results(self, results):
        # "List services" exit_status != 0 only means compose isn't configured;
        # don't treat it as a fatal error — the rebuild itself may have worked.
        ignored = {"List services"}
        return [
            f"'{label}' exited with status {data.get('exit_status')}"
            for label, data in results.items()
            if data.get("exit_status", 1) != 0 and label not in ignored
        ]

    async def on_success(self, results):
        inst = self._inst()
        write_vals = {"status": "ok"}
        services_out = (
            results.get("List services", {}).get("stdout") or ""
        )
        services = [s.strip() for s in services_out.splitlines() if s.strip()]
        if services:
            write_vals["compose_services"] = ",".join(services)
        # Save rebuild fingerprint for smart rebuild decision next time
        write_vals['last_rebuild_fingerprint'] = (
            inst.rebuild_fingerprint
        )
        inst.write(write_vals)
        self._sys(
            f"✓ '{inst.name}' rebuilt and restarted"
            f" with updated configuration."
        )

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        if inst:
            inst.write({"status": "error"})
