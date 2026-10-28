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
from ..net import real_ip
from ..github.http_utils import safe_urlopen
from ._pip_apt_map import resolve_apt_dependencies
from ._repo_requirements import (
    _apply_excludes,
    _parse_repo_addons,
    _repo_alias,
    create_pip_conflict_alert,
    detect_addon_conflicts,
    detect_pip_conflicts,
    fetch_repo_addons,
    fetch_requirements_txt_with_client,
    merge_pip_requirements,
    resolve_github_client,
)
from .abstract_executor import (
    COMPOSE_RUN,
    AbstractSSHExecutor,
    JobRefused,
    sql_escape_literal,
)
from .docker_prune_executor import PROTECT_LABEL
from .secret_key_escrow import SecretKeyEscrowMixin

_logger = logging.getLogger(__name__)

#: Where the instance's Odoo log lives. ``logs/`` sits next to the
#: compose files on the host and is bind-mounted into the container, so
#: the log outlives the container that wrote it — a rebuild recreates
#: the container by design and would otherwise take the only copy of
#: yesterday's log with it.
LOGS_DIRNAME = "logs"
ODOO_LOG_DIR = "/var/log/odoo"
ODOO_LOGFILE = f"{ODOO_LOG_DIR}/odoo.log"

#: The endpoints that check a password: the web form and its second
#: factor, the JSON session login, the password reset and the XML-RPC
#: ``common`` service whose ``authenticate`` the RPC clients call. Each
#: instance's login router matches only POSTs to these: an attempt, not
#: a page view, so an office behind one address opening the form spends
#: nothing of the budget.
LOGIN_PATHS_RULE = (
    "Path(`/web/login`) || Path(`/web/login/totp`)"
    " || Path(`/web/session/authenticate`) || Path(`/web/reset_password`)"
    " || Path(`/xmlrpc/2/common`)"
)
#: Above every router the template gives the same paths (the ``/web``
#: crawler router is 10, the main one 1); the template's higher ones
#: (websocket 20, allowed crawlers 100) match other paths. Kept below 30
#: on purpose: a router put in front of an instance's paths for reasons
#: of its own -- one that parks or refuses them -- sits at 30 and above,
#: and the login limit must never step over it.
LOGIN_ROUTER_PRIORITY = 15

#: The container a host's Traefik runs as: Full Setup starts it with
#: ``docker compose -p inverseproxy`` and its service is ``proxy``. A
#: panel's Odoo shares a Docker network with it — and with every other
#: panel on the host — so it is named in the panel's configuration as
#: the one connection allowed to state who the visitor is
#: (``net/real_ip``). The container's name rather than the alias
#: ``proxy`` avoids picking the wrong container by accident; it is not
#: what the guarantee rests on, since any compose project on the network
#: could declare either as an alias. That rests on only the platform
#: writing compose files onto ``inverseproxy_shared``. The final dot
#: makes it an absolute name, never tried under a search domain.
HOST_PROXY_CONTAINER = "inverseproxy-proxy-1."

#: The command each environment's compose file gives the ``odoo``
#: service, which the override *replaces* — so whatever the template
#: put there has to be repeated here or it is silently lost. Verified
#: against the pinned template (v9.6.1, and v10.0.8 on 20-oct-2026):
#: ``prod.yaml`` sets none, so
#: the image's CMD applies; ``test.yaml`` pins the two worker options
#: below. Re-check both when bumping the template pin (RB-15).
ODOO_COMMAND_PROD = ("/usr/local/bin/odoo",)
ODOO_COMMAND_TEST = ("odoo", "--workers=3", "--max-cron-threads=1")
#: The staging egress gatekeeper of doodba template v10, by digest: the
#: image publishes ``latest`` and pull-request tags, no versions, and a
#: tag that moves would change every staging's egress at its next start.
#: Moving it is a decision, like the template's version (RB-15).
GATEKEEPER_IMAGE = (
    "ghcr.io/tecnativa/egress-gatekeeper@sha256:"
    "ade54ca95d21abaf858c77f5949f8dbbff946a2e9b621b5775aabc33bf0243a8"
)

# Path to the incubacloud_connect module.  Navigate from __file__ via
# abspath (stays in auto/addons/) then realpath the result to resolve
# the doodba symlink into the actual directory with real files — sftp.put
# cannot copy symlinks.
_IC_CONNECT_MODULE = os.path.realpath(
    os.path.normpath(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..",
            "..",
            "incubacloud_connect",
        )
    )
)

# Kill switch for the pre-build requirements re-sync. Absent ⇒ enabled:
# keeping dependencies in step with the code a rebuild ships is the
# desired steady state, and an operator who wants the old frozen
# behaviour sets the parameter to '0' explicitly.
_ICP_RESYNC = "incubacloud.requirements_resync_enabled"


_SSH_RE = re.compile(r"^git@github\.com:(.+?)(?:\.git)?$")

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
    if not url.startswith(("https://", "http://")):
        url = f"https://{url}"
    # Inject token into HTTPS URLs
    if token and "github.com" in url:
        url = url.replace(
            "https://",
            f"https://x-access-token:{token}@",
            1,
        )
    return url


#: Compose keys whose list an addon fragment may add entries to. Any
#: other list (``command``, ``entrypoint``) is one value, never appended.
_ADDITIVE_LISTS = frozenset({"volumes", "env_file", "depends_on"})


def _merge_override_fragment(entry, fragment):
    """Merge an addon's override *fragment* into one service's *entry*.

    Never over what the platform rendered: mappings gain the keys it did
    not set, the lists of ``_ADDITIVE_LISTS`` gain the entries they lack,
    and any other key is set only when absent (see
    ``cloud.instance._compose_override_fragments``).

    :param dict entry: the service's rendered override, changed in place
    :param dict fragment: what the addon adds
    """
    for key, value in fragment.items():
        if isinstance(value, dict):
            target = entry.setdefault(key, {})
            for sub, sub_value in value.items():
                target.setdefault(sub, sub_value)
        elif key in _ADDITIVE_LISTS:
            target = entry.setdefault(key, [])
            target.extend(v for v in value if v not in target)
        else:
            entry.setdefault(key, value)


