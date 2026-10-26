"""A deploy that cannot start waits; one that ends with no outcome fails.

aitrax, 16-oct-2026: the addon pre-flight found 9 modules two of the
customer's repositories defined and raised. The job failed, so the
customer got the conflict alert, then a ``job_failed`` for the same news,
and support@ a third mail from queue_job's chatter; ``on_failure`` never
ran, because it only follows steps that ran, and the instance stayed in
'deploying' — it could be neither deployed again nor deleted.

Now the pre-flight blocks the job (``blocking_alert``): it ends at once,
waits on the conflict alert, and resolving the alert dispatches it. A
step of a chain cannot wait — queue_job would run the next step — so it
fails instead, with the alert raised on a cursor of its own. Any other
exception that ends a job with neither ``on_success`` nor ``on_failure``
reaches ``on_abort``, which for a deploy is the failure path it always
should have been.
"""
import asyncio
import contextlib
import http.client
import inspect
import json
import pathlib
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from odoo.addons.queue_job.exception import JobError, RetryableJobError

from ..github.http_utils import HTTPBudget
from ..models.abstract_executor import (
    BLOCKED_BY_ALERT,
    AbstractExecutor,
    JobCancelled,
    JobRefused,
)
from ..models.cloud_host import CloudHost
from ..models.cloud_instance import ADDON_CHECK_SECONDS
from ..models.deploy_instance_executor import DeployInstanceExecutor
from ..models.move_cutover_executor import MoveCutoverExecutor
from ..models.rebuild_instance_executor import RebuildInstanceExecutor
from .test_cloud_job import _run_after_commit

_CONFLICTS = [
    {"addon": "web_sheet_full_width", "repos": ["odoo-addons", "web"]},
    {"addon": "mail_private", "repos": ["mail-addons", "social"]},
]
_DETECT = (
    "odoo.addons.incubacloud.models.deploy_instance_executor"
    ".detect_addon_conflicts"
)
_DETECT_ON_SAVE = (
    "odoo.addons.incubacloud.models.cloud_instance.detect_addon_conflicts"
)


class _DeployJobCase(TransactionCase):
    """A host, an instance with two repositories and its deploy job."""

    def setUp(self):
        """An instance in 'deploying' on two repositories, and its deploy job."""
        super().setUp()
        self.registry_enter_test_mode()
        self.host = self.env["cloud.host"].create({
            "name": "blocked-host",
            "ip_address": "10.0.0.71",
            "user": "ubuntu",
            "wildcard_domain": "blocked.example.com",
        })
        project = self.env["cloud.project"].create({"name": "Blocked"})
        self.inst = self.env["cloud.instance"].create({
            "name": "blocked-inst",
            "project_id": project.id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self.env["cloud.instance.repo"].with_context(
            skip_apply_requirements=True,
        ).create([
            {"instance_id": self.inst.id, "url": "https://github.com/OCA/web", "branch": "12.0"},
            {"instance_id": self.inst.id, "url": "https://github.com/acme/odoo-addons", "branch": "main"},
        ])
        self.inst._transition("deploying")
        self.job = self._deploy_job("uuid-deploy")
        # ``execute`` clears the environment and the hooks read on a
        # cursor of their own: what they see is what is in the database.
        self.env.flush_all()

    def _deploy_job(self, uuid):
        """Another dispatched deploy job of the instance, under *uuid*."""
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "deploy_instance")], limit=1,
        )
        qjob = self.env["queue.job"].sudo().create({
            "uuid": f"{uuid}-{self.inst.id}",
            "name": "deploy",
            "state": "started",
            "method_name": "execute",
            "model_name": "cloud.job",
            "func_string": "execute()",
        })
        return self.env["cloud.job"].sudo().create({
            "host_id": self.host.id,
            "instance_id": self.inst.id,
            "job_type_id": job_type.id,
            "name": "Deploy Instance",
            "queue_job_uuid": qjob.uuid,
            "queue_job_id": qjob.id,
        })

    def _conflict_alert(self):
        """The instance's active ``addon_conflict`` alert, if any."""
        return self.env["cloud.alert"].search([
            ("instance_id", "=", self.inst.id),
            ("code", "=", "addon_conflict"),
            ("state", "=", "active"),
        ])

    def _execute(self, conflicts, **patches):
        """Run ``execute`` with GitHub reporting *conflicts*.

        ``run`` and ``on_abort`` are mocked: the first would connect, and
        under tests the second's durable cursor is a savepoint the run's
        own can roll back (see ``TestAbortBeforeTheRun``).

        :return: what ``execute`` returned
        """
        with patch(_DETECT, autospec=True, return_value=conflicts), \
                patch.object(
                    DeployInstanceExecutor, "pre_run_checks", autospec=True,
                    **patches,
                ), \
                patch.object(
                    DeployInstanceExecutor, "run", autospec=True,
                ) as run, \
                patch.object(
                    DeployInstanceExecutor, "on_abort", autospec=True,
                ) as aborted:
            self.run, self.aborted = run, aborted
            return self.job.execute()


