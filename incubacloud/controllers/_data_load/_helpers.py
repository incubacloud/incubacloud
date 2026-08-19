"""Module-level helpers shared by every data_load mixin.

Kept here (not on the controller class) because a few are imported
from sibling modules — ``deploy_instance_executor`` uses
``_parse_github_repo_path``, and inheriting controllers can reuse
``_job_response`` to wrap their own deploy responses. The top-level
``data_load.py`` re-exports the public ones so those imports keep
working after the split.
"""
import logging
import re
import shlex
from urllib.parse import quote as _url_quote

_logger = logging.getLogger(__name__)


def _has_encrypted(record, field_name):
    """Return True iff *record.field_name* has a stored (encrypted) value.

    Used ONLY for ``has_xxx`` existence booleans — never use this to
    substitute an actual read of the decrypted value. If the caller
    needs to consume the secret (SSH connection, API call, etc.) let
    the decryption exception propagate: using a value we cannot read
    is worse than failing loud.

    Reading an EncryptedChar field triggers decryption, which raises
    when the current INCUBACLOUD_SECRET_KEY cannot open the ciphertext
    (key rotation gap, corrupted value, cross-database restore). A
    single broken secret must NOT prevent the SPA from loading a
    host / instance / backend detail. Since ``convert_to_record`` only
    reaches the decrypt path when the DB column is non-empty, a
    decrypt exception implies ciphertext IS stored — semantically the
    field IS set. We return ``True`` so the UI reflects reality
    ("there is a password here, just broken") and the operator can
    rotate / re-enter it rather than being tricked into creating a
    duplicate.
    """
    try:
        return bool(record[field_name])
    except Exception as e:  # noqa: BLE001 — any decrypt / ORM failure
        _logger.warning(
            "[encrypted] failed to decrypt %s on %s(id=%s): %s "
            "(reporting as stored-but-unreadable)",
            field_name, record._name, record.id, e,
        )
        return True


# Allowed (model, field) pairs for the /cloud/get_secret endpoint.
_SECRET_FIELDS = {
    'cloud.host': {
        'password', 'traefik_panel_password',
    },
    'cloud.instance': {
        'odoo_admin_password', 'postgres_password', 'smtp_relay_password',
    },
    'cloud.backup.backend': {
        's3_secret_access_key', 'passphrase',
    },
    'res.users': {
        'cloud_telegram_bot_token', 'cloud_webhook_secret',
    },
}


# Tag scope → model. Each scope has its own model so tags of one entity
# don't pollute another's cloud.
_TAG_MODEL_BY_SCOPE = {
    'project':  'cloud.tag',
    'host':     'cloud.host.tag',
    'instance': 'cloud.instance.tag',
}


# Defensive cap for list endpoints that would otherwise fetch every row.
# Protects against OOM/DoS once the system accumulates thousands of
# projects/hosts/instances. The frontend renders a TruncationBanner
# when ``truncated=True`` is returned alongside ``total``.
_LIST_MAX = 200


# Remote path sanitation for /cloud/browse_host_dir.
# Accepts: ~, ~/segment(/segment)*?/?, /segment(/segment)*?/?, relative
# segment(/segment)*?/?. Each segment is [A-Za-z0-9._-]+. Rejects shell
# metacharacters, spaces, path traversal '..'.
_BROWSE_PATH_RE = re.compile(
    r'^~$|^(~/|/)?[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*/?$'
)


def _normalize_domain(domain):
    """Strip protocol prefix and trailing slashes from a domain string.

    Stores only the bare hostname so the frontend can always prepend
    the correct scheme (https://) without ending up doubled.
    """
    if not domain:
        return domain
    d = domain.strip()
    for prefix in ('https://', 'http://'):
        d = d.removeprefix(prefix)
    return d.strip('/')


