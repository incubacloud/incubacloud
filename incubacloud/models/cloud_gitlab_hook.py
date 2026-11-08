"""A GitLab webhook of the panel (plan-gitlab.md §2.3, §4.7.2, §4.7.5).

One row per ``(provider, scope, path)``: a project's webhook, or a
group's that covers every project below it by segments. GitLab delivers
to ``/cloud/gitlab/webhook/<hook_key>`` with the secret in
``X-Gitlab-Token``. The last delivery is not stored on this row but read
from ``cloud.gitlab.event``, so deliveries never write the same row.
"""
import hmac
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..gitlab.client import GitLabClient, GitLabError
from ._concurrency import read_committed_cursor
from ._git_urls import namespace_covers
from .encrypted_char import EncryptedChar

_logger = logging.getLogger(__name__)

#: The receiver's path; a webhook in GitLab whose URL holds it is the panel's.
WEBHOOK_PATH = "/cloud/gitlab/webhook/"

#: Seconds between two test deliveries of one webhook (GitLab: 5 a minute).
VERIFY_INTERVAL_S = 15

#: Days a panel's webhook no line uses is kept before it is deleted in GitLab.
ORPHAN_GRACE_DAYS = 7

GITLAB_HOOK_FAILING_CODE = "gitlab_hook_failing"


def new_hook_key():
    """Return a routing key: 32 hex characters.

    Hexadecimal on purpose: a ``token_urlsafe`` key could start with
    ``wp`` or ``vendor`` and fall into the ``-block`` router of a
    tenant's panel (§2.3).

    :rtype: str
    """
    return secrets.token_hex(16)


def _like_prefix(path):
    """Return *path* escaped for a ``=like`` pattern, followed by ``/%``.

    ``_`` and ``%`` in a GitLab path are literal: ``acm_`` must not
    cover ``acme/web`` (Fable's review of the result of G3).

    :param str path: a group path
    :rtype: str
    """
    escaped = path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}/%"


def _is_panel_hook(remote_url, own_url):
    """Return whether a webhook in GitLab is this panel's, by its URL.

    The receiver's path prefix on this panel's host: a ``hook_key``
    that changed is still adopted, and another panel's webhook on the
    same project is left alone (Fable's review of G3).

    :param str remote_url: the URL of a webhook in GitLab
    :param str own_url: this webhook's URL
    :rtype: bool
    """
    remote, own = urlsplit(remote_url or ""), urlsplit(own_url)
    return remote.hostname == own.hostname and remote.path.startswith(WEBHOOK_PATH)