class TestPreflightBlocks(_DeployJobCase):
    """A deploy nothing runs after waits on its conflict alert."""

    def test_conflicts_end_the_job_blocked_on_one_alert(self):
        """The job ends at once, on one critical alert naming them all."""
        with self.assertNoLogs("odoo.addons.incubacloud", level="ERROR"):
            result = self._execute(_CONFLICTS)
        self.assertEqual(result, BLOCKED_BY_ALERT)
        self.run.assert_not_called()
        alert = self._conflict_alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "critical")
        self.assertEqual(alert.conflict_data, _CONFLICTS)
        self.assertEqual(alert.job_id, self.job)
        self.assertEqual(self.job.blocked_alert_id, alert)

    def test_a_waiting_deploy_leaves_the_instance_in_draft(self):
        """In 'deploying' it could be neither deployed nor deleted."""
        self._execute(_CONFLICTS)
        self.assertEqual(self.inst.state, "draft")

    def test_without_conflicts_the_job_runs(self):
        """No conflict, no wait."""
        result = self._execute([])
        self.assertNotEqual(result, BLOCKED_BY_ALERT)
        self.run.assert_called_once()
        self.assertFalse(self.job.blocked_alert_id)
        self.assertEqual(self.inst.state, "deploying")

    def test_a_conflict_whose_alert_another_transaction_holds_refuses(self):
        """Filed at the same moment by a transaction this one cannot see:
        the deploy must neither wait on nothing nor run."""
        executor = DeployInstanceExecutor(self.job, self.host)
        with patch(_DETECT, autospec=True, return_value=_CONFLICTS), \
                patch.object(
                    type(self.inst), "_raise_addon_conflict_alert",
                    autospec=True, return_value=self.env["cloud.alert"],
                ), self.assertRaises(JobRefused):
            executor.blocking_alert()

    def test_an_executor_that_opts_out_does_not_ask_github(self):
        """A warm claim ships the repositories its warm was built from."""
        executor = DeployInstanceExecutor(self.job, self.host)
        executor._preflight_addons = False
        with patch(_DETECT, autospec=True) as detect:
            self.assertFalse(executor.blocking_alert())
        detect.assert_not_called()

    def test_the_blocked_end_announces_nothing_and_dismisses_nothing(self):
        """No mail, no toast, and an earlier failure stays reported."""
        earlier_job = self._deploy_job("uuid-earlier")
        earlier_job.queue_job_id.write({
            "state": "failed", "exc_message": "an earlier deploy failed",
        })
        earlier = self.env["cloud.alert"].search([
            ("job_id", "=", earlier_job.id), ("code", "=", "job_failed"),
        ])
        self.assertTrue(earlier)
        self._execute(_CONFLICTS)
        CloudJob = type(self.env["cloud.job"])
        with patch.object(
            CloudJob, "_notify_by_email", autospec=True,
        ) as mailed, patch.object(
            CloudJob, "_broadcast_job_update", autospec=True,
        ) as told:
            self.job.queue_job_id.write({
                "state": "done", "result": BLOCKED_BY_ALERT,
            })
        mailed.assert_not_called()
        # 'blocked', which the browser does not toast: "Deploy Instance
        # completed" was shown for a deploy that waited.
        self.assertEqual(told.call_args.kwargs["state"], "blocked")
        self.assertEqual(earlier.state, "active")


