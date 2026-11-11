"""Merge request previews on GitLab: who gets one, and the note that says so.

A ``Merge Request Hook`` delivered by the panel's webhook opens, rebuilds
and closes previews as a GitHub pull request does: only from a branch of
the project's own repository, never from a fork, and only from a
production whose own repository lines follow it. Its note on the merge
request is written by a job queued in the delivery's transaction, which
reconciles the note with what is true when it runs.
"""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from psycopg2 import errors as pg_errors

from odoo.exceptions import UserError

from odoo.addons.queue_job.exception import RetryableJobError

from odoo.addons.incubacloud.gitlab.client import GitLabClient, GitLabError
from odoo.addons.incubacloud.models import cloud_gitlab_mr_note
from odoo.addons.incubacloud.models.cloud_github_event import PR_PREVIEW_FAILED_CODE
from odoo.addons.incubacloud.models.cloud_gitlab_mr_note import (
    PR_PREVIEW_NOTE_FAILED_CODE,
)
from odoo.addons.incubacloud.models.restore_instance_executor import (
    RestoreInstanceExecutor,
)

from ._gitlab_hooks import AFTER, BEFORE, GitLabHookCase

PROJECT_ID = 101
FORK_ID = 202


class _LostRace(pg_errors.SerializationFailure):
    """A serialization failure as PostgreSQL raises it, with its code."""

    pgcode = "40001"


def mr_body(action="open", iid=12, branch="feature-x", path="acme/web",
            source=PROJECT_ID, target=PROJECT_ID, oldrev=None, updated_at=None):
    """Return a GitLab «Merge Request Hook» body.

    :param int source: the project the branch comes from
    :param int target: the project the merge request is opened on
    :param str updated_at: the merge request's ``updated_at``, or None
    :rtype: dict
    """
    attrs = {
        "iid": iid, "action": action, "source_branch": branch,
        "target_branch": "main", "source_project_id": source,
        "target_project_id": target,
        "last_commit": {"id": AFTER, "title": "feat: x"},
    }
    if oldrev:
        attrs["oldrev"] = oldrev
    if updated_at:
        attrs["updated_at"] = updated_at
    return {
        "object_kind": "merge_request",
        "user": {"username": "dev", "email": "dev@example.com"},
        "project": {"id": target, "path_with_namespace": path},
        "object_attributes": attrs,
    }


class _MergeRequestCase(GitLabHookCase):
    """A deployed production of acme/web, in a project with previews on."""

    @classmethod
    def setUpClass(cls):
        """Add the production, its GitLab line and the project's switch."""
        super().setUpClass()
        cls.project.pr_reviews_enabled = True
        cls.prod = cls.env["cloud.instance"].create({
            "name": "gl-prod", "project_id": cls.project.id,
            "host_id": cls.host.id, "environment": "production",
            "state": "deployed",
        })
        cls.prod_line = cls.env["cloud.instance.repo"].create({
            "instance_id": cls.prod.id,
            "url": f"https://gitlab.com/{cls.PATH}.git", "branch": "main",
        })

    def _mr(self, key="mr-1", hook=None, **body):
        """Record and dispatch a merge request delivery.

        :rtype: cloud.gitlab.event
        """
        event = self._event(mr_body(**body), "Merge Request Hook", key=key, hook=hook)
        event._dispatch_safely()
        return event

    def _jobs(self, inst, code):
        """Return the panel's jobs of *code* on *inst*.

        :rtype: cloud.job
        """
        return self.env["cloud.job"].sudo().search([
            ("instance_id", "=", inst.id), ("job_type_id.code", "=", code),
        ])

    def _clone_patch(self, **kwargs):
        """Patch ``clone_to_staging`` on the model class."""
        return patch.object(
            type(self.env["cloud.instance"]), "clone_to_staging", **kwargs,
        )

    def _note_jobs(self):
        """Return the queued note jobs.

        :rtype: queue.job
        """
        return self.env["queue.job"].sudo().search([
            ("model_name", "=", "cloud.gitlab.mr.note"),
            ("method_name", "=", "_job_reconcile"),
        ])

    def _preview(self, iid=12, state="deployed", **vals):
        """Create a preview of merge request *iid*, as a clone leaves it.

        :rtype: cloud.instance
        """
        return self.env["cloud.instance"].sudo().create({
            "name": f"mr-{iid}", "project_id": self.project.id,
            "host_id": self.host.id, "environment": "staging",
            "state": state, "pr_number": iid, "pr_repo": self.PATH,
            "pr_head_branch": "feature-x", "pr_provider_id": self.provider.id,
            "pr_state": "open",
        } | vals)


