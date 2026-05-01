import asyncio
import configparser
import io
import logging
import os
import re
import urllib.error
import urllib.request
from contextlib import suppress

import yaml

from ..controllers._data_load._helpers import _parse_github_repo_path
from ..github.client import GitHubAppClient
from ..github.http_utils import safe_urlopen
from .abstract_executor import AbstractSSHExecutor, sql_escape_literal
from ._repo_requirements import (
    _apply_excludes, _parse_repo_addons, _repo_alias,
    detect_addon_conflicts, detect_pip_conflicts, create_pip_conflict_alert,
    fetch_repo_addons,
)

_logger = logging.getLogger(__name__)

# Path to the incubacloud_connect module.  Navigate from __file__ via
# abspath (stays in auto/addons/) then realpath the result to resolve
# the doodba symlink into the actual directory with real files — sftp.put
# cannot copy symlinks.
_IC_CONNECT_MODULE = os.path.realpath(os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 '..', '..', 'incubacloud_connect')
))

# Prefix for all temp files uploaded to the remote host
_TMP_PREFIX = "/tmp/.incubacloud"


_SSH_RE = re.compile(r'^git@github\.com:(.+?)(?:\.git)?$')

_ADDONS_CONFLICT_RE = re.compile(
    r"AddonsConfigError: Addon (\w+) defined in several repos \{([^}]+)\}"
)


def _github_authed_url(url, token):
    """Normalize a GitHub repo URL to HTTPS and inject a token if available.

    Converts SSH URLs (git@github.com:org/repo.git) to HTTPS so the remote
    server doesn't need an SSH key for GitHub.  Then injects the token for
    private repos when one is available.
    """
    if not url:
        return url
    url = url.strip()
    # Convert SSH → HTTPS
    m = _SSH_RE.match(url)
    if m:
        url = f"https://github.com/{m.group(1)}.git"
    # Ensure HTTPS scheme
    if not url.startswith(('https://', 'http://')):
        url = f"https://{url}"
    # Inject token into HTTPS URLs
    if token and "github.com" in url:
        url = url.replace(
            "https://", f"https://x-access-token:{token}@", 1,
        )
    return url


def _smtp_canonical_domain(inst):
    """Derive the SMTP canonical domain for SRS and the mailserver hostname.

    Priority:
      1. Domain part of smtp_relay_user email (e.g. user@example.com → example.com)
      2. Strip first label from smtp_relay_host (e.g. mail.example.com → example.com)
      3. Empty string (copier will skip SMTP service)
    """
    user = inst.smtp_relay_user or ""
    if "@" in user:
        return user.split("@")[-1]
    host = inst.smtp_relay_host or ""
    parts = host.split(".")
    if len(parts) > 2:
        return ".".join(parts[1:])
    return host