@tagged("post_install", "-at_install")
class TestAConsultantsDeployWaits(_DeployJobCase):
    """The same wait, in the rights a consultant's deploy runs with.

    A job asked for without superuser rights runs without them
    (``cloud.job._dispatch_single``): blocking it, linking its alert and
    writing its log must not need them. ``post_install``: the consultant
    needs a partner, and a module installed later may add a column to it.
    """

    def test_a_consultants_deploy_waits_on_its_conflict_alert(self):
        """Blocked, linked and logged as the consultant, the instance in 'draft'."""
        consultant = new_test_user(
            self.env, login="blocked-consultant",
            groups="base.group_user,incubacloud.group_cloud_consultant",
        )
        self.inst.project_id.member_ids = [(4, consultant.id)]
        self.job = self.job.with_user(consultant)
        self.assertFalse(self.job.env.su)
        with self.assertNoLogs("odoo.addons.incubacloud", level="ERROR"):
            result = self._execute(_CONFLICTS)
        self.assertEqual(result, BLOCKED_BY_ALERT)
        alert = self._conflict_alert()
        self.assertEqual(len(alert), 1)
        self.assertEqual(self.job.sudo().blocked_alert_id, alert)
        self.assertEqual(self.inst.state, "draft")
        self.assertIn(
            "Waiting for the alert to be resolved",
            self.job.sudo()._get_last_system_message(),
        )


class TestAStepOfAChain(_DeployJobCase):
    """A chain cannot wait for one of its steps."""

    def setUp(self):
        """A step that queue_job runs another job after."""
        super().setUp()
        # queue_job runs this job next, once the deploy is stored done.
        self.job.queue_job_id.dependencies = {
            "depends_on": [], "reverse_depends_on": ["uuid-next-step"],
        }

    def test_it_fails_instead_of_waiting(self):
        """Stored done, the next step of a move would stop the source and
        restore onto a target never deployed."""
        with self.assertRaises(JobError):
            self._execute(_CONFLICTS)
        self.run.assert_not_called()
        self.assertFalse(self.job.blocked_alert_id)
        self.aborted.assert_called_once()
        self.assertIsInstance(self.aborted.call_args.args[1], JobRefused)

    def test_its_alert_is_raised_to_last(self):
        """On a cursor of its own: the failure rolls the job's back.

        Caught by hand: Odoo's ``assertRaises`` rolls its block back.
        """
        with patch.object(
            DeployInstanceExecutor, "_as_durable", autospec=True,
            side_effect=AbstractExecutor._as_durable,
        ) as durable:
            try:
                self._execute(_CONFLICTS)
            except JobError:
                pass
            else:
                self.fail("a step of a chain waited")
        durable.assert_called()
        alert = self._conflict_alert()
        self.assertEqual(alert.job_id, self.job)
        self.assertEqual(alert.conflict_data, _CONFLICTS)

    def test_without_conflicts_it_runs(self):
        """The check costs a step of a chain nothing when it passes."""
        self._execute([])
        self.run.assert_called_once()


