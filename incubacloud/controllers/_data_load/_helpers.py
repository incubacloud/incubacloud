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