class TestOpen(_MergeRequestCase):

    def test_open_clones_a_preview_of_the_production(self):
        with self._clone_patch(return_value={}) as clone:
            event = self._mr()
        self.assertTrue(event.processed)
        self.assertFalse(event.error)
        clone.assert_called_once()
        self.assertEqual(clone.call_args.args, ("mr-12",))
        self.assertEqual(clone.call_args.kwargs, {
            "pr_number": 12, "pr_repo": "acme/web",
            "pr_head_branch": "feature-x", "pr_provider_id": self.provider.id,
        })

    def test_reopen_clones_too_and_an_existing_preview_is_kept(self):
        with self._clone_patch(return_value={}) as clone:
            self._mr(action="reopen")
        clone.assert_called_once()
        self._preview()
        with self._clone_patch(return_value={}) as clone:
            self._mr(action="reopen", key="mr-2")
        clone.assert_not_called()

    def test_a_merge_request_from_a_fork_gets_no_preview_and_a_note(self):
        with self._clone_patch(return_value={}) as clone:
            event = self._mr(source=FORK_ID)
        self.assertTrue(event.processed)
        clone.assert_not_called()
        job = self._note_jobs()
        self.assertEqual(len(job), 1)
        self.assertEqual(job.kwargs["kind"], "fork")

    def test_a_merge_request_naming_no_source_counts_as_from_a_fork(self):
        with self._clone_patch(return_value={}) as clone:
            self._mr(source=0)
        clone.assert_not_called()

    def test_a_fork_to_a_repository_nothing_previews_gets_no_note(self):
        self.project.pr_reviews_enabled = False
        with self._clone_patch(return_value={}) as clone:
            self._mr(source=FORK_ID)
        clone.assert_not_called()
        self.assertFalse(self._note_jobs())

    def test_only_a_production_s_own_unpinned_line_decides(self):
        """The project's lines are a template; a pinned line does not move."""
        self.prod_line.unlink()
        self.env["cloud.project.repo"].create({
            "project_id": self.project.id,
            "url": f"https://gitlab.com/{self.PATH}.git", "branch": "main",
        })
        with self._clone_patch(return_value={}) as clone:
            self._mr()
        clone.assert_not_called()
        line = self.env["cloud.instance.repo"].create({
            "instance_id": self.prod.id,
            "url": f"https://gitlab.com/{self.PATH}.git", "branch": "main",
            "commit_sha": "b" * 40,
        })
        with self._clone_patch(return_value={}) as clone:
            self._mr(key="mr-2")
        clone.assert_not_called()
        line.commit_sha = False
        with self._clone_patch(return_value={}) as clone:
            self._mr(key="mr-3")
        clone.assert_called_once()

    def test_the_project_switch_and_the_layer_s_veto_are_respected(self):
        self.project.pr_reviews_enabled = False
        with self._clone_patch(return_value={}) as clone:
            self._mr()
        clone.assert_not_called()
        self.project.pr_reviews_enabled = True
        with patch.object(
            type(self.Event), "_pr_preview_allowed", return_value=False,
        ), self._clone_patch(return_value={}) as clone:
            self._mr(key="mr-2")
        clone.assert_not_called()

    def test_a_repository_the_webhook_does_not_deliver_does_nothing(self):
        with self._clone_patch(return_value={}) as clone:
            event = self._mr(path="other/web")
        self.assertTrue(event.processed)
        clone.assert_not_called()

    def test_an_unsafe_branch_does_nothing(self):
        with self._clone_patch(return_value={}) as clone:
            self._mr(branch="main;rm -rf")
        clone.assert_not_called()

    def test_a_failure_is_an_alert_and_a_note(self):
        with self._clone_patch(side_effect=UserError("No room on the host.")):
            event = self._mr()
        self.assertTrue(event.processed)
        alert = self.env["cloud.alert"].search([
            ("code", "=", PR_PREVIEW_FAILED_CODE),
            ("instance_id", "=", self.prod.id), ("state", "=", "active"),
        ])
        self.assertIn("!12", alert.message)
        self.assertIn("No room on the host.", alert.message)
        job = self._note_jobs()
        self.assertEqual(job.kwargs["kind"], "failed")
        self.assertEqual(job.kwargs["reason"], "No room on the host.")
        self.assertEqual(job.kwargs["prod_id"], self.prod.id)

    def test_a_clone_follows_the_merge_request_s_branch(self):
        """The real clone: its GitLab line takes the source branch."""
        with patch.object(
            type(self.env["cloud.job"]), "enqueue_chain", return_value=[1, 2, 3],
        ):
            self._mr()
        staging = self.env["cloud.instance"].search([
            ("pr_provider_id", "=", self.provider.id), ("pr_number", "=", 12),
        ])
        self.assertEqual(staging.name, "mr-12")
        self.assertEqual(staging.pr_repo, "acme/web")
        self.assertEqual(staging.pr_state, "open")
        line = staging.repo_ids.filtered(lambda r: r.repo_path == self.PATH)
        self.assertEqual(line.branch, "feature-x")
        self.assertFalse(line.commit_sha)
        body = staging._pr_preview_ready_body()
        self.assertIn("merge request", body)
        self.assertIn(
            self.env["cloud.gitlab.mr.note"]._marker(self.project.id, 12), body,
        )

    def test_two_deliveries_at_once_leave_one_preview_and_no_alert(self):
        """The loser of the unique name finds the winner's preview and says nothing."""
        self.registry_enter_test_mode()
        self._preview(state="deploying")
        Event = type(self.Event)
        with patch.object(
            Event, "_mr_previews", return_value=self.env["cloud.instance"],
        ), patch.object(
            Event, "_mr_preview_name", return_value="mr-12", create=True,
        ), patch.object(
            type(self.env["cloud.job"]), "enqueue_chain", return_value=[1, 2, 3],
        ):
            event = self._mr(key="mr-twin")
        self.assertTrue(event.processed)
        self.assertFalse(event.error)
        self.assertFalse(self.env["cloud.alert"].search([
            ("code", "=", PR_PREVIEW_FAILED_CODE), ("state", "=", "active"),
        ]))
        self.assertFalse(self._note_jobs())

    def test_a_unique_violation_without_a_preview_is_still_a_failure(self):
        with self._clone_patch(side_effect=pg_errors.UniqueViolation("dup")):
            event = self._mr()
        self.assertTrue(event.processed)
        alert = self.env["cloud.alert"].search([
            ("code", "=", PR_PREVIEW_FAILED_CODE), ("state", "=", "active"),
        ])
        self.assertIn("internal error", alert.message)

    def test_a_group_s_delivery_is_dropped_for_a_project_with_its_own_webhook(self):
        group = self.Hook.create({
            "provider_id": self.provider.id, "scope": "group", "path": "acme",
            "state": "active",
        })
        self.hook.state = "active"
        with self._clone_patch(return_value={}) as clone:
            event = self._mr(key="mr-group", hook=group)
        self.assertTrue(event.processed)
        clone.assert_not_called()
        self.hook.state = "pending"
        with self._clone_patch(return_value={}) as clone:
            self._mr(key="mr-group-2", hook=group)
        clone.assert_called_once()

    def test_an_open_dispatched_after_the_close_brings_nothing_back(self):
        """The cron's second try of an open, or a resend, after the merge request closed."""
        opening = self._event(
            mr_body(updated_at="2026-11-10 10:00:00 UTC"), "Merge Request Hook", key="o1",
        )
        self._mr(action="close", key="c1", updated_at="2026-11-10 10:05:00 UTC")
        with self._clone_patch(return_value={}) as clone:
            opening._dispatch_safely()
        clone.assert_not_called()
        self.assertTrue(opening.processed)
        with self._clone_patch(return_value={}) as clone:
            self._mr(action="reopen", key="r1", updated_at="2026-11-10 10:09:00 UTC")
        clone.assert_called_once()

    def test_closed_and_reopened_at_once_it_gets_a_new_preview(self):
        """The closed preview still being deleted keeps its name; the new one takes another."""
        old = self._preview(state="deploying", pr_state="closed")
        with self._clone_patch(return_value={}) as clone:
            self._mr(action="reopen")
        clone.assert_called_once()
        self.assertEqual(clone.call_args.args, ("mr-12-2",))
        self.assertTrue(old.exists())