class TestWaitingDeploy(_DeployJobCase):
    """What a deploy that waits on its alert lets the customer do."""

    def setUp(self):
        """The deploy waits on its conflict alert, with the instance in 'draft'."""
        super().setUp()
        self.alert = self.inst._raise_addon_conflict_alert(_CONFLICTS, job=self.job)
        self.job.blocked_alert_id = self.alert
        self.job.queue_job_id.write({"state": "done", "result": BLOCKED_BY_ALERT})
        self.inst._transition("draft")

    def _dispatching(self):
        """Patch the dispatch so it hands back a queue uuid and nothing runs."""
        delay = patch.object(
            type(self.env["cloud.job"]), "with_delay", autospec=True,
        )
        started = delay.start()
        self.addCleanup(delay.stop)
        started.return_value.execute.return_value.uuid = "uuid-redispatched"
        return started

    def test_deploying_again_points_at_the_alert(self):
        """A second deploy would wait beside the first for the same alert."""
        with patch.object(
            type(self.inst), "_check_backup_backend", autospec=True,
        ), patch(
            _DETECT_ON_SAVE, autospec=True, return_value=_CONFLICTS,
        ), self.assertRaisesRegex(UserError, "waiting for an alert"):
            self.inst.deploy()
        self.assertEqual(self.inst.state, "draft")

    def test_deploying_once_github_was_fixed_runs_the_waiting_job(self):
        """Fixed in GitHub, which no save of the panel saw: the deploy
        looks again instead of refusing."""
        delay = self._dispatching()
        with patch.object(
            type(self.inst), "_check_backup_backend", autospec=True,
        ), patch(_DETECT_ON_SAVE, autospec=True, return_value=[]) as detect:
            job_id = self.inst.deploy()
        self.assertEqual(job_id, self.job.id)
        _run_after_commit(self.env)
        delay.assert_called_once()
        self.assertEqual(self.alert.state, "dismissed")
        # Within the time a person waiting on the request can be given.
        self.assertLessEqual(
            detect.call_args.kwargs["budget"].remaining_seconds(),
            ADDON_CHECK_SECONDS,
        )

    def test_a_build_that_went_through_cancels_what_waited(self):
        """Left blocked on a closed alert, a rebuild showed "Action
        required" for ever and could not be cancelled."""
        done = self._deploy_job("uuid-went-through")
        executor = DeployInstanceExecutor(done, self.host)
        executor._dismiss_addon_conflict_alerts()
        executor._cancel_stale_waits()
        # Written on cursors of their own.
        self.job.invalidate_recordset(["state", "blocked_alert_id"])
        self.alert.invalidate_recordset(["state"])
        self.assertEqual(self.alert.state, "dismissed")
        self.assertEqual(self.job.state, "cancelled")
        self.assertFalse(self.job.blocked_alert_id)

    def test_what_waits_on_an_open_alert_keeps_waiting(self):
        """Only a closed alert's waiters are behind the build."""
        done = self._deploy_job("uuid-went-through-2")
        DeployInstanceExecutor(done, self.host)._cancel_stale_waits()
        self.job.invalidate_recordset(["state", "blocked_alert_id"])
        self.assertEqual(self.job.blocked_alert_id, self.alert)

    def test_a_rebuild_that_went_through_cancels_them_too(self):
        """A rebuild's ``on_success`` does not call the deploy's."""
        self.assertIn(
            "self._cancel_stale_waits()",
            inspect.getsource(RebuildInstanceExecutor.on_success),
        )

    def test_the_import_s_check_is_bounded_by_the_time_left(self):
        """Every GitHub request is charged to a budget of that time."""
        with patch(_DETECT_ON_SAVE, autospec=True, return_value=[]) as detect:
            self.inst._check_addon_conflicts(seconds=12.5)
        budget = detect.call_args.kwargs["budget"]
        self.assertIsInstance(budget, HTTPBudget)
        self.assertLessEqual(budget.remaining_seconds(), 12.5)

    def test_a_blocked_job_offers_its_cancel_button(self):
        """The model accepts it; the timelines must show the button."""
        root = pathlib.Path(__file__).parent.parent / "static" / "src" / "components"
        for view in ("instance_detail/instance_detail.xml", "host_detail/host_detail.xml"):
            with self.subTest(view=view):
                self.assertIn(
                    "isActiveJob(job.state) or job.state === 'blocked'",
                    (root / view).read_text(),
                )

    def test_deploying_once_the_alert_closed_runs_the_waiting_job(self):
        """The waiting job is the deploy; it goes, in 'deploying'."""
        self.alert.write({"state": "dismissed"})
        delay = self._dispatching()
        with patch.object(
            type(self.inst), "_check_backup_backend", autospec=True,
        ):
            job_id = self.inst.deploy()
        self.assertEqual(job_id, self.job.id)
        _run_after_commit(self.env)
        delay.assert_called_once()
        self.assertFalse(self.job.blocked_alert_id)
        self.assertEqual(self.inst.state, "deploying")

    def test_a_clean_repository_check_dispatches_it(self):
        """Saving repositories that no longer conflict closes the alert."""
        delay = self._dispatching()
        with patch(_DETECT_ON_SAVE, autospec=True, return_value=[]):
            self.assertEqual(self.inst._check_addon_conflicts(), [])
        self.assertEqual(self.alert.state, "dismissed")
        _run_after_commit(self.env)
        delay.assert_called_once()
        self.assertEqual(self.inst.state, "deploying")

    def test_a_repository_github_would_not_list_closes_nothing(self):
        """It may be the one that conflicts."""
        delay = self._dispatching()

        async def unread(env, repos, unread=None, budget=None):
            """GitHub lists one repository and not the other."""
            unread.append("odoo-addons")
            return []

        with patch(_DETECT_ON_SAVE, autospec=True, side_effect=unread), \
                self.assertNoLogs(
                    "odoo.addons.incubacloud.models.cloud_instance",
                    level="WARNING",
                ):
            self.inst._check_addon_conflicts()
        self.assertEqual(self.alert.state, "active")
        delay.assert_not_called()

    def test_it_deploys_where_the_instance_is_now(self):
        """A host changed while it waited: the old one would get an
        orphan stack while the record named the new one."""
        other = self.env["cloud.host"].create({
            "name": "blocked-host-2",
            "ip_address": "10.0.0.72",
            "user": "ubuntu",
            "wildcard_domain": "blocked2.example.com",
        })
        self.inst.host_id = other
        self._dispatching()
        self.job.unblock_and_enqueue()
        self.assertEqual(self.job.host_id, other)

    def test_the_others_waiting_on_it_are_cancelled(self):
        """Only one is dispatched; the rest used to wait for good."""
        twin = self._deploy_job("uuid-twin")
        twin.blocked_alert_id = self.alert
        twin.queue_job_id.write({"state": "done", "result": BLOCKED_BY_ALERT})
        self._dispatching()
        self.job.unblock_and_enqueue()
        self.assertFalse(twin.blocked_alert_id)
        self.assertEqual(twin.state, "cancelled")

    def test_the_instance_can_be_deleted_while_it_waits(self):
        """Back in draft, nothing holds it."""
        self.inst.unlink()
        self.assertFalse(self.inst.exists())


