import asyncio
import configparser
import io
import logging
import os
import re
import shlex
import shutil
import string
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager, suppress

import asyncssh
import yaml

from odoo import fields
from odoo.exceptions import UserError

from ..controllers._data_load._helpers import _parse_github_repo_path
from ..github.client import GitHubAppClient
from ..net import real_ip
from ..net.git_reader import GitReader, GitReaderError
from ..github.http_utils import read_json_limited, safe_urlopen
from ._git_urls import clone_url_with_credential, parse_provider_repo_url
from ._odoo_versions import (
    VERSIONS,
    copier_defaults,
    debian_archive,
    image_git_safe,
    prefetch_repos,
)
from ._pip_apt_map import resolve_apt_dependencies
from ._repo_requirements import (
    _apply_excludes,
    _parse_repo_addons,
    _repo_alias,
    canonical_repo_url,
    create_pip_conflict_alert,
    detect_addon_conflicts,
    detect_pip_conflicts,
    fetch_repo_addons,
    fetch_requirements_txt_with_client,
    GitSource,
    git_source_requirements,
    is_safe_repo_url,
    merge_pip_requirements,
    resolve_github_client,
)
from .abstract_executor import (
    COMPOSE_RUN,
    AbstractSSHExecutor,
    JobRefused,
    sql_escape_literal,
)
from .cloud_host_edge_guard import WEBSITE_INFO_EXCEPTION_PRIORITY, WEBSITE_INFO_RULE
from .docker_prune_executor import PROTECT_LABEL, build_cache_prune_if_low
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

#: Seconds the job waits for the host's daemon to say how many CPUs it
#: has (``_read_host_cpus``). It answers in well under a second; one that
#: hangs costs this, and the job goes on with the measured cores.
HOST_CPUS_TIMEOUT_S = 15

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
#: The docker-mailserver release a production instance's ``smtp`` relay
#: runs. The copier answers say ``smtp_relay_version: latest`` and every
#: deploy pulls, so the template's tag moved with upstream: v17.0.0 became
#: ``latest`` on 2026-11-05, and each deploy, rebuild or move after it
#: started v17 while the instances deployed before kept what they had
#: pulled. 16.0.1 is the release the tenant panels already run
#: (``incubacloud_saas_manager``) and was chosen on 2026-11-08. A release
#: tag, not a digest: upstream rebuilds it for Debian security updates
#: (``16.0.1-rN``) without moving the release. The pin rides the override
#: and stays out of the answers, whose hash a new value would change for
#: the whole fleet. Moving it is a decision: read the release's breaking
#: changes against the relay's settings in the template's ``common.yaml``.
SMTP_RELAY_IMAGE = "ghcr.io/docker-mailserver/docker-mailserver:16.0.1"

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


@contextmanager
def _ic_connect_for_instances():
    """Yield a copy of incubacloud_connect fit for every series' image.

    Without its tests, nor any bytecode the panel left: doodba compiles
    every addon of the image with the series' own Python (its
    ``500-compile``), and the tests are the panel's, written for
    Python 3.12. Their f-strings stopped the image of 7.0 to 12.0 from
    building at all (Python 2.7 and 3.5; QA of the series, 7-nov-2026).
    The copy is named like the module and goes when the block ends.

    :return: the path of the copy, a directory named ``incubacloud_connect``.
    :rtype: str
    """
    with tempfile.TemporaryDirectory(prefix="ic-connect-") as stage:
        copy = os.path.join(stage, os.path.basename(_IC_CONNECT_MODULE))
        shutil.copytree(
            _IC_CONNECT_MODULE, copy,
            ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"),
        )
        yield copy


# Kill switch for the pre-build requirements re-sync. Absent ⇒ enabled:
# keeping dependencies in step with the code a rebuild ships is the
# desired steady state, and an operator who wants the old frozen
# behaviour sets the parameter to '0' explicitly.
#: The git helper container: doodba's own image of 19.0, pinned by digest
#: (the tag's on 10-nov-2026: git 2.39.5, git-aggregator 4.0), for every
#: series. ``deploy.sh fetch-repos`` downloads each repository with it,
#: with the same ``gitaggregate`` command doodba's build runs, and
#: ``record-heads`` asks a server whose address is pinned from it. One
#: image for every series: the git that handles a credential has to be a
#: maintained one, and 8.0 to 15.0's images carry old ones. The host keeps
#: it under a local tag with the ``PROTECT_LABEL`` the daily prune spares
#: (``deploy.sh``); ``full_setup_install.sh`` prepares it with the host.
#: Review the digest with every version of core.
GIT_HELPER_IMAGE = (
    "ghcr.io/tecnativa/doodba:19.0@sha256:"
    "aceea81257de6906c925d22d45add9cac52cfeca66136a6f49373e2f2b3fa5fe"
)

#: The label of the step that fetches every repository before the build.
#: A layer that drops the build by its label drops this one too.
_LABEL_FETCH = "Fetch the repositories"

#: Seconds the whole of ``fetch-repos`` may take: each repository has
#: 600 s or what is left of these, and is retried once only while some is
#: left (plan of the Git tokens, 10-nov-2026). A fixed budget, not one per
#: repository: three of 1 260 s each already passed the 3 600 s at which
#: the job runner kills a job. It counts for the build's repetition too
#: (``_build_retried_once``).
_FETCH_BUDGET_S = 1200

#: Seconds ``fetch-repos`` waits before its one retry of a repository:
#: the rebuild's ``_BUILD_RETRY_WAIT_S``, which no longer covers a clone
#: that left the build.
_FETCH_RETRY_WAIT_S = 60

#: The fetch step's timeout: the budget, which ``deploy.sh`` keeps by
#: itself with its traps, and a minute more as a second barrier.
_FETCH_STEP_TIMEOUT_S = _FETCH_BUDGET_S + 60

#: Where ``fetch-repos`` writes the seconds it took, relative to the
#: instance's directory, for the build's repetition rule. Under
#: ``custom/src/.ic-*``: out of the build context and of the history.
_FETCH_SECONDS_FILE = "odoo/custom/src/.ic-fetch-seconds"

#: What a ``repos.yaml`` entry may hold, the only keys core renders.
#: git-aggregator runs any ``shell_command_after`` of the file in the
#: helper container, which sees the credential's HOME: a job whose file
#: holds any other key is refused (10-nov-2026).
_REPOS_YAML_KEYS = frozenset({"defaults", "remotes", "merges", "target", "fetch_all"})


def _strip_userinfo(url):
    """Return *url* without its user and token.

    :param str url: a remote of ``repos.yaml``
    :rtype: str
    """
    return re.sub(r"^(https?://)[^/@]*@", r"\1", url or "")


_ICP_RESYNC = "incubacloud.requirements_resync_enabled"

#: The series that clone their repositories in the image again, comma
#: separated: the way back from fetching them on the host, without a new
#: version of core (plan of the Git tokens, 10-nov-2026). Empty by default.
_ICP_REPOS_IN_IMAGE = "incubacloud.repos_in_image_series"

#: The most of GitHub's answer about one repository read, in bytes: the
#: ``GET /repos/{owner}/{repo}`` of a repository is a few kB.
_REPO_ANSWER_BYTES = 256 * 1024


_ADDONS_CONFLICT_RE = re.compile(
    r"AddonsConfigError: Addon (\w+) defined in several repos \{([^}]+)\}"
)

