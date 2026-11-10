"""The panel's note on a merge request, written by a job of its own.

GitLab cuts a delivery at 10 s and counts an error towards disabling the
webhook, so the receiver never calls GitLab: it queues this job in the
transaction that opens, updates or closes a preview, and the restore
that brings a preview up queues it in the one that marks it ready. If
that transaction does not commit, the job does not exist either.

The job reconciles instead of obeying. When it runs it reads what is
true then and makes the note match:

- the preview open and deployed: the «ready» note, with its address;
- no preview, and the job was queued to say why none was created (a
  failure, a merge request from a fork): that note;
- otherwise (the merge request closed, the preview gone): no note.

So jobs queued twice collapse into one (``identity_key``) without a
deletion being hidden behind a pending update: whichever runs decides
from the truth of that moment. A note is found again by its id, else by
the hidden marker in its body (``<!-- incubacloud:preview:<project>:<iid> -->``)
among the token user's notes, and is rewritten, never written twice.

GitLab unreachable, or answering 5xx or 429: retried after 60, 300 and
900 s. Then, or at once on an answer that will not change (no
credential with ``api``, 401, 403), the alert ``pr_preview_note_failed``
says so; the preview itself works without its note.
"""
import logging

from odoo import _, api, models
from odoo.exceptions import UserError

from odoo.addons.queue_job.exception import RetryableJobError

from ..gitlab.client import GitLabClient, GitLabError

_logger = logging.getLogger(__name__)

PR_PREVIEW_NOTE_FAILED_CODE = "pr_preview_note_failed"

#: Seconds before each retry of a note GitLab did not take: a short
#: outage of GitLab leaves no preview without its note; more adds nothing.
RETRY_SECONDS = (60, 300, 900)

#: Errors worth another try: GitLab or the way to it may come back.
_RETRYABLE = frozenset({"unreachable", "timeout", "budget", "tls", "rate_limited", "http"})

#: What the job can be asked to say when no preview exists.
NOTE_KINDS = ("preview", "failed", "fork")