class TestTheImportCheckReadsGitHub(_DeployJobCase):
    """The import's check reads GitHub with the panel's credentials."""

    def test_a_bounded_check_reads_with_the_credentials(self):
        """Every bounded request needs a byte cap: without it each one
        raised, and the check fell back to anonymous calls, which cannot
        read a private repository."""
        tree = {"tree": [{"path": "mod_a/__manifest__.py"}]}
        answer = MagicMock(spec=http.client.HTTPResponse)
        answer.headers = {}
        answer.read.return_value = json.dumps(tree).encode()
        Service = type(self.env["cloud.github.credential.service"])
        with patch.object(Service, "get_pat", autospec=True, return_value="pat"), \
                patch(
                    "odoo.addons.incubacloud.github.client.safe_urlopen",
                    autospec=True,
                ) as opened, patch(
                    "odoo.addons.incubacloud.models._repo_requirements.safe_urlopen",
                    autospec=True,
                ) as anonymous:
            opened.return_value.__enter__.return_value = answer
            conflicts = self.inst._check_addon_conflicts(seconds=30)
        anonymous.assert_not_called()
        self.assertEqual(
            conflicts, [{"addon": "mod_a", "repos": ["odoo-addons", "web"]}],
        )


class TestTheConflictAlert(_DeployJobCase):
    """One alert per instance, however many find its conflicts."""

    def test_a_second_finding_refreshes_the_first(self):
        """Two saves that find the same conflicts end with one alert."""
        first = self.inst._raise_addon_conflict_alert(_CONFLICTS)
        second = self.inst._raise_addon_conflict_alert(_CONFLICTS[:1], job=self.job)
        self.assertEqual(first, second)
        self.assertEqual(second.conflict_data, _CONFLICTS[:1])
        self.assertEqual(second.job_id, self.job)
        self.assertEqual(len(self._conflict_alert()), 1)


