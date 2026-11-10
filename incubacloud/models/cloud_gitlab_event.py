"""Deliveries of GitLab webhooks.

The receiver records each delivery with two partial unique indexes: one
on the delivery (``Idempotency-Key``, which «Resend» keeps) and one on a
push's content (``push:<path>:<ref>:<before>:<after>``). A repeated
delivery fails on one of them and is answered 200 without dispatching.
The raw body is not stored: ``summary`` keeps only what is used, and no
e-mail address.

A push rebuilds the instances that follow its branch. A merge request
opens, rebuilds and closes previews as a GitHub pull request does, with
the same rules: only from a branch of the project's own repository,
never from a fork, and only from a production whose own repository lines
follow it. Its note is written by ``cloud.gitlab.mr.note``, a job queued
here: no call to GitLab inside a delivery.
"""
import json
import logging
import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ._repo_requirements import is_safe_git_ref
from .cloud_github_event import PR_PREVIEW_FAILED_CODE
from .res_users_ext import as_platform

_logger = logging.getLogger(__name__)

ZERO_SHA = "0" * 40

#: The event types the receiver records; any other is answered 200 and dropped.
RECORDED_EVENTS = ("Push Hook", "Tag Push Hook", "Merge Request Hook")

#: A dispatch that failed is retried by the daily cron up to this many times in all.
MAX_DISPATCH_ATTEMPTS = 3

GITLAB_REPO_MOVED_CODE = "gitlab_repo_moved"

#: A merge request's actions that open, and that close, its previews.
#: ``update`` rebuilds them only with ``oldrev`` (new commits); without
#: it (title, labels, reviewers) and the approvals, nothing happens.
MR_OPEN_ACTIONS = ("open", "reopen")
MR_CLOSE_ACTIONS = ("close", "merge")


