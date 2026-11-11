"""A push after a rebuild that failed is not held by that failure.

A failed rebuild holds the instance's queue until it is retried or its
alert dismissed (``cloud.job._queue_held_by``). With auto-rebuild, the
push that fixes what the failed one broke queued a rebuild that waited
for someone to dismiss the alert: the customer pushed the fix and nothing
happened. The rebuild of a push carrying a commit the failed rebuilds
were not asked for passes the hold now (``cloud.job._supersedes_hold``,
``RebuildInstanceExecutor._supersedes_failure``); its success resolves
the alert, its failure refreshes it. Everything else still holds.
"""
from ..models.rebuild_instance_executor import RebuildInstanceExecutor
from .test_cloud_job import _run_after_commit
from .test_rebuild_push_merge import _PushCase


class TestAPushAfterAFailedRebuild(_PushCase):

    def _end_as_the_runner(self, job, state, exc_message="boom"):
        """End a dispatched *job* in *state* the way the runner does."""
        job.queue_job_id.write({"state": "started"})
        vals = {"state": state}
        if state == "failed":
            vals["exc_message"] = exc_message
        job.queue_job_id.write(vals)
        _run_after_commit(self.env)

    def _failed_push(self, sha="bad0001"):
        """A push of *sha* whose rebuild failed; return it and its alert."""
        rebuild = self._push(sha)[-1:]
        self.assertEqual(rebuild.state, "pending")
        self._end_as_the_runner(rebuild, "failed")
        self.assertEqual(rebuild.state, "failed")
        alert = self._job_failed_alerts()
        self.assertEqual(alert.job_id, rebuild)
        return rebuild, alert

    def _job_failed_alerts(self):
        """The instance's active ``job_failed`` alerts."""
        return self.env["cloud.alert"].search([
            ("instance_id", "=", self.instance.id),
            ("code", "=", "job_failed"),
            ("state", "=", "active"),
        ])

    def _manual_rebuild(self):
        """A rebuild asked for by hand, through ``enqueue``."""
        return self.Job.browse(self.Job.enqueue(
            self.host.id, self.instance.id, "rebuild_instance",
        ))

    def test_the_push_of_the_fix_rebuilds_at_once(self):
        failed, alert = self._failed_push()
        fix = self._push("fix0002") - failed
        self.assertEqual(fix.state, "pending")
        self.assertTrue(self.Job._queue_held_by(self.instance.id, None))
        self.assertEqual(alert.state, "active", "only its success resolves it")

    def test_waiting_behind_a_running_job_it_is_not_called_held(self):
        failed, _alert = self._failed_push()
        running = self._running_rebuild()
        fix = self._push("fix0002") - failed - running
        self.assertEqual(fix.state, "queued")
        row = fix._format()[0]
        self.assertFalse(row["queue_held"])
        self.assertFalse(row["requires_action"])
        running.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(fix.state, "pending")

    def test_its_success_resolves_the_failure(self):
        failed, alert = self._failed_push()
        fix = self._push("fix0002") - failed
        self._end_as_the_runner(fix, "done")
        self.assertEqual(alert.state, "dismissed")
        self.assertEqual(alert.resolution, "auto")
        self.assertEqual(alert.resolved_by_job_id, fix)
        self.assertFalse(self._job_failed_alerts())
        self.assertFalse(self.Job._queue_held_by(self.instance.id, None))

    def test_its_failure_leaves_one_alert_on_the_new_failure(self):
        failed, alert = self._failed_push()
        fix = self._push("fix0002") - failed
        self._end_as_the_runner(fix, "failed")
        self.assertEqual(fix.state, "failed")
        self.assertEqual(self._job_failed_alerts(), alert)
        self.assertEqual(alert.job_id, fix)
        self.assertEqual(alert.occurrences, 2)
        # And the next push, another commit, passes this one's hold.
        again = self._push("fix0003") - failed - fix
        self.assertEqual(again.state, "pending")

    def test_the_same_push_delivered_again_stays_held(self):
        failed, alert = self._failed_push("bad0001")
        same = self._push("bad0001") - failed
        self.assertEqual(same.state, "queued")
        row = same._format()[0]
        self.assertTrue(row["queue_held"])
        self.assertTrue(row["requires_action"])
        self.assertEqual(row["queue_held_alert_id"], alert.id)
        self.assertFalse(self.Job._cron_dispatch_queued())
        # A new commit merged into it makes it a new attempt: the sweep
        # starts it.
        self.assertEqual(self._push("fix0002") - failed, same)
        self.assertEqual(self.Job._cron_dispatch_queued(), 1)
        self.env.invalidate_all()
        self.assertEqual(same.state, "pending")

    def test_a_push_after_a_failed_manual_rebuild_rebuilds(self):
        manual = self._manual_rebuild()
        self.assertEqual(manual.state, "pending")
        self._end_as_the_runner(manual, "failed")
        alert = self._job_failed_alerts()
        self.assertEqual(alert.job_id, manual)
        fix = self._push("fix0002") - manual
        self.assertEqual(fix.state, "pending")
        self._end_as_the_runner(fix, "done")
        self.assertEqual(alert.resolved_by_job_id, fix)

    def test_a_manual_rebuild_after_a_failure_stays_held(self):
        """A person who wants the branch rebuilt as it is retries the failure."""
        failed, alert = self._failed_push()
        manual = self._manual_rebuild()
        self.assertEqual(manual.state, "queued")
        row = manual._format()[0]
        self.assertTrue(row["queue_held"])
        self.assertEqual(row["queue_held_alert_id"], alert.id)
        retry = self.Job.browse(failed.retry_job())
        _run_after_commit(self.env)
        self.assertEqual(retry.state, "pending")
        self.assertEqual(manual.state, "queued")

    def _waiting_push(self, sha="fix0002"):
        """A push's rebuild waiting in the queue, created by hand.

        ``enqueue`` would not leave it there with a conflict open: it
        waits for the conflict instead (``blocked_alert_id``), or
        dismisses a ``pip_conflict`` the instance no longer has.
        """
        return self.Job.create({
            "name": "rebuild",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.rebuild_type.id,
            "state": "queued",
            "payload": {"trigger": "webhook", "push_sha": sha},
        })

    def _holding_jobs(self):
        """The failed jobs whose alerts hold the instance's queue."""
        return self.Job._queue_holding_alerts(
            self.instance.id, None,
        ).mapped("job_id")

    def test_an_addon_conflict_keeps_holding(self):
        failed = self._push("bad0001")
        failed.queue_job_id.write({"state": "started"})
        self.env["cloud.alert"].sudo().raise_alert(
            "addon_conflict", "two repositories define web_x",
            level="critical", instance=self.instance, job=failed,
        )
        failed.queue_job_id.write({"state": "failed", "exc_message": "boom"})
        _run_after_commit(self.env)
        self.assertFalse(self._job_failed_alerts())
        self.assertEqual(self._holding_jobs(), failed)
        fix = self._waiting_push()
        self.assertFalse(self.Job._passes_hold(fix, failed))
        self.assertTrue(fix._format()[0]["queue_held"])
        self.assertFalse(self.Job._cron_dispatch_queued())

    def test_a_pip_conflict_beside_the_failure_keeps_holding(self):
        failed, _alert = self._failed_push()
        self.env["cloud.alert"].sudo().raise_alert(
            "pip_conflict", "two pins of requests",
            instance=self.instance, job=failed,
        )
        fix = self._waiting_push()
        self.assertFalse(self.Job._passes_hold(fix, failed))
        self.assertTrue(fix._format()[0]["queue_held"])
        self.assertFalse(self.Job._cron_dispatch_queued())
        self.assertEqual(fix.state, "queued")

    def test_a_failed_deploy_keeps_holding(self):
        deploy = self.Job.create({
            "name": "deploy",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "deploy_instance")], limit=1,
            ).id,
            "state": "failed",
        })
        self.Job._create_job_failed_alert(deploy, exc_message="boom")
        fix = self._push("fix0002")
        self.assertEqual(fix.state, "queued")
        self.assertTrue(fix._format()[0]["queue_held"])

    def test_a_failed_rebuild_of_a_chain_keeps_holding(self):
        """A chain's step that failed left an operation half done."""
        failed, _alert = self._failed_push()
        failed.queue_chain_key = "chain-of-something"
        running = self._running_rebuild()
        fix = self._push("fix0002") - failed - running
        self.assertTrue(fix._format()[0]["queue_held"])

    def test_the_commits_a_payload_names(self):
        shas = RebuildInstanceExecutor._pushed_shas
        self.assertEqual(shas({}), set())
        self.assertEqual(shas({"push_sha": "aaa1111"}), set(), "no trigger")
        self.assertEqual(
            shas({"trigger": "webhook", "push_sha": "aaa1111"}), {"aaa1111"},
        )
        self.assertEqual(shas({
            "trigger": "coalesced", "push_sha": "aaa1111",
            "coalesced_pushes": [
                {"push_sha": "aaa1111"}, {"push_sha": "bbb2222"},
                {"push_sha": ""},
            ],
        }), {"aaa1111", "bbb2222"})