# Anchored at both ends so an attacker cannot smuggle a different host
# as a URL prefix (e.g. ``https://attacker.tld/foo/github.com/u/r``,
# which would otherwise match ``re.search`` and let downstream callers
# clone from ``attacker.tld``).
_GH_URL_RE = re.compile(
    r"^(?:(?:https?://)?(?:www\.)?github\.com/|git@github\.com:)"
    r"([^/]+)/([^/]+)$"
)


def _parse_github_repo_path(url):
    """Extract (owner, repo) from a GitHub URL.

    Accepts only github.com hosts. Supports
    ``https://github.com/owner/repo[.git]`` (with or without
    ``www.``) and ``git@github.com:owner/repo[.git]``.
    """
    url = (url or "").strip().rstrip("/").removesuffix(".git")
    m = _GH_URL_RE.fullmatch(url)
    if not m:
        raise ValueError(f"Cannot parse GitHub URL: {url!r}")
    return m.group(1), m.group(2)


def _gh_seg(s):
    """URL-encode a GitHub API path/query segment.

    ``safe=''`` forces '/' to '%2F'. The GitHub API accepts percent-
    encoded slashes in branch refs (``feature/auth`` → ``feature%2Fauth``)
    and rejects unencoded ones. Encoding everything prevents path-
    traversal tricks like ``main/../../user`` and query injection via
    ``&per_page=999``.
    """
    return _url_quote((s or '').strip(), safe='')


def _has_pat(env):
    """Check if a PAT is configured in cloud.settings."""
    settings = env["cloud.settings"]._get_system()
    return bool((settings.github_pat or "").strip())


def _capped_search(Model, domain=None, order=None, limit=None):
    """Return (records, total, truncated, limit) with a hard cap.

    ``total`` is a separate search_count so the UI can render
    "showing N of TOTAL" banners. ``truncated`` is True when the
    cap actually clipped the result; a WARNING is logged whenever
    that happens so ops can raise the cap or add real pagination.
    """
    dom = domain or []
    eff_limit = limit or _LIST_MAX
    total = Model.sudo().search_count(dom)
    records = Model.search(dom, order=order, limit=eff_limit)
    truncated = total > eff_limit
    if truncated:
        _logger.warning(
            "List result truncated: model=%s total=%d limit=%d",
            Model._name, total, eff_limit,
        )
    return records, total, truncated, eff_limit


def _is_safe_remote_path(p):
    if not isinstance(p, str) or not p:
        return False
    if not _BROWSE_PATH_RE.match(p):
        return False
    return all(seg != '..' for seg in p.split('/'))


def _quote_remote_path(p):
    """shlex-quote a remote path while preserving ~/ home expansion.

    shlex.quote wraps its input in single quotes, inside which the
    shell does not expand ~. Split the '~/' prefix out so the shell
    still resolves $HOME.
    """
    if p == '~':
        return '"$HOME"'
    if p.startswith('~/'):
        return '"$HOME"/' + shlex.quote(p[2:])
    return shlex.quote(p)


# ── Odoo log archive (logs/odoo.log + logrotate) ─────────────────

#: The shapes logrotate can produce for the Odoo log of an instance:
#: the live file, yesterday's (kept plain by ``delaycompress``) and the
#: compressed ones behind it. The name reaches a shell, so nothing else
#: is accepted — no globbing, no traversal, no separators.
_LOG_ARCHIVE_RE = re.compile(r'^odoo\.log(\.\d{4}-\d{2}-\d{2}(\.gz)?)?$')

#: The same shapes, as the ``find -regex`` the host-side commands use
#: to pick their candidates. Matched against ``./<name>`` from inside
#: ``logs/``, and always paired with ``-type f``: a symlink planted in
#: ``logs/`` from inside the container must neither be read nor take
#: up one of the slots a bounded sweep has.
_LOG_ARCHIVE_FIND_RE = r'\./odoo\.log(\.[0-9]{4}-[0-9]{2}-[0-9]{2}(\.gz)?)?'