#: The deploy's step that builds the instance's image. Not the rebuild's
#: «Rebuild Odoo image», which ``apply_plan`` leaves out by its label.
_LABEL_BUILD_DEPLOY = "Build Odoo image"
#: What a failed image build waits before its one repetition, in seconds.
_BUILD_RETRY_WAIT_S = 60
#: The longest a failed first build may have run and still be repeated,
#: in seconds (8-nov-2026), with the download of the repositories before
#: it when the series fetches them (``_FETCH_SECONDS_FILE``, 10-nov-2026).
#: The passing failure the
#: repetition is for came in the image's first layer
#: (``100-repos-aggregate``, cloning from GitHub). The job runner kills a
#: job at 3600 s (``limit_time_real``), and that SIGKILL leaves the job
#: orphaned and the instance in ``deploying``: a first attempt that ran
#: this long leaves no room for a second.
_BUILD_RETRY_MAX_FIRST_S = 900


def _build_retried_once(build, fetched=False):
    """Return a shell command that repeats a failed image build once.

    The build cloned every repository of the image, so a passing failure
    at GitHub would otherwise fail the whole rebuild (QA, 31-oct-2026) or
    deploy (QA, 8-nov-2026). The repetition also runs for a build that
    fails for good: it only costs that build's time, and the job then
    fails with the same error. A first attempt that ran
    ``_BUILD_RETRY_MAX_FIRST_S`` or more is not repeated; with *fetched*,
    the seconds ``fetch-repos`` took before it count as well, so the
    worst case of the job does not grow with the download. It is timed
    with ``date +%s`` (coreutils and busybox), as the step runs in the
    login shell of the host's user: dash, with no ``SECONDS``, when a
    self-hosted user was made with ``useradd`` and no ``-s``.

    :param str build: the ``docker compose build`` command to run, from
        the instance's directory.
    :param bool fetched: whether ``fetch-repos`` ran before it and wrote
        ``_FETCH_SECONDS_FILE``.
    :return: the command, exiting with the status of its last attempt.
    :rtype: str
    """
    before = "ic_fetch_s=0;"
    if fetched:
        before = (
            f"ic_fetch_s=$(cat {_FETCH_SECONDS_FILE} 2>/dev/null);"
            ' case "$ic_fetch_s" in ""|*[!0-9]*) ic_fetch_s=0;; esac;'
        )
    return (
        f"{{ {before} ic_build_t0=$(date +%s); {build} || {{ ic_build_s=$?;"
        f" if [ $(( $(date +%s) - ic_build_t0 + ic_fetch_s ))"
        f" -lt {_BUILD_RETRY_MAX_FIRST_S} ]; then"
        f" echo 'The image build failed: trying once more"
        f" in {_BUILD_RETRY_WAIT_S} s.';"
        f" sleep {_BUILD_RETRY_WAIT_S}; {build};"
        f" else echo 'The image build failed after"
        f" {_BUILD_RETRY_MAX_FIRST_S} s or more, with the download of the"
        f" repositories: not trying again.';"
        f" (exit $ic_build_s); fi; }}; }}"
    )


def _github_authed_url(url, token):
    """Normalize a GitHub repo URL to HTTPS and inject a token if available.

    Converts SSH URLs (git@github.com:org/repo.git) and the other
    spellings of a github.com repository to the canonical HTTPS form
    (``canonical_repo_url``) so the remote server doesn't need an SSH
    key for GitHub. Then injects the token for private repos, only
    into a URL whose host is exactly ``github.com`` over https
    (``is_safe_repo_url``): a host that merely contained the text, as
    ``https://evil.example/github.com/x.git``, was handed the token
    (5-nov-2026).
    """
    if not url:
        return url
    url = canonical_repo_url(url)
    # Ensure HTTPS scheme
    if not url.startswith(("https://", "http://")):
        url = f"https://{url}"
    if token and is_safe_repo_url(url):
        url = f"https://x-access-token:{token}@{url.removeprefix('https://')}"
    return url


#: ``cpu_shares`` by ``cloud.instance.scheduling_priority``: 1024 is
#: Docker's own default, so a normal instance weighs what it always did.
_CPU_SHARES = {"low": 512, "normal": 1024, "high": 2048}

_MEMORY_RE = re.compile(r"^(\d+(?:\.\d+)?)([bkmgt]?)$", re.IGNORECASE)
_LOWER_UNIT = {"t": "g", "g": "m", "m": "k", "k": "b"}


def _half_memory(limit):
    """Return half of a compose memory value, in the same unit when it can.

    ``1280m`` → ``640m``, ``2g`` → ``1g``; a half that is not whole moves
    to the next unit down (``1g`` → ``512m``), as compose reads only a
    whole number of a unit reliably.

    :param str limit: a ``mem_limit`` value
    :return: the half, or '' when *limit* is not a memory value
    :rtype: str
    """
    match = _MEMORY_RE.match(str(limit or "").strip())
    if not match:
        return ""
    number, unit = float(match.group(1)), match.group(2).lower()
    half = number / 2
    while half != int(half) and unit in _LOWER_UNIT:
        half, unit = half * 1024, _LOWER_UNIT[unit]
    if half != int(half) or half <= 0:
        return ""
    return f"{int(half)}{'' if unit == 'b' else unit}"


_MIB = 1024 * 1024
_UNIT_BYTES = {
    "": 1, "b": 1, "k": 1024, "m": _MIB, "g": 1024 * _MIB, "t": 1024**2 * _MIB,
}

#: PostgreSQL's memory settings as a share of the ``db`` container's
#: limit — (setting, divisor, floor, ceiling in MiB, None for none):
#: ``shared_buffers`` 1/4, ``work_mem`` 1/16, ``effective_cache_size``
#: 1/2. Scaled the way the manager's own database was fixed (1 GiB, 64MB
#: of work_mem, 27-Oct-2026); the template's 512MB of work_mem lets one
#: hash take 1 GiB. The floors keep a tiny database usable (4MB is
#: PostgreSQL's own work_mem). ``work_mem`` stops at 64MB, the manager's
#: own (review of 5-Nov-2026): it is per sort or hash of each connection,
#: so a large database with many connections would otherwise multiply
#: it past its limit; the other two are one per server. See
#: docs/architecture.md, "Memory an instance is deployed with".
_PG_MEMORY_SHARES = (
    ("shared_buffers", 4, 16, None),
    ("work_mem", 16, 4, 64),
    ("effective_cache_size", 2, 32, None),
)

#: Odoo's per-process memory limits as a share of the ``odoo``
#: container's limit — (option, share, floor, ceiling), in MiB.
#: ``limit_memory_soft`` is checked against a worker's virtual size
#: (VMS), which sat at 480-500 MB on workers days old (measured
#: 5-Nov-2026), so it never goes below 640 MiB or every request would
#: recycle one. ``limit_memory_hard`` is an ``RLIMIT_AS`` the children
#: inherit (wkhtmltopdf), so it never goes below 1536 MiB. Neither goes
#: above Odoo's own default (2048 and 2560 MiB): the panel only tightens.
#: See docs/architecture.md, "Memory an instance is deployed with".
_ODOO_MEMORY_SHARES = (
    ("limit_memory_soft", 0.6, 640, 2048),
    ("limit_memory_hard", 1.5, 1536, 2560),
)