class TestJobFailedOnItsOwnConflict(_DeployJobCase):
    """A job's own conflict alert stands for its failure if it reached people."""

    def _failed(self):
        """Fail the job; return the ``job_failed`` alert it raised."""
        self.job.queue_job_id.write({
            "state": "failed",
            "exc_name": "odoo.addons.queue_job.exception.JobError",
            "exc_message": "Pre-flight: 2 addon conflict(s)",
        })
        return self.env["cloud.alert"].search([
            ("job_id", "=", self.job.id), ("code", "=", "job_failed"),
        ])

    def test_a_critical_conflict_of_its_own_raises_nothing_more(self):
        """The conflict alert already went to everyone a failure would."""
        self.inst._raise_addon_conflict_alert(_CONFLICTS, job=self.job)
        self.assertFalse(self._failed())

    def test_a_pip_conflict_still_raises_job_failed(self):
        """A warning the default notification level does not send."""
        self.env["cloud.alert"].sudo().create({
            "code": "pip_conflict",
            "level": "warning",
            "message": "Pip dependency conflicts: lxml",
            "instance_id": self.inst.id,
            "job_id": self.job.id,
        })
        self.assertTrue(self._failed())

    def test_any_other_failure_still_raises_job_failed(self):
        """No alert of its own: the failure is the news."""
        self.assertTrue(self._failed())


class TestAbortBeforeTheRun(_DeployJobCase):
    """A failure before ``run`` reaches ``on_abort`` too."""

    def test_a_refused_check_ends_the_job_through_on_abort(self):
        """A staging cloned from a production with pip markers stayed in
        'deploying' when ``pre_run_checks`` refused it."""
        exc = JobRefused("unresolved pip dependency conflicts")
        with self.assertRaises(JobError):
            self._execute([], side_effect=exc)
        self.aborted.assert_called_once()
        self.assertIs(self.aborted.call_args.args[1], exc)
        self.run.assert_not_called()

    def test_a_defect_before_the_run_is_logged_with_its_traceback(self):
        """On a tenant's panel the probe leaves queue_job's tracebacks of
        a ``cloud.job`` out: this line is the one IncubaCloud reads."""
        with patch.object(
            DeployInstanceExecutor, "blocking_alert", autospec=True,
            side_effect=KeyError("repo"),
        ), self.assertLogs("AbstractExecutor", level="ERROR") as logs:
            try:
                self._execute([])
            except JobError:
                pass
            else:
                self.fail("the job did not fail")
        self.assertTrue(logs.records[0].exc_info)
        self.aborted.assert_called_once()

    def test_a_refusal_before_the_run_is_a_warning(self):
        """Its message is the whole story."""
        with self.assertNoLogs("AbstractExecutor", level="ERROR"), \
                contextlib.suppress(JobError):
            self._execute([], side_effect=JobRefused("pip markers"))

    def test_a_failure_while_it_is_left_blocked_ends_through_on_abort(self):
        """It rolls back to a savepoint: what ``on_blocked`` wrote is
        undone and the row ``on_abort`` writes on its own cursor freed."""
        with patch.object(
            type(self.env["cloud.job"]), "_block_on", autospec=True,
            side_effect=RuntimeError("flush"),
        ), patch.object(
            type(self.env.cr), "savepoint", autospec=True,
            wraps=type(self.env.cr).savepoint,
        ) as savepoint:
            try:
                self._execute(_CONFLICTS)
            except JobError:
                pass
            else:
                self.fail("the job did not fail")
        savepoint.assert_called()
        self.aborted.assert_called_once()

    def test_a_retry_before_the_run_is_not_the_end(self):
        """The host's build lock is busy: queue_job runs it again."""
        with self.assertRaises(RetryableJobError):
            self._execute([], side_effect=RetryableJobError("host busy"))
        self.aborted.assert_not_called()