#: Where the instance's Odoo log lives, relative to its remote dir.
LOG_DIRNAME = 'logs'
LOG_BASENAME = 'odoo.log'


def _log_files_newest_first(limit=None):
    """Return the pipeline that names the archive's real files, newest first.

    Runs from inside ``logs/``. Regular files only, logrotate's shapes
    only, ordered by modification time; *limit* caps the list so a
    bounded sweep reads the newest days.

    :param limit: newest N names to keep, or None for all
    :return: the shell pipeline, one file name per line
    """
    pipeline = (
        "find . -maxdepth 1 -type f -regextype posix-extended "
        f"-regex {shlex.quote(_LOG_ARCHIVE_FIND_RE)} "
        "-printf '%T@ %f\\n' 2>/dev/null | sort -rn"
    )
    if limit is not None:
        pipeline += f' | head -n {int(limit)}'
    return pipeline + " | cut -d' ' -f2-"


def _pick_log_file(name):
    """Return the snippet that sets ``$f`` to the file to read for *name*.

    A plain day (``odoo.log.<date>``) is what the viewer lists until
    the next rotation compresses it; a viewer left open over midnight
    then asks for a name that has become ``<name>.gz``. The plain name
    therefore falls back to its compressed twin. Either way only a
    regular file qualifies — a symlink is refused, not followed — and
    the snippet fails when nothing does, so what follows it never runs.

    :param str name: archive file name, already validated
    :return: a shell snippet usable as an ``&&`` operand
    """
    candidates = [name] if name.endswith('.gz') else [name, f'{name}.gz']
    paths = shlex.join(f'{LOG_DIRNAME}/{c}' for c in candidates)
    return (
        f'f=; for c in {paths}; do '
        'if [ -f "$c" ] && [ ! -L "$c" ]; then f="$c"; break; fi; done; '
        '[ -n "$f" ]'
    )


def is_safe_log_archive(name):
    """True iff *name* is a log file the panel is willing to read.

    :param name: candidate file name coming from the browser
    :return: bool
    """
    return bool(isinstance(name, str) and _LOG_ARCHIVE_RE.match(name))


def odoo_log_live_command(inst_dir, yaml_file, lines):
    """Return the command that tails an instance's current Odoo log.

    Prefers the file on the host — the only copy that survives a
    rebuild — and falls back to the container's output for an instance
    that has not been rebuilt since file logging shipped, so the viewer
    never goes blank during the transition.

    :param str inst_dir: remote instance directory (may start with ~/)
    :param str yaml_file: compose file of the instance's environment
    :param int lines: how many trailing lines to return
    :return: the shell command
    """
    qdir = _quote_remote_path(inst_dir)
    n = int(lines)
    live = f'{LOG_DIRNAME}/{LOG_BASENAME}'
    return (
        f'cd {qdir} && '
        f'if [ -f {live} ] && [ ! -L {live} ]; then '
        f'tail -n {n} {live}; '
        f'else docker compose -f {shlex.quote(yaml_file)} '
        f'logs --no-color --tail={n} odoo 2>&1; fi'
    )


def odoo_log_read_command(inst_dir, name, lines, search=''):
    """Return the command that reads one archived Odoo log.

    ``zcat -f`` covers both shapes the archive has (the compressed
    files and the two plain ones), so the caller does not have to
    branch on the extension — nor on whether the plain day it asked
    for has been compressed since it was listed.

    :param str inst_dir: remote instance directory (may start with ~/)
    :param str name: archive file name, already validated
    :param int lines: how many trailing lines to return
    :param str search: optional fixed-string filter applied on the host
    :return: the shell command
    """
    qdir = _quote_remote_path(inst_dir)
    n = int(lines)
    pipeline = 'zcat -f "$f" 2>/dev/null'
    if search:
        pipeline += f' | grep -aF -- {shlex.quote(search)}'
    return (
        f'cd {qdir} && {_pick_log_file(name)} && '
        f'{{ {pipeline} | tail -n {n}; }} || true'
    )