def _memory_bytes(limit):
    """Return a compose memory value in bytes.

    :param str limit: a ``mem_limit`` value (``1g``, ``896m``, ``512000k``)
    :return: the bytes, 0 when *limit* is empty or not a memory value
    :rtype: int
    """
    match = _MEMORY_RE.match(str(limit or "").strip())
    if not match:
        return 0
    return int(float(match.group(1)) * _UNIT_BYTES[match.group(2).lower()])


def _db_conf_extra(limit):
    """Return the ``CONF_EXTRA`` a database with this memory limit runs with.

    One line per setting of ``_PG_MEMORY_SHARES``, in whole MB, which
    PostgreSQL reads as MiB: 256m gives ``shared_buffers = 64MB``,
    ``work_mem = 16MB`` and ``effective_cache_size = 128MB``; 4g gives
    1024MB, 64MB (its ceiling) and 2048MB.

    :param str limit: the instance's ``db_memory_limit``
    :return: the text, '' when the database has no limit (the template's
        own settings then apply)
    :rtype: str
    """
    total = _memory_bytes(limit) // _MIB
    if not total:
        return ""
    lines = []
    for name, divisor, floor, ceiling in _PG_MEMORY_SHARES:
        value = max(total // divisor, floor)
        if ceiling is not None:
            value = min(value, ceiling)
        lines.append(f"{name} = {value}MB\n")
    return "".join(lines)


def _odoo_memory_limits(limit):
    """Return the per-process memory limits for an Odoo with this limit.

    ``1024m`` gives 640 MiB and 1536 MiB (60 % and 150 %, both at their
    floor); ``2g`` gives 1228 MiB and 2560 MiB.

    :param str limit: the instance's ``odoo_memory_limit``
    :return: ``{option: bytes}`` for ``odoo.conf``, empty when the
        container has no limit (Odoo's defaults then apply)
    :rtype: dict
    """
    total = _memory_bytes(limit) // _MIB
    if not total:
        return {}
    return {
        name: min(max(int(total * share), floor), ceiling) * _MIB
        for name, share, floor, ceiling in _ODOO_MEMORY_SHARES
    }


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
    # The disk shown is read from metrics as soon as the job ends.
    _refreshes_host_disk = True

    #: The host's CPUs as its Docker daemon counts them, read by
    #: :meth:`_read_host_cpus` before the override is rendered; None
    #: limits nothing. On the class, so an executor built without
    #: ``__init__`` renders as before.
    _host_cpus = None
    #: What the last render limited: ``((service, set, given), ...)``,
    #: handed to :meth:`_report_cpu_clamps` by the end hooks.
    _cpu_clamps = ()

    def pre_run_checks(self):
        """Never build a move's destination over the copy a move froze there.

        A move back to a host where an earlier move left its frozen copy
        (``move_frozen_source_host_id``) has that copy removed first, by a
        job queued ahead of the chain (``cloud.instance.move_to_host``),
        whose success clears the field. If the removal failed, the
        deploy's ``docker compose up`` would start the old copy, with the
        database it had up to two days before, its crons and its queue,
        until the move stopped it. So the deploy refuses: the chain stops
        before anything starts and the move is rolled back.

        :raises JobRefused: when the frozen copy is still on this host
        """
        inst = self.job.instance_id
        host = self.job.host_id
        if (inst and inst.move_origin_host_id
                and inst.move_target_host_id == host
                == inst.move_frozen_source_host_id):
            raise JobRefused(
                f"Refused: the copy an earlier move froze on {host.name} is "
                "still there, and the move would start it. Remove it "
                "(Clean frozen copy now), then move the instance again."
            )
        super().pre_run_checks()

    # ── Helpers ────────────────────────────────────────────────────────────

    def _github_app_client(self):
        """Return the panel's GitHub App client, or None when it has none.

        Built once per job run.

        :rtype: GitHubAppClient or None
        """
        if "_gh_app_client" not in self.__dict__:
            client = None
            with suppress(Exception):
                client = GitHubAppClient(
                    self.env["cloud.github.credential.service"].get_credentials()
                )
            self.__dict__["_gh_app_client"] = client
        return self.__dict__["_gh_app_client"]

    def _get_github_tokens(self):
        """Return available tokens: (app_token, pat) — either may be None.

        The App's is the installation's own token, for the panel's calls
        to GitHub's API: what a server is handed for a download is the
        scoped one (:meth:`_scoped_github_token`).
        """
        svc = self.env["cloud.github.credential.service"]
        app_token = None
        client = self._github_app_client()
        if client is not None:
            with suppress(Exception):
                app_token = client.get_installation_token()
        pat = None
        with suppress(Exception):
            pat = svc.get_pat()
        return app_token, pat

    def _github_repo_access(self, repo_url, app_token, pat, cache):
        """Return who reads *repo_url* on GitHub, as ``(repo_id, private, by)``.

        ``GET /repos/{owner}/{repo}`` with the App's token, then with the
        PAT; *by* is ``"app"``, ``"pat"`` or None when neither reads it,
        and ``repo_id`` and ``private`` come from GitHub's answer (None
        when nobody reads it). Decided per repository, never per owner:
        an owner's repositories are not all in one installation. Always
        ``(None, None, None)`` for a URL that is not a github.com
        repository (``is_safe_repo_url`` once ``canonical_repo_url``
        spelled it).

        :param str repo_url: the line's URL
        :param app_token: the installation's token, or None
        :param pat: the PAT, or None
        :param dict cache: ``{"owner/repo": answer}``, filled here
        :rtype: tuple
        """
        url = canonical_repo_url((repo_url or "").strip())
        if not is_safe_repo_url(url):
            return None, None, None
        owner, repo = _parse_github_repo_path(url)
        key = f"{owner}/{repo}".lower()
        if key in cache:
            return cache[key]
        answer = (None, None, None)
        api = f"https://api.github.com/repos/{owner}/{repo}"
        for by, token in (("app", app_token), ("pat", pat)):
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
                with safe_urlopen(req, timeout=5) as resp:  # nosec B310 — hardcoded https://api.github.com
                    data = read_json_limited(resp, max_bytes=_REPO_ANSWER_BYTES)
            except Exception:
                continue
            if not isinstance(data, dict) or not isinstance(data.get("id"), int):
                continue
            answer = (data["id"], bool(data.get("private", True)), by)
            break
        cache[key] = answer
        return answer

    def _get_token_for_repo(self, repo_url, app_token, pat, cache):
        """Return the token that reads *repo_url*, the App's first, or None.

        None when neither reads it: no token is handed out on the chance a
        repository needs one (it used to be the App's or the PAT, 10-nov-2026),
        and always for a URL that is not a github.com repository.
        For the panel's own use; what a server gets is decided by
        :meth:`_repos_yaml_content`.

        :param str repo_url: the repository's URL
        :param app_token: the installation's token, or None
        :param pat: the PAT, or None
        :param dict cache: see :meth:`_github_repo_access`
        :rtype: str or None
        """
        by = self._github_repo_access(repo_url, app_token, pat, cache)[2]
        return {"app": app_token, "pat": pat}.get(by)

    def _scoped_github_token(self, repository_ids):
        """Return an App token that reads *repository_ids* only.

        One per job run, asked for every private repository of the
        instance the App reads (:meth:`GitHubAppClient.get_scoped_installation_token`).

        :param list repository_ids: GitHub's ids of those repositories
        :rtype: str
        :raises JobRefused: when the App has no client or GitHub refuses
            the token: the installation's own token is never used instead
        """
        client = self._github_app_client()
        try:
            if client is None:
                raise RuntimeError("the GitHub App is not configured")
            return client.get_scoped_installation_token(repository_ids)
        except Exception as exc:
            raise JobRefused(
                "Refused: GitHub did not issue the App's token limited to "
                f"this instance's private repositories ({exc}). The "
                "installation's own token is not used instead."
            ) from exc

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

    #: The copier settings file of the run (``_copier_settings_env``).
    _COPIER_SETTINGS = "copier-settings.yml"

    def _copier_settings_content(self):
        """Return the copier settings file of this instance's series, or None.

        Its defaults (``_odoo_versions.copier_defaults``): what a question
        the template never asks takes, here the PostgreSQL range of 8.0 to
        10.0, which the template's own does not hold. None for every other
        series, which runs copier as before.

        :rtype: str
        """
        defaults = copier_defaults(self._inst().odoo_version)
        if not defaults:
            return None
        return yaml.dump({"defaults": defaults}, default_flow_style=False)

    def _copier_settings_env(self):
        """Return the environment that points ``ic_copier`` at the settings file.

        :return: ``{"IC_COPIER_SETTINGS": path}`` where the upload wrote one
            (``_copier_settings_content``), else empty.
        :rtype: dict
        """
        if self._copier_settings_content() is None:
            return {}
        return {"IC_COPIER_SETTINGS": self._tmp(self._COPIER_SETTINGS)}

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

    def _github_line_tokens(self, repos, app_token, pat):
        """Return ``{line URL: token}`` for the GitHub lines that need one.

        A private repository the App reads gets the token scoped to the
        instance's private repositories the App reads
        (:meth:`_scoped_github_token`, asked once); one only the PAT reads
        gets the PAT; a public one gets none, whoever reads it. One that
        neither reads gets none either, and the job's log says so: its
        download then fails, naming it.

        :param repos: the instance's ``cloud.instance.repo`` rows
        :param app_token: the installation's token, or None
        :param pat: the PAT, or None
        :rtype: dict
        :raises JobRefused: see :meth:`_scoped_github_token`
        """
        cache = {}
        access = {}
        for repo in repos:
            raw_url = (repo.url or "").strip()
            if not raw_url or getattr(repo, "provider_id", False):
                continue
            if not is_safe_repo_url(canonical_repo_url(raw_url)):
                continue
            access[raw_url] = self._github_repo_access(raw_url, app_token, pat, cache)
            if access[raw_url][2] is None:
                self._sys(
                    f"{_strip_userinfo(raw_url)}: neither the GitHub App's "
                    "installation nor the PAT reads it; it is fetched "
                    "without credentials."
                )
        scoped_ids = sorted({
            repo_id for repo_id, private, by in access.values()
            if private and by == "app"
        })
        scoped = self._scoped_github_token(scoped_ids) if scoped_ids else None
        tokens = {}
        for raw_url, (_repo_id, private, by) in access.items():
            if private and by == "app":
                tokens[raw_url] = scoped
            elif private and by == "pat":
                tokens[raw_url] = pat
        return tokens

    def _repos_yaml_content(self, pinned=None):
        """Build repos.yaml: odoo first, then the instance's git repos.

        Each GitHub line gets the credential :meth:`_github_line_tokens`
        decides for it, and a GitLab line its provider's.

        :param set pinned: when given, receives the alias of every
            repository whose merge is a commit the panel pinned
            (``odoo_commit_sha``, ``commit_sha``, a layer's release pin)
            rather than its branch. Only the origin says so: a branch
            can be named like a commit (``cafebabe``, ``20261105``).
        :return: the file's text
        :rtype: str
        """
        inst = self._inst()
        data = {}
        app_token, pat = self._get_github_tokens()
        if pinned is None:
            pinned = set()
        tokens = self._github_line_tokens(inst.repo_ids, app_token, pat)

        # odoo is always first — public, no token needed
        odoo_merge = inst.odoo_commit_sha or "$ODOO_VERSION"
        if inst.odoo_commit_sha:
            pinned.add("odoo")
        data["odoo"] = {
            "defaults": {"depth": "$DEPTH_DEFAULT"},
            "remotes": {"odoo": "https://github.com/odoo/odoo.git"},
            "target": "odoo $ODOO_VERSION",
            "merges": [f"odoo {odoo_merge}"],
        }

        self._git_servers = {}

        for repo in inst.repo_ids:
            raw_url = (repo.url or "").strip()
            alias = _repo_alias(raw_url)
            branch = (repo.branch or "main").strip()
            if not alias or not raw_url:
                continue
            provider = getattr(repo, "provider_id", False)
            if provider:
                url = self._gitlab_clone_url(raw_url, provider)
            else:
                url = _github_authed_url(raw_url, tokens.get(raw_url))
            merge_ref = self._repo_merge_ref(repo, branch)
            # _repo_merge_ref answers a commit or the branch itself; a
            # repository that replaces an alias replaces its pin too.
            if merge_ref != branch:
                pinned.add(alias)
            else:
                pinned.discard(alias)
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

    # ── Git providers other than GitHub ──────────────

    #: ``{authority: {"provider_id", "ca_pem", "pin_ip"}}`` of the GitLab
    #: servers ``_repos_yaml_content`` rendered, for the spec, the CA
    #: files and the override.
    _git_servers = None

    def _git_pin(self, provider):
        """Return the address this job pins for *provider*, or ''.

        Resolved once per job and kept: the spec, the override and the
        panel's reads all use the same one. A retry is a new job run and
        resolves and checks it again.

        :param provider: a ``cloud.git.provider``
        :rtype: str
        :raises JobRefused: under the ``public`` policy, when the name does
            not resolve to public addresses only, with that message: it is
            rendered outside any ``try`` when the resync of requirements is
            off.
        """
        cache = self.__dict__.setdefault("_git_pin_cache", {})
        if provider.id not in cache:
            try:
                cache[provider.id] = provider.sudo()._resolve_pin() or ""
            except UserError as exc:
                raise JobRefused(str(exc)) from exc
        return cache[provider.id]

    def _gitlab_clone_url(self, raw_url, provider):
        """Return the URL the build clones a GitLab line from, with its token.

        The credential git reads the path with
        (``cloud.git.provider._git_credential``): one that cannot write
        first, as a series that clones in the image carries it there, then the longest
        namespace; none for a public repository. The URL is rebuilt from
        the provider's base, with ``.git``. The server is noted for the
        spec and the override.

        :param str raw_url: the line's URL
        :param provider: the line's ``cloud.git.provider``
        :rtype: str
        :raises JobRefused: when the URL is not the provider's
        """
        provider = provider.sudo()
        ref = parse_provider_repo_url(raw_url, [provider.base_url])
        if not ref:
            raise JobRefused(
                f"{raw_url} is not a repository of {provider.name}."
            )
        self._git_servers[ref.authority] = {
            "provider_id": provider.id,
            "ca_pem": provider.ca_bundle or "",
            "pin_ip": self._git_pin(provider),
        }
        return clone_url_with_credential(ref, provider._git_credential(ref.path))

    def _git_server_columns(self, url):
        """Return the spec's last three columns for *url*, or None for GitHub.

        ``(cred_authority, ca_file, pin_ip)``: ``cred_authority`` is
        filled for every GitLab line, with a credential or not, since
        ``git-trust`` tells them from GitHub by it.

        :param str url: a remote of ``repos.yaml``
        :rtype: tuple or None
        """
        authority = _strip_userinfo(url).partition("://")[2].partition("/")[0]
        server = (self._git_servers or {}).get(authority)
        if not server:
            return None
        ca_file = (
            self._tmp(f"git-ca-{server['provider_id']}.pem")
            if server["ca_pem"] else ""
        )
        return authority, ca_file, server["pin_ip"]

    def _git_private_files(self):
        """Return ``{staged path: PEM}`` of the servers with their own CA.

        :rtype: dict
        """
        files = {}
        for authority, server in (self._git_servers or {}).items():
            if server["ca_pem"]:
                files[self._git_server_columns(f"https://{authority}/")[1]] = (
                    server["ca_pem"]
                )
        return files

    def _prefetches(self):
        """Whether this series fetches its repositories before the build.

        The series' card says so (``prefetch_repos``); the system
        parameter ``incubacloud.repos_in_image_series``, a comma-separated
        list of series, sends one back to cloning in the image without a
        new version of core, except a series whose image git is not safe
        (``image_git_safe``). Its next deploy or rebuild then removes the
        fetched trees (``deploy.sh record-heads``).

        :rtype: bool
        """
        version = self._inst().odoo_version or ""
        if version not in VERSIONS or not prefetch_repos(version):
            return False
        if not image_git_safe(version):
            return True
        return version not in self._repos_in_image_series()

    def _repos_in_image_series(self):
        """Return the series ``incubacloud.repos_in_image_series`` names.

        :rtype: frozenset
        """
        value = self.env["ir.config_parameter"].sudo().get_param(
            _ICP_REPOS_IN_IMAGE, "",
        ) or ""
        return frozenset(filter(None, (v.strip() for v in str(value).split(","))))

    @staticmethod
    def _strip_repo_credentials(repos_yaml):
        """Return *repos_yaml* with no user or token in any remote.

        What a series that fetches its repositories on the host writes
        there: with ``AGGREGATE=false`` nobody clones from it in the
        build, the helper container reads it without credentials (they
        travel in the spec), and it enters the image.

        :param str repos_yaml: the rendered file, with tokens
        :rtype: str
        :raises JobRefused: when an entry holds a key besides
            ``_REPOS_YAML_KEYS``: git-aggregator would run a
            ``shell_command_after`` in the container that holds the
            credential
        """
        data = yaml.safe_load(repos_yaml) or {}
        for alias, entry in data.items():
            unknown = set(entry or {}) - _REPOS_YAML_KEYS
            if unknown:
                raise JobRefused(
                    f"Refused: repos.yaml's {alias} holds "
                    f"{', '.join(sorted(map(str, unknown)))}; only "
                    f"{', '.join(sorted(_REPOS_YAML_KEYS))} are allowed."
                )
            remotes = entry.get("remotes") or {}
            for name, url in remotes.items():
                remotes[name] = _strip_userinfo(url)
        return yaml.dump(
            data, default_flow_style=False, sort_keys=False, allow_unicode=True,
        )

    def _fetch_repos_command(self, d):
        """Return the command that fetches every repository before the build.

        ``deploy.sh fetch-repos`` with the series (``$ODOO_VERSION`` of
        ``repos.yaml``), the retry's wait and the step's budget.

        :param str d: the instance's directory on the host
        :rtype: str
        """
        return self.run_script(
            "deploy.sh", [
                "fetch-repos", d, self._tmp(self._HEADS_SPEC),
                GIT_HELPER_IMAGE, self._inst().odoo_version or "",
                str(_FETCH_RETRY_WAIT_S), str(_FETCH_BUDGET_S),
            ],
        )

    def _fetch_step(self, d):
        """Return the step that fetches every repository before the build.

        Its timeout is fixed (``_FETCH_STEP_TIMEOUT_S``), whatever the
        number of repositories: ``deploy.sh`` keeps the budget by itself.

        :param str d: the instance's directory on the host
        :rtype: tuple
        """
        return (
            _LABEL_FETCH,
            self._fetch_repos_command(d),
            {"stop_on_failure": True, "timeout": _FETCH_STEP_TIMEOUT_S},
        )

    def _git_trust_steps(self, d):
        """Return the step that writes the image's git settings.

        For a series that clones in the image: ``deploy.sh git-trust``
        writes ``build.d/060-incubacloud-git`` and the servers' CAs for
        the image's git, and removes them when no line is of GitLab. Not
        for one that fetches on the host, whose image clones nothing.

        :param str d: the instance's directory on the host
        :rtype: list
        """
        if self._prefetches():
            return []
        return [(
            "Trust the Git servers",
            self.run_script(
                "deploy.sh", ["git-trust", d, self._tmp(self._HEADS_SPEC)],
            ),
        )]

    def _heads_steps(self, d):
        """Return the steps that name, or fetch, each repository's head.

        A series that fetches its repositories does it on the host
        (:meth:`_fetch_step`; the build is a step of its own); the others
        record the heads and trust their Git servers.

        :param str d: the instance's directory on the host
        :rtype: list
        """
        if self._prefetches():
            return [self._fetch_step(d)]
        return [
            ("Record repository heads", self._record_heads_command(d)),
            *self._git_trust_steps(d),
        ]

    def _odoo_build_override(self):
        """Return ``{"build": …}`` for the odoo service in the override, or {}.

        A series that fetches its repositories on the host builds with
        ``AGGREGATE=false``; the others, under the ``public`` policy, pin
        each GitLab server with ``extra_hosts``.
        Neither enters ``rebuild_fingerprint`` nor the configuration
        snapshot: an address that rotates must not mark the instance as
        changed.

        :rtype: dict
        """
        if self._prefetches():
            return {"build": {"args": {"AGGREGATE": "false"}}}
        hosts = [
            f"{re.sub(r':[0-9]+$', '', authority)}:{server['pin_ip']}"
            for authority, server in sorted((self._git_servers or {}).items())
            if server["pin_ip"]
        ]
        return {"build": {"extra_hosts": hosts}} if hosts else {}

    #: The staged list of what ``deploy.sh record-heads`` resolves.
    _HEADS_SPEC = "repo-heads.tsv"

    def _repo_heads_spec(self, repos_yaml, pinned):
        """Return what ``deploy.sh record-heads`` resolves, from *repos_yaml*.

        One ``<alias>TAB<kind>TAB<ref>TAB<url>`` line per merge of the
        file the build clones from, read back from its rendered text so
        whatever a layer renders into it is what gets resolved.
        ``$ODOO_VERSION`` is expanded as the build expands it. *kind* is
        ``pin`` for a repository in *pinned*, whose ref is its own head
        and carries no URL when the image clones it (its token need not
        travel twice), and ``ref`` for a branch or tag, asked to the
        remote. A series that fetches on the host (``fetch-repos``) reads
        every URL, with its credential, from here alone. Whether a ref
        is a pin comes from where it came from, never from its shape
        (5-nov-2026).

        :param str repos_yaml: the rendered ``repos.yaml``
        :param set pinned: the aliases ``_repos_yaml_content`` pinned
        :return: the lines, for a file staged owner-only (the URLs carry
            the repositories' tokens)
        :rtype: str
        """
        version = self._inst().odoo_version or ""
        prefetch = self._prefetches()
        lines = []
        for alias, repo in (yaml.safe_load(repos_yaml) or {}).items():
            remotes = repo.get("remotes") or {}
            for merge in repo.get("merges") or ():
                remote, _sep, ref = str(merge).strip().partition(" ")
                ref = string.Template(ref.strip()).safe_substitute(
                    ODOO_VERSION=version,
                )
                url = remotes.get(remote, "")
                columns = self._git_server_columns(url)
                if alias in pinned:
                    line = f"{alias}\tpin\t{ref}\t{url if prefetch else ''}"
                else:
                    line = f"{alias}\tref\t{ref}\t{url}"
                if columns:
                    line += "\t" + "\t".join(columns)
                lines.append(line)
        return "".join(f"{line}\n" for line in lines)

    def _record_heads_command(self, d):
        """Return the command that writes the build context's heads file.

        ``deploy.sh record-heads`` names, under ``odoo/custom/``, the
        commit each repository of the image is at, so a branch that moved
        invalidates Docker's cache of the clone (5-nov-2026). The URLs it
        asks stay in the staged spec: the command names only paths.

        :param str d: the instance's directory on the host
        :rtype: str
        """
        args = ["record-heads", d, self._tmp(self._HEADS_SPEC)]
        if any(s["pin_ip"] for s in (self._git_servers or {}).values()):
            args.append(GIT_HELPER_IMAGE)
        return self.run_script("deploy.sh", args)

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

    def _debian_archive_steps(self, d):
        """Return the step that lets an image on an archived Debian build.

        Only for a series whose card says ``debian_archive`` (8.0 to 10.0,
        on Debian 8): ``deploy.sh debian-archive`` puts apt's switch to
        Freexian's Extended LTS first in the image's build. Any other series
        gets no step, and its deploy and rebuild run as they did; so does
        a value the table does not know.

        :param str d: remote instance directory
        :return: a list of zero or one ``(label, command)`` tuple
        :rtype: list
        """
        series = self._inst().odoo_version
        if series not in VERSIONS or not debian_archive(series):
            return []
        return [(
            "Read apt from the Debian archive",
            self.run_script("deploy.sh", ["debian-archive", d]),
        )]

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

    def _website_info_router_labels(self):
        """Return the routers that let this instance answer /website/info.

        Every host's proxy refuses the page (the edge guard,
        ``cloud_host_edge_guard.py``, priority 2000). An instance with
        ``website_info_public`` gets, for each group of its names, a
        router for the same paths just above it, to its own Odoo. Labels
        rather than a file of the host's: they go with the containers, so
        a deploy, rebuild, move or revive writes them from the field, a
        rebuild with the field off drops them and a deleted instance
        leaves nothing behind.

        The router repeats what the template gives its ``/website/info``
        router (``forbiddenCrawlers``): name scheme, service, middlewares,
        the TLS twin. The same knowledge of the template
        ``_login_router_labels`` relies on: should a template release
        rename them, Traefik drops these routers and the page stays
        refused.

        :return: ``{label: value}`` to merge into the odoo service; empty
            while the switch is off
        """
        inst = self._inst()
        if not inst.website_info_public:
            return {}
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
                continue  # a redirect serves no page
            hosts = group["hosts"]
            domains = (
                f"Host(`{hosts[0]}`)" if len(hosts) == 1
                else "(" + " || ".join(f"Host(`{h}`)" for h in hosts) + ")"
            )
            basic = (["whitelist"] if answers.get("cidr_whitelist") else [])
            if group.get("cert_resolver"):
                basic += ["addSTS", "forceSecure"]
            middlewares = sorted(basic + ["buffering", "compress", "forbid-crawlers"])
            chain = ", ".join(f"{key}-{m}" for m in middlewares)
            names = [f"{key}-website-info-{index}"]
            if group.get("cert_resolver"):
                names.append(f"{key}-website-info-secure-{index}")
            for name in names:
                router = f"traefik.http.routers.{name}"
                labels[f"{router}.rule"] = f"{domains} && ({WEBSITE_INFO_RULE})"
                labels[f"{router}.service"] = f"{key}-main"
                labels[f"{router}.middlewares"] = chain
                labels[f"{router}.priority"] = str(WEBSITE_INFO_EXCEPTION_PRIORITY)
                if name.startswith(f"{key}-website-info-secure-"):
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

        The ``db`` service gets a ``CONF_EXTRA`` sized to its memory limit
        (:func:`_db_conf_extra`): the template's ``work_mem = 512MB`` lets
        one hash take 1 GiB, more than most databases the panel deploys
        are given.

        A service's ``cpus`` is at most the host's CPUs as its daemon
        counts them (:meth:`_read_host_cpus`): Docker refuses to create a
        container that asks for more. The instance's field keeps its
        value; each limit is logged and kept in ``_cpu_clamps`` for the
        end hooks. Nothing is limited when the count could not be read.

        The production ``smtp`` service gets ``ENABLE_UPDATE_CHECK=0`` and
        the image :data:`SMTP_RELAY_IMAGE`. Both belong in the override
        rather than in ``.docker/smtp.env`` or the compose files because
        ``copier update`` regenerates all three on every rebuild, which
        would drop them.

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
        clamps = []
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
            # Docker refuses a container whose ``cpus`` is above the
            # daemon's count, and compose has recreated ``db`` by then
            # (job 457397, 8-nov-2026). The field keeps what was set;
            # only what the host is given is limited.
            if self._host_cpus and cpus > self._host_cpus:
                clamps.append((svc, cpus, self._host_cpus))
                self._sys(
                    f"{svc}: {cpus:g} CPUs set, the server has"
                    f" {self._host_cpus}: limited to {self._host_cpus}."
                )
                cpus = self._host_cpus
            if mem or cpus:
                entry = {}
                if mem:
                    entry["mem_limit"] = mem
                if cpus:
                    entry["cpus"] = cpus
                services[svc] = entry
        self._cpu_clamps = tuple(clamps)
        # PostgreSQL's memory follows the database's limit. The override
        # replaces the template's whole CONF_EXTRA (its only line is
        # ``work_mem = 512MB``), and the template's files stay untouched.
        db_conf = _db_conf_extra(inst.db_memory_limit) if "db" in allowed else ""
        if db_conf:
            services.setdefault("db", {})["environment"] = {"CONF_EXTRA": db_conf}
        for svc in ("odoo", "db"):
            if svc in allowed:
                services.setdefault(svc, {}).update(
                    self._scheduling_priority_entry(inst, services[svc]),
                )
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
            # The release is the panel's too: see SMTP_RELAY_IMAGE.
            services["smtp"]["image"] = SMTP_RELAY_IMAGE
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
            services[router]["labels"].update(self._website_info_router_labels())
        if "odoo" in allowed:
            services["odoo"]["command"] = self._odoo_command()
            services["odoo"]["volumes"] = [
                f"./{LOGS_DIRNAME}:{ODOO_LOG_DIR}",
            ]
            services["odoo"].update(self._odoo_build_override())
        for svc, fragment in self._addon_override_fragments().items():
            _merge_override_fragment(services.setdefault(svc, {}), fragment)
        data = {"services": services}
        return yaml.dump(
            data,
            default_flow_style=False,
            allow_unicode=True,
        )

    def _scheduling_priority_entry(self, inst, entry):
        """Return what the instance's priority adds to a service's override.

        ``cpu_shares`` weighs the service's CPU against the other
        containers of the host only when the host is busy: 512, 1024 (the
        Docker default) or 2048 for a low, normal or high priority. A high
        one also reserves half of the service's memory limit
        (``mem_reservation``), which the kernel reclaims from the others
        first under memory pressure.

        :param inst: the ``cloud.instance``
        :param dict entry: the service's override so far, ``mem_limit``
            included when it has one
        :return: the keys to add
        :rtype: dict
        """
        priority = inst.scheduling_priority or "normal"
        added = {"cpu_shares": _CPU_SHARES.get(priority, 1024)}
        if priority == "high" and entry.get("mem_limit"):
            reservation = _half_memory(entry["mem_limit"])
            if reservation:
                added["mem_reservation"] = reservation
        return added

    def _odoo_runs_workers(self, cp):
        """Tell whether the instance's Odoo server runs in prefork mode.

        The ``--workers`` of the service's command wins, as it does in
        Odoo: staging's (``ODOO_COMMAND_TEST``) pins 3. Otherwise the
        ``workers`` of the configuration, 0 (threaded mode) when it has
        none or one that is not a number.

        :param configparser.ConfigParser cp: the ``odoo.conf`` being
            rendered
        :return: True when Odoo runs at least one worker
        :rtype: bool
        """
        for arg in self._odoo_command():
            if arg.startswith("--workers="):
                with suppress(ValueError):
                    return int(arg.split("=", 1)[1]) > 0
        try:
            return cp.getint("options", "workers", fallback=0) > 0
        except ValueError:
            return False

    def _conf_content(self):
        """Return Odoo conf with the proxy settings the platform owns.

        Every instance we deploy is fronted by Traefik (the host's
        traefik service per ``data/traefik/``). Without proxy_mode,
        Odoo treats X-Forwarded-* headers naively, breaking URL
        building (web.base.url), OAuth callbacks, OIDC redirects, and
        per-IP rate-limit attribution. The platform enforces it here
        so a future copier-template change or a user-supplied
        ``odoo_conf`` value cannot silently disable it.

        It also gives each Odoo process a memory limit sized to the
        container's (:func:`_odoo_memory_limits`), unless the instance's
        ``odoo_conf`` sets either of them, in which case the user's
        settings stand alone: Odoo's defaults (2 and 2.5 GiB per worker)
        never recycle a worker in a container of 1 GiB. Only when Odoo
        runs workers (:meth:`_odoo_runs_workers`): in threaded mode the
        soft limit is measured on the whole server, which is past
        640 MiB of virtual size from the start, and Odoo would reload
        itself in a loop.
        """
        raw = self._inst().odoo_conf or (
            f"# Generated by IncubaCloud for instance {self._inst().name}\n[options]\n"
        )
        cp = configparser.ConfigParser()
        cp.read_string(raw)
        if not cp.has_section("options"):
            cp.add_section("options")
        cp.set("options", "proxy_mode", "True")
        # Both or neither: a hard limit of ours under a soft one of the
        # user's would fail allocations before any worker recycled. And
        # only with workers: in threaded mode the soft limit is checked
        # against the whole server's virtual size and Odoo reloads itself
        # each time it is past it (review of 5-Nov-2026).
        limits = (
            _odoo_memory_limits(self._inst().odoo_memory_limit)
            if self._odoo_runs_workers(cp) else {}
        )
        if not any(cp.has_option("options", option) for option in limits):
            for option, value in limits.items():
                cp.set("options", option, str(value))
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
            if repo.provider_id:
                read, content = await self._gitlab_requirements(repo)
                if not read:
                    continue
            else:
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
            for item in result["satisfied"]:
                self._sys(
                    f"· {label}: {item['incoming']} is already met by "
                    f"{item['existing']}."
                )
            for item in result["kept"]:
                self._sys(
                    f"· {label}: {item['incoming']} was turned down when a "
                    f"conflict was resolved — keeping {item['existing']}."
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

    async def _gitlab_requirements(self, repo):
        """Read a GitLab line's ``requirements.txt`` for the re-sync.

        - A correct read, with the file or without it, stamps the line's
          ``requirements_read_at`` and closes the provider's
          ``gitlab_unreachable`` alert.
        - A server the panel cannot reach (``unreachable``, ``timeout``)
          is tolerated only when the line's requirements were read
          before: the job says so, keeps or opens the alert and goes on,
          as the host builds and may reach what the panel does not.
          Never read before, the job fails rather than build blind.
        - Anything else (``private_address`` under the ``public``
          policy, a refused token, a missing repository) fails the job.

        :param repo: a ``cloud.instance.repo`` of a Git provider
        :return: ``(read, content)``: *read* False when the stored
            dependencies are kept; *content* None when there is no file
        :rtype: tuple
        :raises JobRefused: when the job must not build
        """
        provider = repo.provider_id.sudo()
        host = provider.authority
        ref = parse_provider_repo_url(repo.url, [provider.base_url])
        key = f"git_provider:{provider.id}"
        try:
            source = GitSource(
                GitReader(
                    ca_pem=provider.ca_bundle or None,
                    pin_ip=self._git_pin(provider) or None,
                ),
                ref, provider._git_credential(ref.path),
            )
            content = await asyncio.to_thread(
                git_source_requirements, source, repo.branch,
            )
        except UserError as exc:
            raise JobRefused(str(exc)) from exc
        except GitReaderError as exc:
            if exc.code not in ("unreachable", "timeout"):
                raise JobRefused(
                    f"The panel could not read the requirements of "
                    f"{ref.path} on {host} ({exc.code})."
                ) from exc
            if not repo.requirements_read_at:
                raise JobRefused(
                    f"The panel cannot reach {host} and never read the "
                    f"requirements of {ref.path}: not building blind."
                ) from exc
            message = (
                f"The panel cannot reach {host}: requirements.txt of "
                f"{ref.path} was not read again; building with the last "
                f"ones known."
            )
            self._sys(f"⚠ {message}")
            with self._durable_env() as env:
                env["cloud.alert"].sudo().raise_alert(
                    "gitlab_unreachable", message, level="warning", key=key,
                )
            return False, None
        self._persist(repo.sudo(), {"requirements_read_at": fields.Datetime.now()})
        with self._durable_env() as env:
            env["cloud.alert"].sudo().resolve_alert("gitlab_unreachable", key=key)
        return True, content

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

    async def _read_host_cpus(self, transport):
        """Read how many CPUs the host's Docker daemon has, for the render.

        ``docker info``'s ``NCPU`` is the number Docker checks a
        container's ``cpus`` against, read now: the host's ``cpu_cores``
        comes from the metrics, up to five minutes late after a resize
        and 0 on a host never measured. That field is the fallback when
        the daemon fails, answers no positive count or does not answer
        within :data:`HOST_CPUS_TIMEOUT_S`, or the link to the host fails
        (``asyncssh.Error``, ``OSError``); when it is 0 too, nothing is
        limited and the log says so. Sets ``self._host_cpus``; writes
        nothing to the database.

        :param transport: the job's open transport
        """
        try:
            result = await transport.run(
                "docker info --format '{{.NCPU}}'", timeout=HOST_CPUS_TIMEOUT_S,
            )
            ncpu = int(result.stdout.strip()) if result.exit_status == 0 else 0
        except (TimeoutError, ValueError, asyncssh.Error, OSError):
            ncpu = 0
        if ncpu <= 0:
            ncpu = self._host().cpu_cores or 0
        self._host_cpus = ncpu if ncpu > 0 else None
        if self._host_cpus is None:
            self._sys(
                "⚠ The server's CPUs could not be read: each service's"
                " CPUs are written as set, not limited to the server's."
            )

    def _report_cpu_clamps(self):
        """Report what the render limited to the host's CPUs (``_cpu_clamps``).

        Called from the end hooks, on their durable cursor, whatever the
        outcome. Core says it in the job's log only, as the render does:
        a layer that sells limits extends this to tell its operator.
        """

    async def _upload_copier_files(self, transport):
        """Upload all files needed by copier copy/update to the remote host.

        The host's CPUs are read first (:meth:`_read_host_cpus`): the
        override rendered here is limited to them.
        """
        await self._read_host_cpus(transport)
        await self._resync_repo_requirements()
        inst = self._inst()
        answers_yml = yaml.dump(
            self._build_answers(),
            default_flow_style=False,
            allow_unicode=True,
        )
        # The answers hold the database, admin and SMTP passwords,
        # repos.yaml and the heads spec the repositories' tokens in their
        # URLs, and backup.env the bucket's keys: owner-only, and removed
        # when the job ends whether or not a step got to move them.
        pinned = set()
        repos_yaml = self._repos_yaml_content(pinned)
        private = {
            self._tmp("answers.yml"): answers_yml,
            # A series that fetches on the host: the file the host and
            # the image get has no credential, which the spec carries.
            self._tmp("repos.yaml"): (
                self._strip_repo_credentials(repos_yaml)
                if self._prefetches() else repos_yaml
            ),
            self._tmp(self._HEADS_SPEC): self._repo_heads_spec(
                repos_yaml, pinned,
            ),
            **self._git_private_files(),
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
        copier_settings = self._copier_settings_content()
        if copier_settings is not None:
            files[self._tmp(self._COPIER_SETTINGS)] = copier_settings

        await self._upload_private_files(transport, private)
        await self._upload_files(transport, files)

        # Stage incubacloud_connect; get_commands moves it after copier.
        await self._make_modules_stage_dir(transport)
        with _ic_connect_for_instances() as module:
            await transport.upload_dir(module, self._modules_stage_dir())

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

    def on_never_run(self):
        """A deploy that left its queue but never ran leaves 'draft' too.

        Cancelled once dispatched, before it started, or ended with its
        chain by a step before it (``clone_to_staging``'s): nothing of
        it reached the host, and in 'deploying' the instance could be
        neither deployed again nor deleted. Same move as
        ``on_cancelled_queued``; a rebuild's instance is 'deployed', and
        stays so.
        """
        self.on_cancelled_queued()

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
                    env=self._copier_settings_env(),
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
            # 4b. Fetch every repository on the host (every series but
            #     19.0), or name each one's head in the build context: the
            #     image is built by «Build Odoo image» below, and Docker's
            #     cache of the clone has to follow the branches, not the
            #     token in repos.yaml.
            *self._heads_steps(d),
            # 5. Copy incubacloud_connect into the private src tree so Odoo
            #    can find and install it at init time, over no copy of an
            #    older one: ``cp -r`` would leave behind what this one no
            #    longer ships, like the tests/ an instance got before
            #    7-nov-2026.
            (
                "Install incubacloud_connect",
                f"mkdir -p {src}/private"
                f" && rm -rf {src}/private/incubacloud_connect"
                f" && cp -r {self._modules_stage_dir()}/incubacloud_connect"
                f" {src}/private/"
                f" && rm -rf {self._modules_stage_dir()}",
            ),
            *self._debian_archive_steps(d),
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
            # 8. Remove the answers and copier settings files (others were
            #    moved above).
            (
                "Remove temp files",
                f"rm -f {tmp_answers} {self._tmp(self._COPIER_SETTINGS)}",
            ),
            # 8b. Empty Docker's build cache first when the host's disk
            #     is low: a build that runs out of disk fails half-way.
            #     Never fails the deploy.
            (
                "Free the build cache if the disk is low",
                build_cache_prune_if_low(),
            ),
            # 9. Pull images first (may take a while; separated so the
            #    actual start step is always fast).
            #    Non-fatal: if a service uses a local build or the registry is
            #    unavailable, docker compose up will still use cached images.
            (
                "Pull images",
                f"cd {d} && docker compose pull --ignore-pull-failures || true",
            ),
            # 9b. Build the image in a step of its own, repeated once if
            #     it fails. The first ``compose run`` or ``up`` that found
            #     it missing built it, never twice, and a passing failure
            #     of GitHub failed a deploy in «Start instance» (QA,
            #     8-nov-2026). Cached and without a pull, as that build
            #     was; with the image there, nothing after it builds.
            (
                _LABEL_BUILD_DEPLOY,
                f"cd {d} && " + _build_retried_once(
                    "docker compose build odoo", fetched=self._prefetches(),
                ),
                {"stop_on_failure": True},
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
            #     The click-odoo-contrib of 8.0 to 10.0's images (1.12, the
            #     last on Python 2) has no ``--only-compute-hashes``: there
            #     a plain run stores them, after updating the modules of a
            #     database that has just been created (QA of the series,
            #     7-nov-2026).
            (
                "Initialize module checksums",
                f"cd {d} && {COMPOSE_RUN} --rm odoo sh -c"
                " 'if click-odoo-update --help | grep -q -- --only-compute-hashes;"
                ' then exec click-odoo-update --only-compute-hashes --database "$0";'
                ' fi; exec click-odoo-update --database "$0"\''
                f" {shlex.quote(inst.postgres_dbname or 'prod')}",
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
        if inst.move_origin_host_id:
            skipped = self._move_skipped_labels()
            cmds = [cmd for cmd in cmds if cmd[0] not in skipped]
        return cmds

    def _move_skipped_labels(self):
        """Return the steps a move's deploy of its destination leaves out.

        A move deploys the destination only to receive the source's
        database: whatever these steps would put in a database of its
        own, the restore replaces minutes later. Code, images and every
        container still come — Odoo starts with no database and serves
        nothing, and the next step of the move stops it; the restore
        starts Odoo with ``compose start``, which creates no container.
        Layers above add their own steps.

        :return: step labels
        :rtype: set
        """
        return {
            "Start database",
            "Initialize database",
            "Initialize module checksums",
            "Set system parameters",
        }

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
        # By addon: a build repeated once prints the same error twice.
        conflicts = {}
        for chunk in chunks:
            m = _ADDONS_CONFLICT_RE.search(chunk.content)
            if m:
                addon = m.group(1)
                repos = [r.strip().strip("'\"") for r in m.group(2).split(",")]
                conflicts.setdefault(addon, {"addon": addon, "repos": repos})
        if not conflicts:
            return
        # ``on_failure`` runs on a durable cursor already (see
        # ``_dispatch_outcome``), so the alert outlives the failed job.
        inst._raise_addon_conflict_alert(list(conflicts.values()), job=self.job)

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
        self._report_cpu_clamps()
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
        self._report_cpu_clamps()
        self._detect_pip_conflicts()
        self._detect_addon_conflicts()
        inst = self._inst()
        inst.write({"status": "error"})
        # Back to 'draft': nothing usable was left on the host, and
        # 'status' carries why. Re-running deploy() is then legal again.
        if inst.state == "deploying":
            inst._transition("draft")
