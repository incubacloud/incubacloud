"""Deliveries of GitLab webhooks (plan-gitlab.md §2.4, §4.7, §4.8).

The receiver records each delivery with two partial unique indexes: one
on the delivery (``Idempotency-Key``, which «Resend» keeps) and one on a
push's content (``push:<path>:<ref>:<before>:<after>``). A repeated
delivery fails on one of them and is answered 200 without dispatching.
The raw body is not stored: ``summary`` keeps only what is used, and no
e-mail address.
"""
import json
import logging
from datetime import timedelta

from odoo import _, api, fields, models

from ._repo_requirements import is_safe_git_ref
from .res_users_ext import as_platform

_logger = logging.getLogger(__name__)

ZERO_SHA = "0" * 40

#: The event types the receiver records; any other is answered 200 and dropped.
RECORDED_EVENTS = ("Push Hook", "Tag Push Hook", "Merge Request Hook")

#: A dispatch that failed is retried by the daily cron up to this many times in all.
MAX_DISPATCH_ATTEMPTS = 3

GITLAB_REPO_MOVED_CODE = "gitlab_repo_moved"


def summarize(body):
    """Return what the panel keeps of a delivery's body.

    The project, the refs and SHAs, the merge request's numbers (G4),
    ``user_username`` and the id and title of the pushed head commit:
    no e-mail, no name, no message body (Fable's review of G3).

    :param dict body: the decoded delivery
    :rtype: dict
    """
    project = body.get("project") or {}
    attrs = body.get("object_attributes") or {}
    after = body.get("after") or ""
    head = next(
        (c for c in body.get("commits") or () if isinstance(c, dict) and c.get("id") == after),
        None,
    ) or attrs.get("last_commit") or {}
    user = body.get("user") or {}
    summary = {
        "object_kind": body.get("object_kind") or "",
        "ref": body.get("ref") or "",
        "before": body.get("before") or "",
        "after": after,
        "checkout_sha": body.get("checkout_sha") or "",
        "path": (project.get("path_with_namespace") or "").lower(),
        "project_id": body.get("project_id") or project.get("id") or 0,
        "user_username": body.get("user_username") or user.get("username") or "",
        "last_commit": {
            "id": head.get("id") or "",
            "title": (head.get("title") or "")[:200],
        },
    }
    if attrs:
        summary.update({
            "iid": attrs.get("iid") or 0,
            "action": attrs.get("action") or "",
            "oldrev": attrs.get("oldrev") or "",
            "source_project_id": attrs.get("source_project_id") or 0,
            "target_project_id": attrs.get("target_project_id") or 0,
            "source_branch": attrs.get("source_branch") or "",
        })
    return summary