def summarize(body):
    """Return what the panel keeps of a delivery's body.

    The project, the refs and SHAs, the merge request's numbers,
    ``user_username`` and the id and title of the pushed head commit:
    no e-mail, no name, no message body.

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
        200.

        :return: whether the dispatch went through
        :rtype: bool
        """
        self.ensure_one()
        event = as_platform(self)
        attempts = event.dispatch_attempts + 1
        # Outside the dispatch's savepoint: a valid delivery makes the
        # webhook active even when its dispatch fails. In one of its own:
        # a state that cannot be written must not cost the delivery nor
        # answer 500.
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
        elif self.event_type == "Merge Request Hook":
            self._process_merge_request()
        else:
            # Tag pushes (the «Verify» test included) count as deliveries only.
            self.write({"processed": True})

    def _moved(self, summary):
        """Raise ``gitlab_repo_moved`` when the project's path changed in GitLab.

        GitLab keeps a project's id when it is renamed or moved; the
        lines are not changed by the panel, because the URL decides what
        is cloned with which token.

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
        """Queue the rebuilds a push asks for."""
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

    # ── Merge requests ─────────────────────────────────────────────────

    def _process_merge_request(self):
        """Open, rebuild or close the previews a merge request asks for.

        ``open``/``reopen`` open them, ``update`` with new commits
        (``oldrev``) rebuilds them, ``close``/``merge`` delete them; any
        other action, an unsafe branch, a path the webhook does not
        deliver or a moved project does nothing.
        """
        self.ensure_one()
        summary = self._summary()
        path = summary.get("path", "")
        iid = summary.get("iid") or 0
        branch = summary.get("source_branch", "")
        action = summary.get("action", "")
        hook = self.hook_id.sudo()
        if (
            self._moved(summary) or not hook._covers(path) or not iid
            or not branch or not is_safe_git_ref(branch)
        ):
            self.write({"processed": True})
            return
        review = {
            "provider": hook.provider_id, "path": path, "iid": iid,
            "branch": branch,
            "from_fork": self._from_fork(summary),
        }
        if action in MR_OPEN_ACTIONS:
            self._open_mr_previews(review)
        elif action == "update" and summary.get("oldrev"):
            self._sync_mr_previews(review)
        elif action in MR_CLOSE_ACTIONS:
            self._close_mr_previews(review)
        self.write({"processed": True})

    @staticmethod
    def _from_fork(summary):
        """Return whether the merge request's branch comes from another project.

        Its source must be the very project it is opened on, the one the
        webhook belongs to: a fork's branch is code written by someone
        without access to the repository. A merge request that does not
        name both projects counts as one from a fork.

        :param dict summary: the event's summary
        :rtype: bool
        """
        source = summary.get("source_project_id") or 0
        target = summary.get("target_project_id") or 0
        project = summary.get("project_id") or 0
        return not (source and source == target and (not project or project == target))

    def _mr_preview_sources(self, review):
        """Return the production instances a merge request may preview.

        Those whose own repository lines follow the merge request's
        project on the webhook's server, unpinned, deployed, in a project
        with previews switched on. The instance's lines decide, not its
        project's, which are only a template for new instances.

        :param dict review: what :meth:`_process_merge_request` read
        :return: ``cloud.instance`` recordset
        """
        return self.env["cloud.instance.repo"].sudo().search([
            ("provider_id", "=", review["provider"].id),
            ("repo_path", "=", review["path"]),
            ("commit_sha", "in", (False, "")),
            ("instance_id.environment", "=", "production"),
            ("instance_id.deployed", "=", True),
            ("instance_id.project_id.pr_reviews_enabled", "=", True),
        ]).mapped("instance_id")

    def _mr_previews(self, review, project=None):
        """Return the active previews of the merge request, of one project or all.

        :rtype: cloud.instance
        """
        domain = [
            ("pr_provider_id", "=", review["provider"].id),
            ("pr_repo", "=", review["path"]),
            ("pr_number", "=", review["iid"]),
        ]
        if project is not None:
            domain.append(("project_id", "=", project.id))
        return self.env["cloud.instance"].sudo().search(domain)

    def _open_mr_previews(self, review):
        """Clone a preview ``mr-<iid>`` of each production the merge request reaches.

        One per project, as on GitHub; a project that has one already
        gets nothing. A merge request from a fork gets no preview, and a
        note saying why when some production would have had one.

        :param dict review: what :meth:`_process_merge_request` read
        """
        Note = self.env["cloud.gitlab.mr.note"]
        sources = self._mr_preview_sources(review)
        if review["from_fork"]:
            if sources.filtered(self._pr_preview_allowed):
                _logger.info(
                    "GitLab MR !%s on %s comes from a fork: no preview.",
                    review["iid"], review["path"],
                )
                Note._enqueue_for(
                    review["provider"].id, review["path"], review["iid"], 0,
                    kind="fork",
                )
            return
        for prod in sources:
            if not self._pr_preview_allowed(prod):
                continue
            if self._mr_previews(review, prod.project_id):
                continue
            try:
                # Savepoint: a clone that fails half-way leaves nothing,
                # or every later «reopen» would find a preview that is not.
                with self.env.cr.savepoint():
                    prod.clone_to_staging(
                        f"mr-{review['iid']}",
                        pr_number=review["iid"],
                        pr_repo=review["path"],
                        pr_head_branch=review["branch"],
                        pr_provider_id=review["provider"].id,
                    )
            except (UserError, ValidationError) as exc:
                _logger.warning(
                    "MR preview refused for !%s (%s): %s",
                    review["iid"], review["path"], exc,
                )
                self._report_mr_preview_failure(prod, review, str(exc))
            except Exception:
                ref = uuid.uuid4().hex[:8]
                _logger.exception(
                    "Failed to create MR preview for !%s (ref: %s)",
                    review["iid"], ref,
                )
                self._report_mr_preview_failure(
                    prod, review, f"An internal error occurred (ref: {ref}).",
                )
            else:
                _logger.info(
                    "MR preview instance created for !%s (%s) → project %s",
                    review["iid"], review["path"], prod.project_id.name,
                )
                self.env["cloud.alert"].sudo().resolve_alert(
                    PR_PREVIEW_FAILED_CODE, instance=prod,
                )

    def _sync_mr_previews(self, review):
        """Rebuild the merge request's previews after new commits.

        Each one active, deployed and on an active host has its line of
        the merge request's project follow the source branch, and its
        rebuild queued in a savepoint of its own; its note is reconciled.

        :param dict review: what :meth:`_process_merge_request` read
        """
        Note = self.env["cloud.gitlab.mr.note"]
        for inst in self._mr_previews(review):
            if not (inst.active and inst.deployed
                    and inst.host_id and inst.host_id.active):
                continue
            job_type = self._rebuild_job_type(inst)
            if not job_type:
                continue
            try:
                with self.env.cr.savepoint():
                    for line in inst.repo_ids:
                        if (line.provider_id == review["provider"]
                                and line.repo_path == review["path"]
                                and line.branch != review["branch"]):
                            line.write({"branch": review["branch"], "commit_sha": False})
                    self.env["cloud.job"].enqueue(inst.host_id.id, inst.id, job_type)
                    Note._enqueue(inst)
            except Exception:
                _logger.exception("Failed to enqueue MR rebuild for %s", inst.name)
                continue
            _logger.info(
                "MR preview rebuild triggered for %s (!%s)", inst.name, review["iid"],
            )

    def _close_mr_previews(self, review):
        """Delete the merge request's previews, and take their note off.

        Each one is marked closed before its note's job is queued, so
        the job deletes the note whatever ran first; a preview on its
        host is deleted by a job, one that never got there at once.

        :param dict review: what :meth:`_process_merge_request` read
        """
        Note = self.env["cloud.gitlab.mr.note"]
        for inst in self._mr_previews(review):
            name = inst.name
            try:
                with self.env.cr.savepoint():
                    inst.write({"pr_state": "closed"})
                    Note._enqueue(inst)
                    if inst.deployed and inst.host_id:
                        self.env["cloud.job"].enqueue(
                            inst.host_id.id, inst.id, "delete_instance",
                        )
                    else:
                        inst.unlink()
            except Exception:
                _logger.exception("Failed to delete MR preview %s", name)
                continue
            _logger.info(
                "MR preview instance %s queued for deletion (!%s closed)",
                name, review["iid"],
            )

    def _report_mr_preview_failure(self, prod, review, reason):
        """Say where people look that a preview was not created.

        An alert on the production for whoever runs the panel, and a
        note on the merge request for whoever opened it, by the note's
        job.

        :param prod: the production ``cloud.instance`` to be cloned
        :param dict review: what :meth:`_process_merge_request` read
        :param str reason: text safe to publish — a ``UserError``
            message, or a generic line carrying a log reference
        """
        reason = " ".join((reason or "").split())
        self.env["cloud.alert"].sudo().raise_alert(
            PR_PREVIEW_FAILED_CODE,
            _(
                "Merge request !%(number)s on %(repo)s asked for a preview "
                "of %(instance)s and none could be created: %(reason)s",
                number=review["iid"], repo=review["path"],
                instance=prod.name, reason=reason,
            ),
            level="warning",
            instance=prod,
        )
        self.env["cloud.gitlab.mr.note"]._enqueue_for(
            review["provider"].id, review["path"], review["iid"],
            prod.project_id.id, kind="failed", reason=reason, prod_id=prod.id,
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