class TestAbortInTheRun(_DeployJobCase):
    """``on_abort``: the end of a run that has no outcome.

    The wiring is checked with ``on_abort`` mocked, its effect by calling
    it: under tests the durable cursor is a savepoint inside the test's
    transaction, and one opened earlier by the run can roll it back,
    which a real, separate cursor never does.
    """

    def _run_entry(self, exc):
        """Run ``_async_entry`` with ``before_execute`` raising *exc*.

        :return: the ``on_abort`` mock
        """
        executor = DeployInstanceExecutor(self.job, self.host)
        with patch.object(CloudHost, "get_transport", autospec=True), \
                patch.object(
                    DeployInstanceExecutor, "before_execute",
                    autospec=True, side_effect=exc,
                ), \
                patch.object(
                    DeployInstanceExecutor, "on_abort", autospec=True,
                ) as aborted, \
                self.assertRaises(type(exc)):
            asyncio.run(executor._async_entry())
        return aborted

    def _failing_steps(self, **on_failure):
        """Run ``_async_entry`` with a step that failed.

        :return: the ``on_failure`` and ``on_abort`` mocks
        """
        executor = DeployInstanceExecutor(self.job, self.host)
        failed = {"Build": {"stdout": "", "exit_status": 1}}
        with patch.object(CloudHost, "get_transport", autospec=True), \
                patch.object(
                    DeployInstanceExecutor, "before_execute", autospec=True,
                ), \
                patch.object(
                    DeployInstanceExecutor, "get_commands", autospec=True,
                    return_value=[],
                ), \
                patch.object(
                    DeployInstanceExecutor, "_run_steps", autospec=True,
                    return_value=(failed, []),
                ), \
                patch.object(
                    DeployInstanceExecutor, "on_failure", autospec=True,
                    **on_failure,
                ) as failure, \
                patch.object(
                    DeployInstanceExecutor, "on_abort", autospec=True,
                ) as aborted, \
                self.assertRaises(Exception):
            asyncio.run(executor._async_entry())
        return failure, aborted

    def test_a_refused_check_ends_the_run_through_on_abort(self):
        """A refusal is the job's news, not an incident."""
        exc = JobRefused("the host holds none")
        with self.assertNoLogs("AbstractExecutor", level="ERROR"):
            aborted = self._run_entry(exc)
        aborted.assert_called_once()
        self.assertIs(aborted.call_args.args[1], exc)

    def test_an_unexpected_exception_is_logged_and_ends_it_too(self):
        """A defect of ours keeps its traceback."""
        with self.assertLogs("AbstractExecutor", level="ERROR"):
            aborted = self._run_entry(KeyError("boom"))
        aborted.assert_called_once()

    def test_an_access_error_is_a_defect(self):
        """A ``UserError``, but in a job our code read what it may not."""
        with self.assertLogs("AbstractExecutor", level="ERROR"):
            self._run_entry(AccessError("no"))

    def test_an_attempt_that_will_be_retried_is_not_the_end(self):
        """queue_job runs it again."""
        aborted = self._run_entry(RetryableJobError("host busy"))
        aborted.assert_not_called()

    def test_a_cancellation_is_not_an_abort(self):
        """A person asked for it; ``queue_job_ext`` records it."""
        with self.assertNoLogs("AbstractExecutor", level="WARNING"):
            aborted = self._run_entry(JobCancelled())
        aborted.assert_not_called()

    def test_steps_that_failed_go_to_on_failure_only(self):
        """``on_failure`` dealt with them."""
        with self.assertNoLogs("AbstractExecutor", level="ERROR"):
            failure, aborted = self._failing_steps()
        failure.assert_called_once()
        aborted.assert_not_called()

    def test_a_hook_that_raises_is_a_defect_and_ends_through_on_abort(self):
        """It did not deal with the outcome: logged with its traceback,
        not as the step failure it was handed."""
        with self.assertLogs("AbstractExecutor", level="ERROR"):
            failure, aborted = self._failing_steps(side_effect=KeyError("x"))
        failure.assert_called_once()
        aborted.assert_called_once()

    def test_a_hook_that_raises_a_user_error_is_a_defect_too(self):
        """A ``ValidationError`` out of ``on_success`` is not a refusal."""
        with self.assertLogs("AbstractExecutor", level="ERROR"):
            self._failing_steps(side_effect=UserError("no transition"))

    def test_an_aborted_deploy_fails_back_to_draft(self):
        """Nothing reached the host: deployable again, ``status`` says why."""
        executor = DeployInstanceExecutor(self.job, self.host)
        asyncio.run(executor.on_abort(JobRefused("the host holds none")))
        self.assertEqual(self.inst.state, "draft")
        self.assertEqual(self.inst.status, "error")

    def test_an_aborted_rebuild_leaves_the_instance_alone(self):
        """A host that did not answer is no fault of a running instance."""
        self.inst.status = "ok"
        executor = RebuildInstanceExecutor(self.job, self.host)
        asyncio.run(executor.on_abort(JobRefused("no route to host")))
        self.assertEqual(self.inst.status, "ok")

    def test_the_failure_is_written_to_the_job_log_once(self):
        """An executor whose ``on_abort`` reports it leaves the line to it."""
        self.assertTrue(DeployInstanceExecutor(self.job, self.host)._handles_abort())
        self.assertFalse(MoveCutoverExecutor(self.job, self.host)._handles_abort())