def log_archive_list_command(inst_dir):
    """Return the command that lists an instance's Odoo log archive.

    One line per file as ``name|size|mtime``. Tolerates an instance
    with no ``logs/`` at all (never rebuilt since file logging), which
    is a state to report, not an error.

    :param str inst_dir: remote instance directory (may start with ~/)
    :return: the shell command
    """
    qdir = _quote_remote_path(inst_dir)
    return (
        f'cd {qdir}/{LOG_DIRNAME} 2>/dev/null && '
        "find . -maxdepth 1 -type f -regextype posix-extended "
        f"-regex {shlex.quote(_LOG_ARCHIVE_FIND_RE)} "
        "-printf '%f|%s|%Ts\\n' 2>/dev/null || true"
    )


def log_archive_search_command(inst_dir, term, max_files=60, timeout_s=30):
    """Return the command that finds which archived days mention *term*.

    Newest day first, one ``name|count`` line per day that matches, and
    a final ``IC_DONE`` marker: without it a sweep the timeout cut
    short would be indistinguishable from one that found nothing,
    which is the wrong answer to give someone hunting an incident.

    The *max_files* budget is spent on real log files only: candidates
    come from ``find -type f`` restricted to logrotate's shapes, so a
    symlink or a junk file planted in ``logs/`` from inside the
    container cannot push the real days out of the sweep.

    :param str inst_dir: remote instance directory (may start with ~/)
    :param str term: fixed string to look for
    :param int max_files: newest N files to sweep
    :param int timeout_s: seconds before the sweep is cut short
    :return: the shell command
    """
    qdir = _quote_remote_path(inst_dir)
    inner = (
        f'for f in $({_log_files_newest_first(max_files)}); do '
        f'[ -f "$f" ] || continue; [ -L "$f" ] && continue; '
        f'n=$(zcat -f "$f" 2>/dev/null | grep -acF -- {shlex.quote(term)}); '
        f'[ "$n" -gt 0 ] && printf "%s|%s\\n" "$f" "$n"; '
        f'done; printf "IC_DONE\\n"'
    )
    return (
        f'cd {qdir}/{LOG_DIRNAME} 2>/dev/null && '
        f'timeout {int(timeout_s)} sh -c {shlex.quote(inner)} || true'
    )


def log_archive_download_command(inst_dir, name, max_bytes=64 * 1024 * 1024):
    """Return the command that streams one archived log as gzip bytes.

    Already-compressed files are sent as they are; the two plain ones
    are compressed on the way out. Bounded, because a log whose
    rotation stalled can reach sizes that would take the Odoo worker's
    memory with it — the first *max_bytes* of an absurd log are a
    better outcome than a dead worker, and the health probe alerts on
    exactly that instance anyway. A plain day that was compressed since
    it was listed is served from its ``.gz`` twin.

    :param str inst_dir: remote instance directory (may start with ~/)
    :param str name: archive file name, already validated
    :param int max_bytes: cap on the bytes returned
    :return: the shell command
    """
    qdir = _quote_remote_path(inst_dir)
    return (
        f'cd {qdir} && {_pick_log_file(name)} && '
        '{ case "$f" in *.gz) cat "$f";; *) gzip -c "$f";; esac; } '
        f'2>/dev/null | head -c {int(max_bytes)}'
    )


def _job_response(env, job_id):
    """Return a consistent dict for a deploy/rebuild job response.

    If the job is blocked by a pip-conflict alert, the response
    includes ``blocked=True`` plus the conflict details so the
    frontend can show a clear notification instead of opening the
    log page.
    """
    job = env['cloud.job'].browse(job_id)
    alert = job.blocked_alert_id
    if not alert:
        return {'job_id': job_id}
    return {
        'job_id': job_id,
        'blocked': True,
        'alert_id': alert.id,
        'alert_code': alert.code or '',
        'message': alert.message or '',
        'conflicts': alert.conflict_data or [],
    }