class TestAPushAfterAPreviewUpdateFailed(_PushCase):
    """The push of a fix rebuilds a preview whose restore failed in its update.

    A preview's restore is the last step of its clone's chain. When the
    update of its modules to the request's code fails, the restore's
    ``job_failed`` holds the preview's queue, and a failed step of a chain
    holds against everything: the push with the fix waited for someone to
    dismiss the alert. The database is loaded and neutralized, and a
    rebuild is what updates it; any push's rebuild passes that hold now,
    and its success resolves it.
    """

    _end_as_the_runner = TestAPushAfterAFailedRebuild._end_as_the_runner
    _manual_rebuild = TestAPushAfterAFailedRebuild._manual_rebuild

    def setUp(self):
        """Make the instance a pull request preview."""
        super().setUp()
        self.instance.write({
            "pr_number": 7, "pr_state": "open",
            "pr_repo": self.REPO_URL.split("github.com/")[1].removesuffix(".git"),
            "pr_head_branch": self.BRANCH, "pr_comment_id": 31,
        })

    def _failed_restore(self, update_failed=True):
        """The preview's restore, a step of a chain, failed with its alert.

        :param bool update_failed: whether the update was what failed
        """
        restore = self.Job.create({
            "name": "restore",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "restore_instance")], limit=1,
            ).id,
            "state": "failed",
            "queue_chain_key": "clone-chain",
        })
        if update_failed:
            self.instance.pr_update_error = "Updating its modules failed."
        alert = self.Job._create_job_failed_alert(restore, exc_message="boom")
        return restore, alert

    def test_the_push_of_the_fix_passes_the_restore_s_hold(self):
        restore, alert = self._failed_restore()
        fix = self._push("fix0002")
        self.assertEqual(fix.state, "pending")
        self.assertEqual(alert.state, "active", "only its success resolves it")
        self.assertTrue(restore._is_failed_preview_update())

    def test_its_success_resolves_the_restore_s_failure(self):
        _restore, alert = self._failed_restore()
        fix = self._push("fix0002")
        self._end_as_the_runner(fix, "done")
        self.assertEqual(fix.state, "done")
        self.instance._preview_rebuilt(fix)
        self.assertEqual(alert.state, "dismissed")
        self.assertEqual(alert.resolved_by_job_id, fix)
        self.assertFalse(self.instance.pr_update_error)
        self.assertFalse(self.Job._queue_held_by(self.instance.id, None))

    def test_another_failure_of_the_restore_keeps_holding(self):
        self._failed_restore(update_failed=False)
        fix = self._push("fix0002")
        self.assertEqual(fix.state, "queued")
        self.assertTrue(fix._format()[0]["queue_held"])

    def test_a_manual_rebuild_still_waits(self):
        _restore, alert = self._failed_restore()
        manual = self._manual_rebuild()
        self.assertEqual(manual.state, "queued")
        self.assertEqual(manual._format()[0]["queue_held_alert_id"], alert.id)

    def test_a_restore_that_is_no_preview_s_keeps_holding(self):
        self._failed_restore()
        self.instance.pr_number = 0
        fix = self._push("fix0002")
        self.assertEqual(fix.state, "queued")