class TestUpdateAndClose(_MergeRequestCase):

    def _rebuild_jobs(self, inst):
        """Return the rebuild jobs of *inst*."""
        return self._rebuilds(inst)

    def test_new_commits_rebuild_the_preview_and_reconcile_its_note(self):
        preview = self._preview()
        self.env["cloud.instance.repo"].create({
            "instance_id": preview.id,
            "url": f"https://gitlab.com/{self.PATH}.git", "branch": "old",
        })
        self._mr(action="update", oldrev=BEFORE)
        self.assertEqual(len(self._rebuild_jobs(preview)), 1)
        self.assertEqual(preview.repo_ids.branch, "feature-x")
        self.assertEqual(len(self._note_jobs()), 1)

    def test_an_update_without_commits_and_approvals_do_nothing(self):
        preview = self._preview()
        for i, action in enumerate(("update", "approved", "unapproved")):
            with self.subTest(action=action):
                event = self._mr(action=action, key=f"u{i}")
                self.assertTrue(event.processed)
        self.assertFalse(self._rebuild_jobs(preview))
        self.assertFalse(self._note_jobs())

    def test_close_and_merge_delete_the_preview_and_its_note(self):
        for i, action in enumerate(("close", "merge")):
            with self.subTest(action=action):
                preview = self._preview(iid=20 + i, pr_comment_id=77)
                self._mr(action=action, iid=20 + i, key=f"c{i}")
                self.assertEqual(preview.pr_state, "closed")
                deletes = self.env["cloud.job"].search([
                    ("instance_id", "=", preview.id),
                    ("job_type_id.code", "=", "delete_instance"),
                ])
                self.assertEqual(len(deletes), 1)
        jobs = self._note_jobs()
        self.assertEqual(len(jobs), 2)
        self.assertEqual(set(jobs.mapped(lambda j: j.kwargs["comment_id"])), {77})

    def test_a_preview_never_deployed_goes_at_once(self):
        preview = self._preview(state="draft")
        self._mr(action="close")
        self.assertFalse(preview.exists())
        self.assertEqual(len(self._note_jobs()), 1)

    def test_a_close_while_the_clone_deploys_deletes_it_once_its_chain_ends(self):
        """The real clone's chain: the delete waits, what waits in the queue is cancelled.

        The chain's dump and restore wait on queue_job's dependencies, not
        in the preview's queue, and the chain holds the preview until its
        last step: the delete runs after the restore, which neither
        announces the preview nor queues a second delete.
        """
        self._mr()
        staging = self.env["cloud.instance"].search([
            ("pr_provider_id", "=", self.provider.id), ("pr_number", "=", 12),
        ])
        self.assertEqual(staging.state, "deploying")
        deploy = self._jobs(staging, "deploy_instance")
        restore = self._jobs(staging, "restore_instance")
        dump = self._jobs(self.prod, "backup_download")
        self.assertTrue(deploy and restore and dump)
        waiting = self.env["cloud.job"].sudo().create({
            "name": "restart", "host_id": self.host.id, "instance_id": staging.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "restart_instance")], limit=1,
            ).id,
        })
        self.env.cr.execute(
            "UPDATE cloud_job SET state = 'started' WHERE id = %s", (deploy.id,),
        )
        self.env.cr.execute(
            "UPDATE cloud_job SET state = 'queued' WHERE id = %s", (waiting.id,),
        )
        self.env.invalidate_all()
        event = self._mr(action="close", key="mr-close")
        self.assertTrue(event.processed)
        self.assertFalse(event.error)
        self.assertTrue(staging.exists())
        self.assertEqual(staging.pr_state, "closed")
        self.assertEqual(staging.state, "deploying")
        delete = self._jobs(staging, "delete_instance")
        self.assertEqual(len(delete), 1)
        self.assertEqual(delete.state, "queued", "it waits for the chain")
        self.assertEqual(deploy.state, "started")
        self.assertEqual(waiting.state, "cancelled")
        self.assertNotEqual(restore.state, "cancelled")
        self.assertNotEqual(dump.state, "cancelled")
        closing = self._note_jobs().filtered(lambda j: j.kwargs["kind"] == "closed")
        self.assertEqual(len(closing), 1)
        # The deploy ends; the restore comes up on a closed preview.
        self.env.cr.execute(
            "UPDATE cloud_job SET state = 'done' WHERE id = %s", (deploy.id,),
        )
        self.env.invalidate_all()
        staging._transition("deployed")
        executor = RestoreInstanceExecutor(restore, self.host)
        with patch.object(
            RestoreInstanceExecutor, "_cleanup_cross_host_handoff", new=AsyncMock(),
        ):
            asyncio.run(executor.on_success({}))
        self.assertEqual(self._jobs(staging, "delete_instance"), delete)
        self.assertEqual(self._note_jobs(), closing)

    def test_a_delete_that_cannot_be_queued_fails_the_event(self):
        """The event is left in error for the cron, and what the close did stays.

        Not undone with the failed delete: a restore running meanwhile
        would see the preview open and announce it on a closed merge
        request. The retry deletes what was left.
        """
        self._preview()
        self._preview(name="mr-12-b")
        previews = self.env["cloud.instance"].sudo().search([
            ("pr_provider_id", "=", self.provider.id), ("pr_number", "=", 12),
        ])
        self.assertEqual(len(previews), 2)
        failing, queued = previews[-1], previews[:-1]
        Job = type(self.env["cloud.job"])
        enqueue = Job.enqueue

        def refuse_one(job, host_id, instance_id, job_type_code, *args, **kwargs):
            if instance_id == failing.id and job_type_code == "delete_instance":
                raise UserError("The queue is full.")
            return enqueue(job, host_id, instance_id, job_type_code, *args, **kwargs)

        with patch.object(Job, "enqueue", autospec=True, side_effect=refuse_one):
            event = self._mr(action="close")
        self.assertFalse(event.processed)
        self.assertIn("The queue is full.", event.error)
        self.assertEqual(event.dispatch_attempts, 1)
        self.assertEqual(set(previews.mapped("pr_state")), {"closed"})
        self.assertEqual(len(self._note_jobs().filtered(
            lambda j: j.kwargs["kind"] == "closed",
        )), 1, "one job of the note per merge request")
        for preview in queued:
            self.assertEqual(len(self._jobs(preview, "delete_instance")), 1)
        self.assertFalse(self._jobs(failing, "delete_instance"))
        event._dispatch_safely()
        self.assertTrue(event.processed)
        self.assertFalse(event.error)
        for preview in previews:
            self.assertEqual(len(self._jobs(preview, "delete_instance")), 1)

    def test_a_lost_race_reaches_odoo_s_retry(self):
        self._preview()
        event = self._event(mr_body(action="close"), "Merge Request Hook", key="race")
        with patch.object(
            type(self.env["cloud.job"]), "enqueue", side_effect=_LostRace("lost"),
        ), self.assertRaises(pg_errors.SerializationFailure):
            event._dispatch_safely()

    def test_a_rebuild_that_cannot_be_queued_keeps_the_others(self):
        """The event keeps its error; the rebuild queued stays, and the
        cron's second try queues only the one that failed: the other
        collapses into its rebuild still waiting."""
        self._preview()
        self._preview(name="mr-12-b")
        previews = self.env["cloud.instance"].sudo().search([
            ("pr_provider_id", "=", self.provider.id), ("pr_number", "=", 12),
        ])
        self.assertEqual(len(previews), 2)
        failing, queued = previews[-1], previews[:-1]
        # A job running on the preview: its rebuild waits behind it.
        running = self.env["cloud.job"].sudo().create({
            "name": "restart", "host_id": self.host.id, "instance_id": queued.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "restart_instance")], limit=1,
            ).id,
        })
        self.env.cr.execute(
            "UPDATE cloud_job SET state = 'started' WHERE id = %s", (running.id,),
        )
        self.env.invalidate_all()
        Job = type(self.env["cloud.job"])
        enqueue = Job.enqueue

        def refuse_one(job, host_id, instance_id, job_type_code, *args, **kwargs):
            if instance_id == failing.id:
                raise UserError("The queue is full.")
            return enqueue(job, host_id, instance_id, job_type_code, *args, **kwargs)

        with patch.object(Job, "enqueue", autospec=True, side_effect=refuse_one):
            event = self._mr(action="update", oldrev=BEFORE)
        self.assertFalse(event.processed)
        self.assertIn("The queue is full.", event.error)
        self.assertEqual(len(self._rebuild_jobs(queued)), 1)
        self.assertEqual(self._rebuild_jobs(queued).state, "queued")
        self.assertFalse(self._rebuild_jobs(failing))
        event._dispatch_safely()
        self.assertTrue(event.processed)
        self.assertFalse(event.error)
        for preview in previews:
            self.assertEqual(len(self._rebuild_jobs(preview)), 1)

    def test_a_rebuild_that_cannot_be_queued_fails_the_event(self):
        self._preview()
        with patch.object(
            type(self.env["cloud.job"]), "enqueue",
            side_effect=UserError("The queue is full."),
        ):
            event = self._mr(action="update", oldrev=BEFORE)
        self.assertFalse(event.processed)
        self.assertIn("The queue is full.", event.error)

    def test_the_close_has_a_job_of_its_own(self):
        """Not collapsed into a waiting one, whose arguments it would run with."""
        preview = self._preview()
        self.env["cloud.gitlab.mr.note"]._enqueue(preview)
        self._mr(action="close")
        self.assertEqual(
            sorted(self._note_jobs().mapped(lambda j: j.kwargs["kind"])),
            ["closed", "preview"],
        )

    def test_the_close_resolves_the_alert_of_the_merge_request_s_note(self):
        Note = self.env["cloud.gitlab.mr.note"]
        self.env["cloud.alert"].sudo().raise_alert(
            PR_PREVIEW_NOTE_FAILED_CODE, "x", level="warning",
            key=Note._request_key(self.provider, self.PATH, 12),
        )
        self._mr(action="close")
        self.assertFalse(self.env["cloud.alert"].search([
            ("code", "=", PR_PREVIEW_NOTE_FAILED_CODE), ("state", "=", "active"),
        ]))

    def test_a_github_pull_request_and_a_merge_request_of_one_number_stay_apart(self):
        """GitHub's events never reach a GitLab preview of the same name and number."""
        preview = self._preview(iid=5, pr_repo="acme/web")
        github = self.env["cloud.github.event"].create({
            "event_type": "pull_request", "delivery_id": "gh-5-closed",
            "payload": (
                '{"action": "closed", "installation": {"id": 1},'
                ' "pull_request": {"number": 5,'
                ' "base": {"repo": {"clone_url": "https://github.com/acme/web.git"}},'
                ' "head": {"ref": "feature-x",'
                ' "repo": {"clone_url": "https://github.com/acme/web.git"}}},'
                ' "repository": {"full_name": "acme/web"}}'
            ),
        })
        with patch.object(
            type(self.env["cloud.github.event"]), "_is_own_pull_request",
            return_value=True,
        ):
            github._process_pull_request_event()
        self.assertEqual(preview.pr_state, "open")
        self.assertFalse(self.env["cloud.job"].search([
            ("instance_id", "=", preview.id),
        ]))