class DeployInstanceExecutor(AbstractSSHExecutor):
    """Deploy a doodba instance via copier copy."""

    _job_type = "deploy_instance"

    # ── Helpers ────────────────────────────────────────────────────────────

    def _get_github_tokens(self):
        """Return available tokens: (app_token, pat) — either may be None."""
        svc = self.env["cloud.github.credential.service"]
        app_token = None
        with suppress(Exception):
            creds = svc.get_credentials()
            app_token = GitHubAppClient(creds).get_installation_token()
        pat = None
        with suppress(Exception):
            pat = svc.get_pat()
        return app_token, pat

    def _get_token_for_repo(self, repo_url, app_token, pat, cache):
        """Check which token can access a repo.

        App first, PAT fallback. Caches results by owner to avoid
        repeated API calls for repos in the same org.
        Returns the working token or None.
        """
        url = (repo_url or "").strip()
        if not url:
            return None

        try:
            owner, repo = _parse_github_repo_path(url)
        except ValueError:
            return app_token or pat

        # Check cache by owner — repos in the same org use the same token
        if owner in cache:
            return cache[owner]

        api = f"https://api.github.com/repos/{owner}/{repo}"
        for token in (app_token, pat):
            if not token:
                continue
            req = urllib.request.Request(api, headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            })
            try:
                with safe_urlopen(req, timeout=5):  # nosec B310 — hardcoded https://api.github.com
                    cache[owner] = token
                    return token
            except Exception:
                continue

        fallback = app_token or pat
        cache[owner] = fallback
        return fallback

    def _inst(self):
        return self.job.instance_id

    def _tmp(self, suffix):
        """Return a /tmp path namespaced by instance name."""
        return f"{_TMP_PREFIX}-{self._inst().doodba_project_name}-{suffix}"

    def _backup_enabled(self):
        """Whether the backup container should be provisioned for this deploy.

        Hook: inheriting executors can override to gate the backup
        container on their own business rules (e.g. disable for some
        instance categories regardless of backend availability).
        """
        inst = self._inst()
        bb = inst.effective_backup_backend
        return bool(bb and bb.backup_dst)

    def _backup_retention(self):
        """Effective retention string (e.g. '7D', '3M') for the backup job.

        Hook: inheriting executors can override to source the retention
        from their own logic (e.g. a per-instance policy) instead of
        the backend's default.
        """
        inst = self._inst()
        bb = inst.effective_backup_backend
        return (bb.backup_retention or '3M').strip() if bb else '3M'

    def _backup_env_content(self):
        """Generate .docker/backup.env content, or None if no backup backend."""
        if not self._backup_enabled():
            return None
        inst = self._inst()
        # sudo: executor is trusted; secret fields are restricted to managers.
        bb = inst.effective_backup_backend.sudo()
        lines = []
        if bb.s3_access_key_id:
            lines.append(f"AWS_ACCESS_KEY_ID={bb.s3_access_key_id}")
        if bb.s3_secret_access_key:
            lines.append(f"AWS_SECRET_ACCESS_KEY={bb.s3_secret_access_key}")
        if bb.s3_endpoint_url:
            lines.append(f"AWS_ENDPOINT_URL={bb.s3_endpoint_url}")
        lines.append(f"PASSPHRASE={bb.passphrase or ''}")
        # Override retention if different from copier default (3M)
        retention = self._backup_retention()
        if retention != '3M':
            lines.append(
                f"JOB_800_WHAT=dup --force remove-older-than"
                f" {retention} $DST"
            )
        return "\n".join(lines) + "\n"

    def _build_answers(self):
        """Return a dict with all copier template answers."""
        # sudo: executor is trusted; secret fields have group restrictions.
        inst = self._inst().sudo()

        # Build domain entries from domain_ids
        entries = []
        for d in inst.domain_ids:
            hostname = (
                (d.hostname or "")
                .replace("https://", "")
                .replace("http://", "")
                .strip("/")
            )
            if not hostname:
                continue
            entry = {"hosts": [hostname]}
            if d.redirect_to:
                entry["redirect_to"] = d.redirect_to
            entries.append(entry)

        if inst.environment == "production":
            domains_prod = entries
            domains_test = []
        else:
            domains_prod = []
            domains_test = entries

        bb = inst.effective_backup_backend
        if bb:
            bb = bb.sudo()
        has_backup = self._backup_enabled()

        answers = {
            "project_author": inst.project_id.project_author or "IncubaCloud",
            "project_license": inst.project_id.project_license or "BSL-1.0",
            "project_name": inst.doodba_project_name,
            "odoo_version": float(inst.odoo_version),
            "odoo_initial_lang": inst.odoo_initial_lang.code or "en_US",
            "odoo_admin_password": inst.odoo_admin_password or "",
            "odoo_proxy": inst.odoo_proxy or "traefik",
            "postgres_version": str(inst.postgres_version or "17"),
            "postgres_dbname": inst.postgres_dbname or "prod",
            "postgres_username": inst.postgres_username or "odoo",
            "postgres_password": inst.postgres_password or "",
            "domains_prod": domains_prod,
            "domains_test": domains_test,
            "smtp_relay_host": inst.smtp_relay_host or "",
            "smtp_relay_port": inst.smtp_relay_port or 587,
            "smtp_relay_version": "13",
            "smtp_relay_user": inst.smtp_relay_user or "",
            "smtp_relay_password": inst.smtp_relay_password or "",
            "smtp_default_from": inst.smtp_relay_user or inst.odoo_admin_email or "",
            # canonical_default = sending domain (from relay user email if set,
            # else strip first subdomain off relay host, e.g. mail.x.com → x.com)
            "smtp_canonical_default": _smtp_canonical_domain(inst),
            "smtp_canonical_domains": [],
            # Backup defaults (always empty/disabled; overridden below when
            # a backup backend is configured and enabled).
            "backup_dst": "",
            "backup_image_version": "",
            "backup_email_from": "",
            "backup_email_to": "",
            "backup_smtp_report_success": False,
            "backup_deletion": False,
            "backup_tz": "UTC",
            "backup_aws_access_key_id": "",
            "backup_aws_secret_access_key": "",
            "backup_passphrase": "",
        }
        if has_backup and bb:
            answers.update({
                "backup_dst": inst.instance_backup_dst or "",
                "backup_image_version": bb.backup_image_version or "latest",
                "backup_email_from": bb.email_from or "",
                "backup_email_to": bb.email_to or "",
                "backup_smtp_report_success": bb.smtp_report_success,
                "backup_deletion": bb.deletion_via_cron,
                "backup_tz": bb.backup_tz or "UTC",
                "backup_aws_access_key_id": bb.s3_access_key_id or "",
                "backup_aws_secret_access_key": bb.s3_secret_access_key or "",
                "backup_passphrase": bb.passphrase or "",
            })
        return answers

    def _repos_yaml_content(self):
        """Build repos.yaml: odoo first, then the instance's git repos.

        For each repo, verifies which token (App or PAT) has access
        and injects the correct one into the URL.
        """
        inst = self._inst()
        data = {}
        app_token, pat = self._get_github_tokens()

        # odoo is always first — public, no token needed
        odoo_merge = inst.odoo_commit_sha or "$ODOO_VERSION"
        data["odoo"] = {
            "defaults": {"depth": "$DEPTH_DEFAULT"},
            "remotes": {"odoo": "https://github.com/odoo/odoo.git"},
            "target": "odoo $ODOO_VERSION",
            "merges": [f"odoo {odoo_merge}"],
        }

        token_cache = {}  # owner → token

        for repo in inst.repo_ids:
            raw_url = (repo.url or "").strip()
            alias = _repo_alias(raw_url)
            branch = (repo.branch or "main").strip()
            if not alias or not raw_url:
                continue
            token = self._get_token_for_repo(
                raw_url, app_token, pat, token_cache,
            )
            url = _github_authed_url(raw_url, token)
            merge_ref = repo.commit_sha or branch
            data[alias] = {
                "defaults": {"depth": "$DEPTH_DEFAULT"},
                "merges": [f"{alias} {merge_ref}"],
                "remotes": {alias: url},
                "target": f"{alias} {branch}",
            }

        return yaml.dump(
            data,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    async def _addons_yaml_content(self):
        """Build addons.yaml from the instance's repo addons config.

        When a repo has ``excludes`` set and ``addons`` is empty (*), we
        fetch the repo's full module list from GitHub (via
        ``asyncio.to_thread``) so we can generate an explicit include list
        that omits the excluded modules.  This avoids the
        ``AddonsConfigError: Addon X defined in several repos`` error that
        doodba raises when the same module name appears in two repos.
        """
        inst = self._inst()
        data = {}

        for repo in inst.repo_ids:
            alias = _repo_alias(repo.url)
            if not alias:
                continue

            addons_str = (repo.addons or "").strip()
            excludes_str = (repo.excludes or "").strip()

            if not excludes_str:
                # Fast path: no excludes — use the existing simple logic
                data[alias] = _parse_repo_addons(addons_str)
                continue

            if addons_str:
                # Explicit include list + excludes: just subtract
                incl = _parse_repo_addons(addons_str)
                data[alias] = _apply_excludes(incl, excludes_str)
            else:
                # addons = "*" with excludes: need the full module list
                modules = await asyncio.to_thread(
                    fetch_repo_addons,
                    self.job.env,
                    repo.url,
                    repo.branch or "main",
                )
                if modules is None:
                    _logger.warning(
                        "Could not fetch module list for %s — "
                        "excludes ignored, using all modules (*)",
                        repo.url,
                    )
                    data[alias] = ["*"]
                else:
                    data[alias] = _apply_excludes(modules, excludes_str)

        return yaml.dump(
            data,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        ) if data else ""

    # Services present per environment. backup and smtp only exist in
    # production doodba deployments; including them in the override for
    # test/staging would cause a Docker Compose validation error.
    _TEST_SERVICES = ('odoo', 'db')

    def _prod_services(self):
        """Return the services present in a production compose file.

        Only include services that are actually rendered by copier:
        - smtp only when an SMTP relay host is configured.
        - backup only when a backup backend is enabled.
        Otherwise the override would reference phantom services and
        ``docker compose`` fails with "neither image nor build".
        """
        inst = self._inst()
        svcs = ['odoo', 'db']
        if self._backup_enabled():
            svcs.append('backup')
        if inst.smtp_relay_host:
            svcs.append('smtp')
        return tuple(svcs)

    def _resource_override_content(self):
        """Generate docker-compose.override.yml with resource limits.

        Only includes services that actually exist in the target
        environment's compose file to avoid Docker Compose errors.
        """
        inst = self._inst()
        allowed = (
            self._prod_services()
            if inst.environment == 'production'
            else self._TEST_SERVICES
        )
        field_map = {
            'odoo':   ('odoo_memory_limit',   'odoo_cpus'),
            'db':     ('db_memory_limit',     'db_cpus'),
            'backup': ('backup_memory_limit', 'backup_cpus'),
            'smtp':   ('smtp_memory_limit',   'smtp_cpus'),
        }
        services = {}
        for svc in allowed:
            mem_field, cpu_field = field_map[svc]
            mem = getattr(inst, mem_field, '') or ''
            cpus = getattr(inst, cpu_field, 0) or 0
            if mem or cpus:
                entry = {}
                if mem:
                    entry['mem_limit'] = mem
                if cpus:
                    entry['cpus'] = cpus
                services[svc] = entry
        if not services:
            return None
        data = {'services': services}
        return yaml.dump(
            data, default_flow_style=False, allow_unicode=True,
        )

    def _conf_content(self):
        """Return Odoo conf with proxy_mode forced True.

        Every instance we deploy is fronted by Traefik (the host's
        traefik service per ``data/traefik/``). Without proxy_mode,
        Odoo treats X-Forwarded-* headers naively, breaking URL
        building (web.base.url), OAuth callbacks, OIDC redirects, and
        per-IP rate-limit attribution. The platform enforces it here
        so a future copier-template change or a user-supplied
        ``odoo_conf`` value cannot silently disable it.
        """
        raw = self._inst().odoo_conf or (
            f"# Generated by IncubaCloud for instance"
            f" {self._inst().name}\n"
            "[options]\n"
        )
        cp = configparser.ConfigParser()
        cp.read_string(raw)
        if not cp.has_section('options'):
            cp.add_section('options')
        cp.set('options', 'proxy_mode', 'True')
        out = io.StringIO()
        cp.write(out)
        return out.getvalue()

    def _base_url(self):
        """Return the full HTTPS URL for web.base.url (scheme always https)."""
        domain = (self._inst().domain or "").strip()
        if not domain:
            return ""
        if domain.startswith(("http://", "https://")):
            return domain.rstrip("/")
        return f"https://{domain}"

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    async def _upload_copier_files(self, transport):
        """Upload all files needed by copier copy/update to the remote host."""
        inst = self._inst()
        answers_yml = yaml.dump(
            self._build_answers(),
            default_flow_style=False,
            allow_unicode=True,
        )
        files = {
            self._tmp("answers.yml"): answers_yml,
            self._tmp("addons.yaml"): await self._addons_yaml_content(),
            self._tmp("repos.yaml"): self._repos_yaml_content(),
            self._tmp("odoo.conf"): self._conf_content(),
            self._tmp("pip.txt"): inst.pip_dependencies or "",
            self._tmp("apt.txt"): inst.apt_dependencies or "",
        }
        backup_env = self._backup_env_content()
        if backup_env is not None:
            files[self._tmp("backup.env")] = backup_env
        resource_override = self._resource_override_content()
        if resource_override is not None:
            files[self._tmp("override.yml")] = resource_override

        await transport.upload_text_files(files)

        # Stage incubacloud_connect in /tmp; get_commands moves it after copier.
        tmp_ic_parent = f"/tmp/.ic-modules-{inst.doodba_project_name}"
        # Clean up leftovers from previous failed attempts, then recreate.
        result = await transport.run(
            f"rm -rf {tmp_ic_parent} && mkdir -p {tmp_ic_parent}",
        )
        if result.exit_status != 0:
            raise RuntimeError(
                f"Failed to prepare staging directory: {result.stdout}"
            )
        await transport.upload_dir(_IC_CONNECT_MODULE, tmp_ic_parent)

        self._sys("✓ Configuration files uploaded.")

    async def _preflight_addon_check(self):
        """Detect ALL addon conflicts before running gitaggregate."""
        inst = self._inst()
        if not inst or not inst.repo_ids:
            return
        self._sys("Checking for addon conflicts across repos…")
        conflicts = await detect_addon_conflicts(
            self.job.env, inst.repo_ids,
        )
        if not conflicts:
            self._sys("✓ No addon conflicts found.")
            return
        names = ', '.join(c['addon'] for c in conflicts[:5])
        if len(conflicts) > 5:
            names += f' (+{len(conflicts) - 5} more)'
        self._sys(
            f"✗ Found {len(conflicts)} addon conflict(s): "
            f"{names}"
        )
        self._sys("Resolve all conflicts before deploying.")
        self._create_preflight_addon_alert(conflicts)
        raise RuntimeError(
            f"Pre-flight: {len(conflicts)} addon conflict(s)"
        )

    def _create_preflight_addon_alert(self, conflicts):
        """Create a single addon_conflict alert with ALL conflicts."""
        inst = self._inst()
        names = ', '.join(c['addon'] for c in conflicts[:3])
        if len(conflicts) > 3:
            names += f' (+{len(conflicts) - 3} more)'
        message = (
            f"Addon conflict: {names} defined in multiple"
            " repos — resolve all conflicts and redeploy"
        )
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            Alert = env['cloud.alert']
            existing = Alert.search([
                ('instance_id', '=', inst.id),
                ('code', '=', 'addon_conflict'),
                ('state', '=', 'active'),
            ], limit=1)
            vals = {
                'message': message,
                'conflict_data': conflicts,
                'job_id': self.job.id,
            }
            if existing:
                existing.write(vals)
            else:
                Alert.create({
                    'instance_id': inst.id,
                    'code': 'addon_conflict',
                    'level': 'critical',
                    **vals,
                })

    async def before_execute(self, transport):
        inst = self._inst()
        if not inst:
            raise ValueError("deploy_instance job has no instance_id")
        await self._preflight_addon_check()
        self._sys(f"Preparing deployment for '{inst.name}'...")
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

        # SSH sessions don't load .bashrc, so ~/.local/bin (pipx tools) is
        # absent from PATH.  Export it so copier AND every subprocess it
        # spawns (invoke, pre-commit, …) can find the tools they need.
        path_prefix = (
            'export PATH="$HOME/.local/bin:$PATH"'
            ' && git config --global init.defaultBranch master'
        )
        copier_bin = "$HOME/.local/bin/copier"

        # Copier creates docker-compose.yml as a symlink to devel.yaml.
        # Replace it with the correct target for the environment.
        compose_target = (
            "prod.yaml"
            if inst.environment == "production"
            else "test.yaml"
        )

        lang = (
            inst.odoo_initial_lang.code
            if inst.odoo_initial_lang
            else "en_US"
        ) or "en_US"

        has_smtp = bool(inst.smtp_relay_host)

        cmds = [
            # 0. Clean up any leftover directory from a previous failed deploy.
            #    Full teardown: stop containers, remove volumes + images so the
            #    new deploy starts from a completely clean slate and does not
            #    conflict with orphaned containers, networks, or stale DB data.
            (
                "Remove previous deploy if present",
                f"if [ -d {d} ]; then"
                f"  echo 'Tearing down previous deploy...';"
                f"  cd {d} && docker compose down --volumes --rmi all --remove-orphans 2>/dev/null || true;"
                f"  echo 'Removing directory {d}...';"
                f"  rm -rf {d};"
                f"else"
                f"  echo 'No previous deploy found, clean slate.';"
                f"fi",
            ),
            # 1. Let copier create the full project structure first.
            #    PATH is exported so child processes inherit it.
            (
                "Deploy with copier",
                (
                    f"{path_prefix} && "
                    f"{copier_bin} copy --defaults --overwrite --trust "
                    f"--data-file {tmp_answers} "
                    f"gh:Tecnativa/doodba-copier-template {d}"
                ),
            ),
            # 2. Fix docker-compose.yml symlink: copier points to devel.yaml;
            #    use prod.yaml (production) or test.yaml (staging).
            (
                "Fix docker-compose symlink",
                f"rm -f {d}/docker-compose.yml && "
                f"ln -s {compose_target} {d}/docker-compose.yml",
            ),
            # 2a. Set COMPOSE_PROJECT_NAME so container names are unique
            #     across projects on the same host.  Docker defaults to
            #     the dir basename which collides when multiple projects
            #     have an instance named "production".
            (
                "Set compose project name",
                f'echo "COMPOSE_PROJECT_NAME={name}" >> {d}/.env',
            ),
            # 2b. Ensure .docker/incubacloud.env exists with a Fernet key.
            #     If the file already exists (previous deploy), leave it.
            (
                "Ensure incubacloud.env",
                f'[ -f {d}/.docker/incubacloud.env ] || '
                f'python3 -c "'
                f"from cryptography.fernet import Fernet; "
                f"print(f'INCUBACLOUD_SECRET_KEY={{Fernet.generate_key().decode()}}')"
                f'" > {d}/.docker/incubacloud.env',
            ),
            # 2c. Inject incubacloud.env into prod.yaml and test.yaml.
            #     The env_file block lives in those files (not common.yaml).
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
        ]

        # 2d. Strip the smtp service from prod.yaml when no SMTP relay
        #     is configured.  The copier template always generates the
        #     block, but without an image it causes a compose error.
        if not has_smtp and inst.environment == 'production':
            cmds.append((
                "Strip smtp service (not configured)",
                # Use awk to remove the smtp service block from prod.yaml.
                # Matches "  smtp:" and all following lines with deeper indent
                # (or blank lines) until the next top-level service key.
                f"awk '/^  smtp:/ {{skip=1; next}}"
                f" skip && /^  [a-z]/ {{skip=0}}"
                f" skip && /^[^ ]/ {{skip=0}}"
                f" !skip' {d}/prod.yaml > {d}/prod.yaml.tmp"
                f" && mv {d}/prod.yaml.tmp {d}/prod.yaml",
            ))

        # 2e. Strip the backup service when no backup backend is active.
        #     Same pattern as smtp: copier leaves an imageless block that
        #     breaks `docker compose`.
        if not self._backup_enabled() and inst.environment == 'production':
            # Strip the backup service from every compose file that may
            # contain it (copier emits it in prod.yaml, some templates
            # also place shared defs in common.yaml).
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
            # 3. Overwrite backup.env with ours (adds AWS_ENDPOINT_URL if set;
            #    copier's version lacks it). No-op if no backup backend.
            (
                "Write backup.env",
                f"f={self._tmp('backup.env')};"
                f" [ -f \"$f\" ] &&"
                f" mv \"$f\" {d}/.docker/backup.env || true",
            ),
            # 3b. Write docker-compose.override.yml with resource limits.
            #     No-op if no resource limits are configured.
            (
                "Write resource limits",
                f"f={self._tmp('override.yml')};"
                f" [ -f \"$f\" ] &&"
                f" mv \"$f\" {d}/docker-compose.override.yml || true",
            ),
            # 4. Overwrite repos.yaml and addons.yaml in the copier tree
            (
                "Write addons.yaml",
                f"mv {tmp_addons} {src}/addons.yaml",
            ),
            (
                "Write repos.yaml",
                f"mv {tmp_repos} {src}/repos.yaml",
            ),
            # 5. Copy incubacloud_connect into the private src tree so Odoo
            #    can find and install it at init time.
            (
                "Install incubacloud_connect",
                f"mkdir -p {src}/private"
                f" && cp -r /tmp/.ic-modules-{name}/incubacloud_connect"
                f" {src}/private/"
                f" && rm -rf /tmp/.ic-modules-{name}",
            ),
            # 6. Create conf.d (copier may not) and drop the conf file
            (
                "Write Odoo conf",
                f"mkdir -p {confd} && mv {tmp_conf} {confd}/{name}.conf",
            ),
            # 7. Write pip.txt and apt.txt (extra dependencies)
            (
                "Write pip.txt",
                f"mkdir -p {d}/odoo/custom/dependencies && "
                f"mv {tmp_pip} {d}/odoo/custom/dependencies/pip.txt",
            ),
            (
                "Write apt.txt",
                f"mv {tmp_apt} {d}/odoo/custom/dependencies/apt.txt",
            ),
            # 8. Remove the answers file (others were moved above)
            (
                "Remove temp files",
                f"rm -f {tmp_answers}",
            ),
            # 9. Pull images first (may take a while; separated so the
            #    actual start step is always fast).
            #    Non-fatal: if a service uses a local build or the registry is
            #    unavailable, docker compose up will still use cached images.
            (
                "Pull images",
                f"cd {d} && docker compose pull --ignore-pull-failures || true",
            ),
            # 10. Start the database service first so it is ready for init.
            (
                "Start database",
                f"cd {d} && docker compose up -d db",
            ),
            # 11. Initialize the Odoo database.
            #     docker compose run --rm honours depends_on health checks,
            #     so it waits for the db to be healthy before running.
            (
                "Initialize database",
                f"cd {d} && docker compose run --rm odoo "
                f"odoo --stop-after-init -i base,incubacloud_connect --load-language {lang}",
                {"stop_on_failure": True},
            ),
            # 12. Store module checksums baseline so future rebuilds
            #     can detect which modules changed via click-odoo-update.
            (
                "Initialize module checksums",
                f"cd {d} && docker compose run --rm odoo"
                f" click-odoo-update --only-compute-hashes"
                f" --database {inst.postgres_dbname or 'prod'}",
            ),
            # 13. Set web.base.url and report.url before the stack starts so
            #     any post-init steps and Odoo
            #     itself see the correct public URL on first boot.
            #     Wrapped in a DO block so it's a no-op if the DB was not
            #     initialised yet (should not happen at this point, but safe).
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
            # 13. Capture service list for on_success.
            ("List services", f"cd {d} && docker compose config --services"),
            # 14. Bring the full stack up (odoo, proxy, …).
            (
                "Start instance",
                f"cd {d} && docker compose up -d",
            ),
        ]
        return cmds

    def parse_results(self, results):
        errors = []
        ignored = {"List services"}
        for label, data in results.items():
            exit_code = data.get("exit_status", 1)
            if exit_code != 0 and label not in ignored:
                errors.append(f"'{label}' exited with status {exit_code}")
        return errors

    def _detect_addon_conflicts(self):
        """Scan stderr chunks for AddonsConfigError and create/update an alert."""
        inst = self._inst()
        if not inst:
            return
        chunks = self.env['cloud.job.log.chunk'].search([
            ('job_id', '=', self.job.id),
            ('source', '=', 'stderr'),
            ('content', 'ilike', 'AddonsConfigError'),
        ])
        conflicts = []
        for chunk in chunks:
            m = _ADDONS_CONFLICT_RE.search(chunk.content)
            if m:
                addon = m.group(1)
                repos = [r.strip().strip("'\"") for r in m.group(2).split(',')]
                conflicts.append({'addon': addon, 'repos': repos})
        if not conflicts:
            return
        names = ', '.join(c['addon'] for c in conflicts[:3])
        if len(conflicts) > 3:
            names += f' (+{len(conflicts) - 3} more)'
        message = (
            f"Addon conflict: {names} defined in multiple repos"
            " — add excludes to the offending repo and redeploy"
        )
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            Alert = env['cloud.alert']
            existing = Alert.search([
                ('instance_id', '=', inst.id),
                ('code', '=', 'addon_conflict'),
                ('state', '=', 'active'),
            ], limit=1)
            if existing:
                existing.write({
                    'message': message,
                    'conflict_data': conflicts,
                    'job_id': self.job.id,
                })
            else:
                Alert.create({
                    'instance_id': inst.id,
                    'code': 'addon_conflict',
                    'level': 'critical',
                    'message': message,
                    'conflict_data': conflicts,
                    'job_id': self.job.id,
                })

    def _dismiss_addon_conflict_alerts(self):
        """Dismiss any active addon_conflict alerts for this instance."""
        inst = self._inst()
        if not inst:
            return
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            env['cloud.alert'].search([
                ('instance_id', '=', inst.id),
                ('code', '=', 'addon_conflict'),
                ('state', '=', 'active'),
            ]).write({'state': 'dismissed'})

    def _dismiss_pip_conflict_alerts(self):
        inst = self._inst()
        if not inst:
            return
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            env['cloud.alert'].search([
                ('instance_id', '=', inst.id),
                ('code', '=', 'pip_conflict'),
                ('state', '=', 'active'),
            ]).write({'state': 'dismissed'})

    async def on_success(self, results):
        self._sys("✓ Instance deployed successfully.")
        self._dismiss_pip_conflict_alerts()
        self._dismiss_addon_conflict_alerts()
        inst = self._inst()
        services_out = results.get("List services", {}).get("stdout") or ""
        services = [s.strip() for s in services_out.splitlines() if s.strip()]
        inst.write({
            "status": "ok", "deployed": True, "running": True,
            "compose_services": ",".join(services) if services else "odoo,db",
            "last_rebuild_fingerprint": inst.rebuild_fingerprint,
        })

    def _detect_pip_conflicts(self):
        """If pip_dependencies has conflict markers, create/update a pip_conflict alert."""
        inst = self._inst()
        if not inst:
            return
        conflicts = detect_pip_conflicts(inst.pip_dependencies)
        if not conflicts:
            return
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            create_pip_conflict_alert(env, conflicts, instance_id=inst.id)

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        self._detect_pip_conflicts()
        self._detect_addon_conflicts()
        self._inst().write({"status": "error"})
