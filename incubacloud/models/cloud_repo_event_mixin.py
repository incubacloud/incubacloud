"""What the webhook events of GitHub and GitLab share.

``cloud.github.event`` and ``cloud.gitlab.event`` inherit from this
mixin, and modules layered on top override it here,
so their routing of rebuilds and their exclusion from previews hold for
both providers. The concrete models never redefine
:meth:`_rebuild_job_type` nor :meth:`_pr_preview_allowed`: doing so
would hide such an override.
"""
import logging

from odoo import SUPERUSER_ID, api, fields, models

from ._concurrency import read_committed_cursor
from .registry import executor_registry

_logger = logging.getLogger(__name__)

#: A job dispatched to queue_job and not ended yet.
_DISPATCHED_STATES = ("pending", "enqueued", "wait_dependencies", "started")


class CloudRepoEventMixin(models.AbstractModel):
    _name = "cloud.repo.event.mixin"
    _description = "Webhook event of a git provider"

    def _rebuild_job_type(self, inst):
        """Job code used by webhook-driven auto-rebuild.

        Default: the core ``rebuild_instance``. Modules layered on top
        override this to route some instances to an executor of their
        own, which carries what the core executor lacks.

        A subclass may return a falsy value to decline the auto-rebuild
        entirely; the webhook handler then skips the instance without
        enqueuing anything.
        """
        return 'rebuild_instance'

    def _pr_preview_allowed(self, inst):
        """Whether a pull request may spawn a preview of this instance.

        Default: yes — the project's own switch has already been checked
        by the time this is asked. Modules layered on top override this
        to keep instances they manage themselves out of the flow,
        whatever their repositories look like: a preview is a copy of
        the instance's production data, and not every production
        instance is somebody's to copy.

        :param inst: the production ``cloud.instance`` a preview would
            be cloned from
        :return: ``True`` to go ahead, falsy to skip it silently
        """
        return True

    def _free_preview_name(self, project, base):
        """Return the name of a new preview: *base*, or ``<base>-<n>``.

        The suffix only while another instance of the project holds the
        name — the preview of the same pull or merge request closed a
        moment ago, still being deleted: a name is unique in a project,
        and its directory on the host is built from it.

        :param project: the ``cloud.project`` of the preview
        :param str base: ``pr-<number>`` or ``mr-<iid>``
        :rtype: str
        """
        taken = set(self.env["cloud.instance"].sudo().with_context(
            active_test=False,
        ).search([
            ("project_id", "=", project.id), ("name", "=like", f"{base}%"),
        ]).mapped("name"))
        name, n = base, 1
        while name in taken:
            n += 1
            name = f"{base}-{n}"
        return name

    def _open_preview_committed(self, provider_id, repo, number, project):
        """Return whether an open preview of the request is committed in *project*.

        Read at READ COMMITTED: what a concurrent delivery committed
        while this one waited on the unique name is not in this
        transaction's snapshot.

        :param provider_id: the ``cloud.git.provider`` id of a GitLab
            merge request, ``False`` for a GitHub pull request
        :param str repo: the request's repository (``pr_repo``)
        :param int number: the request's number
        :param project: the ``cloud.project`` of the preview
        :rtype: bool
        """
        with read_committed_cursor(self.env.registry) as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            return bool(env["cloud.instance"].with_context(
                active_test=False,
            ).search_count([
                ("pr_provider_id", "=", provider_id),
                ("pr_repo", "=", repo),
                ("pr_number", "=", number),
                ("project_id", "=", project.id),
                ("pr_state", "!=", "closed"),
            ], limit=1))

    def _rebuild_for_push(self, lines, push_info):
        """Queue the rebuild of every instance a push reaches.

        For each repository line the push matched, the instance is
        rebuilt when it is active, follows pushes, is deployed and sits
        on an active host, and :meth:`_rebuild_job_type` names a job.
        Each enqueue runs in a savepoint of its own: one that fails
        (a database error included) is logged and skipped without
        aborting the transaction the others and the event are written
        in.

        :param lines: the ``cloud.instance.repo`` lines the push matched
        :param dict push_info: the ``push_*`` keys of the job's payload
        :return: the names of the instances whose rebuild was queued
        :rtype: list
        """
        triggered = []
        for repo in lines:
            inst = repo.instance_id
            # Never auto-rebuild an archived instance or one whose host is
            # deactivated/deleted: the repo match can surface archived
            # instances (e.g. recycled ones) and ``host_id`` may point at
            # an archived host, both of which would just fail SSH.
            if not (inst.active and inst.auto_rebuild and inst.deployed
                    and inst.host_id and inst.host_id.active):
                continue
            job_type = self._rebuild_job_type(inst)
            if not job_type:
                continue
            try:
                with self.env.cr.savepoint():
                    queued = self._queue_push_rebuild(inst, job_type, push_info)
            except Exception:
                _logger.exception(
                    "Failed to enqueue auto-rebuild for %s", inst.name,
                )
                continue
            if queued:
                triggered.append(inst.name)
        return triggered

    def _queue_push_rebuild(self, inst, job_type, push_info):
        """Queue the rebuild of *inst* a push asks for, unless one runs for it.

        A push queues its rebuild. One that arrives while a rebuild runs
        waits in the instance's queue; one that arrives while a push's
        rebuild waits merges into it
        (``RebuildInstanceExecutor._merge_queued_payload``), so no commit
        is built twice and none is dropped. One whose rebuild was already
        dispatched (:meth:`_push_being_built`) queues nothing: a
        preview's commit comes as a push and as the pull request's
        ``synchronize`` or the merge request's ``update``, which name the
        same commit. ``last_auto_rebuild`` is written when a rebuild is
        queued or merged into.

        No savepoint of its own: the caller runs it in one.

        :param inst: the ``cloud.instance`` to rebuild
        :param str job_type: the job code :meth:`_rebuild_job_type` named
        :param dict push_info: the ``push_*`` keys of the job's payload
        :return: whether a rebuild was queued or merged into
        :rtype: bool
        """
        if self._push_being_built(inst, job_type, push_info):
            _logger.info(
                "Rebuild of %s for %s@%s already dispatched: not queued again",
                inst.name, push_info.get('push_repo'), push_info.get('push_sha'),
            )
            return False
        self.env['cloud.job'].enqueue(
            inst.host_id.id, inst.id, job_type,
            payload={'trigger': 'webhook', **push_info},
        )
        inst.write({'last_auto_rebuild': fields.Datetime.now()})
        return True

    def _push_being_built(self, inst, job_type, push_info):
        """Return whether a dispatched rebuild of *inst* was asked for this push.

        Dispatched and not ended: it fetches its branches once it runs,
        after the push it names, so it builds that commit or a later one.
        The type's executor says whether a payload names the push
        (``_carries_push``); a type without it never does.

        :param inst: the ``cloud.instance``
        :param str job_type: the rebuild's job code
        :param dict push_info: the ``push_*`` keys of the push
        :rtype: bool
        """
        carries = getattr(executor_registry.get(job_type), '_carries_push', None)
        if not carries:
            return False
        jobs = self.env['cloud.job'].sudo().search([
            ('instance_id', '=', inst.id),
            ('job_type_id.code', '=', job_type),
            ('state', 'in', _DISPATCHED_STATES),
        ])
        return any(carries(job.payload, push_info) for job in jobs)