class TestNoteJob(_MergeRequestCase):
    """``cloud.gitlab.mr.note._job_reconcile`` against a mocked GitLab."""

    def setUp(self):
        """A credential with api acting as ``panel-bot``, and a mocked client."""
        super().setUp()
        self.cred = self.env["cloud.git.credential"].create({
            "provider_id": self.provider.id, "namespace": "acme",
            "kind": "group", "token": "glpat-routes12",
        })
        self.cred.sudo().write({"scopes": "api", "token_user": "panel-bot"})
        self.api = MagicMock(spec=GitLabClient)
        self.api.create_mr_note.return_value = {"id": 501}
        self.api.list_mr_notes.return_value = []
        self.api.get_merge_request.return_value = {"iid": 12, "state": "opened"}
        patcher = patch.object(
            cloud_gitlab_mr_note, "GitLabClient", autospec=True, return_value=self.api,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.Note = self.env["cloud.gitlab.mr.note"]

    def _run(self, iid=12, project_id=None, **kwargs):
        """Run the job as queue_job would, outside a queue (one attempt)."""
        return self.Note._job_reconcile(
            self.provider.id, self.PATH, iid,
            self.project.id if project_id is None else project_id, **kwargs,
        )

    def _marker(self, iid=12):
        """Return the marker of the project's note on *iid*."""
        return self.Note._marker(self.project.id, iid)

    def _note_alert(self):
        """Return the open alerts about notes."""
        return self.env["cloud.alert"].search([
            ("code", "=", PR_PREVIEW_NOTE_FAILED_CODE), ("state", "=", "active"),
        ])

    def test_a_ready_preview_gets_its_note_once(self):
        preview = self._preview()
        self._run()
        self.api.create_mr_note.assert_called_once()
        path, iid, body = self.api.create_mr_note.call_args.args
        self.assertEqual((path, iid), ("acme/web", 12))
        self.assertIn("IncubaCloud Preview", body)
        self.assertIn(self._marker(), body)
        self.assertEqual(preview.pr_comment_id, 501)
        self._run()
        self.api.create_mr_note.assert_called_once()
        self.api.update_mr_note.assert_called_once_with("acme/web", 12, 501, body)

    def test_a_note_gitlab_lost_is_found_by_its_marker_among_the_bot_s(self):
        preview = self._preview(pr_comment_id=400)
        self.api.update_mr_note.side_effect = [GitLabError("not_found", 404), {}]
        self.api.list_mr_notes.return_value = [
            {"id": 9, "system": True, "body": self._marker(), "author": {"username": "panel-bot"}},
            {"id": 10, "body": self._marker(), "author": {"username": "someone"}},
            {"id": 11, "body": "x " + self._marker(), "author": {"username": "panel-bot"}},
        ]
        self._run()
        self.api.create_mr_note.assert_not_called()
        self.assertEqual(self.api.update_mr_note.call_args.args[2], 11)
        self.assertEqual(preview.pr_comment_id, 11)

    def test_a_preview_not_up_yet_waits_for_its_restore(self):
        self._preview(state="deploying")
        self._run()
        self.api.create_mr_note.assert_not_called()
        self.api.list_mr_notes.assert_not_called()

    def test_a_closed_merge_request_loses_its_note(self):
        preview = self._preview(pr_comment_id=77, pr_state="closed")
        self._run()
        self.api.delete_mr_note.assert_called_once_with("acme/web", 12, 77)
        self.assertEqual(preview.pr_comment_id, 0)
        self.api.get_merge_request.assert_not_called()

    def test_a_merge_request_merged_meanwhile_loses_its_preview_and_its_note(self):
        """The open and the close crossed: the close found no preview to close."""
        preview = self._preview(pr_comment_id=77)
        self.api.get_merge_request.return_value = {"iid": 12, "state": "merged"}
        self._run()
        self.api.get_merge_request.assert_called_once_with("acme/web", 12)
        self.api.create_mr_note.assert_not_called()
        self.api.update_mr_note.assert_not_called()
        self.api.delete_mr_note.assert_called_once_with("acme/web", 12, 77)
        self.assertEqual(preview.pr_state, "closed")
        self.assertEqual(preview.pr_comment_id, 0)
        self.assertEqual(len(self._jobs(preview, "delete_instance")), 1)

    def test_a_failure_s_note_on_a_closed_merge_request_is_deleted_not_written(self):
        """Its open event may be gone: GitLab says the merge request is closed."""
        self.api.get_merge_request.return_value = {"iid": 40, "state": "closed"}
        self._run(iid=40, kind="failed", reason="No room.", prod_id=self.prod.id,
                  comment_id=91)
        self.api.create_mr_note.assert_not_called()
        self.api.update_mr_note.assert_not_called()
        self.api.delete_mr_note.assert_called_once_with("acme/web", 40, 91)
        self.assertFalse(self._note_alert())

    def test_a_merge_request_that_cannot_be_read_gets_its_note(self):
        """A read that fails, or says nothing usable, costs no retry and no alert."""
        preview = self._preview()
        for answer in (GitLabError("unreachable"), GitLabError("forbidden", 403),
                       [], {"iid": 12}, {"iid": 12, "state": "locked"}):
            with self.subTest(answer=answer):
                self.api.reset_mock()
                if isinstance(answer, Exception):
                    self.api.get_merge_request.side_effect = answer
                else:
                    self.api.get_merge_request.side_effect = None
                    self.api.get_merge_request.return_value = answer
                with patch.object(type(self.Note), "_last_attempt", return_value=False):
                    self._run()
                self.api.get_merge_request.assert_called_once()
                self.assertTrue(
                    self.api.create_mr_note.called or self.api.update_mr_note.called,
                )
                self.assertEqual(preview.pr_state, "open")
                self.assertFalse(self._note_alert())

    def test_a_deleted_preview_s_note_goes_by_its_id_or_its_marker(self):
        self._run(iid=30, comment_id=88)
        self.api.delete_mr_note.assert_called_once_with("acme/web", 30, 88)
        self.api.delete_mr_note.reset_mock()
        self.api.list_mr_notes.return_value = [
            {"id": 12, "body": self._marker(31), "author": {"username": "panel-bot"}},
        ]
        self._run(iid=31)
        self.api.delete_mr_note.assert_called_once_with("acme/web", 31, 12)

    def test_a_note_already_gone_is_fine(self):
        self.api.delete_mr_note.side_effect = GitLabError("not_found", 404)
        self._run(iid=30, comment_id=88)
        self.assertFalse(self._note_alert())

    def test_a_note_that_cannot_be_deleted_only_goes_to_the_log(self):
        """Nothing would ever resolve the alert of a merge request that is closed."""
        self._preview(pr_comment_id=77, pr_state="closed")
        self.api.delete_mr_note.side_effect = GitLabError("forbidden", 403)
        with self.assertLogs(cloud_gitlab_mr_note._logger.name, "WARNING"):
            self._run()
        self.assertFalse(self._note_alert())

    def test_a_closed_preview_with_no_delete_gets_one(self):
        """The note's job is the last guard of a closed preview."""
        preview = self._preview(pr_state="closed")
        self._run()
        self.assertEqual(len(self._jobs(preview, "delete_instance")), 1)
        self._run()
        self.assertEqual(len(self._jobs(preview, "delete_instance")), 1)

    def test_a_failure_and_a_fork_say_why_there_is_no_preview(self):
        self._run(iid=40, kind="failed", reason="No room.", prod_id=self.prod.id)
        body = self.api.create_mr_note.call_args.args[2]
        self.assertIn("could not be created", body)
        self.assertIn("No room.", body)
        self.assertIn(self._marker(40), body)
        self._run(iid=41, project_id=0, kind="fork")
        body = self.api.create_mr_note.call_args.args[2]
        self.assertIn("not for merge requests from a fork", body)
        self.assertIn(self.Note._marker(0, 41), body)

    def test_the_marker_names_the_panel(self):
        """Two panels on one merge request never take each other's note."""
        tag = self.Note._panel_tag()
        self.assertRegex(tag, r"^[0-9a-f]{12}$")
        self.assertEqual(
            self._marker(), f"<!-- incubacloud:preview:{tag}:{self.project.id}:12 -->",
        )

    def test_the_project_s_name_cannot_start_a_quick_action(self):
        self.project.name = "Acme `x`  web"
        self._run(iid=40, kind="failed", reason="No\n/merge `room`.", prod_id=self.prod.id)
        body = self.api.create_mr_note.call_args.args[2]
        self.assertIn("| **Project** | `Acme x web` |", body)
        self.assertIn("| **Reason** | No /merge room. |", body)
        self.assertFalse([line for line in body.splitlines() if line.startswith("/")])

    def test_the_notes_are_read_page_after_page(self):
        self._preview(pr_comment_id=0)
        other = [{"id": n, "body": "x", "author": {"username": "panel-bot"}}
                 for n in range(100)]
        self.api.list_mr_notes.side_effect = [
            other, [{"id": 700, "body": self._marker(), "author": {"username": "panel-bot"}}],
        ]
        self._run()
        self.api.create_mr_note.assert_not_called()
        self.assertEqual(self.api.update_mr_note.call_args.args[2], 700)
        self.assertEqual(
            [c.kwargs.get("page") for c in self.api.list_mr_notes.call_args_list], [1, 2],
        )

    def test_the_client_has_a_budget(self):
        self._preview()
        self._run()
        self.assertIsNotNone(cloud_gitlab_mr_note.GitLabClient.call_args.kwargs["deadline"])

    def test_a_failure_s_note_after_the_close_is_deleted_not_written(self):
        """Waiting for its retry when the merge request closed."""
        opening = self._event(mr_body(), "Merge Request Hook", key="o1")
        closing = self._event(mr_body(action="close"), "Merge Request Hook", key="c1")
        closing.sudo().write({"processed": True})
        self._run(iid=12, kind="failed", reason="No room.", prod_id=self.prod.id,
                  comment_id=91, event_id=opening.id)
        self.api.create_mr_note.assert_not_called()
        self.api.update_mr_note.assert_not_called()
        self.api.delete_mr_note.assert_called_once_with("acme/web", 12, 91)

    def test_gitlab_down_is_retried_and_the_last_attempt_alerts(self):
        preview = self._preview()
        self.api.create_mr_note.side_effect = GitLabError("unreachable")
        with patch.object(type(self.Note), "_last_attempt", return_value=False), \
                self.assertRaises(RetryableJobError):
            self._run()
        self.assertFalse(self._note_alert())
        self._run()
        alert = self._note_alert()
        self.assertEqual(alert.instance_id, preview)
        self.assertIn("!12", alert.message)
        self.api.create_mr_note.side_effect = None
        self._run()
        self.assertFalse(self._note_alert(), "a note that went through resolves it")

    def test_a_refusal_alerts_at_once(self):
        self._preview()
        self.api.create_mr_note.side_effect = GitLabError("forbidden", 403)
        with patch.object(type(self.Note), "_last_attempt", return_value=False):
            self._run()
        self.assertIn("403", self._note_alert().message)

    def test_a_400_or_a_422_is_not_retried_and_a_5xx_is(self):
        self._preview()
        for status in (400, 422):
            with self.subTest(status=status):
                self._note_alert().write({"state": "dismissed"})
                self.api.create_mr_note.side_effect = GitLabError("http", status)
                with patch.object(type(self.Note), "_last_attempt", return_value=False):
                    self._run()
                self.assertIn(str(status), self._note_alert().message)
        self.api.create_mr_note.side_effect = GitLabError("http", 502)
        with patch.object(type(self.Note), "_last_attempt", return_value=False), \
                self.assertRaises(RetryableJobError):
            self._run()

    def test_no_credential_with_api_only_goes_to_the_log(self):
        """The repository shows no_notes: no alert for each merge request."""
        self.cred.sudo().write({"scopes": "read_repository"})
        self._preview()
        with patch.object(
            cloud_gitlab_mr_note, "_logger", MagicMock(spec=logging.Logger),
        ) as log:
            self._run()
        log.info.assert_called_once()
        self.assertFalse(self._note_alert())
        self.api.create_mr_note.assert_not_called()

    def test_a_note_that_goes_through_resolves_every_alert_of_the_merge_request(self):
        preview = self._preview()
        Alert = self.env["cloud.alert"].sudo()
        for target in ({"instance": self.prod}, {"instance": preview},
                       {"key": self.Note._request_key(self.provider, self.PATH, 12)}):
            Alert.raise_alert(PR_PREVIEW_NOTE_FAILED_CODE, "x", level="warning", **target)
        self.assertEqual(len(self._note_alert()), 3)
        self._run(prod_id=self.prod.id)
        self.assertFalse(self._note_alert())

    def test_the_retries_wait_longer_each_time(self):
        job = SimpleNamespace(retry=0, max_retries=4)
        with patch.object(type(self.Note), "_queue_job", return_value=job):
            waits = []
            for done in range(3):
                job.retry = done
                waits.append(self.Note._retry_seconds())
                self.assertFalse(self.Note._last_attempt())
            job.retry = 3
            self.assertTrue(self.Note._last_attempt())
        self.assertEqual(waits, [60, 300, 900])

    def test_queued_twice_it_is_one_job(self):
        preview = self._preview()
        self.Note._enqueue(preview)
        self.Note._enqueue(preview)
        self.assertEqual(len(self._note_jobs()), 1)


class TestRestoreQueuesTheNote(_MergeRequestCase):

    def test_the_restore_of_a_merge_request_preview_queues_its_note(self):
        preview = self._preview()
        job = self.env["cloud.job"].sudo().create({
            "name": "restore", "host_id": self.host.id,
            "instance_id": preview.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "restore_instance")], limit=1,
            ).id,
        })
        executor = RestoreInstanceExecutor(job, self.host)
        with patch.object(
            RestoreInstanceExecutor, "_cleanup_cross_host_handoff", new=AsyncMock(),
        ), patch.object(
            type(self.env["cloud.instance"]), "_post_or_update_pr_comment",
        ) as github:
            asyncio.run(executor.on_success({}))
        github.assert_not_called()
        self.assertEqual(len(self._note_jobs()), 1)

    def _restore(self, preview):
        """Run the restore's ``on_success`` on *preview*, its handoff left alone."""
        job = self.env["cloud.job"].sudo().create({
            "name": "restore", "host_id": self.host.id,
            "instance_id": preview.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "restore_instance")], limit=1,
            ).id,
        })
        executor = RestoreInstanceExecutor(job, self.host)
        with patch.object(
            RestoreInstanceExecutor, "_cleanup_cross_host_handoff", new=AsyncMock(),
        ):
            asyncio.run(executor.on_success({}))

    def test_the_restore_of_a_pull_request_preview_queues_its_comment(self):
        """GitHub's comment is a job too: written in the restore, a GitHub
        away for a moment left the pull request without it for good."""
        preview = self._preview(iid=71, pr_provider_id=False, pr_repo="acme/web")
        with patch.object(
            type(self.env["cloud.instance"]), "_post_or_update_pr_comment",
        ) as github:
            self._restore(preview)
        github.assert_not_called()
        self.assertEqual(preview.status, "ok")
        self.assertEqual(len(self.env["queue.job"].sudo().search([
            ("model_name", "=", "cloud.instance"),
            ("method_name", "=", "_job_pr_comment"),
        ])), 1)
        self.assertFalse(self._note_jobs())

    def test_a_note_that_cannot_be_queued_does_not_fail_the_restore(self):
        preview = self._preview()
        with patch.object(
            type(self.env["cloud.gitlab.mr.note"]), "_enqueue",
            side_effect=UserError("no"),
        ):
            self._restore(preview)
        self.assertEqual(preview.status, "ok")

    def test_the_restore_of_a_closed_preview_deletes_it(self):
        """Closed while it was built, and its delete cancelled: the restore queues it."""
        for provider in (self.provider, self.env["cloud.git.provider"]):
            with self.subTest(gitlab=bool(provider)):
                preview = self._preview(
                    iid=60 + len(provider), pr_state="closed",
                    pr_provider_id=provider.id,
                )
                with patch.object(
                    type(self.env["cloud.instance"]), "_post_or_update_pr_comment",
                ) as github:
                    self._restore(preview)
                github.assert_not_called()
                self.assertEqual(len(self._jobs(preview, "delete_instance")), 1)
        self.assertFalse(self._note_jobs())