class CloudGitLabHook(models.Model):
    _name = "cloud.gitlab.hook"
    _description = "GitLab webhook"
    _order = "path"

    provider_id = fields.Many2one(
        "cloud.git.provider", required=True, ondelete="restrict", index=True,
    )
    scope = fields.Selection(
        [("project", "Project"), ("group", "Group")],
        required=True, default="project",
    )
    path = fields.Char(required=True, help="Project or group path, lower case.")
    gitlab_id = fields.Integer(
        string="GitLab id", help="Id of the project or group in GitLab.",
    )
    hook_key = fields.Char(
        required=True, index=True, copy=False, default=lambda _self: new_hook_key(),
        groups="incubacloud.group_cloud_consultant",
    )
    secret = EncryptedChar(
        groups="incubacloud.group_cloud_manager", copy=False,
        default=lambda self: secrets.token_urlsafe(32),
    )
    remote_hook_id = fields.Integer(
        help="Id of the webhook in GitLab when the panel created it; empty "
        "when it was set up by hand.",
    )
    state = fields.Selection(
        [
            ("pending", "Pending"), ("active", "Active"),
            ("failing", "Failing"), ("orphan", "Orphan"),
        ],
        required=True, default="pending",
    )
    orphan_since = fields.Datetime(copy=False)
    verify_requested_at = fields.Datetime(copy=False)
    last_status_check = fields.Datetime(copy=False)

    _path_uniq = models.Constraint(
        "unique (provider_id, scope, path)",
        "One webhook per provider, scope and path.",
    )
    _key_uniq = models.Constraint("unique (hook_key)", "The routing key is unique.")

    # ── Matching ───────────────────────────────────────────────────────

    def _covers(self, path):
        """Return whether this webhook delivers the project *path*.

        :param str path: a project path
        :rtype: bool
        """
        self.ensure_one()
        path = (path or "").lower()
        if self.scope == "group":
            return path != self.path and namespace_covers(self.path, path)
        return path == self.path

    def _lines(self):
        """Return the instance and project repository lines this webhook serves.

        :return: ``(instance lines, project lines)``
        :rtype: tuple
        """
        self.ensure_one()
        domain = [("provider_id", "=", self.provider_id.id)]
        if self.scope == "project":
            domain.append(("repo_path", "=", self.path))
        else:
            domain.append(("repo_path", "=like", _like_prefix(self.path)))
        return tuple(
            self.env[model].sudo().search(domain)
            for model in ("cloud.instance.repo", "cloud.project.repo")
        )

    @api.model
    def _find_for(self, provider, path):
        """Return the webhook delivering *path*: its project's, else a group's.

        :rtype: cloud.gitlab.hook (empty when none)
        """
        hooks = self.sudo().search([("provider_id", "=", provider.id)])
        own = hooks.filtered(lambda h: h.scope == "project" and h.path == path)
        if own:
            return own[0]
        groups = hooks.filtered(
            lambda h: h.scope == "group" and h.state == "active" and h._covers(path),
        )
        return groups[:1]

    # ── Deliveries ─────────────────────────────────────────────────────

    def _token_matches(self, token):
        """Return whether *token* is this webhook's secret, in constant time.

        :rtype: bool
        """
        self.ensure_one()
        secret = self.sudo().secret or ""
        return bool(secret) and hmac.compare_digest(
            secret.encode(), (token or "").encode(),
        )

    def _mark_delivered(self):
        """Turn a ``pending`` or ``failing`` webhook ``active`` after a valid delivery.

        Written only when it changes, so deliveries do not rewrite the
        same row (Fable's review of G3). An ``orphan`` stays one: no line
        uses it, and a delivery must not restart its grace, or a webhook
        that keeps receiving pushes would never be deleted; the daily
        check turns it back when a line uses it again. A ``failing`` one
        has its ``gitlab_hook_failing`` alert resolved, since GitLab
        delivers to it again (Fable's review of the result of G3).
        """
        Alert = self.env["cloud.alert"].sudo()
        for hook in self.sudo():
            if hook.state not in ("pending", "failing"):
                continue
            if hook.state == "failing":
                Alert.resolve_alert(GITLAB_HOOK_FAILING_CODE, key=f"gitlab_hook:{hook.id}")
            hook.write({"state": "active"})

    def _url(self):
        """Return the URL GitLab delivers to.

        :rtype: str
        """
        self.ensure_one()
        base = self.env["cloud.alert"].sudo()._base_url()
        return f"{base}{WEBHOOK_PATH}{self.sudo().hook_key}"

    def _last_delivery_at(self):
        """Return when the last delivery to this webhook was recorded, or False.

        :rtype: datetime or bool
        """
        self.ensure_one()
        last = self.env["cloud.gitlab.event"].sudo().search(
            [("hook_id", "=", self.id)], order="id desc", limit=1,
        )
        return last.create_date or False

    # ── GitLab's API ───────────────────────────────────────────────────

    def _kind(self):
        """Return the API collection of the webhook: ``projects`` or ``groups``.

        :rtype: str
        """
        return "groups" if self.scope == "group" else "projects"

    def _client(self, need="api_write"):
        """Return an API client with the credential of the path for *need*.

        :rtype: gitlab.client.GitLabClient
        :raises UserError: when no credential qualifies
        """
        self.ensure_one()
        provider = self.provider_id.sudo()
        cred = provider._credential_for(self.path, need)
        return GitLabClient(
            provider.api_url, cred.token, ca_pem=provider.ca_bundle or None,
            pin_ip=provider._resolve_pin(),
        )

    def _register_remote(self):
        """Create the webhook in GitLab, or adopt the panel's that is there.

        Idempotent: an existing ``remote_hook_id`` is read first; a
        webhook in GitLab whose URL holds the receiver's path is adopted
        and rewritten with the URL and the token together.

        :return: ``None`` when registered, else the reason it must be
            set up by hand
        :rtype: str or None
        """
        self.ensure_one()
        hook = self.sudo()
        try:
            client = hook._client("api_write")
        except UserError as exc:
            return str(exc.args[0])
        kind = hook._kind()
        url = hook._url()
        try:
            if not hook.gitlab_id and kind == "projects":
                hook.gitlab_id = client.get_project(hook.path)["id"]
            target = hook.gitlab_id or hook.path
            remote = None
            if hook.remote_hook_id:
                try:
                    remote = client.get_hook(kind, target, hook.remote_hook_id)
                except GitLabError as exc:
                    if exc.code != "not_found":
                        raise
            if not remote:
                remote = next((
                    h for h in client.list_hooks(kind, target)
                    if _is_panel_hook(h.get("url"), url)
                ), None)
            if remote:
                client.update_hook(kind, target, remote["id"], url, hook.secret)
            else:
                remote = client.create_hook(kind, target, url, hook.secret)
        except GitLabError as exc:
            if exc.code == "forbidden":
                return _(
                    "The credential does not have the Maintainer role in the "
                    "project (Owner for a group)."
                )
            return _("GitLab did not accept the webhook (%(code)s).", code=exc.code)
        if hook.remote_hook_id != remote["id"]:
            hook.remote_hook_id = remote["id"]
        return None

    def _rotate_secret(self):
        """Give the webhook a new secret, and GitLab too when the webhook is the panel's.

        The panel keeps the new secret only once GitLab took it: one that
        GitLab refused would leave GitLab on the old one, and every
        delivery would be a 401 (Fable's review of the result of G3). A
        webhook set up by hand gets it at once, to be pasted in GitLab.

        :raises UserError: when GitLab refuses the new secret, or no
            credential may change it
        """
        self.ensure_one()
        hook = self.sudo()
        new_secret = secrets.token_urlsafe(32)
        if hook.remote_hook_id:
            try:
                hook._client("api_write").update_hook(
                    hook._kind(), hook.gitlab_id or hook.path,
                    hook.remote_hook_id, hook._url(), new_secret,
                )
            except GitLabError as exc:
                raise UserError(_(
                    "GitLab did not take the new secret (%(code)s).", code=exc.code,
                )) from exc
        hook.secret = new_secret

    def _stamp_verify_request(self, now):
        """Record that a test was asked at *now*, unless one was in the last 15 s.

        On a READ COMMITTED cursor of its own, committed before GitLab is
        called: the test delivery arrives in another request, and two
        «Verify» at once meet the same row instead of both passing
        (§4.7.4, Fable's review of the result of G3).

        :param datetime now: the moment of the request
        :return: whether the stamp was written
        :rtype: bool
        """
        self.ensure_one()
        with read_committed_cursor(self.env.registry) as cr:
            cr.execute("""
                UPDATE cloud_gitlab_hook SET verify_requested_at = %s
                 WHERE id = %s
                   AND (verify_requested_at IS NULL OR verify_requested_at <= %s)
                RETURNING id
            """, (now, self.id, now - timedelta(seconds=VERIFY_INTERVAL_S)))
            stamped = bool(cr.fetchone())
            cr.commit()
        self.invalidate_recordset(["verify_requested_at"])
        return stamped

    def _request_test(self):
        """Ask GitLab for a test delivery, at most one per webhook every 15 s.

        :return: ``{ok, verify_requested_at}`` or ``{ok: False, error}``
        :rtype: dict
        """
        self.ensure_one()
        hook = self.sudo()
        now = fields.Datetime.now()
        if not hook.remote_hook_id:
            return {"ok": False, "error": _(
                "This webhook was set up by hand: press Test → Tag push events "
                "in GitLab."
            )}
        if not hook._stamp_verify_request(now):
            return {"ok": False, "error": _(
                "A test was asked for a moment ago: wait a few seconds."
            )}
        try:
            hook._client("api_write").test_hook(
                hook._kind(), hook.gitlab_id or hook.path, hook.remote_hook_id,
            )
        except GitLabError as exc:
            if exc.code == "rate_limited":
                return {"ok": False, "error": _(
                    "GitLab limits the tests: wait a minute."
                )}
            return {"ok": False, "error": _(
                "GitLab answered %(code)s; check that GitLab reaches %(url)s.",
                code=exc.status or exc.code, url=hook._url(),
            )}
        return {"ok": True, "verify_requested_at": fields.Datetime.to_string(now)}

    # ── Daily check ────────────────────────────────────────────────────

    def _check_remote_state(self):
        """Read the webhook's state in GitLab and raise or resolve its alert.

        Never sends a test nor re-enables it: «Verify» does, by hand
        (§4.7.5).
        """
        self.ensure_one()
        hook = self.sudo()
        try:
            remote = hook._client("api_read").get_hook(
                hook._kind(), hook.gitlab_id or hook.path, hook.remote_hook_id,
            )
        except GitLabError as exc:
            if exc.code != "forbidden":
                raise
            # A credential without the Maintainer role (Owner for a group)
            # cannot read the webhook: nothing is known of its state.
            _logger.warning(
                "GitLab webhook %s of %s: the credential cannot read it (403); "
                "its state is not checked", hook.id, hook.path,
            )
            return
        hook.last_status_check = fields.Datetime.now()
        disabled = remote.get("alert_status") in ("disabled", "temporarily_disabled") \
            or bool(remote.get("disabled_until"))
        Alert = self.env["cloud.alert"].sudo()
        key = f"gitlab_hook:{hook.id}"
        if disabled:
            if hook.state != "failing":
                hook.state = "failing"
            Alert.raise_alert(GITLAB_HOOK_FAILING_CODE, _(
                "GitLab has disabled the webhook of %(path)s after failed "
                "deliveries: the pushes of that time were lost. Press Verify "
                "under Settings → Git providers and rebuild by hand the "
                "instances that need it.", path=hook.path,
            ), key=key)
        else:
            if hook.state == "failing":
                hook.state = "active"
            Alert.resolve_alert(GITLAB_HOOK_FAILING_CODE, key=key)

    def _sweep_orphan(self):
        """Mark a webhook no line uses, and delete the panel's after 7 days.

        A 404 from GitLab is taken as done.
        """
        self.ensure_one()
        hook = self.sudo()
        if any(hook._lines()):
            if hook.state == "orphan":
                # Deliveries leave an orphan alone (``_mark_delivered``):
                # one that had them is active again, not waiting for one.
                state = "active" if hook._last_delivery_at() else "pending"
                hook.write({"state": state, "orphan_since": False})
            return
        now = fields.Datetime.now()
        if hook.state != "orphan":
            hook.write({"state": "orphan", "orphan_since": now})
            return
        if not hook.remote_hook_id or now - hook.orphan_since < timedelta(days=ORPHAN_GRACE_DAYS):
            return
        try:
            hook._client("api_write").delete_hook(
                hook._kind(), hook.gitlab_id or hook.path, hook.remote_hook_id,
            )
        except GitLabError as exc:
            if exc.code != "not_found":
                raise
        hook.unlink()