class TestQueueJobFailureMessages(_DeployJobCase):
    """queue_job's own failure message, mailed to its managers."""

    def test_the_platform_jobs_failure_is_left_to_its_alert(self):
        """The ``cloud.job`` failure has its alert; no chatter, no mail."""
        qjob = self.job.queue_job_id
        qjob.write({"state": "failed", "exc_info": "Traceback"})
        before = qjob.message_ids
        qjob._message_post_on_failure()
        self.assertEqual(qjob.message_ids, before)
        self.assertFalse(qjob.message_partner_ids)

    def test_a_job_that_is_not_the_platforms_keeps_it(self):
        """Nothing else reports it."""
        other = self.env["queue.job"].sudo().create({
            "uuid": "uuid-not-ours", "name": "other", "state": "failed",
            "method_name": "noop", "model_name": "res.partner",
            "func_string": "noop()", "exc_info": "Traceback",
        })
        with patch.object(
            type(other), "_message_failed_job", autospec=True,
            return_value="It failed",
        ):
            other._message_post_on_failure()
        self.assertTrue(other.message_ids.filtered(
            lambda m: "It failed" in (m.body or ""),
        ))


@tagged("post_install", "-at_install")
class TestCronBotInbox(TransactionCase):
    """The cron bot's notifications stay in Odoo."""

    def test_the_bot_reads_its_notifications_in_odoo(self):
        """It has no address: by email, each one ended as a failure."""
        Users = self.env["res.users"]
        Users._incubacloud_ensure_cron_bot()
        bot = Users._get_cron_bot()
        self.assertEqual(bot.notification_type, "inbox")
        self.assertIn(
            self.env.ref("mail.group_mail_notification_type_inbox"),
            bot.group_ids,
        )


class TestLogLevel(_DeployJobCase):
    """A refusal is logged as the warning it is."""

    def test_a_user_error_is_a_warning_without_traceback(self):
        """Its message is the whole story."""
        # One capture only: ``assertLogs`` replaces the logger's handlers,
        # so a second one nested on the same logger blinds the first.
        with self.assertLogs("AbstractExecutor", level="WARNING") as logs:
            self._abort(UserError("Host has no trusted SSH key"))
        self.assertTrue(any("no trusted SSH key" in line for line in logs.output))
        self.assertFalse([r for r in logs.records if r.levelno >= 40])

    def _abort(self, exc):
        """Run ``_async_entry`` with ``before_execute`` raising *exc*."""
        executor = DeployInstanceExecutor(self.job, self.host)
        with patch.object(CloudHost, "get_transport", autospec=True), \
                patch.object(
                    DeployInstanceExecutor, "before_execute",
                    autospec=True, side_effect=exc,
                ), self.assertRaises(type(exc)):
            asyncio.run(executor._async_entry())
