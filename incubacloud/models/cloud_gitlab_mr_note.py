"""The panel's note on a merge request, written by a job of its own.

GitLab cuts a delivery at 10 s and counts an error towards disabling the
webhook, so the receiver never calls GitLab: it queues this job in the
transaction that opens, updates or closes a preview, and the restore
that brings a preview up queues it in the one that marks it ready. If
that transaction does not commit, the job does not exist either.

The job reconciles instead of obeying. When it runs it reads what is
true then and makes the note match:

- the preview open and deployed: the «ready» note, with its address,
  or, while its restore could not update its modules
  (``pr_update_error``), the note saying so;
- no preview, and the job was queued to say why none was created (a
  failure, a merge request from a fork): that note;
- otherwise (the merge request closed, the preview gone): no note.

Before writing, it asks GitLab whether the merge request is still open.
An open and a close delivered at once leave the close without a preview
to close (it was not committed yet), and this job is all that runs after
both: a merge request closed or merged meanwhile has its open previews
closed and their delete queued, and loses its note instead. A read that
fails, or says no state, counts as open, as before: logged, with no
retry and no alert.

So jobs queued twice collapse into one (``identity_key``) without a
deletion being hidden behind a pending update: whichever runs decides
from the truth of that moment. The close queues a job of its own (the
key ends ``:closed``): collapsed into one waiting for its retry, it
would run with that one's arguments. A failure's or a fork's job for a
merge request closed after the delivery that queued it deletes instead.
A note is found again by its id, else by the hidden marker in its body
(``<!-- incubacloud:preview:<panel>:<project>:<iid> -->``, the panel
being a hash of the database's uuid, so two panels on one merge request
never take each other's note) among the token user's notes, and is
rewritten, never written twice.

The job is also the last guard of a closed preview: one still active
whose delete is not queued nor running has it queued here.

GitLab unreachable, or answering 5xx or 429: retried after 60, 300 and
900 s. Then, or at once on an answer that will not change (401, 403,
400, 422), the alert ``pr_preview_note_failed`` says so; the preview
itself works without its note. A note that could not be deleted, and a
repository with no credential with ``api`` (``no_notes`` in its
``repo_capabilities``), only go to the log: an alert nobody can resolve
helps no one.
"""
import hashlib
import logging
import time

from odoo import _, api, models
from odoo.exceptions import UserError

from odoo.addons.queue_job.exception import RetryableJobError

from ..gitlab.client import NOTES_PER_PAGE, GitLabClient, GitLabError
from ._concurrency import is_concurrency_error

_logger = logging.getLogger(__name__)

PR_PREVIEW_NOTE_FAILED_CODE = "pr_preview_note_failed"

#: Seconds before each retry of a note GitLab did not take: a short
#: outage of GitLab leaves no preview without its note; more adds nothing.
RETRY_SECONDS = (60, 300, 900)

#: Errors worth another try: GitLab or the way to it may come back. An
#: ``http`` error only without a status or with a 5xx: a 400 or a 422
#: says the same on every try.
_RETRYABLE = frozenset({"unreachable", "timeout", "budget", "tls", "rate_limited"})

#: What the job can be asked to say when no preview exists; ``closed``
#: is the close's job, which deletes the note.
NOTE_KINDS = ("preview", "failed", "fork", "closed")

#: Seconds one run of the job may spend on GitLab, all calls together
#: (10-nov-2026): the notes' pages and a write, at 20 s each at most,
#: would otherwise hold a queue_job worker for minutes on a slow server.
NOTE_BUDGET_S = 60

#: Pages of a merge request's notes read at most looking for the
#: panel's (100 each, oldest first): a merge request with more than a
#: thousand notes has its note rewritten by id, and written again only
#: when GitLab lost that id.
MAX_NOTE_PAGES = 10