class CloudGitLabMrNote(models.AbstractModel):
    _name = "cloud.gitlab.mr.note"
    _description = "Note of a merge request preview"

    # ── Queueing ───────────────────────────────────────────────────────

    @api.model
    def _marker(self, project_id, iid):
        """Return the hidden marker a note of the panel carries.

        :param int project_id: the panel's project of the preview (0 for
            the note on a merge request from a fork)
        :param int iid: the merge request's number
        :rtype: str
        """
        return f"<!-- incubacloud:preview:{project_id or 0}:{iid} -->"

    @api.model
    def _enqueue(self, inst):
        """Queue the reconciliation of a preview's note.

        :param inst: the preview ``cloud.instance``
        :return: the queued job, or None when the instance is no preview
            of a merge request
        """
        if not (inst.pr_number and inst.pr_provider_id):
            return None
        return self._enqueue_for(
            inst.pr_provider_id.id, inst.pr_repo, inst.pr_number,
            inst.project_id.id, comment_id=inst.pr_comment_id or 0,
        )

    @api.model
    def _enqueue_for(self, provider_id, path, iid, project_id, comment_id=0,
                     kind="preview", reason="", prod_id=0):
        """Queue the reconciliation of the note of one merge request.

        In the caller's transaction: nothing is queued if it rolls back.

        :param int provider_id: the ``cloud.git.provider``
        :param str path: the project's path in GitLab
        :param int iid: the merge request's number
        :param int project_id: the panel's project (0 for a fork's note)
        :param int comment_id: the note's id when known
        :param str kind: ``preview``, ``failed`` or ``fork``
            (:data:`NOTE_KINDS`)
        :param str reason: what a ``failed`` note says, safe to publish
        :param int prod_id: the production a failure is about, for its alert
        :return: the queued job (or the pending one it collapsed into)
        """
        return self.sudo().with_delay(
            max_retries=len(RETRY_SECONDS) + 1,
            channel="root.bg",
            description=f"Note of merge request !{iid} of {path}",
            identity_key=f"gitlab-mr-note:{provider_id}:{path}:{iid}:{project_id or 0}",
        )._job_reconcile(
            provider_id, path, iid, project_id or 0, comment_id=comment_id or 0,
            kind=kind, reason=reason or "", prod_id=prod_id or 0,
        )

    # ── The job ────────────────────────────────────────────────────────

    def _job_reconcile(self, provider_id, path, iid, project_id, comment_id=0,
                       kind="preview", reason="", prod_id=0):
        """Make the merge request's note say what is true now (queued).

        See the module's docstring. The arguments are those of
        :meth:`_enqueue_for`.

        :raises RetryableJobError: when GitLab may answer on a later try
        """
        provider = self.env["cloud.git.provider"].sudo().browse(provider_id).exists()
        if not provider or kind not in NOTE_KINDS:
            return
        Instance = self.env["cloud.instance"].sudo().with_context(active_test=False)
        previews = Instance.browse()
        if project_id:
            previews = Instance.search([
                ("pr_provider_id", "=", provider.id),
                ("pr_repo", "=", path),
                ("pr_number", "=", iid),
                ("project_id", "=", project_id),
            ])
        live = previews.filtered(lambda i: i.active and i.pr_state != "closed")[:1]
        known = comment_id or next(
            (i.pr_comment_id for i in previews if i.pr_comment_id), 0,
        )
        prod = Instance.browse(prod_id).exists() if prod_id else Instance.browse()
        target = prod or live
        marker = self._marker(project_id, iid)
        try:
            client, bot = self._client(provider, path)
            if live:
                if live.state != "deployed":
                    # Up later: the restore that brings it up queues this again.
                    return
                note_id = self._upsert(
                    client, bot, path, iid, marker, known,
                    live._pr_preview_ready_body(),
                )
                if note_id and live.pr_comment_id != note_id:
                    live.write({"pr_comment_id": note_id})
            elif kind in ("failed", "fork"):
                self._upsert(
                    client, bot, path, iid, marker, known,
                    self._message_body(kind, reason, prod) + "\n\n" + marker,
                )
            else:
                self._delete(client, bot, path, iid, marker, known)
                previews.filtered("pr_comment_id").write({"pr_comment_id": 0})
        except GitLabError as exc:
            if exc.code in _RETRYABLE and not self._last_attempt():
                raise RetryableJobError(
                    f"GitLab did not take the note of !{iid} of {path}: {exc.code}",
                    seconds=self._retry_seconds(),
                ) from exc
            self._report(provider, path, iid, target, _(
                "GitLab answered %(code)s.", code=exc.status or exc.code,
            ))
            return
        except UserError as exc:
            self._report(provider, path, iid, target, str(exc))
            return
        self._resolve(provider, path, iid, target)

    # ── GitLab ─────────────────────────────────────────────────────────

    @api.model
    def _client(self, provider, path):
        """Return an API client for *path* and the user name its token acts as.

        :rtype: tuple
        :raises UserError: when no credential has ``api``, or the server
            is refused by the network policy
        """
        cred = provider._credential_for(path, "api_write")
        client = GitLabClient(
            provider.api_url, cred.token, ca_pem=provider.ca_bundle or None,
            pin_ip=provider._resolve_pin(),
        )
        return client, cred.token_user or ""

    @api.model
    def _find(self, client, user, path, iid, marker):
        """Return the id of the panel's note carrying *marker*, or 0.

        Only a note of the token's user counts when the panel knows it:
        anybody can paste the marker in a note of their own.

        :rtype: int
        """
        for note in client.list_mr_notes(path, iid) or ():
            if not isinstance(note, dict) or note.get("system"):
                continue
            author = (note.get("author") or {}).get("username") or ""
            if marker in (note.get("body") or "") and (not user or author == user):
                return note.get("id") or 0
        return 0

    @api.model
    def _upsert(self, client, user, path, iid, marker, known, body):
        """Rewrite the panel's note with *body*, or write it; return its id.

        :param int known: the note's id when known; one GitLab no longer
            has is looked for by its marker
        :rtype: int
        """
        if known:
            try:
                client.update_mr_note(path, iid, known, body)
                return known
            except GitLabError as exc:
                if exc.code != "not_found":
                    raise
        found = self._find(client, user, path, iid, marker)
        if found:
            client.update_mr_note(path, iid, found, body)
            return found
        return (client.create_mr_note(path, iid, body) or {}).get("id") or 0

    @api.model
    def _delete(self, client, user, path, iid, marker, known):
        """Delete the panel's note; one already gone, or never written, is fine.

        :param int known: the note's id when known, else it is looked
            for by its marker
        """
        note_id = known or self._find(client, user, path, iid, marker)
        if not note_id:
            return
        try:
            client.delete_mr_note(path, iid, note_id)
        except GitLabError as exc:
            if exc.code != "not_found":
                raise

    @api.model
    def _message_body(self, kind, reason, prod):
        """Return the Markdown of a note that says why there is no preview.

        In English, as the pull request's comments on GitHub: the note is
        read by whoever opened the merge request, in GitLab.

        :param str kind: ``failed`` or ``fork``
        :param str reason: a failure's reason, safe to publish
        :param prod: the production that was to be cloned, or empty
        :rtype: str
        """
        if kind == "fork":
            return (
                "ℹ️ **IncubaCloud Preview** — not created\n\n"
                "Previews are only created for branches of this project's "
                "own repository, not for merge requests from a fork: a "
                "preview runs the branch with a copy of production's data."
            )
        reason = " ".join((reason or "").split())
        project = prod.project_id.name if prod else ""
        return (
            "⚠️ **IncubaCloud Preview** — could not be created\n\n"
            "| | |\n|---|---|\n"
            f"| **Project** | `{project}` |\n"
            f"| **Reason** | {reason} |\n\n"
            "_Close and reopen the merge request to try again._"
        )

    # ── Attempts and alerts ────────────────────────────────────────────

    def _queue_job(self):
        """Return the ``queue.job`` running this method, or an empty recordset.

        :rtype: queue.job
        """
        uuid = self.env.context.get("job_uuid")
        Job = self.env["queue.job"].sudo()
        return Job.search([("uuid", "=", uuid)], limit=1) if uuid else Job.browse()

    def _last_attempt(self):
        """Return whether this run is the job's last; True outside queue_job.

        queue_job counts the run before calling the method and stores the
        count after it: the stored ``retry`` is the number of runs before
        this one.

        :rtype: bool
        """
        job = self._queue_job()
        return not job or job.retry + 1 >= job.max_retries

    def _retry_seconds(self):
        """Return the seconds before the next run: 60, then 300, then 900.

        :rtype: int
        """
        job = self._queue_job()
        done = job.retry if job else 0
        return RETRY_SECONDS[min(done, len(RETRY_SECONDS) - 1)]

    @api.model
    def _alert_target(self, provider, path, iid, target):
        """Return the keywords that name the alert of one merge request's note.

        On the instance when there is one (the production a failure is
        about, else the preview), else on the merge request.

        :rtype: dict
        """
        if target:
            return {"instance": target}
        return {"key": f"gitlab_mr:{provider.id}:{path}:{iid}"}

    @api.model
    def _report(self, provider, path, iid, target, reason):
        """Raise ``pr_preview_note_failed``: the note could not be written.

        :param target: the instance the alert goes on, or empty
        :param str reason: what failed
        """
        _logger.warning(
            "The note of merge request !%s of %s was not written: %s", iid, path, reason,
        )
        self.env["cloud.alert"].sudo().raise_alert(
            PR_PREVIEW_NOTE_FAILED_CODE,
            _(
                "The panel could not write its note on merge request !%(iid)s "
                "of %(repo)s on %(server)s: %(reason)s The preview works without "
                "it; check the credential of the repository under Settings → "
                "Git providers (it needs the api scope).",
                iid=iid, repo=path, server=provider.authority, reason=reason,
            ),
            level="warning",
            **self._alert_target(provider, path, iid, target),
        )

    @api.model
    def _resolve(self, provider, path, iid, target):
        """Resolve ``pr_preview_note_failed`` once a note went through."""
        self.env["cloud.alert"].sudo().resolve_alert(
            PR_PREVIEW_NOTE_FAILED_CODE,
            **self._alert_target(provider, path, iid, target),
        )