class TestNoteOfAPreviewNotUpdated(_MergeRequestCase):
    """A merge request preview whose restore could not update its modules.

    The restore failed in «Update changed modules», Odoo stayed stopped,
    and the merge request heard nothing: its note was queued by the
    restore's success alone. The failure queues it now, and the note says
    the preview could not be updated, and why, until a restore or a
    rebuild of the preview succeeds.
    """

    REASON = "Updating its modules to this code failed (exit status 1)."

    # The mocked GitLab of ``TestNoteJob``, without its tests.
    _run = TestNoteJob._run
    _marker = TestNoteJob._marker

    def setUp(self):
        """A credential with api acting as ``panel-bot``, and a mocked client."""
        super().setUp()
        cred = self.env["cloud.git.credential"].create({
            "provider_id": self.provider.id, "namespace": "acme",
            "kind": "group", "token": "glpat-routes12",
        })
        cred.sudo().write({"scopes": "api", "token_user": "panel-bot"})
        self.api = MagicMock(spec=GitLabClient)
        self.api.create_mr_note.return_value = {"id": 501}
        self.api.list_mr_notes.return_value = []
        self.api.get_merge_request.return_value = {"iid": 12, "state": "opened"}
        patcher = patch.object(
            cloud_gitlab_mr_note, "GitLabClient", autospec=True, return_value=self.api,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.Note = self.env["cloud.gitlab.mr.note"]

    def test_the_note_says_it_could_not_be_updated_and_why(self):
        self._preview(pr_update_error=self.REASON)
        self._run()
        body = self.api.create_mr_note.call_args.args[2]
        self.assertIn("could not be updated", body)
        self.assertIn(self.REASON, body)
        self.assertIn("merge request", body)
        self.assertIn(self._marker(), body)

    def test_the_restore_s_failure_queues_it(self):
        preview = self._preview()
        job = self.env["cloud.job"].sudo().create({
            "name": "restore", "host_id": self.host.id,
            "instance_id": preview.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "restore_instance")], limit=1,
            ).id,
        })
        executor = RestoreInstanceExecutor(job, self.host)
        with patch.object(RestoreInstanceExecutor, "_sys", autospec=True):
            asyncio.run(executor.on_failure(
                {"Update changed modules": {"exit_status": 2, "stdout": ""}},
                ["'Update changed modules' exited with status 2"],
            ))
        self.assertEqual(preview.status, "error")
        self.assertIn("exit status 2", preview.pr_update_error)
        self.assertEqual(len(self._note_jobs()), 1)

    def test_a_restore_that_succeeds_clears_it(self):
        preview = self._preview(pr_update_error=self.REASON)
        job = self.env["cloud.job"].sudo().create({
            "name": "restore", "host_id": self.host.id,
            "instance_id": preview.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "restore_instance")], limit=1,
            ).id,
        })
        executor = RestoreInstanceExecutor(job, self.host)
        with patch.object(
            RestoreInstanceExecutor, "_cleanup_cross_host_handoff", new=AsyncMock(),
        ):
            asyncio.run(executor.on_success({}))
        self.assertFalse(preview.pr_update_error)
        self._run()
        self.assertIn("ready", self.api.create_mr_note.call_args.args[2])