class CloudGitLabMrNote(models.AbstractModel):
    _name = "cloud.gitlab.mr.note"
    _description = "Note of a merge request preview"

    # ── Queueing ───────────────────────────────────────────────────────

    @api.model
    def _panel_tag(self):
        """Return what tells this panel's notes from another panel's.

        The first 12 hex characters of the SHA-256 of the database's
        uuid: stable for the panel, and saying nothing about it.

        :rtype: str
        """
        db_uuid = self.env["ir.config_parameter"].sudo().get_param("database.uuid") or ""
        return hashlib.sha256(db_uuid.encode()).hexdigest()[:12]

    @api.model
    def _marker(self, project_id, iid):
        """Return the hidden marker a note of the panel carries.

        :param int project_id: the panel's project of the preview (0 for
            the note on a merge request from a fork)
        :param int iid: the merge request's number
        :rtype: str
        """
        return f"<!-- incubacloud:preview:{self._panel_tag()}:{project_id or 0}:{iid} -->"

    @api.model
    def _enqueue(self, inst, closed=False):
        """Queue the reconciliation of a preview's note.

        :param inst: the preview ``cloud.instance``
        :param bool closed: queued by the close, as a job of its own
        :return: the queued job, or None when the instance is no preview
            of a merge request
        """
        if not (inst.pr_number and inst.pr_provider_id):
            return None
        return self._enqueue_for(
            inst.pr_provider_id.id, inst.pr_repo, inst.pr_number,
            inst.project_id.id, comment_id=inst.pr_comment_id or 0,
            kind="closed" if closed else "preview",
        )

    @api.model
    def _enqueue_for(self, provider_id, path, iid, project_id, comment_id=0,
                     kind="preview", reason="", prod_id=0, event_id=0):
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
        :param int event_id: the ``cloud.gitlab.event`` that queued a
            failure's or a fork's note
        :return: the queued job (or the pending one it collapsed into)
        """
        identity = f"gitlab-mr-note:{provider_id}:{path}:{iid}:{project_id or 0}"
        if kind == "closed":
            identity += ":closed"
        return self.sudo().with_delay(
            max_retries=len(RETRY_SECONDS) + 1,
            channel="root.bg",
            description=f"Note of merge request !{iid} of {path}",
            identity_key=identity,
        )._job_reconcile(
            provider_id, path, iid, project_id or 0, comment_id=comment_id or 0,
            kind=kind, reason=reason or "", prod_id=prod_id or 0,
            event_id=event_id or 0,
        )

    # ── The job ────────────────────────────────────────────────────────

    def _job_reconcile(self, provider_id, path, iid, project_id, comment_id=0,
                       kind="preview", reason="", prod_id=0, event_id=0):
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
        known = comment_id or next(
            (i.pr_comment_id for i in previews if i.pr_comment_id), 0,
        )
        self._remove_closed(previews)
        previews = previews.exists()
        live = previews.filtered(lambda i: i.active and i.pr_state != "closed")[:1]
        if live and live.state != "deployed":
            # Up later: the restore that brings it up queues this again.
            return
        prod = Instance.browse(prod_id).exists() if prod_id else Instance.browse()
        if kind in ("failed", "fork") and self.env["cloud.gitlab.event"]._closed_after(
            provider.id, path, iid,
            self.env["cloud.gitlab.event"].sudo().browse(event_id).exists(),
        ):
            kind = "closed"
        writing = bool(live) or kind in ("failed", "fork")
        try:
            cred = provider._credential_for(path, "api_write")
        except UserError as exc:
            # ``no_notes`` in the repository's capabilities: no alert for
            # each merge request, and without a credential nothing can be
            # deleted.
            _logger.info(
                "The note of merge request !%s of %s is not %s: %s",
                iid, path, "written" if writing else "deleted", exc,
            )
            return
        marker = self._marker(project_id, iid)
        try:
            client = self._client(provider, cred)
            bot = cred.token_user or ""
            if writing and self._merge_request_closed(client, path, iid):
                self._close_previews(previews)
                previews = previews.exists()
                live, kind, writing = live.browse(), "closed", False
            if live:
                note_id = self._upsert(
                    client, bot, path, iid, marker, known,
                    live._pr_preview_body(),
                )
                if note_id and live.pr_comment_id != note_id:
                    live.write({"pr_comment_id": note_id})
            elif writing:
                self._upsert(
                    client, bot, path, iid, marker, known,
                    self._message_body(kind, reason, prod) + "\n\n" + marker,
                )
            else:
                self._delete(client, bot, path, iid, marker, known)
                previews.filtered("pr_comment_id").write({"pr_comment_id": 0})
        except GitLabError as exc:
            if self._retryable(exc) and not self._last_attempt():
                raise RetryableJobError(
                    f"GitLab did not take the note of !{iid} of {path}: {exc.code}",
                    seconds=self._retry_seconds(),
                ) from exc
            reason = _("GitLab answered %(code)s.", code=exc.status or exc.code)
            if not writing:
                _logger.warning(
                    "The note of merge request !%s of %s was not deleted: %s",
                    iid, path, reason,
                )
                return
            self._report(provider, path, iid, prod or live, reason)
            return
        except UserError as exc:
            if not writing:
                _logger.warning(
                    "The note of merge request !%s of %s was not deleted: %s",
                    iid, path, exc,
                )
                return
            self._report(provider, path, iid, prod or live, str(exc))
            return
        self._resolve(provider, path, iid, prod | previews)

    @api.model
    def _close_previews(self, previews):
        """Close the merge request's open previews and queue their delete.

        Each one is marked closed before its delete's savepoint
        (:meth:`_remove_closed`): a delete that cannot be queued leaves
        it known to be closed, and a close delivered meanwhile waits on
        the same row instead of queueing a second delete.

        :param previews: the merge request's ``cloud.instance`` records,
            with sudo
        """
        previews.filtered(
            lambda i: i.active and i.pr_state != "closed",
        ).write({"pr_state": "closed"})
        self._remove_closed(previews)

    @api.model
    def _remove_closed(self, previews):
        """Queue the delete of each closed preview still active that has none.

        The close queues it; this catches one whose delete was cancelled
        or never queued. A failure is logged: the note goes on.

        :param previews: the merge request's ``cloud.instance`` records
        """
        for inst in previews.filtered(lambda i: i.active and i.pr_state == "closed"):
            try:
                with self.env.cr.savepoint():
                    inst._remove_closed_preview()
            except Exception as exc:
                if is_concurrency_error(exc):
                    raise
                _logger.exception("The closed preview %s was not deleted", inst.name)

    # ── GitLab ─────────────────────────────────────────────────────────

    @api.model
    def _client(self, provider, cred):
        """Return an API client of *cred*, with the run's budget.

        :param provider: the ``cloud.git.provider``
        :param cred: the ``cloud.git.credential`` with ``api``
        :rtype: GitLabClient
        :raises UserError: when the network policy refuses the server
        """
        return GitLabClient(
            provider.api_url, cred.token, ca_pem=provider.ca_bundle or None,
            pin_ip=provider._resolve_pin(),
            deadline=time.monotonic() + NOTE_BUDGET_S,
        )

    @api.model
    def _merge_request_closed(self, client, path, iid):
        """Return whether GitLab says the merge request is closed or merged.

        ``locked`` (a merge being made) counts as open. A read that
        fails, or an answer without its ``state``, counts as open too: it
        is logged and costs no retry nor alert, and
        ``cloud.gitlab.event._closed_after`` is still there for a
        failure's or a fork's note.

        :param GitLabClient client: the run's client
        :param str path: the project's path
        :param int iid: the merge request's number
        :rtype: bool
        """
        try:
            merge_request = client.get_merge_request(path, iid)
        except (GitLabError, UserError) as exc:
            _logger.info(
                "Merge request !%s of %s was not read before its note: %s",
                iid, path, getattr(exc, "code", None) or exc,
            )
            return False
        state = merge_request.get("state") if isinstance(merge_request, dict) else None
        if not state:
            _logger.info(
                "Merge request !%s of %s was read without its state before its note",
                iid, path,
            )
            return False
        return state in ("closed", "merged")

    @staticmethod
    def _retryable(exc):
        """Return whether GitLab may take the note on a later try.

        :param GitLabError exc: what the call raised
        :rtype: bool
        """
        if exc.code == "http":
            return exc.status is None or exc.status >= 500
        return exc.code in _RETRYABLE

    @api.model
    def _find(self, client, user, path, iid, marker):
        """Return the id of the panel's note carrying *marker*, or 0.

        Only a note of the token's user counts when the panel knows it:
        anybody can paste the marker in a note of their own. The pages
        are read in order, up to :data:`MAX_NOTE_PAGES`.

        :rtype: int
        """
        for page in range(1, MAX_NOTE_PAGES + 1):
            notes = client.list_mr_notes(path, iid, page=page) or []
            for note in notes:
                if not isinstance(note, dict) or note.get("system"):
                    continue
                author = (note.get("author") or {}).get("username") or ""
                if marker in (note.get("body") or "") and (not user or author == user):
                    return note.get("id") or 0
            if len(notes) < NOTES_PER_PAGE:
                break
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
        def one_line(text):
            """Return *text* on one line, without backticks.

            A line break could start a line with ``/``, a quick action of
            GitLab, and a backtick close the code span.
            """
            return " ".join((text or "").replace("`", "").split())

        if kind == "fork":
            return (
                "ℹ️ **IncubaCloud Preview** — not created\n\n"
                "Previews are only created for branches of this project's "
                "own repository, not for merge requests from a fork: a "
                "preview runs the branch with a copy of production's data."
            )
        reason = one_line(reason)
        project = one_line(prod.project_id.name if prod else "")
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
    def _request_key(self, provider, path, iid):
        """Return the key of the alert about a merge request with no instance.

        :rtype: str
        """
        return f"gitlab_mr:{provider.id}:{path}:{iid}"

    @api.model
    def _alert_target(self, provider, path, iid, target):
        """Return the keywords that name the alert of one merge request's note.

        On the instance when there is one (the production a failure is
        about, else the preview), else on the merge request.

        :rtype: dict
        """
        if target:
            return {"instance": target}
        return {"key": self._request_key(provider, path, iid)}

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
    def _resolve(self, provider, path, iid, instances):
        """Resolve ``pr_preview_note_failed`` once a note went through.

        Wherever it may have gone: on the production a failure was
        about, on the previews and on the merge request itself.

        :param instances: the ``cloud.instance`` records of the merge request
        """
        Alert = self.env["cloud.alert"].sudo()
        for inst in instances:
            Alert.resolve_alert(PR_PREVIEW_NOTE_FAILED_CODE, instance=inst)
        self._resolve_request(provider, path, iid)

    @api.model
    def _resolve_request(self, provider, path, iid):
        """Resolve the alert about the note of a merge request with no instance.

        The close does it too: a closed merge request gets no note.
        """
        self.env["cloud.alert"].sudo().resolve_alert(
            PR_PREVIEW_NOTE_FAILED_CODE, key=self._request_key(provider, path, iid),
        )