class DeployInstanceExecutor(SecretKeyEscrowMixin, AbstractSSHExecutor):
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
            req = urllib.request.Request(
                api,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
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
        """Return where *suffix* is staged: in the job's own directory.

        :param str suffix: the staged file's name
        :rtype: str
        """
        return self._job_path(suffix)

    def _modules_stage_dir(self):
        """Return where the modules this job installs are staged.

        :rtype: str
        """
        return self._job_path("modules")

    async def _make_modules_stage_dir(self, transport):
        """Create :meth:`_modules_stage_dir` on the host (idempotent).

        :param transport: the job's open transport
        :raise RuntimeError: when it could not be created
        """
        await self._ensure_job_dir(transport)
        stage = self._modules_stage_dir()
        result = await transport.run(f"mkdir -p -m 700 {stage}")
        if result.exit_status != 0:
            raise RuntimeError(
                f"Failed to prepare staging directory {stage}: {result.stdout}"
            )

    def _backup_enabled(self):
        """Whether the backup container should be provisioned for this deploy.

        Thin delegator to :meth:`cloud.instance._backup_enabled` so that
        downstream modules (e.g. SaaS plan logic) only have to override
        the model side and every executor that needs the answer sees
        the same value.
        """
        return self._inst()._backup_enabled()

    def _backup_retention(self):
        """Effective retention string (e.g. '7D', '3M') for the backup job.

        Hook: inheriting executors can override to source the retention
        from their own logic (e.g. a per-instance policy) instead of
        the backend's default.
        """
        inst = self._inst()
        bb = inst.effective_backup_backend
        return (bb.backup_retention or "3M").strip() if bb else "3M"

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
        if retention != "3M":
            lines.append(f"JOB_800_WHAT=dup --force remove-older-than {retention} $DST")
        return "\n".join(lines) + "\n"

    def _copier_template(self):
        """Return ``(template_url, vcs_ref)`` for the copier run.

        An empty ref keeps the historical behaviour — track the
        template's default branch — and ``deploy.sh`` logs whichever one
        is in effect, so the built tree is always traceable to a
        revision. Split out of ``get_commands`` so the command list can
        be built without a database, the way the executor's other
        environment reads already are.
        """
        settings = self.env["cloud.settings"].sudo()._get()
        return (
            settings.copier_template_url
            or "gh:Tecnativa/doodba-copier-template",
            settings.copier_template_ref or "",
        )

    def _build_answers(self):
        """Return a dict with all copier template answers.

        The render lives on ``cloud.instance._render_copier_answers``
        (single source of truth shared with the config-drift hash);
        this executor only dumps it into the remote answers file.
        """
        return self._inst()._render_copier_answers()

    def _repo_merge_ref(self, repo, branch):
        """Return the ref ``repos.yaml`` should merge for *repo*.

        A pinned row builds that exact commit; an unpinned one tracks
        its branch.

        A method rather than an inline expression so a subclass can
        answer from somewhere other than the row. That matters when the
        pin is written on a different transaction than the one
        rendering this file: Odoo runs its cursors REPEATABLE READ, so
        such a row is invisible here no matter how the cache is
        invalidated, and a subclass has to supply the value directly.

        Args:
            repo: The ``cloud.instance.repo`` row being rendered.
            branch: Its branch, already stripped and defaulted.

        Returns:
            str: A commit SHA or a branch name.
        """
        return repo.commit_sha or branch

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
                raw_url,
                app_token,
                pat,
                token_cache,
            )
            url = _github_authed_url(raw_url, token)
            merge_ref = self._repo_merge_ref(repo, branch)
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

        return (
            yaml.dump(
                data,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
            if data
            else ""
        )

    def _prod_services(self):
        """Return the services present in a production compose file.

        Thin delegator to :meth:`cloud.instance.expected_services` so
        that any extension that changes which services an instance
        actually runs (e.g. SaaS plans that disable ``backup``) is
        honoured uniformly across deploy, rebuild and the health
        probe. The stack as this run renders it: with the template it
        is about to run.
        """
        return self._inst().expected_services(
            template_ref=self._copier_template()[1],
        )

    def _odoo_command(self):
        """Return the ``command`` the override gives the odoo service.

        The environment's own command plus ``--logfile``, so Odoo's log
        lands on the host instead of inside the container. Passing the
        flag here rather than in ``odoo.conf`` is deliberate: the panel
        runs one-shot containers (module installs, ``click-odoo-update``
        for the boot test) whose output has to keep reaching the job
        log, and ``docker compose run`` replaces the command — so those
        runs are untouched by construction, while a ``logfile`` in the
        conf would have swallowed them.

        :return: argv list for the compose ``command`` key
        """
        base = (
            ODOO_COMMAND_PROD
            if self._inst().environment == "production"
            else ODOO_COMMAND_TEST
        )
        return [*base, f"--logfile={ODOO_LOGFILE}"]

    def _log_archive_days(self):
        """Days of Odoo log the host keeps for this instance."""
        settings = self.env["cloud.settings"].sudo()._get()
        return settings.odoo_log_archive_days

    def _prepare_logs_step(self, d):
        """Return the get_commands step that readies ``logs/`` on the host.

        Must run before the stack starts: Docker creates a missing
        bind-mount source itself, as root, and Odoo (uid 1000) would
        then fail to write and fall back to stdout — the silent failure
        this whole feature exists to remove.

        :param str d: remote instance directory
        :return: a ``(label, command)`` tuple
        """
        return (
            "Prepare log directory",
            self.run_script("instance_logs.sh", [
                "install",
                d,
                self._inst().doodba_project_name,
                str(self._log_archive_days()),
            ]),
        )

    def _gatekeeper_override(self):
        """Return what the override adds to template v10's ``gatekeeper``.

        **The image, by digest.** Upstream publishes ``latest`` and
        pull-request tags only; see ``GATEKEEPER_IMAGE``.

        **A restart policy.** The template gives the gatekeeper none,
        and ``odoo`` runs inside its network namespace: after a host
        reboot Docker brings ``odoo`` back (``unless-stopped``) into the
        namespace of a container that is not running, which fails, and
        the staging stays down until someone starts it by hand. The same
        omission left ``odoo_net_setup`` behind at every maintenance
        reboot (a customer's staging, 2026-10-08). ``unless-stopped``,
        like odoo's. Nothing opens up meanwhile: the filter lives in the
        namespace, and without the gatekeeper odoo has no network at all.

        The healthcheck is the image's own (the sync loop runs, the
        nftables table is there, the allowed set is not empty), which is
        what ``odoo`` waits for (``condition: service_healthy``).

        :return: the service fragment to merge into the override
        """
        return {
            "image": GATEKEEPER_IMAGE,
            "restart": "unless-stopped",
        }

    def _net_setup_override(self):
        """Return what the override has to undo on ``odoo_net_setup``.

        The egress sidecar is upstream's, and the template renders it
        with two things this platform will not ship. Neither is behind
        a copier question, so the only place to answer them is here.

        **The host's docker socket.** ``test.yaml.jinja`` mounts
        ``/var/run/docker.sock`` into the sidecar unconditionally. It is
        read by exactly one function of the image's entrypoint, guarded
        by ``DNS_INTERNAL_FROM_DOCKER`` — whose default *in the script*
        is ``0``; it is the template that turns it on. The work that
        matters (find the gateway, pick the interface, replace the
        default route) never touches Docker, and the DNS the function
        builds is already served by Docker's own resolver at
        ``127.0.0.11``, which the sidecar's dnsmasq forwards to and
        which answers service names and network aliases alike.

        So the socket buys nothing, and it costs everything: ``:ro``
        marks the *mount*, not the protocol — talking to a unix socket
        is ``connect()`` + ``send()``, and the Docker API has no
        read-only mode. The same channel takes ``POST
        /containers/create`` with a bind of ``/`` and ``privileged``.
        On a host shared between customers that is not "can see other
        containers", it is root over the machine and everyone on it.

        An empty ``volumes:`` list would not remove it: compose merges
        volumes by target path, so the base file's mount survives. The
        target is therefore re-pointed at ``/dev/null``, which is a
        mount nobody can connect to.

        **A disabled healthcheck.** The template sends
        ``healthcheck: disable: true``. The route injection happens once,
        at start; restart the odoo container and Docker hands it a fresh
        netns with the default NAT route, the injection is gone — and
        the sidecar, whose last line is ``tail -f /dev/null``, stays
        ``Up``. The instance is on the open internet and every light is
        green. That is not theory: it happened to this panel on
        2026-08-06 and ran for three and a half hours, sending Telegram
        to the live channel. ``devel.yaml`` has carried the check below
        ever since; this is the same one. ``disable: false`` has to be
        said out loud, because compose merges the healthcheck mapping
        key by key and ``disable: true`` would otherwise survive.

        **No restart policy.** The template gives the sidecar none, so a
        host reboot brought odoo back and left the sidecar ``Exited``:
        the staging on the open internet until someone restarted it by
        hand. Every staging with an allowlist on a host did, at each of
        its maintenance reboots (a customer's staging, 2026-10-08).
        ``unless-stopped``, like odoo's: at boot Docker starts the
        sidecar once odoo runs, since it joins odoo's network namespace,
        and the injection lands in that namespace.

        :return: the service fragment to merge into the override
        """
        return {
            "restart": "unless-stopped",
            "volumes": ["/dev/null:/var/run/docker.sock:ro"],
            "environment": {"DNS_INTERNAL_FROM_DOCKER": "0"},
            "healthcheck": {
                "disable": False,
                # The image ships /sbin/ip and little else — no grep, no
                # awk, no getent — so the check is pure shell. A live
                # injection points the default route at the gateway
                # container; a lost one points it back at Docker's own
                # bridge gateway, which is always the .1 of the subnet.
                # Comparing the last octet tells the two apart without
                # knowing the proxy's address.
                "test": [
                    "CMD-SHELL",
                    'set -- $$(ip route show default); gw=$$3; '
                    '[ -n "$$gw" ] && [ "$${gw##*.}" != "1" ]',
                ],
                "interval": "30s",
                "timeout": "5s",
                "retries": 2,
                "start_period": "20s",
            },
        }

    def _staging_shared_middleware_labels(self):
        """Return the Traefik middlewares a staging's Odoo routers need.

        test.yaml declares the middlewares its Odoo routers use
        (``addSTS``, ``buffering``, ``compress``, ``forbid-crawlers``,
        ``forceSecure``) on the ``smtp`` container's labels, not on
        Odoo's own. The override takes ``smtp`` out of Traefik to keep
        the mail catcher off the internet, and Traefik then ignores
        every label it carries, definitions included: each Odoo router
        names a middleware that no longer exists, Traefik drops them
        all, and the staging answers 404. Every staging did, from
        2026-09-21 until this was found on the first one in production
        (2026-10-03). prod.yaml declares them on Odoo and is unaffected.

        Declaring the same middlewares on Odoo, with the template's
        names and values, leaves ``smtp`` out of Traefik regardless of
        what the template calls its routers: should it ever rename
        these, the staging fails visibly in Traefik's log, but the
        mailbox does not reappear.

        :return: ``{label: value}`` to merge into the odoo service
        """
        inst = self._inst()
        version = (inst.odoo_version or "").replace(".", "-")
        prefix = (
            f"traefik.http.middlewares.{inst.doodba_project_name}"
            f"-{version}-test"
        )
        return {
            f"{prefix}-addSTS.headers.forceSTSHeader": "true",
            f"{prefix}-buffering.buffering.retryExpression": (
                "IsNetworkError() && Attempts() < 5"
            ),
            f"{prefix}-compress.compress": "true",
            f"{prefix}-forbid-crawlers.headers.customResponseHeaders"
            ".X-Robots-Tag": "noindex, nofollow",
            f"{prefix}-forceSecure.redirectScheme.permanent": "true",
            f"{prefix}-forceSecure.redirectScheme.scheme": "https",
        }

    def _login_router_labels(self):
        """Return a login router beside each of the instance's routers.

        The proxy limits every client to 300 requests a minute, the same
        for every path, which lets one address try 300 passwords a minute;
        Odoo's own cool-down counts per worker process. A router that
        matches only the login endpoints and puts ``loginlimit@file`` (the
        host's strict limit) first gives them their own, much lower
        budget, the same whether the host is reached directly or through
        a CDN, because the limit keys on the real client.

        The router repeats what the template gives the router it takes
        the login paths from -- name scheme, service, middlewares (the
        instance's IP allowlist included), the TLS twin -- so the login
        is never less protected than the rest of the site. That is
        knowledge of the template, the same kind
        ``_staging_shared_middleware_labels`` relies on: should a template
        release rename these, Traefik drops this router alone and the login
        falls back to the template's own, unlimited but working. RB-15 has
        the check for a template bump.

        :return: ``{label: value}`` to merge into the odoo service
        """
        inst = self._inst()
        answers = inst._render_copier_answers()
        production = inst.environment == "production"
        groups = answers["domains_prod" if production else "domains_test"]
        key = (
            f"{answers['project_name']}-{float(answers['odoo_version']):.1f}"
            f"-{'prod' if production else 'test'}"
        ).replace(".", "-")
        labels = {}
        for index, group in enumerate(groups):
            if group.get("redirect_to"):
                continue  # a redirect serves no login
            hosts = group["hosts"]
            domains = (
                f"Host(`{hosts[0]}`)" if len(hosts) == 1
                else "(" + " || ".join(f"Host(`{h}`)" for h in hosts) + ")"
            )
            # The panel does not offer the template's IP allowlist today,
            # so this never applies yet. It is here so that offering it
            # one day cannot leave the login, which outranks the routers
            # carrying the allowlist, open to every address.
            basic = (["whitelist"] if answers.get("cidr_whitelist") else [])
            if group.get("cert_resolver"):
                basic += ["addSTS", "forceSecure"]
            # The template's /web router adds forbid-crawlers; the login
            # paths are all under /web.
            middlewares = sorted(basic + ["buffering", "compress", "forbid-crawlers"])
            chain = ", ".join(
                ["loginlimit@file"] + [f"{key}-{m}" for m in middlewares],
            )
            names = [f"{key}-login-{index}"]
            if group.get("cert_resolver"):
                names.append(f"{key}-login-secure-{index}")
            for name in names:
                router = f"traefik.http.routers.{name}"
                labels[f"{router}.rule"] = (
                    f"{domains} && Method(`POST`) && ({LOGIN_PATHS_RULE})"
                )
                labels[f"{router}.service"] = f"{key}-main"
                labels[f"{router}.middlewares"] = chain
                labels[f"{router}.priority"] = str(LOGIN_ROUTER_PRIORITY)
                if name.startswith(f"{key}-login-secure-"):
                    labels[f"{router}.tls"] = "true"
                    if isinstance(group["cert_resolver"], str):
                        labels[f"{router}.tls.certResolver"] = (
                            group["cert_resolver"]
                        )
        return labels

    def _resource_override_content(self):
        """Generate docker-compose.override.yml: limits, label, logs.

        Only includes services that actually exist in the target
        environment's compose file to avoid Docker Compose errors.

        The ``odoo`` service additionally gets a ``command`` carrying
        ``--logfile`` and a bind mount of the instance's ``logs/``
        directory: Odoo's own log is archived per day on the host by
        logrotate, which is the copy that survives a rebuild. See
        :meth:`_odoo_command` for why the flag rides on the command.

        The ``smtp`` service gets ``ENABLE_UPDATE_CHECK=0``. It belongs
        in the override rather than in ``.docker/smtp.env`` or the
        compose files because ``copier update`` regenerates all three on
        every rebuild, which would drop it.

        A staging with an egress whitelist of its own also carries
        ``odoo_net_setup``, whose template defaults are corrected here;
        see :meth:`_net_setup_override` for what and why.

        Every service also carries a ``logging:`` block (``json-file``
        with the ``max-size``/``max-file`` from ``cloud.settings``).
        doodba's Odoo logs to stdout and the copier template sets no
        logging driver, so a container's log grows until the disk is
        full unless something limits it — and only hosts that ran
        ``host_hardening`` have a ``daemon.json`` doing so. Emitting the
        limit per service makes the rotation independent of how the
        host was set up; on hardened hosts it merely restates the
        daemon default. The values are read from settings at render
        time and are deliberately absent from the per-instance config
        snapshot: bumping the retention must not mark the fleet as
        drifted, it simply lands on each instance's next rebuild.

        Every service carries ``PROTECT_LABEL`` so the daily
        ``docker_prune`` (``docker system prune -af --filter
        "label!=…"``) never sweeps panel-managed resources. The
        invariant is "deployed by the panel, may legitimately sit
        stopped" (a warm pool spare, a Sablier-slept free instance, a
        manually stopped one): an unlabelled stopped container
        *matches* the ``label!=`` filter and is deleted. Anything the
        panel deploys is destroyed by ``delete_instance`` (``compose
        down``), never by the prune, so the label costs nothing.
        Because the labels are unconditional this method never returns
        ``None`` anymore — every consumer already tolerates both shapes.

        The project's ``default`` network is deliberately NOT labelled.
        Labelling it changes the network's definition, and ``docker
        compose`` answers that by recreating it: on an instance whose
        network predates the label that means tearing the live stack
        down mid-command, which failed outright ("has active endpoints"
        during a rebuild's boot test, "is not connected to the network"
        on a plain ``up``) and left tenants stopped.

        That used to be paired with the claim that protecting the
        containers already protects the network, because Docker keeps a
        network alive while any container — running or stopped — holds
        an endpoint on it. **That claim is false.** Stopping a container
        tears its sandbox down and releases the endpoint, so a stopped
        stack's network counts as unused and ``docker system prune``
        deletes it however well its containers are labelled. On
        2026-08-20 it deleted the networks of both ``ready`` warm
        spares: the containers survived, the post-prune check reported
        every stack intact, and neither stack could start again —
        a container that compose merely *starts* keeps the network id
        it was created with. The prune therefore no longer touches
        networks at all; see ``ansible/playbooks/host_maintenance.yml``.
        """
        inst = self._inst()
        # One source of truth for both environments. A separate constant
        # for the test one drifted from ``expected_services`` and said
        # staging had no ``smtp``, which the template renders there
        # unconditionally — so the mail catcher fell out of the override
        # entirely (no limits, no rotation, and no way to reach its
        # labels from here).
        # The stack as this deploy renders it: the template it is about
        # to run, not the one that rendered it last.
        allowed = inst.expected_services(template_ref=self._copier_template()[1])
        field_map = {
            "odoo": ("odoo_memory_limit", "odoo_cpus"),
            "db": ("db_memory_limit", "db_cpus"),
            "backup": ("backup_memory_limit", "backup_cpus"),
            "smtp": ("smtp_memory_limit", "smtp_cpus"),
        }
        services = {}
        for svc in allowed:
            # Not every expected service has limit fields: the egress
            # sidecars are two shell scripts and a dnsmasq, and giving
            # them a memory ceiling the operator never set would be
            # inventing a number. They still fall through to the label
            # and logging loop below, which is what they need.
            if svc not in field_map:
                continue
            mem_field, cpu_field = field_map[svc]
            mem = getattr(inst, mem_field, "") or ""
            cpus = getattr(inst, cpu_field, 0) or 0
            if mem or cpus:
                entry = {}
                if mem:
                    entry["mem_limit"] = mem
                if cpus:
                    entry["cpus"] = cpus
                services[svc] = entry
        key, value = PROTECT_LABEL.split("=", 1)
        settings = self.env["cloud.settings"].sudo()._get()
        for svc in allowed:
            entry = services.setdefault(svc, {})
            entry.setdefault("labels", {})[key] = value
            # A fresh mapping per service: PyYAML renders one dict
            # referenced from several places as an anchor plus aliases,
            # which compose reads fine but nobody wants to edit by hand.
            entry["logging"] = {
                "driver": "json-file",
                "options": {
                    "max-size": settings.container_log_max_size,
                    "max-file": str(settings.container_log_max_file),
                },
            }
        # Where the Traefik labels live: on ``odoo``, except in a staging
        # closed by template v10's gatekeeper, whose network ``odoo`` uses
        # and which carries them.
        router = "gatekeeper" if "gatekeeper" in allowed else "odoo"
        if "smtp" in allowed and inst.environment == "production":
            # Production's ``smtp`` is the real relay (docker-mailserver).
            #
            # docker-mailserver runs an ``update-check`` service that asks
            # GitHub for the latest release and mails postmaster once per
            # container start. Which tag an instance runs is the panel's
            # call (``smtp_relay_version``), not the container's to
            # announce — and the announcement arrived at fleet scale:
            # copier renders the hostname as ``smtp.<relay domain>``, so
            # every instance relaying through the same domain reports
            # itself under one name, and a free tenant restarts on each
            # Sablier wake, turning "once per start" into dozens of
            # identical mails a day.
            services["smtp"]["environment"] = {"ENABLE_UPDATE_CHECK": "0"}
        elif "smtp" in allowed:
            # A staging's ``smtp`` is a different animal: the mail
            # catcher (MailHog), so there is no update check to switch
            # off. What it needs is to come off the public internet.
            #
            # The template gives staging's ``smtp`` a Traefik router on
            # ``<domain>/smtpfake/`` — the whole mailbox, and MailHog
            # only asks for a password when ``/etc/mailhog/auth`` exists,
            # which nothing here writes. A staging carries a copy of
            # production, so that URL serves this instance's password
            # resets and invitations to whoever opens it, and the
            # hostname is not a secret: it comes from the host's
            # wildcard and Certificate Transparency publishes it.
            #
            # Compose merges labels by key and the override wins, so
            # flipping ``traefik.enable`` here is enough — the router
            # labels stay in the file and Traefik ignores the container.
            # Odoo keeps reaching the catcher over the internal network
            # (``smtplocal:1025``); only the window is closed.
            services["smtp"].setdefault("labels", {})["traefik.enable"] = (
                "false"
            )
            if router in allowed:
                services[router]["labels"].update(
                    self._staging_shared_middleware_labels(),
                )
        if "odoo_net_setup" in allowed:
            services["odoo_net_setup"].update(self._net_setup_override())
        if "gatekeeper" in allowed:
            services["gatekeeper"].update(self._gatekeeper_override())
        if router in allowed:
            services[router]["labels"].update(self._login_router_labels())
        if "odoo" in allowed:
            services["odoo"]["command"] = self._odoo_command()
            services["odoo"]["volumes"] = [
                f"./{LOGS_DIRNAME}:{ODOO_LOG_DIR}",
            ]
        for svc, fragment in self._addon_override_fragments().items():
            _merge_override_fragment(services.setdefault(svc, {}), fragment)
        data = {"services": services}
        return yaml.dump(
            data,
            default_flow_style=False,
            allow_unicode=True,
        )

    def _conf_content(self):
        """Return Odoo conf with the proxy settings the platform owns.

        Every instance we deploy is fronted by Traefik (the host's
        traefik service per ``data/traefik/``). Without proxy_mode,
        Odoo treats X-Forwarded-* headers naively, breaking URL
        building (web.base.url), OAuth callbacks, OIDC redirects, and
        per-IP rate-limit attribution. The platform enforces it here
        so a future copier-template change or a user-supplied
        ``odoo_conf`` value cannot silently disable it.
        """
        raw = self._inst().odoo_conf or (
            f"# Generated by IncubaCloud for instance {self._inst().name}\n[options]\n"
        )
        cp = configparser.ConfigParser()
        cp.read_string(raw)
        if not cp.has_section("options"):
            cp.add_section("options")
        cp.set("options", "proxy_mode", "True")
        # Which header states the visitor, whose word to take for it,
        # and which container is the proxy that relays it. Together or
        # not at all: a header believed from anywhere is one any visitor
        # can write, and on the host's shared network "anywhere private"
        # includes every other panel. Cleared rather than left behind
        # when the host stops declaring one, so moving an instance to a
        # host reached directly does not leave it believing a header
        # nobody sets.
        host = self._inst().host_id
        header = host._effective_client_ip_header() if host else ""
        trusted = host._effective_trusted_proxy_ranges() if host else []
        if header and trusted:
            cp.set("options", real_ip.HEADER_OPTION, header)
            cp.set("options", real_ip.TRUSTED_OPTION, ", ".join(trusted))
            cp.set("options", real_ip.PROXY_OPTION, HOST_PROXY_CONTAINER)
        else:
            cp.remove_option("options", real_ip.HEADER_OPTION)
            cp.remove_option("options", real_ip.TRUSTED_OPTION)
            cp.remove_option("options", real_ip.PROXY_OPTION)
        out = io.StringIO()
        cp.write(out)
        return out.getvalue()

    # ── AbstractSSHExecutor hooks ──────────────────────────────────────────

    async def _resync_repo_requirements(self):
        """Re-read each unpinned repo's ``requirements.txt`` before building.

        A repo line without a pinned commit is aggregated at the tip of
        its branch, so this job ships whatever code upstream published
        since the last build — including modules that need libraries the
        stored ``pip_dependencies`` never heard of. Reading the manifest
        here, in the same job that pulls the code, is what keeps the two
        in step; a line that ships a new dependency and a build that
        installs it become one event instead of two.

        Pinned repos are skipped: frozen code, frozen dependencies.

        Additive changes and upstream maintaining its own lines are
        applied and reported in the job log. A spec that contradicts the
        operator (or another repo) raises after filing the usual
        ``pip_conflict`` alert, which blocks the retry until a human
        picks a side. A failed fetch is non-fatal — the stored list is
        used, because a GitHub blip must not stop the fleet.
        """
        inst = self._inst()
        icp = self.env["ir.config_parameter"].sudo()
        if icp.get_param(_ICP_RESYNC, "1") != "1":
            return
        repos = [
            repo
            for repo in inst.repo_ids.sorted("sequence")
            if repo.url and repo.branch and not (repo.commit_sha or "").strip()
        ]
        if not repos:
            return

        self._sys("Re-syncing dependencies from repo requirements…")
        # Credentials are resolved here, on the main thread: the ORM is
        # not thread-safe and the fetches below run in worker threads.
        client = resolve_github_client(self.env)

        text = inst.pip_dependencies or ""
        sources = dict(inst.pip_dependency_sources or {})
        added, updated, conflicts = [], [], []
        for repo in repos:
            label = f"{_repo_alias(repo.url)}@{repo.branch}"
            content = await asyncio.to_thread(
                fetch_requirements_txt_with_client,
                client, repo.url, repo.branch,
            )
            if content is None:
                # No requirements.txt, or GitHub unreachable. Both are
                # indistinguishable from here and neither is worth
                # failing a deploy over.
                self._sys(
                    f"· {label}: no requirements.txt read — keeping the "
                    f"stored dependencies."
                )
                continue
            result = merge_pip_requirements(
                text, content,
                repo_url=repo.url, repo_branch=repo.branch,
                sources=sources,
            )
            text, sources = result["content"], result["sources"]
            added.extend(result["added"])
            updated.extend(result["updated"])
            conflicts.extend(result["conflicts"])
            if result["added"] or result["updated"] or result["adopted"]:
                self._sys(
                    f"· {label}: +{len(result['added'])} added, "
                    f"{len(result['updated'])} updated, "
                    f"{len(result['adopted'])} adopted."
                )
            for name in result["removed"]:
                self._sys(
                    f"· {label}: {name} is gone from upstream requirements "
                    f"— kept, dependencies are never auto-removed."
                )

        self._persist_resynced_requirements(inst, text, sources)
        for spec in added:
            self._sys(f"  + {spec}")
        for change in updated:
            self._sys(f"  ~ {change['name']}: {change['old']} → {change['new']}")

        if conflicts:
            names = ", ".join(c["name"] for c in conflicts[:5])
            if len(conflicts) > 5:
                names += f" (+{len(conflicts) - 5} more)"
            self._sys(
                f"✗ {len(conflicts)} dependency conflict(s) between the "
                f"repos and this instance's list: {names}"
            )
            self._sys(
                "Resolve them in Alerts (the stored spec wins unless you "
                "pick the repo's) and run this job again."
            )
            with self._durable_env() as env:
                create_pip_conflict_alert(
                    env, conflicts, instance_id=inst.id, job_id=self.job.id,
                )
            raise JobRefused(
                f"Pre-flight: {len(conflicts)} pip dependency conflict(s)"
            )
        if not (added or updated):
            self._sys("✓ Dependencies already in sync with the repos.")

    def _persist_resynced_requirements(self, inst, text, sources):
        """Store the merged dependency list durably, and let the job see it.

        Committed independently of the job so the merged list (and any
        conflict markers) survive a later failure of this same job —
        otherwise the retry would re-derive them from scratch and the
        alert would point at a field that never changed.

        Through :meth:`_persist`, which also hands the committed values to
        the job's cache. The upload step reads ``pip_dependencies`` right
        after this, and the full-rebuild decision reads
        ``rebuild_fingerprint``, which depends on it. This used to
        invalidate the cache instead, and the job's snapshot then served
        the pre-merge list back: measured in devel on 2026-09-28, the
        database held the merged list while ``pip.txt`` would have gone
        out without it.

        :param inst: the instance being deployed
        :param text: merged ``pip_dependencies`` content
        :param sources: updated provenance map
        """
        vals = {}
        if text != (inst.pip_dependencies or ""):
            vals["pip_dependencies"] = text
            needed = resolve_apt_dependencies(text)
            missing = needed - set((inst.apt_dependencies or "").split())
            if missing:
                vals["apt_dependencies"] = (
                    (inst.apt_dependencies or "").rstrip()
                    + "\n" + "\n".join(sorted(missing))
                ).strip()
        if sources != (inst.pip_dependency_sources or {}):
            vals["pip_dependency_sources"] = sources
        if not vals:
            return
        self._persist(
            inst.sudo().with_context(pip_provenance_managed=True), vals,
        )

    async def _upload_copier_files(self, transport):
        """Upload all files needed by copier copy/update to the remote host."""
        await self._resync_repo_requirements()
        inst = self._inst()
        answers_yml = yaml.dump(
            self._build_answers(),
            default_flow_style=False,
            allow_unicode=True,
        )
        # The answers hold the database, admin and SMTP passwords,
        # repos.yaml the repositories' tokens in its URLs, and backup.env
        # the bucket's keys: owner-only, and removed when the job ends
        # whether or not a step got to move them.
        private = {
            self._tmp("answers.yml"): answers_yml,
            self._tmp("repos.yaml"): self._repos_yaml_content(),
        }
        files = {
            self._tmp("addons.yaml"): await self._addons_yaml_content(),
            self._tmp("odoo.conf"): self._conf_content(),
            self._tmp("pip.txt"): inst.pip_dependencies or "",
            self._tmp("apt.txt"): inst.apt_dependencies or "",
        }
        backup_env = self._backup_env_content()
        if backup_env is not None:
            private[self._tmp("backup.env")] = backup_env
        resource_override = self._resource_override_content()
        if resource_override is not None:
            files[self._tmp("override.yml")] = resource_override

        await self._upload_private_files(transport, private)
        await self._upload_files(transport, files)

        # Stage incubacloud_connect; get_commands moves it after copier.
        await self._make_modules_stage_dir(transport)
        await transport.upload_dir(_IC_CONNECT_MODULE, self._modules_stage_dir())

        self._sys("✓ Configuration files uploaded.")

    #: Whether this executor checks the instance's repositories for an
    #: addon defined twice before it starts (``blocking_alert``). A warm
    #: claim ships the repositories the warm was built from and skips it.
    _preflight_addons = True

    def blocking_alert(self):
        """Hold the job on an ``addon_conflict`` alert before it connects.

        Doodba refuses an addon that two repositories define, so a deploy
        with one fails half-way. Every conflict is found here instead,
        from GitHub's module lists, and the job waits on one alert that
        names them all: resolving it dispatches the job again. It used to
        fail instead, which left the instance in 'deploying' and mailed
        the same news three times (16-oct-2026).

        :return: the active ``addon_conflict`` alert, or an empty recordset
        """
        Alert = self.job.env["cloud.alert"]
        inst = self._inst()
        if not self._preflight_addons or not inst or not inst.repo_ids:
            return Alert.browse()
        self._sys("Checking for addon conflicts across repos…")
        conflicts = asyncio.run(
            detect_addon_conflicts(self.job.env, inst.repo_ids),
        )
        if not conflicts:
            self._sys("✓ No addon conflicts found.")
            return Alert.browse()
        names = ", ".join(c["addon"] for c in conflicts[:5])
        if len(conflicts) > 5:
            names += f" (+{len(conflicts) - 5} more)"
        self._sys(f"✗ Found {len(conflicts)} addon conflict(s): {names}")
        self._sys("Resolve all conflicts; the job then runs by itself.")
        alert = inst._raise_addon_conflict_alert(conflicts, job=self.job)
        if not alert:
            # Filed at the same moment by a transaction this one cannot
            # see: nothing to wait on here, and the deploy must not run.
            raise JobRefused(
                f"Pre-flight: {len(conflicts)} addon conflict(s): {names}",
            )
        return alert

    def on_blocked(self, alert):
        """A deploy that waits leaves the instance in 'draft'.

        Nothing reached the host. In 'deploying' the instance could be
        neither deployed again nor deleted while it waited; it goes back
        to 'deploying' when the job is dispatched again (``on_unblocked``).
        """
        inst = self._inst()
        if inst.state == "deploying":
            inst._transition("draft")

    def on_unblocked(self):
        """A deploy that waited in 'draft' enters 'deploying' again."""
        inst = self._inst()
        if inst and inst.state == "draft":
            inst._transition("deploying")

    def on_cancelled_queued(self):
        """A deploy cancelled while it waited leaves the instance in 'draft'.

        ``deploy`` and ``clone_to_staging`` put the instance in
        'deploying' before queueing; cancelled in the queue, nothing
        will take it out, and it could be neither deployed again nor
        deleted. Same move as ``on_blocked``.
        """
        inst = self._inst()
        if inst and inst.state == "deploying":
            inst._transition("draft")

    async def on_abort(self, exc):
        """A run that ended before its steps failed like one whose steps did.

        A refused check, a host that would not connect: ``on_failure``
        only followed steps that ran, so the instance stayed in
        'deploying' for good (aitrax, 16-oct-2026), and the layers above
        never heard of the failure either.
        """
        await self.on_failure({}, [f"{type(exc).__name__}: {exc}"])

    async def before_execute(self, transport):
        inst = self._inst()
        if not inst:
            raise ValueError("deploy_instance job has no instance_id")
        inst._check_template_ref(self._copier_template()[1])
        self._preflight_tls_check()
        self._sys(f"Preparing deployment for '{inst.name}'...")
        await self._upload_copier_files(transport)
        await self._ship_secret_key(transport)
        await self._stage_extra_env_files(transport)

    def _addon_override_fragments(self):
        """Return what addons add to the stack's override, by service.

        :return: ``cloud.instance._compose_override_fragments()``
        :rtype: dict
        """
        return self._inst()._compose_override_fragments() or {}

    def _addon_env_files(self):
        """Return the files addons add to the instance's directory.

        :return: ``cloud.instance._extra_env_files()``, path → content
        :rtype: dict
        """
        return self._inst()._extra_env_files() or {}

    def _extra_env_tmp(self, relpath):
        """Return where an addon's env file is staged on the job's host.

        :param str relpath: the file's path relative to the instance dir
        :rtype: str
        """
        return self._tmp("extra-" + relpath.replace("/", "_"))

    async def _stage_extra_env_files(self, transport):
        """Upload the files ``cloud.instance._extra_env_files`` lists, 0600.

        :param transport: the job's open transport
        """
        files = self._addon_env_files()
        if files:
            await self._upload_private_files(
                transport,
                {self._extra_env_tmp(rel): text for rel, text in files.items()},
            )

    def _write_extra_env_step(self, d):
        """Return the step moving the staged addon files into place, or ().

        :param str d: the instance directory
        :return: a one-step tuple, empty when no addon ships a file
        :rtype: tuple
        """
        rels = sorted(self._addon_env_files())
        if not rels:
            return ()
        # One failed move fails the step, wherever it is in the list.
        moves = " && ".join(
            f'{{ f={self._extra_env_tmp(rel)}; if [ -f "$f" ]; then'
            f' mkdir -p "$(dirname {d}/{rel})" && mv "$f" {d}/{rel}'
            f" && chmod 600 {d}/{rel}; fi; }}"
            for rel in rels
        )
        return (("Write extra env files", moves),)

    def _preflight_tls_check(self):
        """Say how each domain will get its certificate; refuse the impossible.

        ``custom`` means "serve a certificate the host holds": its default
        one (Traefik tab), or one loaded by hand and declared in its
        ``config.yml`` (RB-12). A host with neither answers every
        handshake with a throwaway certificate and nothing says so, so
        that combination stops the deploy here, before the host is
        touched. Short of that the check only informs: a default
        certificate that does not cover the name is warned about, and a
        declared one cannot be read from here at all.

        Every domain also gets one log line naming what its choice
        resolved to, which is the only place the decision ``auto`` took
        is visible without reading code.

        :raise JobRefused: when a domain asks for the host's
            certificate and the host holds none.
        """
        inst = self._inst().sudo()
        host = inst.host_id
        missing = []
        for domain in inst.domain_ids:
            if not domain.hostname:
                continue
            description = self._describe_tls(domain)
            if domain.cert_resolver == "custom":
                cert, key = host._effective_tls_default() if host else ("", "")
                declared = bool(host) and host._declares_store_certificates()
                covered = bool(cert and key) and host._tls_default_covers(
                    domain.hostname,
                )
                if not covered and declared:
                    description = (
                        "a certificate declared in the host's config.yml "
                        "(not checked)"
                    )
                elif not (cert and key):
                    missing.append(domain.hostname)
                    continue
                elif not covered:
                    self._sys(
                        f"⚠ {domain.hostname}: the host's certificate does "
                        "not cover this name; browsers will warn."
                    )
            self._sys(f"TLS for {domain.hostname}: {description}")
        if missing:
            raise JobRefused(
                "These domains ask for the host's certificate and the host "
                f"holds none: {', '.join(missing)}. Load one on the host "
                "(Traefik tab, or RB-12) or pick another option for the "
                "domain."
            )

    @staticmethod
    def _describe_tls(domain):
        """Return one line naming what *domain*'s certificate choice means.

        :param domain: a ``cloud.instance.domain`` row
        :rtype: str
        """
        if domain.cert_resolver == "auto":
            if domain._cert_resolver_answer() is True:
                return "automatic → host's certificate (a CDN answers for it)"
            return "automatic → Let's Encrypt"
        return {
            "letsencrypt": "Let's Encrypt",
            "custom": "host's certificate",
            "none": "no TLS",
        }[domain.cert_resolver]

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

        # Copier creates docker-compose.yml as a symlink to devel.yaml.
        # Replace it with the correct target for the environment.
        compose_target = (
            "prod.yaml" if inst.environment == "production" else "test.yaml"
        )

        lang = (
            inst.odoo_initial_lang.code if inst.odoo_initial_lang else "en_US"
        ) or "en_US"

        has_smtp = bool(inst.smtp_relay_host)

        template_url, template_ref = self._copier_template()

        cmds = [
            # 0. Clean up any leftover directory from a previous failed deploy.
            (
                "Remove previous deploy if present",
                self.run_script("deploy.sh", ["teardown-previous", d]),
            ),
            # 1. Let copier create the full project structure first.
            (
                "Deploy with copier",
                self.run_script(
                    "deploy.sh",
                    [
                        "copier-deploy", d, tmp_answers,
                        template_url, template_ref,
                    ],
                ),
            ),
            # 2. Fix docker-compose.yml symlink: copier points to devel.yaml;
            #    use prod.yaml (production) or test.yaml (staging).
            #    Left inline: a trivial rm + ln, with the shell expanding ~.
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
            # 2b. Put the panel's copy of the encryption key in place,
            #     then make sure the file exists (it does, by now).
            self._write_secret_key_step(d),
            (
                "Ensure incubacloud.env",
                self.run_script("deploy.sh", ["ensure-secret-key", d]),
            ),
            # 2c. Inject incubacloud.env into prod.yaml and test.yaml.
            (
                "Inject incubacloud.env in prod.yaml and test.yaml",
                self.run_script("deploy.sh", ["inject-secret-env", d]),
            ),
        ]

        # 2d. Strip the smtp service from prod.yaml when no SMTP relay is
        #     configured — copier renders an imageless block that breaks
        #     ``docker compose``.
        if not has_smtp and inst.environment == "production":
            cmds.append(
                (
                    "Strip smtp service (not configured)",
                    self.run_script(
                        "strip_compose_service.sh", [d, "smtp", "prod.yaml"],
                    ),
                )
            )

        # 2e. Strip the backup service when no backup backend is active
        #     (same imageless-block problem). copier emits it in prod.yaml;
        #     some templates also place shared defs in common.yaml.
        if not self._backup_enabled() and inst.environment == "production":
            cmds.append(
                (
                    "Strip backup service (not configured)",
                    self.run_script(
                        "strip_compose_service.sh",
                        [d, "backup", "prod.yaml", "common.yaml"],
                    ),
                )
            )

        # 2f. Cap every container hostname at 64 bytes. doodba renders
        #     them from the instance's domain — the odoo service's is the
        #     domain itself, the backup's "backup.<domain>" — and a long
        #     domain pushes them past the kernel limit (__NEW_UTS_LEN =
        #     64): Docker then refuses to create the container ("hostname
        #     ... is too long") and the deploy stops at its first
        #     ``docker compose run``.
        cmds.append(
            (
                "Cap container hostnames",
                self.run_script("deploy.sh", ["cap-hostnames", d]),
            )
        )

        cmds += [
            # 3. Overwrite backup.env with ours (adds AWS_ENDPOINT_URL if set;
            #    copier's version lacks it). No-op if no backup backend.
            (
                "Write backup.env",
                f"f={self._tmp('backup.env')};"
                f' [ -f "$f" ] &&'
                f' mv "$f" {d}/.docker/backup.env || true',
            ),
            *self._write_extra_env_step(d),
            # 3b. Write docker-compose.override.yml: resource limits,
            #     protect label and container log rotation.
            (
                "Write compose override",
                f"f={self._tmp('override.yml')};"
                f' [ -f "$f" ] &&'
                f' mv "$f" {d}/docker-compose.override.yml || true',
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
                f" && cp -r {self._modules_stage_dir()}/incubacloud_connect"
                f" {src}/private/"
                f" && rm -rf {self._modules_stage_dir()}",
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
                f"cd {d} && {COMPOSE_RUN} --rm odoo "
                f"odoo --stop-after-init -i base,incubacloud_connect --load-language {lang}",
                {"stop_on_failure": True},
            ),
            # 12. Store module checksums baseline so future rebuilds
            #     can detect which modules changed via click-odoo-update.
            (
                "Initialize module checksums",
                f"cd {d} && {COMPOSE_RUN} --rm odoo"
                f" click-odoo-update --only-compute-hashes"
                f" --database {inst.postgres_dbname or 'prod'}",
            ),
            # 13. Set web.base.url and report.url before the stack starts so
            #     any post-init steps and Odoo itself see the correct public
            #     URL on first boot. ``base_url`` is sql-escaped as a
            #     defense-in-depth layer on top of the @api.constrains regex
            #     on cloud.instance.domain.hostname before it reaches the DO
            #     block inside the script.
            (
                "Set system parameters",
                self.run_script(
                    "deploy.sh",
                    [
                        "set-system-params", d,
                        inst.postgres_username or "odoo",
                        inst.postgres_dbname or "prod",
                        sql_escape_literal(self._base_url()),
                        "http://localhost:8069",
                    ],
                ),
            ),
            # 13. Capture service list for on_success.
            ("List services", f"cd {d} && docker compose config --services"),
            # 14. Ready ``logs/`` before anything starts: Docker would
            #     otherwise create the missing bind-mount source as root.
            self._prepare_logs_step(d),
            # 15. Bring the full stack up (odoo, proxy, …).
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
        chunks = self.env["cloud.job.log.chunk"].search(
            [
                ("job_id", "=", self.job.id),
                ("source", "=", "stderr"),
                ("content", "ilike", "AddonsConfigError"),
            ]
        )
        conflicts = []
        for chunk in chunks:
            m = _ADDONS_CONFLICT_RE.search(chunk.content)
            if m:
                addon = m.group(1)
                repos = [r.strip().strip("'\"") for r in m.group(2).split(",")]
                conflicts.append({"addon": addon, "repos": repos})
        if not conflicts:
            return
        # ``on_failure`` runs on a durable cursor already (see
        # ``_dispatch_outcome``), so the alert outlives the failed job.
        inst._raise_addon_conflict_alert(conflicts, job=self.job)

    def _dismiss_addon_conflict_alerts(self):
        """Close this instance's addon_conflict alert: the deploy went through.

        Through ``resolve_alert``, like the repository save that finds
        none left, so the closing is the platform's and is announced.
        """
        inst = self._inst()
        if not inst:
            return
        with self._durable_env() as env:
            env["cloud.alert"].resolve_alert(
                "addon_conflict", instance=env["cloud.instance"].browse(inst.id),
            )

    def _dismiss_pip_conflict_alerts(self):
        """Close this instance's pip_conflict alerts: the deploy went through."""
        inst = self._inst()
        if not inst:
            return
        with self._durable_env() as env:
            closed = env["cloud.alert"].search(
                [
                    ("instance_id", "=", inst.id),
                    ("code", "=", "pip_conflict"),
                    ("state", "=", "active"),
                ]
            )
            closed.write({"state": "dismissed"})

    def _cancel_stale_waits(self):
        """Cancel this instance's jobs that wait on an alert no longer open.

        A build of the instance went through, so what waited is behind
        it — on the alert this build closed, or on one a repository save
        closed while the job could not be dispatched (another job ran).
        Left blocked on a closed alert, a rebuild queued by an earlier
        push showed "Action required" for ever, its Resolve led to an
        empty Alerts page, and it could not be cancelled.

        Written with ``sudo``: they are the queue's fields, and a deploy
        asked for without superuser rights runs without them.
        """
        inst = self._inst()
        if not inst:
            return
        with self._durable_env() as env:
            env["cloud.job"].search([
                ("instance_id", "=", inst.id),
                ("blocked_alert_id", "!=", False),
                ("blocked_alert_id.state", "!=", "active"),
                ("id", "!=", self.job.id),
            ]).sudo().write({"state": "cancelled", "blocked_alert_id": False})

    async def on_success(self, results):
        self._sys("✓ Instance deployed successfully.")
        self._dismiss_pip_conflict_alerts()
        self._dismiss_addon_conflict_alerts()
        self._cancel_stale_waits()
        inst = self._inst()
        services_out = results.get("List services", {}).get("stdout") or ""
        services = [s.strip() for s in services_out.splitlines() if s.strip()]
        inst.write(
            {
                "status": "ok",
                **inst._running_confirmed_vals(),
                "stack_template_ref": self._copier_template()[1] or False,
                "compose_services": ",".join(services) if services else "odoo,db",
                "last_rebuild_fingerprint": inst.rebuild_fingerprint,
                # Config-drift anchor: this deploy just shipped exactly
                # the current snapshot, so the saved config is applied.
                **inst._applied_config_vals(),
            }
        )
        # A move deploys the record on its target while it is still live
        # on its source: it is 'deployed' already and stays so, and the
        # cutover is what moves it. Transitioning here refused 'deployed'
        # -> 'deployed' and failed every move at its first step, from the
        # state machine (2026-07-30) until a customer's upgrade hit it
        # (2026-10-15).
        if not inst.move_origin_host_id:
            inst._transition("deployed")
        # The host's metric label map is a snapshot of its instance list;
        # without this the new instance's containers report unattributed.
        inst.host_id.refresh_observability_labels(reason="deploy")
        self._call_addon_hook(inst, "_after_stack_rendered")

    def _detect_pip_conflicts(self):
        """If pip_dependencies has conflict markers, create/update a pip_conflict alert."""
        inst = self._inst()
        if not inst:
            return
        conflicts = detect_pip_conflicts(inst.pip_dependencies)
        if not conflicts:
            return
        with self._durable_env() as env:
            create_pip_conflict_alert(env, conflicts, instance_id=inst.id)

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        self._detect_pip_conflicts()
        self._detect_addon_conflicts()
        inst = self._inst()
        inst.write({"status": "error"})
        # Back to 'draft': nothing usable was left on the host, and
        # 'status' carries why. Re-running deploy() is then legal again.
        if inst.state == "deploying":
            inst._transition("draft")