class CloudGitLabEvent(models.Model):
    _name = "cloud.gitlab.event"
    _inherit = "cloud.repo.event.mixin"
    _description = "GitLab Webhook Event"
    _order = "id desc"

    hook_id = fields.Many2one(
        "cloud.gitlab.hook", required=True, ondelete="cascade", index=True,
    )
    event_type = fields.Char(help="X-Gitlab-Event")
    delivery_key = fields.Char(help="Idempotency-Key; empty when GitLab sends none.")
    semantic_key = fields.Char(help="push:<path>:<ref>:<before>:<after>, pushes only.")
    summary = fields.Text(help="What is kept of the body, as compact JSON.")
    processed = fields.Boolean(default=False, index=True)
    error = fields.Text()
    dispatch_attempts = fields.Integer(default=0)

    def init(self):
        """Create the partial unique indexes a repeated delivery fails on."""
        cr = self.env.cr
        cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS cloud_gitlab_event_delivery_uidx
            ON cloud_gitlab_event (hook_id, delivery_key)
            WHERE delivery_key IS NOT NULL AND delivery_key <> ''
        """)
        cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS cloud_gitlab_event_semantic_uidx
            ON cloud_gitlab_event (hook_id, semantic_key)
            WHERE semantic_key IS NOT NULL AND semantic_key <> ''
        """)

    @api.model
    def _record_delivery(self, hook, event_type, delivery_key, body):
        """Insert a delivery; a repeated one raises ``UniqueViolation``.

        Run it in a savepoint: the caller answers a duplicate with 200.

        :param hook: the ``cloud.gitlab.hook`` delivered to
        :param str event_type: ``X-Gitlab-Event``
        :param str delivery_key: ``Idempotency-Key``, or ''
        :param dict body: the decoded delivery
        :rtype: cloud.gitlab.event
        """
        summary = summarize(body)
        semantic = ""
        if event_type == "Push Hook":
            semantic = ":".join((
                "push", summary["path"], summary["ref"],
                summary["before"], summary["after"],
            ))
        return as_platform(self).create({
            "hook_id": hook.id,
            "event_type": event_type,
            "delivery_key": (delivery_key or "")[:255],
            "semantic_key": semantic,
            "summary": json.dumps(summary, separators=(",", ":")),
        })

    def _summary(self):
        """Return the stored summary as a dict.

        :rtype: dict
        """
        self.ensure_one()
        try:
            return json.loads(self.summary or "{}")
        except ValueError:
            return {}

    # ── Dispatch ───────────────────────────────────────────────────────

    def _dispatch_safely(self):
        """Dispatch the event in a savepoint; a failure is kept, never raised.

        GitLab never retries a delivery, and an error answer counts
        towards disabling the webhook: a dispatch that raises leaves the
        event with its ``error`` and ``processed=False`` for the daily
        cron or the manager to reprocess, and the receiver still answers
        200 (Fable's review of G3).

        :return: whether the dispatch went through
        :rtype: bool
        """
        self.ensure_one()
        event = as_platform(self)
        attempts = event.dispatch_attempts + 1
        # Outside the dispatch's savepoint: a valid delivery makes the
        # webhook active even when its dispatch fails. In one of its own:
        # a state that cannot be written must not cost the delivery nor
        # answer 500 (Fable's review of the result of G3).
        try:
            with self.env.cr.savepoint():
                event.hook_id._mark_delivered()
        except Exception:
            _logger.exception("GitLab event %s: the webhook's state was not written", event.id)
        try:
            with self.env.cr.savepoint():
                event._dispatch()
                event.write({"error": False, "dispatch_attempts": attempts})
        except Exception as exc:
            _logger.exception("GitLab event %s: the dispatch failed", event.id)
            event.write({
                "processed": False, "dispatch_attempts": attempts,
                "error": f"{type(exc).__name__}: {exc}"[:2000],
            })
            return False
        return True

    def _dispatch(self):
        """Route the event to its handler; every path ends ``processed``."""
        self.ensure_one()
        if self.event_type == "Push Hook":
            self._process_push()
        else:
            # Tag pushes (the «Verify» test included) count as deliveries
            # only; merge requests arrive with G4.
            self.write({"processed": True})

    def _moved(self, summary):
        """Raise ``gitlab_repo_moved`` when the project's path changed in GitLab.

        GitLab keeps a project's id when it is renamed or moved; the
        lines are not changed by the panel, because the URL decides what
        is cloned with which token (§4.7.6).

        :param dict summary: the event's summary
        :return: whether the project moved
        :rtype: bool
        """
        hook = self.hook_id.sudo()
        if hook.scope != "project":
            return False
        if not hook.gitlab_id:
            if summary["path"] == hook.path and summary["project_id"]:
                hook.gitlab_id = summary["project_id"]
            return False
        if summary["project_id"] != hook.gitlab_id or summary["path"] == hook.path:
            return False
        self.env["cloud.alert"].sudo().raise_alert(GITLAB_REPO_MOVED_CODE, _(
            "The repository %(old)s is now %(new)s in GitLab: update the URL "
            "of its repository lines.", old=hook.path, new=summary["path"],
        ), key=f"gitlab_hook:{hook.id}")
        return True

    def _process_push(self):
        """Queue the rebuilds a push asks for (§4.8)."""
        self.ensure_one()
        summary = self._summary()
        ref = summary.get("ref", "")
        branch = ref.removeprefix("refs/heads/")
        path = summary.get("path", "")
        hook = self.hook_id.sudo()
        if (
            self._moved(summary) or not hook._covers(path)
            or summary.get("after") == ZERO_SHA or branch == ref or not branch
            or not is_safe_git_ref(branch)
        ):
            self.write({"processed": True})
            return
        lines = self.env["cloud.instance.repo"].sudo().search([
            ("provider_id", "=", hook.provider_id.id),
            ("repo_path", "=", path),
            ("branch", "=", branch),
            ("commit_sha", "in", (False, "")),
        ])
        sha = summary.get("checkout_sha") or summary.get("after") or ""
        push_info = {
            "push_repo": path,
            "push_branch": branch,
            "push_sha": sha[:7],
            "push_message": summary.get("last_commit", {}).get("title", "")[:80],
            "push_by": summary.get("user_username", ""),
            "push_provider": "gitlab",
        }
        triggered = self._rebuild_for_push(lines, push_info)
        self.write({"processed": True})
        if triggered:
            _logger.info(
                "Auto-rebuild triggered for: %s (GitLab push %s@%s)",
                ", ".join(triggered), path, branch,
            )

    # ── Daily cron ─────────────────────────────────────────────────────

    @api.model
    def _cron_purge_gitlab_events(self):
        """Reprocess the failed dispatches, then delete old processed events.

        A dispatch that failed fewer than :data:`MAX_DISPATCH_ATTEMPTS`
        times is dispatched again. Processed events older than
        ``gitlab_event_retention_days`` (0 keeps them) are deleted;
        unprocessed and failed ones are kept, as GitHub's are.
        """
        for event in self.sudo().search([
            ("processed", "=", False), ("error", "!=", False),
            ("dispatch_attempts", "<", MAX_DISPATCH_ATTEMPTS),
        ], order="id"):
            event._dispatch_safely()
        days = self.env["cloud.settings"].sudo()._get().gitlab_event_retention_days or 0
        if days > 0:
            old = self.sudo().search([
                ("processed", "=", True),
                ("create_date", "<", fields.Datetime.now() - timedelta(days=days)),
            ])
            if old:
                _logger.info("cloud.gitlab.event: purged %d row(s)", len(old))
                old.unlink()
