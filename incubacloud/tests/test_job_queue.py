"""The job queue per instance (``cloud.job``, 1.0.198).

An instance runs one visible job at a time; what is asked for meanwhile
waits in its queue and starts when the running job ends, first come first
served, with what the running job queued for itself first. A host
serialises its host-level jobs the same way. The spec's cases that live
in core are here: order, heads, collapse, exclusive types, the cap, a
held queue and its release, deletes, archived and moved instances, the
sweep, cancelling while waiting, chains as one entry, hidden types and
the mirror of queue_job's transitions.

Jobs are put in a state with ``create``/``write`` of ``state``: it is the
job's own field now. A running job ends through ``_end``: on its
queue.job when the dispatcher gave it one, so the end arrives the way
the runner's does (the mirror of ``queue_job_ext``), and on the job
itself when the test created it running by hand. What waits is served
once the event's transaction commits; a test runs that with
``_run_after_commit``, once after what it queued — so the dispatch the
queueing registered is spent — and once after the event under test.
"""
import asyncio
import contextlib
import json
import re
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields, http
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase, new_test_user

from odoo.addons.queue_job.exception import JobError
from odoo.addons.queue_job.job import Job as QueueJob

from ..controllers import data_load
from ..controllers._data_load import _routes_crud
from ..controllers._data_load._helpers import _job_response
from ..models.abstract_executor import (
    CANCELLED_WHILE_RUNNING,
    AbstractExecutor,
)
from ..models.cloud_job import QUEUE_MAX_WAITING
from ..models.instance_health_executor import InstanceHealthExecutor
from ..models.move_cutover_executor import MoveCutoverExecutor
from ..models.rebuild_instance_executor import RebuildInstanceExecutor
from ..models.registry import executor_registry
from ..models.restart_instance_executor import RestartInstanceExecutor
from ..models.restore_instance_executor import RestoreInstanceExecutor
from ..models.transport import SSHTransport
from .test_cloud_job import _run_after_commit

#: An id no job of the test's transaction has: what a fresh read returns
#: for a job another request inserted after this transaction's snapshot.
_UNSEEN_ID = 2**31 - 1


class _QueueCase(TransactionCase):
    """A host, a project and a staging instance on it, deployed."""

    def setUp(self):
        super().setUp()
        # The dispatcher and the fast path read through cursors of their
        # own: in test mode they ride on this transaction, outside it they
        # would be real connections that see nothing of the test and wait
        # for its locks.
        self.registry_enter_test_mode()
        self.host = self.env["cloud.host"].create({
            "name": "queue-host",
            "ip_address": "10.0.0.81",
            "user": "ubuntu",
            "wildcard_domain": "queue.example.com",
            "status": "compatible",
            "traefik_deployed": True,
        })
        self.project = self.env["cloud.project"].create({"name": "Queue"})
        self.inst = self._instance("queue-inst")
        self.Job = self.env["cloud.job"]

    def _instance(self, name, environment="staging", state="deployed"):
        """Another instance of the project on the host, in *state*."""
        return self.env["cloud.instance"].create({
            "name": name,
            "project_id": self.project.id,
            "host_id": self.host.id,
            "environment": environment,
            "state": state,
        })

    def _type(self, code):
        """The job type *code* of the data files."""
        job_type = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        self.assertTrue(job_type, code)
        return job_type

    def _job(self, code, state="started", inst=None, host=None, **vals):
        """A job of *code* on *inst* (or the host, with ``inst=False``), in *state*."""
        if inst is None:
            inst = self.inst
        return self.Job.create({
            "name": code,
            "host_id": (host or self.host).id,
            "instance_id": inst.id if inst else False,
            "job_type_id": self._type(code).id,
            "state": state,
        } | vals)

    def _enqueue(self, code, inst=None, **kwargs):
        """``enqueue`` *code* on *inst* and return the record."""
        if inst is None:
            inst = self.inst
        job_id = self.Job.enqueue(
            self.host.id, inst.id if inst else False, code, **kwargs,
        )
        return self.Job.browse(job_id)

    def _note(self, job):
        """The last system message of *job*'s log."""
        return job._get_last_system_message()

    def _end(self, job, state="done"):
        """End *job* as the runner would: through its queue.job if it has one."""
        if job.queue_job_id:
            job.queue_job_id.write({"state": state})
        else:
            job.write({"state": state})

    @contextlib.contextmanager
    def _fresh_read_before_commit(self, jobs):
        """Let the fresh read see *jobs* as they were before this test cancelled them.

        In production the READ COMMITTED read cannot see what this
        transaction wrote and has not committed: *jobs*, cancelled here,
        are still ``queued`` there. Under tests the read shares the
        transaction; this puts them back in what it returns.
        """
        Job = type(self.Job)
        read = Job._fresh_rows
        queued = ("state", "=", "queued")

        def before_commit(job_model, domain, fnames=()):
            """The real fresh read, plus *jobs* wherever they waited."""
            rows = read(job_model, domain, fnames)
            if queued not in domain:
                return rows
            stale = [
                ("state", "=", "cancelled") if leaf == queued else leaf
                for leaf in domain
            ] + [("id", "in", jobs.ids)]
            return rows + read(job_model, stale, fnames)

        with patch.object(
            Job, "_fresh_rows", autospec=True, side_effect=before_commit,
        ) as fresh:
            yield fresh

    def _failed_with_alert(self, code, inst=None):
        """A failed job of *code* with its ``job_failed`` alert raised."""
        job = self._job(code, state="failed", inst=inst)
        self.Job._create_job_failed_alert(job, exc_message="boom")
        alert = self.env["cloud.alert"].search([
            ("job_id", "=", job.id), ("code", "=", "job_failed"),
            ("state", "=", "active"),
        ])
        self.assertTrue(alert)
        return job, alert

    def _consultant(self, login, *groups, member=True):
        """A consultant, in *groups* besides, member of the project unless not *member*.

        Only in a ``post_install`` class: the person needs a partner, and
        a module installed after this one may add a column to it that only
        its own defaults fill.
        """
        user = new_test_user(
            self.env, login=login,
            groups=",".join((
                "base.group_user", "incubacloud.group_cloud_consultant", *groups,
            )),
        )
        if member:
            self.project.member_ids = [(4, user.id)]
        return user


class TestDispatch(_QueueCase):
    """Free targets start at once; busy ones queue, in order."""

    def test_a_free_instance_starts_the_job_at_once(self):
        job = self._enqueue("rebuild_instance")
        self.assertEqual(job.state, "pending")
        self.assertTrue(job.queue_job_uuid)
        self.assertEqual(_job_response(self.env, job.id)["state"], "pending")

    def test_a_busy_instance_queues_the_job(self):
        rebuild = self._job("rebuild_instance")
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart.state, "queued")
        self.assertFalse(restart.queue_job_uuid)
        self.assertEqual(restart._format()[0]["queue_position"], 0)
        self.assertEqual(_job_response(self.env, restart.id)["state"], "queued")
        rebuild.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "pending")
        self.assertTrue(restart.queue_job_uuid)

    def test_the_queue_is_served_in_order_of_creation(self):
        rebuild = self._job("rebuild_instance")
        restart = self._enqueue("restart_instance")
        backup = self._enqueue("backup_create")
        self.assertEqual(backup._format()[0]["queue_position"], 1)
        rebuild.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "pending")
        self.assertEqual(backup.state, "queued")
        self.assertEqual(backup._format()[0]["queue_position"], 0)
        self._end(restart)
        _run_after_commit(self.env)
        self.assertEqual(backup.state, "pending")

    def test_a_follow_up_of_the_running_job_goes_first(self):
        """``bypass_running_check``: part of the job running now."""
        deploy = self._job("deploy_instance")
        rebuild = self._enqueue("rebuild_instance")
        follow = self._enqueue("restart_instance", bypass_running_check=True)
        self.assertEqual(follow.state, "queued")
        self.assertTrue(follow.queue_head)
        self.assertEqual(follow._format()[0]["queue_position"], 0)
        self.assertEqual(rebuild._format()[0]["queue_position"], 1)
        deploy.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(follow.state, "pending")
        self.assertEqual(rebuild.state, "queued")

    def test_heads_go_by_creation_among_themselves(self):
        deploy = self._job("deploy_instance")
        first = self._enqueue("restart_instance", bypass_running_check=True)
        second = self._enqueue("backup_create", bypass_running_check=True)
        deploy.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(first.state, "pending")
        self.assertEqual(second.state, "queued")

    def test_queued_counts_as_active(self):
        self.assertIn("queued", self.Job._active_states)
        self.assertNotIn("queued", self.Job._running_states)
        for state in ("done", "failed", "cancelled"):
            self.assertNotIn(state, self.Job._active_states)

    def test_a_hidden_type_never_waits_nor_occupies(self):
        self._job("rebuild_instance")
        probe = self._enqueue("instance_health")
        self.assertEqual(probe.state, "pending")
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart.state, "queued")
        # Only the probe runs: nothing holds the restart back.
        self.assertEqual(
            self.Job.search([
                ("instance_id", "=", self.inst.id), ("state", "=", "started"),
            ]).mapped("job_type_id.code"), ["rebuild_instance"],
        )

    def test_host_jobs_queue_per_host_apart_from_instance_jobs(self):
        hardening = self._job("host_hardening", inst=False)
        setup = self._enqueue("full_setup", inst=False)
        self.assertEqual(setup.state, "queued")
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart.state, "pending")
        hardening.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(setup.state, "pending")

    def test_the_mirror_follows_the_queue_job_and_moves_the_queue_on(self):
        """queue_job writes ``started`` and ``done``; the queue follows."""
        rebuild = self._enqueue("rebuild_instance")
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart.state, "queued")
        rebuild.queue_job_id.write({"state": "started"})
        self.assertEqual(rebuild.state, "started")
        self.assertEqual(restart.state, "queued")
        rebuild.queue_job_id.write({"state": "done"})
        self.assertEqual(rebuild.state, "done")
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "pending")

    def test_a_cooperative_cancel_moves_nothing_until_the_job_stops(self):
        rebuild = self._enqueue("rebuild_instance")
        rebuild.queue_job_id.write({"state": "started"})
        restart = self._enqueue("restart_instance")
        rebuild.cancel_job()
        self.assertEqual(rebuild.state, "cancelled")
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "queued", "the executor still runs")
        # The flag is read from the row by the mirror, as from another
        # transaction in production.
        self.env.flush_all()
        rebuild.queue_job_id.write({
            "state": "done", "result": CANCELLED_WHILE_RUNNING,
        })
        self.assertEqual(rebuild.state, "cancelled")
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "pending")

    def test_a_job_cancelled_while_it_runs_still_occupies_the_target(self):
        """The flag is written while the executor winds down: nothing
        enqueued meanwhile, and no sweep, starts beside it."""
        rebuild = self._enqueue("rebuild_instance")
        rebuild.queue_job_id.write({"state": "started"})
        rebuild.cancel_job()
        self.env.flush_all()
        self.assertTrue(self.Job._scope_busy(self.inst.id, None))
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart.state, "queued")
        follow = self._enqueue("backup_create", bypass_running_check=True)
        self.assertEqual(follow.state, "queued")
        self.assertEqual(self.Job._cron_dispatch_queued(), 0)
        self.assertEqual(restart.state, "queued")
        rebuild.queue_job_id.write({
            "state": "done", "result": CANCELLED_WHILE_RUNNING,
        })
        _run_after_commit(self.env)
        self.assertEqual(follow.state, "pending")
        self.assertEqual(restart.state, "queued")


class TestAdmission(_QueueCase):
    """What the type says: collapse, exclusive, the cap."""

    def test_an_identical_waiting_job_is_returned_instead_of_a_second(self):
        self._job("deploy_instance")
        first = self._enqueue("rebuild_instance")
        second = self._enqueue("rebuild_instance")
        self.assertEqual(first, second)

    def test_a_running_job_is_not_collapsed_into(self):
        running = self._job("restart_instance")
        queued = self._enqueue("restart_instance")
        self.assertNotEqual(running, queued)
        self.assertEqual(queued.state, "queued")

    def test_a_different_payload_is_another_job(self):
        self._job("deploy_instance")
        first = self._enqueue("backup_create", payload={"with_filestore": True})
        second = self._enqueue("backup_create", payload={"with_filestore": False})
        self.assertNotEqual(first, second)

    def test_a_different_secret_is_another_job(self):
        """The identity a layer gives the load (secret apart) is compared.

        Plays the SaaS manager: ``_queue_identity`` splits the secret off
        the payload the way its ``create`` stores it.
        """
        self._job("deploy_instance")
        Job = type(self.Job)

        def split(payload):
            """Return ``(secret, payload without it)``, as the layer stores them."""
            payload = dict(payload or {})
            return payload.pop("secret", None) or False, payload

        with patch.object(
            Job, "_queue_identity", autospec=True,
            side_effect=lambda self, payload: tuple(reversed(split(payload))),
        ):
            first = self._enqueue("restart_instance", payload={"a": 1, "secret": "x"})
            first.write({"payload": {"a": 1}, "secret_payload": "x"})
            second = self._enqueue("restart_instance", payload={"a": 1, "secret": "y"})
            second.write({"payload": {"a": 1}, "secret_payload": "y"})
            third = self._enqueue("restart_instance", payload={"a": 1, "secret": "x"})
        self.assertNotEqual(first, second)
        self.assertEqual(first, third)

    def test_a_second_waiting_exclusive_job_is_refused(self):
        self._job("deploy_instance")
        self._enqueue("restore_instance", payload={"backup": "a"})
        with self.assertRaisesRegex(UserError, "already queued"):
            self._enqueue("restore_instance", payload={"backup": "b"})

    def test_an_exclusive_job_only_a_fresh_read_sees_refuses_a_second(self):
        """Committed by another request after this one's snapshot."""
        self._job("deploy_instance")
        with patch.object(
            type(self.Job), "_fresh_rows", autospec=True,
            return_value=[{"id": _UNSEEN_ID}],
        ) as fresh, self.assertRaisesRegex(UserError, "already queued"):
            self._enqueue("restore_instance", payload={"backup": "b"})
        fresh.assert_called_once()

    def test_the_sixth_entry_is_refused_but_a_head_enters(self):
        self._job("deploy_instance")
        for n in range(QUEUE_MAX_WAITING):
            self._enqueue("backup_create", payload={"n": n})
        with self.assertRaisesRegex(UserError, "waiting in its queue"):
            self._enqueue("backup_create", payload={"n": 99})
        follow = self._enqueue("restart_instance", bypass_running_check=True)
        self.assertEqual(follow.state, "queued")

    def test_a_chain_counts_once_towards_the_cap(self):
        self._job("deploy_instance")
        for n in range(QUEUE_MAX_WAITING - 1):
            self._enqueue("backup_create", payload={"n": n})
        self.Job.enqueue_chain([
            {"host_id": self.host.id, "instance_id": self.inst.id,
             "job_type_code": "backup_create"},
            {"host_id": self.host.id, "instance_id": self.inst.id,
             "job_type_code": "backup_list"},
        ])
        with self.assertRaisesRegex(UserError, "waiting in its queue"):
            self._enqueue("restart_instance")


class TestHold(_QueueCase):
    """A failure of a holding type holds the queue until its alert closes."""

    def test_a_failed_deploy_holds_the_queue(self):
        _deploy, alert = self._failed_with_alert("deploy_instance")
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart.state, "queued")
        row = restart._format()[0]
        self.assertTrue(row["queue_held"])
        self.assertTrue(row["requires_action"])
        self.assertEqual(row["queue_held_alert_id"], alert.id)
        self.assertFalse(self.Job._cron_dispatch_queued())
        self.assertEqual(restart.state, "queued")

    def test_dismissing_the_alert_releases_the_queue(self):
        _deploy, alert = self._failed_with_alert("deploy_instance")
        restart = self._enqueue("restart_instance")
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "queued", "held")
        alert.write({"state": "dismissed", "resolution": "manual"})
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "pending")

    def test_a_failure_of_a_non_holding_type_holds_nothing(self):
        self._failed_with_alert("backup_create")
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart.state, "pending")

    def test_the_failed_job_cannot_be_cancelled_to_release_it(self):
        deploy, _alert = self._failed_with_alert("deploy_instance")
        restart = self._enqueue("restart_instance")
        with self.assertRaises(UserError):
            deploy.cancel_job()
        self.assertTrue(self.Job._queue_held_by(self.inst.id, None))
        self.assertEqual(restart.state, "queued")

    def test_the_retry_passes_the_hold_and_its_success_releases_it(self):
        deploy, alert = self._failed_with_alert("deploy_instance")
        restart = self._enqueue("restart_instance")
        retry = self.Job.browse(deploy.retry_job())
        self.assertTrue(retry.queue_head)
        self.assertEqual(retry.state, "queued")
        _run_after_commit(self.env)
        self.assertEqual(retry.state, "pending")
        self.assertEqual(restart.state, "queued")
        with self.assertRaisesRegex(UserError, "already waiting"):
            deploy.retry_job()
        retry.queue_job_id.write({"state": "done"})
        self.assertEqual(alert.state, "dismissed")
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "pending")

    def test_a_retry_only_a_fresh_read_sees_refuses_a_second(self):
        """Two clicks at once: the first retry committed after the
        second request's snapshot."""
        deploy, _alert = self._failed_with_alert("deploy_instance")
        with patch.object(
            type(self.Job), "_fresh_rows", autospec=True,
            return_value=[{"id": _UNSEEN_ID}],
        ) as fresh, self.assertRaisesRegex(UserError, "already waiting"):
            deploy.retry_job()
        fresh.assert_called_once()
        self.assertFalse(self.Job.search([("retry_of_id", "=", deploy.id)]))

    def test_a_retry_while_another_job_runs_waits_at_the_head(self):
        deploy, _alert = self._failed_with_alert("deploy_instance")
        rebuild = self._job("rebuild_instance")
        restart = self._enqueue("restart_instance")
        retry = self.Job.browse(deploy.retry_job())
        self.assertEqual(retry.state, "queued")
        with self.assertRaisesRegex(UserError, "already waiting"):
            deploy.retry_job()
        rebuild.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(retry.state, "pending")
        self.assertEqual(restart.state, "queued")

    def test_the_lists_do_not_call_a_waiting_retry_of_the_failure_held(self):
        """The retry waits for the running job alone; the restart, for the alert."""
        deploy, _alert = self._failed_with_alert("deploy_instance")
        self._job("rebuild_instance")
        restart = self._enqueue("restart_instance")
        retry = self.Job.browse(deploy.retry_job())
        self.assertEqual(retry.state, "queued")
        self.assertFalse(retry._format()[0]["queue_held"])
        self.assertTrue(restart._format()[0]["queue_held"])

    def test_the_lists_do_not_call_a_waiting_delete_held(self):
        """A delete waits for the running job alone; a restart, for the alert."""
        self._failed_with_alert("deploy_instance")
        self._job("backup_create")
        delete = self._enqueue("delete_instance")
        restart = self._enqueue("restart_instance")
        self.assertEqual(delete.state, "queued")
        self.assertEqual(restart.state, "queued")
        self.assertFalse(delete._format()[0]["queue_held"])
        self.assertTrue(restart._format()[0]["queue_held"])

    def test_a_critical_conflict_alert_holds_like_the_failure_would(self):
        """``_create_job_failed_alert`` raises nothing for a job that
        failed on a critical conflict of its own; that alert holds."""
        deploy = self._job("deploy_instance", state="failed")
        self.env["cloud.alert"].sudo().raise_alert(
            "addon_conflict", "two repositories define web_x",
            level="critical", instance=self.inst, job=deploy,
        )
        self.assertFalse(self.Job._create_job_failed_alert(deploy, "boom"))
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart.state, "queued")
        self.assertTrue(self.Job._queue_held_by(self.inst.id, None))

    def test_a_delete_passes_the_hold(self):
        """The delete waits for the running job alone (spec §8)."""
        self._failed_with_alert("deploy_instance")
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart.state, "queued")
        delete = self._enqueue("delete_instance")
        self.assertEqual(restart.state, "cancelled")
        self.assertEqual(delete.state, "pending")

    def test_a_delete_behind_a_running_job_passes_the_hold_when_it_ends(self):
        self._failed_with_alert("deploy_instance")
        running = self._job("backup_create")
        delete = self._enqueue("delete_instance")
        self.assertEqual(delete.state, "queued")
        running.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(delete.state, "pending")

    def test_only_the_retry_of_the_holding_failure_passes(self):
        """A retry of some other failed job waits like everything else."""
        deploy, alert = self._failed_with_alert("deploy_instance")
        backup, _other = self._failed_with_alert("backup_create")
        backup_retry = self.Job.browse(backup.retry_job())
        self.assertTrue(backup_retry.queue_head)
        self.assertEqual(backup_retry.state, "queued")
        deploy_retry = self.Job.browse(deploy.retry_job())
        _run_after_commit(self.env)
        self.assertEqual(deploy_retry.state, "pending")
        self.assertEqual(backup_retry.state, "queued")
        self._end(deploy_retry)
        self.assertEqual(alert.state, "dismissed")
        _run_after_commit(self.env)
        self.assertEqual(backup_retry.state, "pending")

    def test_a_host_level_failure_holds_the_host_not_its_instances(self):
        job_type = self._type("host_hardening")
        job_type.failure_holds_queue = True
        self._failed_with_alert("host_hardening", inst=False)
        setup = self._enqueue("full_setup", inst=False)
        self.assertEqual(setup.state, "queued")
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart.state, "pending")


@tagged("-at_install", "post_install")
class TestOnlyThePlatform(_QueueCase):
    """What only the platform may ask of a queue (``_check_internal_queue_flags``).

    After every module is installed: the person asking needs a partner,
    and a module installed after this one may add a column to it that
    only its own defaults fill.
    """

    def test_only_the_platform_goes_to_the_head_or_past_the_hold(self):
        """``enqueue`` and ``enqueue_chain`` are reachable through ``call_kw``:
        a manager without superuser rights is refused the head of the queue
        and the move rollback's pass, and queues nothing; with ``sudo``,
        as the platform's own callers do, both are honoured."""
        self._failed_with_alert("deploy_instance")
        manager = new_test_user(
            self.env, login="queue-flags-manager",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        self.project.member_ids = [(4, manager.id)]
        Job = self.Job.with_user(manager)
        before = self.Job.search_count([("instance_id", "=", self.inst.id)])
        step = {
            "host_id": self.host.id, "instance_id": self.inst.id,
            "job_type_code": "restart_instance",
        }
        for refused in (
            lambda: Job.enqueue(
                self.host.id, self.inst.id, "restart_instance",
                bypass_running_check=True,
            ),
            lambda: Job.enqueue(
                self.host.id, self.inst.id, "restart_instance",
                payload={"move_rollback": True},
            ),
            lambda: Job.enqueue_chain([step], bypass_running_check=True),
            lambda: Job.enqueue_chain([step | {"payload": {"move_rollback": True}}]),
        ):
            with self.assertRaises(AccessError):
                refused()
        self.assertEqual(
            self.Job.search_count([("instance_id", "=", self.inst.id)]), before,
        )
        waiting = self.Job.browse(
            Job.enqueue(self.host.id, self.inst.id, "restart_instance"),
        )
        self.assertFalse(waiting.queue_head)
        self.assertTrue(waiting._format()[0]["queue_held"])
        head = self.Job.browse(Job.sudo().enqueue(
            self.host.id, self.inst.id, "backup_create", bypass_running_check=True,
        ))
        self.assertTrue(head.queue_head)
        self.assertEqual(head.create_uid, manager)

    def test_a_job_cannot_be_created_straight_into_the_queue(self):
        """A consultant cannot ``create`` a job with the queue's fields."""
        Job = self.Job.with_user(self._consultant("queue-direct-create"))
        target = {"name": "x", "host_id": self.host.id, "instance_id": self.inst.id}
        before = self.Job.search_count([("instance_id", "=", self.inst.id)])
        for vals in (
            {"job_type_id": self._type("delete_instance").id, "state": "queued"},
            {
                "job_type_id": self._type("restore_instance").id,
                "state": "queued",
                "queue_head": True,
                "payload": {"move_rollback": True},
            },
        ):
            with self.subTest(vals=vals), self.assertRaises(AccessError):
                Job.create(target | vals)
        self.assertEqual(
            self.Job.search_count([("instance_id", "=", self.inst.id)]), before,
        )

    def test_the_queue_fields_of_a_job_cannot_be_written_directly(self):
        """A consultant can neither reorder, change nor requeue a job by ``write``."""
        consultant = self._consultant("queue-direct-write")
        self._job("rebuild_instance")
        waiting = self._enqueue("restart_instance")
        self.assertEqual(waiting.state, "queued")
        failed = self._job("restart_instance", state="failed")
        for job, vals in (
            (waiting, {"state": "pending"}),
            (waiting, {"queue_head": True}),
            (waiting, {"payload": {"move_rollback": True}}),
            (waiting, {"job_type_id": self._type("delete_instance").id}),
            (waiting, {"asked_as_superuser": True}),
            (failed, {"state": "queued"}),
            (failed, {"date_done": fields.Datetime.now()}),
        ):
            with self.subTest(vals=vals), self.assertRaises(AccessError):
                job.with_user(consultant).write(vals)
        # What only describes the job stays theirs to write.
        waiting.with_user(consultant).write({"name": "renamed"})
        self.assertEqual(waiting.state, "queued")
        self.assertFalse(waiting.queue_head)
        self.assertFalse(waiting.payload)
        self.assertEqual(waiting.job_type_id, self._type("restart_instance"))
        self.assertEqual(failed.state, "failed")
        self.assertFalse(failed.date_done)

    def test_a_consultant_still_queues_retries_and_cancels_their_jobs(self):
        """The queue's entrypoints work for a consultant, the jobs' creator."""
        consultant = self._consultant("queue-entrypoints")
        Job = self.Job.with_user(consultant)
        running = self._job("rebuild_instance")
        waiting = self.Job.browse(
            Job.enqueue(self.host.id, self.inst.id, "restart_instance"),
        )
        self.assertEqual(waiting.state, "queued")
        self.assertEqual(waiting.create_uid, consultant)
        waiting.with_user(consultant).cancel_job()
        self.assertEqual(waiting.state, "cancelled")
        failed = self._job("restart_instance", state="failed")
        retry = self.Job.browse(failed.with_user(consultant).retry_job())
        self.assertEqual(retry.state, "queued")
        self.assertTrue(retry.queue_head)
        self.assertEqual(retry.create_uid, consultant)
        running.with_user(consultant).cancel_job()
        self.assertEqual(running.state, "cancelled")

    def test_a_consultant_unblocks_a_job_waiting_for_an_alert_and_only_that(self):
        """``unblock_and_enqueue`` queues a blocked job, never one that ended."""
        consultant = self._consultant("queue-unblock")
        alert = self.env["cloud.alert"].sudo().raise_alert(
            "addon_conflict", "two repositories define web_x",
            level="critical", instance=self.inst,
        )
        dropped = self._job("rebuild_instance", state=False, blocked_alert_id=alert.id)
        dropped.with_user(consultant).cancel_job()
        self.assertEqual(dropped.state, "cancelled")
        self.assertFalse(dropped.blocked_alert_id)
        done = self._job("rebuild_instance", state="done")
        with self.assertRaises(UserError):
            done.with_user(consultant).unblock_and_enqueue()
        self.assertEqual(done.state, "done")
        blocked = self._job("rebuild_instance", state=False, blocked_alert_id=alert.id)
        blocked.with_user(consultant).unblock_and_enqueue()
        self.assertFalse(blocked.blocked_alert_id)
        self.assertEqual(blocked.state, "queued")

    def test_the_mirror_of_a_queue_job_ended_without_superuser_rights(self):
        """A queue.job written outside ``sudo`` (one of 1.0.197) still moves its job."""
        consultant = self._consultant(
            "queue-mirror", "queue_job.group_queue_job_manager",
        )
        job = self._enqueue("restart_instance")
        self.assertEqual(job.state, "pending")
        job.queue_job_id.with_user(consultant).write({"state": "done"})
        self.assertEqual(job.state, "done")


@tagged("-at_install", "post_install")
class TestTheRightsAJobRunsWith(_QueueCase):
    """A job runs as its creator, with superuser rights only if asked with them.

    As until 1.0.197, when ``enqueue`` handed queue_job the caller's own
    environment: the dispatcher runs as the platform, and gives the job
    its creator and ``asked_as_superuser`` (``_dispatch_single``). A
    consultant's job runs under their record rules.
    """

    def _run(self, job, executor_cls=RestartInstanceExecutor, fails=None, **patches):
        """Run *job* as queue_job's runner does, on the recordset its queue.job keeps.

        The executor's ``run`` only notes the environment it runs in, and
        raises *fails* if given; *patches* replace more of the executor
        (``name=patch keyword arguments``).

        :return: ``(uid, su)`` of the environment the executor ran in
        """
        seen = []

        def run(executor):
            """Note the environment the executor runs its job in."""
            seen.append((executor.env.uid, executor.env.su))
            if fails:
                raise fails

        runner = job.queue_job_id.records
        # ``execute`` clears the environment at its end.
        self.env.flush_all()
        with contextlib.ExitStack() as stack:
            for name, kwargs in patches.items():
                stack.enter_context(
                    patch.object(executor_cls, name, autospec=True, **kwargs),
                )
            stack.enter_context(patch.object(
                executor_cls, "run", autospec=True, side_effect=run,
            ))
            runner.execute()
        self.assertEqual(len(seen), 1)
        return seen[0]

    def test_a_consultants_job_runs_as_them_without_superuser_rights(self):
        """Asked for through ``call_kw``, it runs under their record rules."""
        consultant = self._consultant("rights-consultant")
        job = self.Job.browse(self.Job.with_user(consultant).enqueue(
            self.host.id, self.inst.id, "restart_instance",
        ))
        self.assertEqual(job.state, "pending")
        self.assertFalse(job.asked_as_superuser)
        self.assertEqual(self._run(job), (consultant.id, False))

    def test_what_is_asked_for_with_sudo_runs_as_the_superuser(self):
        """The platform's code asks with ``sudo``; the creator stays the person."""
        consultant = self._consultant("rights-sudo")
        job = self.Job.browse(self.Job.with_user(consultant).sudo().enqueue(
            self.host.id, self.inst.id, "restart_instance",
        ))
        self.assertTrue(job.asked_as_superuser)
        self.assertEqual(job.create_uid, consultant)
        self.assertEqual(self._run(job), (consultant.id, True))

    def test_a_job_that_waited_keeps_the_rights_it_was_asked_with(self):
        """Dispatched later by the platform's dispatcher, still the consultant's."""
        consultant = self._consultant("rights-waited")
        running = self._job("rebuild_instance")
        job = self.Job.browse(self.Job.with_user(consultant).enqueue(
            self.host.id, self.inst.id, "restart_instance",
        ))
        self.assertEqual(job.state, "queued")
        _run_after_commit(self.env)
        self._end(running)
        _run_after_commit(self.env)
        self.assertEqual(job.state, "pending")
        self.assertEqual(self._run(job), (consultant.id, False))

    def test_every_step_of_a_consultants_chain_runs_without_superuser_rights(self):
        """A chain is dispatched whole, each step as it was asked for."""
        consultant = self._consultant("rights-chain")
        steps = self.Job.browse(self.Job.with_user(consultant).enqueue_chain([
            {"host_id": self.host.id, "instance_id": self.inst.id,
             "job_type_code": code}
            for code in ("stop_instance", "start_instance")
        ]))
        self.assertEqual(steps.mapped("state"), ["pending", "wait_dependencies"])
        for step in steps:
            records = step.queue_job_id.records
            self.assertEqual(records, step)
            self.assertEqual((records.env.uid, records.env.su), (consultant.id, False))

    def test_unblocked_by_a_consultant_a_job_runs_without_superuser_rights(self):
        """Whoever sets a blocked job going decides; the creator stays."""
        consultant = self._consultant("rights-unblock")
        alert = self.env["cloud.alert"].sudo().raise_alert(
            "addon_conflict", "two repositories define web_x",
            level="critical", instance=self.inst,
        )
        blocked = self.Job.with_user(consultant).sudo().create({
            "name": "rebuild_instance",
            "host_id": self.host.id,
            "instance_id": self.inst.id,
            "job_type_id": self._type("rebuild_instance").id,
            "blocked_alert_id": alert.id,
        })
        self.assertTrue(blocked.asked_as_superuser)
        blocked.with_user(consultant).unblock_and_enqueue()
        self.assertFalse(blocked.asked_as_superuser)
        _run_after_commit(self.env)
        self.assertEqual(blocked.state, "pending")
        records = blocked.queue_job_id.records
        self.assertEqual((records.env.uid, records.env.su), (consultant.id, False))

    def test_a_consultants_failure_raises_its_alert_and_holds_the_queue(self):
        """The failure is stored as the runner stores it, in the job's own rights."""
        consultant = self._consultant("rights-failure")
        Job = self.Job.with_user(consultant)
        job = self.Job.browse(Job.enqueue(
            self.host.id, self.inst.id, "rebuild_instance",
        ))
        self.assertEqual(job.state, "pending")
        with self.assertRaises(JobError):
            self._run(
                job, RebuildInstanceExecutor,
                fails=RuntimeError("the build broke"),
                pre_run_checks={},
                blocking_alert={"return_value": self.env["cloud.alert"]},
            )
        stored = QueueJob.load(self.env, job.queue_job_id.uuid)
        self.assertEqual(
            (stored.env.uid, stored.env.su), (consultant.id, False),
        )
        stored.set_failed(
            exc_name="JobError", exc_message="the build broke", exc_info="",
        )
        stored.store()
        self.assertEqual(job.state, "failed")
        alert = self.env["cloud.alert"].search([
            ("job_id", "=", job.id), ("code", "=", "job_failed"),
            ("state", "=", "active"),
        ])
        self.assertTrue(alert)
        self.assertEqual(self.Job._queue_held_by(self.inst.id, None), alert)
        _run_after_commit(self.env)
        waiting = self.Job.browse(Job.enqueue(
            self.host.id, self.inst.id, "restart_instance",
        ))
        _run_after_commit(self.env)
        self.assertEqual(waiting.state, "queued")


@tagged("-at_install", "post_install")
class TestAnotherProjectsQueue(_QueueCase):
    """A consultant outside the project never reaches its queue.

    ``enqueue`` reads the queue with ``sudo`` to collapse, merge and
    refuse; the caller's right to read the target is checked before
    (``_check_target_access``), and ``_create_queued`` checks the record
    rules on the rows it creates.
    """

    def setUp(self):
        """A rebuild running on the instance, and a consultant of another project."""
        super().setUp()
        self.running = self._job("rebuild_instance")
        self.outsider = self._consultant("queue-outsider", member=False)
        self.Outsider = self.Job.with_user(self.outsider)

    def _count(self):
        """How many jobs the instance has."""
        return self.Job.search_count([("instance_id", "=", self.inst.id)])

    def test_a_push_is_not_merged_into_another_projects_rebuild(self):
        """Refused before the merge: the waiting rebuild's pushes are as they were."""
        push = {
            "trigger": "webhook", "push_repo": "acme/addons",
            "push_branch": "main", "push_sha": "a" * 40,
            "push_message": "fix", "push_by": "dev",
        }
        waiting = self._enqueue("rebuild_instance", payload=push)
        self.assertEqual(waiting.state, "queued")
        before = self._count()
        with self.assertRaises(AccessError):
            self.Outsider.enqueue(
                self.host.id, self.inst.id, "rebuild_instance",
                payload=push | {"push_sha": "b" * 40, "push_by": "outsider"},
            )
        self.assertEqual(self._count(), before)
        self.assertEqual(waiting.payload, push)

    def test_no_waiting_job_of_another_project_is_answered(self):
        """Collapse would have returned the waiting restart's id."""
        waiting = self._enqueue("restart_instance")
        self.assertEqual(waiting.state, "queued")
        before = self._count()
        for ask in (
            lambda: self.Outsider.enqueue(
                self.host.id, self.inst.id, "restart_instance",
            ),
            lambda: self.Outsider.enqueue_chain([{
                "host_id": self.host.id, "instance_id": self.inst.id,
                "job_type_code": "restart_instance",
            }]),
        ):
            with self.assertRaises(AccessError):
                ask()
        self.assertEqual(self._count(), before)

    def test_a_restart_on_another_projects_instance_creates_nothing(self):
        """On a free instance, too: no row, nothing dispatched."""
        self._end(self.running)
        before = self._count()
        with self.assertRaises(AccessError):
            self.Outsider.enqueue(self.host.id, self.inst.id, "restart_instance")
        self.assertEqual(self._count(), before)

    def test_the_new_rows_are_held_to_the_record_rules(self):
        """``_create_queued`` creates with ``sudo``, then checks the rows."""
        before = self._count()
        with self.assertRaises(AccessError):
            self.Outsider._create_queued({
                "name": "restart_instance",
                "host_id": self.host.id,
                "instance_id": self.inst.id,
                "job_type_id": self._type("restart_instance").id,
                "state": "queued",
            })
        self.assertEqual(self._count(), before)


@tagged("-at_install", "post_install")
class TestTheHostOfAChainsStep(_QueueCase):
    """A step of a chain runs where its instance is, or where it moves.

    The dispatcher puts a lone job on its instance's host, but the steps
    of a chain keep the hosts they name, and ``cloud.host`` has no record
    rule: ``enqueue_chain`` holds them to the instance's host and its
    move's (``_check_step_host``).
    """

    def setUp(self):
        """Another host, ready to receive a move, and a consultant of the project."""
        super().setUp()
        self.other = self.env["cloud.host"].create({
            "name": "queue-other-host",
            "ip_address": "10.0.0.82",
            "user": "ubuntu",
            "wildcard_domain": "other.queue.example.com",
            "status": "compatible",
            "traefik_deployed": True,
        })
        self.consultant = self._consultant("queue-step-host")

    def _count(self):
        """How many jobs the instance has."""
        return self.Job.search_count([("instance_id", "=", self.inst.id)])

    def _deploy_on(self, host):
        """Ask, as the consultant, for a chain that deploys the instance on *host*.

        :return: the ids ``enqueue_chain`` returns
        """
        return self.Job.with_user(self.consultant).enqueue_chain([{
            "host_id": host.id, "instance_id": self.inst.id,
            "job_type_code": "deploy_instance",
        }])

    def test_a_consultants_instance_is_not_deployed_on_another_host(self):
        """Refused before anything is created."""
        before = self._count()
        with self.assertRaises(AccessError):
            self._deploy_on(self.other)
        self.assertEqual(self._count(), before)
        self.assertFalse(self.Job.search([("host_id", "=", self.other.id)]))

    def test_on_its_instances_host_the_chain_is_queued(self):
        """The host the instance is on is its own."""
        job = self.Job.browse(self._deploy_on(self.host))
        self.assertEqual(job.host_id, self.host)
        self.assertEqual(job.create_uid, self.consultant)

    def test_a_move_without_superuser_rights_names_both_hosts(self):
        """Its origin and destination are set before its chain is queued."""
        manager = new_test_user(
            self.env, login="queue-step-mover",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        self.project.member_ids = [(4, manager.id)]
        self.inst.with_user(manager).move_to_host(self.other)
        steps = self.Job.search([("instance_id", "=", self.inst.id)])
        self.assertEqual(steps.host_id, self.host | self.other)
        self.assertEqual(steps.create_uid, manager)


@tagged("-at_install", "post_install")
class TestTheSourceOfARestore(_QueueCase):
    """A restore reads the job it takes its data from as its creator may.

    Its job runs without superuser rights when it was asked for without
    them; ``exists`` alone would hand it a job of any project.
    """

    def setUp(self):
        """A download done in another project, and a consultant of this one."""
        super().setUp()
        elsewhere = self.env["cloud.project"].create({"name": "Elsewhere"})
        self.foreign = self._job(
            "backup_download", state="done",
            inst=self.env["cloud.instance"].create({
                "name": "queue-foreign", "project_id": elsewhere.id,
                "host_id": self.host.id, "environment": "production",
                "state": "deployed",
            }),
        )
        self.env["ir.attachment"].create({
            "name": "queue-foreign-backup-20261026.zip",
            "res_model": "cloud.job",
            "res_id": self.foreign.id,
            "raw": b"PK not really a zip",
        })
        self.consultant = self._consultant("queue-restorer")

    def _executor(self, mode, source):
        """The executor of a restore from *source*, as the consultant runs it."""
        job = self.Job.with_user(self.consultant)._create_queued({
            "name": "restore_instance",
            "host_id": self.host.id,
            "instance_id": self.inst.id,
            "job_type_id": self._type("restore_instance").id,
            "payload": {"mode": mode, "source_job_id": source.id},
            "state": "queued",
        })
        self.assertFalse(job.asked_as_superuser)
        return RestoreInstanceExecutor(job, self.host)

    def test_a_source_of_another_project_is_refused(self):
        """``from_host``: the source job is not resolved."""
        with self.assertRaises(AccessError):
            self._executor("from_host", self.foreign)._source_job()

    def test_its_archive_is_not_read(self):
        """``from_job``: refused before the archive is read or sent."""
        transport = MagicMock(spec=SSHTransport)
        with self.assertRaises(AccessError):
            asyncio.run(
                self._executor("from_job", self.foreign).before_execute(transport),
            )
        transport.upload_file.assert_not_called()

    def test_a_source_of_the_same_project_is_resolved(self):
        """What the consultant may read, the restore reads."""
        own = self._job("backup_download", state="done")
        self.assertEqual(self._executor("from_host", own)._source_job(), own)


class TestCancellation(_QueueCase):
    """Cancelled while it waits: at once, with a note, with the undo hook."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Optional modules may add NOT NULL columns to res_partner without
        # SQL defaults, which ``new_test_user`` trips on. Same workaround
        # as ``TestEnqueueJobTypeGate``.
        cls.env.cr.execute("""
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_name = 'res_partner'
               AND is_nullable = 'NO'
               AND column_default IS NULL
               AND column_name NOT IN ('id', 'name', 'company_type',
                                       'type', 'lang', 'active',
                                       'create_uid', 'write_uid',
                                       'create_date', 'write_date')
        """)
        for col, dtype in cls.env.cr.fetchall():
            default = "''" if 'char' in dtype or 'text' in dtype else "'no'"
            cls.env.cr.execute(
                f'ALTER TABLE res_partner '
                f'ALTER COLUMN "{col}" SET DEFAULT {default}'
            )

    def test_a_queued_job_is_cancelled_at_once_with_a_note(self):
        self._job("rebuild_instance")
        restart = self._enqueue("restart_instance")
        restart.cancel_job()
        self.assertEqual(restart.state, "cancelled")
        self.assertIn("Cancelled in the queue by", self._note(restart))

    def test_whoever_may_ask_for_the_type_may_cancel_it_nobody_else(self):
        self._job("rebuild_instance")
        restart = self._enqueue("restart_instance")
        viewer = new_test_user(
            self.env, login="queue-viewer",
            groups="base.group_user,incubacloud.group_cloud_user",
        )
        with self.assertRaises(AccessError):
            restart.with_user(viewer).cancel_job()
        self.assertEqual(restart.state, "queued")
        consultant = new_test_user(
            self.env, login="queue-consultant",
            groups="base.group_user,incubacloud.group_cloud_consultant",
        )
        self.project.member_ids = [(4, consultant.id)]
        restart.with_user(consultant).cancel_job()
        self.assertEqual(restart.state, "cancelled")

    def test_cancelling_the_waiting_job_serves_the_next(self):
        """A chain first in the staging's queue waits for production; once
        it is cancelled, what waited behind it on the staging starts."""
        prod = self._instance("queue-prod", environment="production")
        self._job("backup_create", inst=prod)
        staging = self._instance("queue-staging")
        chain = self.Job.browse(self.Job.enqueue_chain([
            {"host_id": self.host.id, "instance_id": prod.id,
             "job_type_code": "backup_download"},
            {"host_id": self.host.id, "instance_id": staging.id,
             "job_type_code": "restore_instance"},
        ]))
        restart = self._enqueue("restart_instance", inst=staging)
        _run_after_commit(self.env)
        self.assertEqual(set(chain.mapped("state")), {"queued"})
        self.assertEqual(restart.state, "queued", "behind the chain")
        chain[0].cancel_job()
        _run_after_commit(self.env)
        self.assertEqual(set(chain.mapped("state")), {"cancelled"})
        self.assertEqual(restart.state, "pending")

    def test_the_executor_hook_runs_when_a_queued_job_is_cancelled(self):
        self._job("rebuild_instance")
        restart = self._enqueue("restart_instance")
        executor_cls = executor_registry.get("restart_instance")
        with patch.object(
            executor_cls, "on_cancelled_queued", autospec=True,
        ) as hook:
            restart.cancel_job()
        hook.assert_called_once()
        self.assertEqual(hook.call_args.args[0].job, restart)

    def test_a_deploy_cancelled_in_the_queue_puts_the_instance_in_draft(self):
        draft = self._instance("queue-draft", state="draft")
        self._job("backup_create", inst=draft)
        draft._transition("deploying")
        deploy = self._enqueue("deploy_instance", inst=draft)
        self.assertEqual(deploy.state, "queued")
        deploy.cancel_job()
        self.assertEqual(draft.state, "draft")
        self.assertTrue(draft.exists())

    def test_a_delete_clears_the_queue_and_waits_for_the_running_job(self):
        rebuild = self._job("rebuild_instance")
        restart = self._enqueue("restart_instance")
        backup = self._enqueue("backup_create")
        delete = self._enqueue("delete_instance")
        self.assertEqual(restart.state, "cancelled")
        self.assertEqual(backup.state, "cancelled")
        self.assertIn("asked to be deleted", self._note(restart))
        self.assertEqual(delete.state, "queued")
        rebuild.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(delete.state, "pending")

    def test_a_delete_on_a_full_queue_is_let_in_and_empties_it(self):
        """A fresh read taken before the commit still sees waiting what
        the delete has just cancelled; this snapshot judges those."""
        self._job("rebuild_instance")
        waiting = self.Job.browse()
        for n in range(QUEUE_MAX_WAITING):
            waiting |= self._enqueue("backup_create", payload={"n": n})
        with self._fresh_read_before_commit(waiting) as fresh:
            delete = self._enqueue("delete_instance")
        fresh.assert_called()
        self.assertEqual(set(waiting.mapped("state")), {"cancelled"})
        self.assertEqual(delete.state, "queued")
        self.assertEqual(self.Job._queued_on((self.inst.id, None)), delete)

    def test_a_second_delete_replaces_the_one_waiting(self):
        """A delete asked for again cancels the one waiting and takes its place."""
        self._job("rebuild_instance")
        first = self._enqueue("delete_instance")
        self.assertEqual(first.state, "queued")
        with self._fresh_read_before_commit(first) as fresh:
            second = self._enqueue("delete_instance")
        fresh.assert_called()
        self.assertNotEqual(first, second)
        self.assertEqual(first.state, "cancelled")
        self.assertEqual(second.state, "queued")

    def test_an_archived_instance_cancels_its_waiting_job_at_dispatch(self):
        rebuild = self._job("rebuild_instance")
        restart = self._enqueue("restart_instance")
        self.inst.active = False
        rebuild.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "cancelled")
        self.assertIn("no longer there", self._note(restart))

    def test_a_job_asked_for_on_an_archived_instance_is_cancelled(self):
        """The fast path too: a restart has nothing left to act on."""
        self.inst.active = False
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart.state, "cancelled")
        self.assertIn("no longer there", self._note(restart))

    def test_the_purge_of_an_archived_instance_starts_at_once(self):
        """Its instance is archived by definition: not a reason to cancel."""
        self.inst.active = False
        purge = self._enqueue("purge_archived_backups")
        self.assertEqual(purge.state, "pending")
        self.assertTrue(purge.queue_job_uuid)

    def test_the_purge_of_an_archived_instance_is_served_from_the_queue(self):
        running = self._job("backup_create")
        self.inst.active = False
        purge = self._enqueue("purge_archived_backups")
        _run_after_commit(self.env)
        self.assertEqual(purge.state, "queued")
        running.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(purge.state, "pending")
        self.assertTrue(purge.queue_job_uuid)

    def test_unlinking_the_instance_cancels_what_waited(self):
        draft = self._instance("queue-gone", state="draft")
        self._job("backup_create", inst=draft)
        waiting = self._enqueue("grant_restore_upload_key", inst=draft)
        self.assertEqual(waiting.state, "queued")
        draft.unlink()
        self.assertEqual(waiting.state, "cancelled")
        self.assertIn("no longer there", self._note(waiting))

    def test_a_waiting_job_starts_on_the_host_the_instance_moved_to(self):
        other = self.env["cloud.host"].create({
            "name": "queue-host-2",
            "ip_address": "10.0.0.82",
            "user": "ubuntu",
            "wildcard_domain": "queue2.example.com",
        })
        rebuild = self._job("rebuild_instance")
        restart = self._enqueue("restart_instance")
        self.inst.host_id = other
        rebuild.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "pending")
        self.assertEqual(restart.host_id, other)


class TestSweep(_QueueCase):

    def test_the_sweep_serves_a_queue_nothing_dispatched(self):
        lost = self._job("restart_instance", state="queued")
        self.assertEqual(self.Job._cron_dispatch_queued(), 1)
        # Served through a cursor of its own.
        self.env.invalidate_all()
        self.assertEqual(lost.state, "pending")

    def test_the_sweep_leaves_a_busy_queue_alone(self):
        self._job("rebuild_instance")
        waiting = self._job("restart_instance", state="queued")
        self.assertEqual(self.Job._cron_dispatch_queued(), 0)
        self.assertEqual(waiting.state, "queued")


class TestChains(_QueueCase):
    """A chain is one entry, dispatched whole, cancelled whole."""

    def _chain(self, codes, inst=None):
        """``enqueue_chain`` *codes* on *inst* (the staging) and return the steps."""
        steps = [
            {"host_id": self.host.id, "instance_id": (inst or self.inst).id,
             "job_type_code": code}
            for code in codes
        ]
        return self.Job.browse(self.Job.enqueue_chain(steps))

    def test_a_chain_waits_as_one_entry_and_is_dispatched_whole(self):
        rebuild = self._job("rebuild_instance")
        chain = self._chain(["backup_create", "backup_list"])
        self.assertEqual(set(chain.mapped("state")), {"queued"})
        self.assertEqual(len(set(chain.mapped("queue_chain_key"))), 1)
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart._format()[0]["queue_position"], 1)
        rebuild.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(chain.mapped("state"), ["pending", "wait_dependencies"])
        self.assertTrue(all(chain.mapped("queue_job_uuid")))
        self.assertEqual(restart.state, "queued")
        # The first step's end moves nothing: the chain still runs.
        self._end(chain[0])
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "queued")
        self._end(chain[1])
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "pending")

    def test_a_free_instance_dispatches_the_chain_at_once(self):
        chain = self._chain(["backup_create", "backup_list"])
        self.assertEqual(chain.mapped("state"), ["pending", "wait_dependencies"])
        self.assertEqual(
            chain[0].queue_job_id.graph_uuid, chain[1].queue_job_id.graph_uuid,
        )

    def test_chain_references_are_resolved_at_dispatch(self):
        running = self._job("rebuild_instance")
        chain = self.Job.browse(self.Job.enqueue_chain([
            {"host_id": self.host.id, "instance_id": self.inst.id,
             "job_type_code": "backup_download",
             "payload": {"time": "latest"}},
            {"host_id": self.host.id, "instance_id": self.inst.id,
             "job_type_code": "restore_instance",
             "payload": {"source_job_id": "__chain_job_0__"}},
        ]))
        self.assertEqual(chain[1].payload["source_job_id"], "__chain_job_0__")
        running.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(chain[1].payload["source_job_id"], chain[0].id)

    def test_cancelling_one_step_cancels_the_chain(self):
        self._job("rebuild_instance")
        chain = self._chain(["backup_create", "backup_download", "restore_instance"])
        chain[1].cancel_job()
        self.assertEqual(set(chain.mapped("state")), {"cancelled"})

    def test_a_dispatched_chain_cancelled_before_it_started_frees_the_queue(self):
        """``cancel_job`` on the pending head: queue_job cancels the next
        steps' queue.jobs with SQL, which nothing mirrors; the mirror of
        the head's end ends their cloud jobs too, tells the layers, and
        the queue moves on."""
        chain = self._chain(["backup_create", "backup_list"])
        self.assertEqual(chain.mapped("state"), ["pending", "wait_dependencies"])
        restart = self._enqueue("restart_instance")
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "queued")
        with patch.object(
            type(self.Job), "_after_job_terminal", autospec=True,
        ) as hook:
            chain[0].cancel_job()
        self.assertEqual(set(chain.mapped("state")), {"cancelled"})
        self.assertFalse(self.Job._scope_busy(self.inst.id, None))
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "pending")
        self.assertEqual(
            sorted((c.args[0].id, c.args[1]) for c in hook.call_args_list),
            [(chain[0].id, "cancelled"), (chain[1].id, "cancelled")],
        )

    def test_a_step_after_one_that_failed_ends_cancelled_not_failed(self):
        """Only the step that failed is a failure. The next one never ran:
        it ends cancelled, says why in its log, and offers no retry that
        would run it out of its chain."""
        chain = self._chain(["backup_create", "backup_list"])
        first, second = chain
        first.queue_job_id.write({"state": "started"})
        with patch.object(
            type(self.Job), "_after_job_terminal", autospec=True,
        ) as hook:
            first.queue_job_id.write({"state": "failed", "exc_message": "boom"})
        self.assertEqual(first.state, "failed")
        self.assertEqual(second.state, "cancelled")
        self.assertEqual(second.queue_job_id.state, "cancelled")
        self.assertEqual(
            sorted((c.args[0].id, c.args[1]) for c in hook.call_args_list),
            [(first.id, "failed"), (second.id, "cancelled")],
        )
        self.assertEqual(
            self._note(second), f"Not run: step #{first.id} ended failed.",
        )
        with self.assertRaisesRegex(UserError, "Only failed jobs"):
            second.retry_job()

    def test_a_probe_that_fails_leaves_a_chain_on_its_instance_alone(self):
        """A hidden job runs beside the chain, outside it: its end is not
        the end of steps that happen to have higher ids."""
        probe = self._enqueue("instance_health")
        chain = self._chain(["backup_create", "backup_list"])
        self.assertLess(probe.id, chain[0].id)
        self.assertEqual(chain.mapped("state"), ["pending", "wait_dependencies"])
        probe.queue_job_id.write({"state": "failed", "exc_message": "boom"})
        self.assertEqual(probe.state, "failed")
        self.assertEqual(chain.mapped("state"), ["pending", "wait_dependencies"])
        self.assertEqual(chain[1].queue_job_id.state, "wait_dependencies")

    def test_a_chain_across_instances_waits_in_every_queue(self):
        prod = self._instance("queue-prod", environment="production")
        prod_backup = self._job("backup_create", inst=prod)
        staging = self._instance("queue-staging", state="draft")
        chain = self.Job.browse(self.Job.enqueue_chain([
            {"host_id": self.host.id, "instance_id": staging.id,
             "job_type_code": "deploy_instance"},
            {"host_id": self.host.id, "instance_id": prod.id,
             "job_type_code": "backup_download"},
            {"host_id": self.host.id, "instance_id": staging.id,
             "job_type_code": "restore_instance"},
        ]))
        self.assertEqual(set(chain.mapped("state")), {"queued"})
        # The staging is free, but the chain waits for production; a
        # restart asked for on the staging waits behind the chain.
        restart = self._enqueue("restart_instance", inst=staging)
        self.assertEqual(restart.state, "queued")
        self.assertEqual(restart._format()[0]["queue_position"], 1)
        self.assertEqual(self.Job._cron_dispatch_queued(), 0)
        prod_backup.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(
            chain.mapped("state"),
            ["pending", "wait_dependencies", "wait_dependencies"],
        )
        self.assertEqual(restart.state, "queued")
        # Both instances are occupied by the chain until its last step
        # ends: the staging while its download runs on production, and
        # production while the restore runs on the staging.
        self._end(chain[0])
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "queued")
        self.assertTrue(self.Job._scope_busy(prod.id, None))
        self._end(chain[1])
        _run_after_commit(self.env)
        self.assertTrue(self.Job._scope_busy(prod.id, None))
        self.assertTrue(self.Job._scope_busy(staging.id, None))
        self._end(chain[2])
        _run_after_commit(self.env)
        self.assertFalse(self.Job._scope_busy(prod.id, None))
        self.assertEqual(restart.state, "pending")

    def test_a_chain_to_the_head_goes_before_what_waits(self):
        rebuild = self._job("rebuild_instance")
        restart = self._enqueue("restart_instance")
        chain = self.Job.browse(self.Job.enqueue_chain([
            {"host_id": self.host.id, "instance_id": self.inst.id,
             "job_type_code": "backup_create"},
            {"host_id": self.host.id, "instance_id": self.inst.id,
             "job_type_code": "backup_list"},
        ], bypass_running_check=True))
        rebuild.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(chain[0].state, "pending")
        self.assertEqual(restart.state, "queued")


class TestMoves(_QueueCase):
    """A move's chain in the queue, its cancellation and its rollback."""

    def setUp(self):
        super().setUp()
        self.target = self.env["cloud.host"].create({
            "name": "queue-target",
            "ip_address": "10.0.0.83",
            "user": "ubuntu",
            "wildcard_domain": "target.example.com",
            "status": "compatible",
            "traefik_deployed": True,
        })

    def test_a_move_cancelled_in_the_queue_leaves_no_marker(self):
        self._job("rebuild_instance")
        self.inst.move_to_host(self.target)
        chain = self.inst._get_move_chain_jobs()
        self.assertEqual(set(chain.mapped("state")), {"queued"})
        self.assertTrue(self.inst.move_origin_host_id)
        with patch.object(
            type(self.inst), "_on_move_rolled_back", autospec=True,
        ) as undone:
            chain[0].cancel_job()
        self.assertEqual(set(chain.mapped("state")), {"cancelled"})
        self.assertFalse(self.inst.move_origin_host_id)
        self.assertFalse(self.inst.move_target_host_id)
        self.assertFalse(self.inst.move_chain_job_ids)
        undone.assert_called_once()
        self.assertEqual(undone.call_args.kwargs["target_host"], self.target)
        # Nothing for the stuck-move watchdog to recover.
        self.env["cloud.instance"].cron_recover_stuck_moves()
        self.assertFalse(self.env["cloud.alert"].search([
            ("instance_id", "=", self.inst.id), ("code", "=", "move_stuck"),
        ]))

    def test_rolling_back_a_move_still_waiting_undoes_it_without_jobs(self):
        self._job("rebuild_instance")
        self.inst.move_to_host(self.target)
        result = self.inst.rollback_move()
        self.assertEqual(result, {"ok": True, "job_id": None})
        self.assertFalse(self.inst.move_origin_host_id)
        self.assertFalse(self.inst.move_rollback_in_progress)

    def test_the_cutover_hook_leaves_a_rollback_in_progress_alone(self):
        self.inst.write({
            "move_origin_host_id": self.host.id,
            "move_target_host_id": self.target.id,
            "move_rollback_in_progress": True,
        })
        cutover = self._job("move_cutover", state="queued", host=self.target)
        self.inst.move_chain_job_ids = str(cutover.id)
        MoveCutoverExecutor(cutover, self.target).on_cancelled_queued()
        self.assertEqual(self.inst.move_origin_host_id, self.host)

    def test_the_rollback_of_a_failed_move_passes_the_hold(self):
        """A step that holds the queue failed after the source was
        stopped: the watchdog's rollback is the recovery, and starts
        without waiting for the alert to be dismissed. The alert stays
        open, and the queue stays held for everything else."""
        self.inst.write({
            "move_origin_host_id": self.host.id,
            "move_target_host_id": self.target.id,
        })
        cutover, alert = self._failed_with_alert("move_cutover")
        self.inst.move_chain_job_ids = str(cutover.id)
        self.env["cloud.instance"].cron_recover_stuck_moves()
        start, cleanup = self.Job.search([
            ("instance_id", "=", self.inst.id),
            ("job_type_id.code", "in", ("start_instance", "move_rollback_cleanup")),
        ], order="id")
        _run_after_commit(self.env)
        self.assertEqual(start.job_type_id.code, "start_instance")
        self.assertEqual(start.state, "pending")
        self.assertEqual(cleanup.state, "queued")
        restart = self._enqueue("restart_instance")
        # The lists say so: the cleanup waits for the start alone, the
        # restart for the alert.
        self.assertFalse(cleanup._format()[0]["queue_held"])
        self.assertFalse(cleanup._format()[0]["requires_action"])
        self.assertTrue(restart._format()[0]["queue_held"])
        self._end(start)
        _run_after_commit(self.env)
        self.assertEqual(cleanup.state, "pending")
        self.assertEqual(restart.state, "queued")
        self.assertEqual(alert.state, "active")
        self.assertEqual(self.Job._queue_held_by(self.inst.id, None), alert)

    def _rollback_jobs(self):
        """The jobs a rollback of the move queued on the instance, oldest first."""
        return self.Job.search([
            ("instance_id", "=", self.inst.id),
            ("job_type_id.code", "in", ("start_instance", "move_rollback_cleanup")),
        ], order="id")

    def test_a_job_asked_for_after_a_failed_move_does_not_stop_its_rollback(self):
        """A restart asked for after the failure waits behind its hold;
        it is not the move going on, and the watchdog rolls back."""
        self.inst.write({
            "move_origin_host_id": self.host.id,
            "move_target_host_id": self.target.id,
        })
        cutover, alert = self._failed_with_alert("move_cutover")
        self.inst.move_chain_job_ids = str(cutover.id)
        restart = self._enqueue("restart_instance")
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "queued")
        self.env["cloud.instance"].cron_recover_stuck_moves()
        _run_after_commit(self.env)
        start, cleanup = self._rollback_jobs()
        self.assertEqual(start.job_type_id.code, "start_instance")
        self.assertEqual(start.state, "pending")
        self.assertEqual(cleanup.state, "queued")
        self.assertEqual(restart.state, "queued")
        self.assertEqual(alert.state, "active")

    def test_a_rollback_already_waiting_is_not_asked_for_twice(self):
        """The watchdog finds the rollback waiting in the queue and queues no other."""
        self.inst.write({
            "move_origin_host_id": self.host.id,
            "move_target_host_id": self.target.id,
            "move_rollback_in_progress": True,
        })
        cutover, _alert = self._failed_with_alert("move_cutover")
        self.inst.move_chain_job_ids = str(cutover.id)
        waiting = self._job(
            "move_rollback_cleanup", state="queued", host=self.target,
            queue_head=True, payload={"move_rollback": True},
        )
        self.env["cloud.instance"].cron_recover_stuck_moves()
        self.assertEqual(self._rollback_jobs(), waiting)
        self.assertTrue(self.inst.move_rollback_in_progress)
        self.assertEqual(self.inst.move_origin_host_id, self.host)
        self.assertFalse(self.env["cloud.alert"].search([
            ("instance_id", "=", self.inst.id),
            ("code", "in", ("move_stuck", "move_rollback_failed")),
        ]))

    def test_a_move_waiting_its_turn_is_not_rolled_back(self):
        """Its chain waits behind a hold left by another failure: the move
        has not started, and the source still runs."""
        self._failed_with_alert("deploy_instance")
        self.inst.move_to_host(self.target)
        chain = self.inst._get_move_chain_jobs()
        _run_after_commit(self.env)
        self.assertEqual(set(chain.mapped("state")), {"queued"})
        self.env["cloud.instance"].cron_recover_stuck_moves()
        self.assertEqual(set(chain.mapped("state")), {"queued"})
        self.assertFalse(self._rollback_jobs())
        self.assertEqual(self.inst.move_target_host_id, self.target)

    def test_the_rollback_jobs_go_to_the_head_and_wait_for_the_stopped_step(self):
        """Rolling back a move with a step running: the step is cancelled
        cooperatively, and start_instance then move_rollback_cleanup wait
        for it to stop, in that order, before anything a user queued."""
        step = self._enqueue("backup_download")
        step.queue_job_id.write({"state": "started"})
        self.inst.write({
            "move_origin_host_id": self.host.id,
            "move_target_host_id": self.target.id,
            "move_chain_job_ids": str(step.id),
        })
        user_restart = self._enqueue("restart_instance")
        result = self.inst.rollback_move()
        self.assertTrue(result["ok"])
        cleanup = self.Job.browse(result["job_id"])
        start = self.Job.search([
            ("instance_id", "=", self.inst.id),
            ("job_type_id.code", "=", "start_instance"),
        ])
        self.assertEqual(step.state, "cancelled")
        self.assertEqual((start.state, cleanup.state), ("queued", "queued"))
        self.assertTrue(start.queue_head and cleanup.queue_head)
        self.assertEqual(self.Job._cron_dispatch_queued(), 0, "the step still runs")
        # The executor stops on the flag and queue_job stores the end.
        self.env.flush_all()
        step.queue_job_id.write({
            "state": "done", "result": CANCELLED_WHILE_RUNNING,
        })
        _run_after_commit(self.env)
        self.assertEqual(start.state, "pending")
        self.assertEqual(cleanup.state, "queued")
        self.assertEqual(user_restart.state, "queued")
        self._end(start)
        _run_after_commit(self.env)
        self.assertEqual(cleanup.state, "pending")


class TestTheStoredName(_QueueCase):
    """A job's name and its audit action are stored in English; a reader
    sees the type's name in their own language."""

    def setUp(self):
        """Spanish active, with the rebuild's name written in it."""
        super().setUp()
        self.env["res.lang"]._activate_lang("es_ES")
        self.rebuild = self._type("rebuild_instance")
        self.english = self.rebuild.with_context(lang="en_US").name
        self.rebuild.with_context(lang="es_ES").name = "Reconstruir instancia"

    def _audit(self, jobs):
        """The audit actions recorded for *jobs*, in their order."""
        return [
            self.env["cloud.audit.log"].search([("job_id", "=", job.id)]).action
            for job in jobs
        ]

    def test_a_job_asked_for_in_spanish_is_stored_in_english(self):
        """Name and audit action in English; ``_format`` in the reader's language."""
        Job = self.Job.with_context(lang="es_ES")
        job = Job.browse(Job.enqueue(self.host.id, self.inst.id, "rebuild_instance"))
        self.assertEqual(job.name, self.english)
        self.assertEqual(self._audit(job), [self.english])
        self.assertEqual(
            job.with_context(lang="es_ES")._format()[0]["name"],
            "Reconstruir instancia",
        )
        self.assertEqual(
            job.with_context(lang="en_US")._format()[0]["name"], self.english,
        )

    def test_the_home_page_names_the_type_in_the_readers_language(self):
        """``/cloud/get_dashboard``'s recent jobs, read by a Spanish user."""
        job = self._enqueue("rebuild_instance")
        fake_request = MagicMock(spec=http.Request)
        fake_request.env = self.Job.with_context(lang="es_ES").env
        with (
            patch.object(http, "request", fake_request),
            patch.object(data_load, "request", fake_request),
            patch.object(_routes_crud, "request", fake_request),
        ):
            data = data_load.CloudDataLoadController().cloud_get_dashboard()
        row = next(row for row in data["recent_jobs"] if row["id"] == job.id)
        self.assertEqual(row["name"], "Reconstruir instancia")
        self.assertEqual(job.name, self.english)

    def test_a_chain_asked_for_in_spanish_is_stored_in_english(self):
        """Every step of a chain asked for in Spanish is stored in English."""
        Job = self.Job.with_context(lang="es_ES")
        chain = Job.browse(Job.enqueue_chain([
            {"host_id": self.host.id, "instance_id": self.inst.id,
             "job_type_code": "rebuild_instance"},
            {"host_id": self.host.id, "instance_id": self.inst.id,
             "job_type_code": "backup_create"},
        ]))
        backup = self._type("backup_create").with_context(lang="en_US").name
        self.assertEqual(chain.mapped("name"), [self.english, backup])
        self.assertEqual(self._audit(chain), [self.english, backup])


class TestReaders(_QueueCase):
    """Who counts a waiting job as occupation, and who does not."""

    def test_the_health_probe_ignores_a_waiting_rebuild(self):
        probe = self._job("instance_health", state="started")
        executor = InstanceHealthExecutor(probe, self.host)
        self._job("rebuild_instance", state="queued")
        self.assertFalse(executor._is_blocked_by_active_job())
        self._job("rebuild_instance", state="started")
        self.assertTrue(executor._is_blocked_by_active_job())

    def test_blocked_jobs_are_neither_waiting_nor_occupying(self):
        alert = self.env["cloud.alert"].sudo().raise_alert(
            "pip_conflict", "conflict", instance=self.inst,
        )
        blocked = self._job("deploy_instance", state=False, blocked_alert_id=alert.id)
        self.assertEqual(blocked._format()[0]["state"], "blocked")
        self.assertFalse(self.Job._scope_busy(self.inst.id, None))
        self.assertEqual(self.Job._queue_entries((self.inst.id, None)), [])

    def test_unblocking_queues_the_job_when_the_instance_is_busy(self):
        alert = self.env["cloud.alert"].sudo().raise_alert(
            "addon_conflict", "conflict", instance=self.inst,
        )
        blocked = self._job("rebuild_instance", state=False, blocked_alert_id=alert.id)
        running = self._job("backup_create")
        blocked.unblock_and_enqueue()
        self.assertEqual(blocked.state, "queued")
        self.assertFalse(blocked.blocked_alert_id)
        running.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(blocked.state, "pending")

    def test_the_upload_key_revocation_waits_its_turn(self):
        self._job("rebuild_instance")
        grant = self.env["cloud.restore.upload.grant"].create({
            "host_id": self.host.id,
            "instance_id": self.inst.id,
            "token": "queue-token",
            "public_key": "ssh-ed25519 AAAA queue",
            "expires_at": fields.Datetime.now() + timedelta(hours=1),
            "state": "granted",
        })
        job = self.Job.browse(grant._revoke())
        self.assertEqual(job.state, "queued")
        self.assertFalse(job.queue_head)

    def test_the_merge_hook_defaults_to_nothing(self):
        waiting = self._job("restart_instance", state="queued")
        self.assertFalse(AbstractExecutor._merge_queued_payload(waiting, {"a": 1}))


class TestDispatchAfterCommit(_QueueCase):
    """The queues move on fresh cursors once the event's transaction commits.

    The runner's transaction reads the snapshot it took when the job
    started, so it never dispatches what was queued meanwhile; and nothing
    the dispatcher or a layer's hook does decides how the job ended.
    """

    _LOGGER = "odoo.addons.incubacloud.models.cloud_job"

    def test_a_job_the_runner_ends_done_dispatches_the_next(self):
        rebuild = self._enqueue("rebuild_instance")
        restart = self._enqueue("restart_instance")
        rebuild.queue_job_id.write({"state": "started"})
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "queued")
        rebuild.queue_job_id.write({"state": "done"})
        self.assertEqual(restart.state, "queued", "not before the commit")
        _run_after_commit(self.env)
        self.assertEqual(rebuild.state, "done")
        self.assertEqual(restart.state, "pending")

    def test_a_job_the_runner_ends_failed_dispatches_the_next(self):
        backup = self._enqueue("backup_create")
        restart = self._enqueue("restart_instance")
        backup.queue_job_id.write({"state": "started"})
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "queued")
        backup.queue_job_id.write({"state": "failed", "exc_message": "boom"})
        self.assertEqual(restart.state, "queued", "not before the commit")
        _run_after_commit(self.env)
        self.assertEqual(backup.state, "failed")
        self.assertEqual(restart.state, "pending")

    def test_a_holding_failure_ended_by_the_runner_holds_the_queue(self):
        deploy = self._enqueue("deploy_instance")
        restart = self._enqueue("restart_instance")
        deploy.queue_job_id.write({"state": "started"})
        deploy.queue_job_id.write({"state": "failed", "exc_message": "boom"})
        _run_after_commit(self.env)
        self.assertEqual(deploy.state, "failed")
        self.assertEqual(restart.state, "queued")
        self.assertEqual(self.Job._queue_held_by(self.inst.id, None).job_id, deploy)

    def test_a_dispatcher_that_raises_leaves_the_end_as_it_was(self):
        rebuild = self._enqueue("rebuild_instance")
        restart = self._enqueue("restart_instance")
        rebuild.queue_job_id.write({"state": "started"})
        with (
            patch.object(
                type(self.Job), "_dispatch_next", autospec=True,
                side_effect=RuntimeError("dispatcher"),
            ) as dispatch,
            self.assertLogs(self._LOGGER, "ERROR") as logs,
        ):
            rebuild.queue_job_id.write({"state": "done"})
            dispatch.assert_not_called()
            _run_after_commit(self.env)
        dispatch.assert_called_once()
        self.assertEqual(rebuild.state, "done")
        self.assertEqual(rebuild.queue_job_id.state, "done")
        self.assertEqual(restart.state, "queued")
        self.assertIn(
            f"could not serve the queue of instance {self.inst.id}",
            "\n".join(logs.output),
        )
        # The sweep comes by.
        self.assertEqual(self.Job._cron_dispatch_queued(), 1)
        self.env.invalidate_all()
        self.assertEqual(restart.state, "pending")

    def test_a_layer_hook_that_raises_does_not_fail_a_job_that_succeeded(self):
        rebuild = self._enqueue("rebuild_instance")
        restart = self._enqueue("restart_instance")
        rebuild.queue_job_id.write({"state": "started"})
        with (
            patch.object(
                type(self.Job), "_after_job_terminal", autospec=True,
                side_effect=RuntimeError("layer"),
            ) as hook,
            self.assertLogs(self._LOGGER, "ERROR") as logs,
        ):
            rebuild.queue_job_id.write({"state": "done"})
        hook.assert_called_once_with(rebuild, "done")
        self.assertIn("_after_job_terminal(done) raised", "\n".join(logs.output))
        self.assertEqual(rebuild.state, "done")
        self.assertEqual(rebuild.queue_job_id.state, "done")
        self.assertFalse(self.env["cloud.alert"].search([
            ("job_id", "=", rebuild.id), ("code", "=", "job_failed"),
        ]))
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "pending")

    def test_a_target_busy_for_a_fresh_read_keeps_the_new_job_waiting(self):
        """This snapshot sees the instance free, a READ COMMITTED read
        sees it busy (a start committed after the snapshot was taken)."""
        with patch.object(
            type(self.Job), "_scope_quiet_fresh", autospec=True,
            return_value=False,
        ) as fresh:
            restart = self._enqueue("restart_instance")
        fresh.assert_called_once()
        self.assertEqual(restart.state, "queued")
        self.assertFalse(restart.queue_job_uuid)
        # The dispatch registered for after the commit serves it.
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "pending")

    def test_the_cap_counts_what_only_a_fresh_read_sees(self):
        """None waits in this snapshot; a fresh read sees a full queue
        that other requests committed after it."""
        self._job("deploy_instance")
        with patch.object(
            type(self.Job), "_fresh_rows", autospec=True,
            return_value=[
                {"id": _UNSEEN_ID - n, "queue_chain_key": False}
                for n in range(QUEUE_MAX_WAITING)
            ],
        ), self.assertRaisesRegex(UserError, "waiting in its queue"):
            self._enqueue("backup_create")

    def test_cancelling_in_the_queue_tells_the_layers_after_the_undo(self):
        self._job("rebuild_instance")
        restart = self._enqueue("restart_instance")
        executor_cls = executor_registry.get("restart_instance")
        calls = []
        with (
            patch.object(
                executor_cls, "on_cancelled_queued", autospec=True,
                side_effect=lambda executor: calls.append("undo"),
            ),
            patch.object(
                type(self.Job), "_after_job_terminal", autospec=True,
                side_effect=lambda job, state: calls.append(state),
            ) as hook,
        ):
            restart.cancel_job()
        hook.assert_called_once_with(restart, "cancelled")
        self.assertEqual(calls, ["undo", "cancelled"])

    def test_unblocking_tells_the_layers_of_the_twins_it_cancels(self):
        alert = self.env["cloud.alert"].sudo().raise_alert(
            "addon_conflict", "conflict", instance=self.inst,
        )
        blocked = self._job("rebuild_instance", state=False, blocked_alert_id=alert.id)
        twin = self._job("rebuild_instance", state=False, blocked_alert_id=alert.id)
        with patch.object(
            type(self.Job), "_after_job_terminal", autospec=True,
        ) as hook:
            blocked.unblock_and_enqueue()
        self.assertEqual(twin.state, "cancelled")
        hook.assert_called_once_with(twin, "cancelled")

    def test_a_queue_the_sweep_cannot_serve_does_not_stop_the_others(self):
        other = self._instance("queue-other")
        broken = self._job("restart_instance", state="queued")
        served = self._job("restart_instance", state="queued", inst=other)
        Job = type(self.Job)
        dispatch = Job._dispatch_next
        broken_id = self.inst.id

        def fail_on_one(job_model, instance_id=None, host_id=None):
            """Raise for the first instance's queue; serve the others."""
            if instance_id == broken_id:
                raise RuntimeError("this queue is broken")
            return dispatch(job_model, instance_id, host_id)

        with (
            patch.object(
                Job, "_dispatch_next", autospec=True, side_effect=fail_on_one,
            ),
            self.assertLogs(self._LOGGER, "ERROR") as logs,
        ):
            self.assertEqual(self.Job._cron_dispatch_queued(), 1)
        self.env.invalidate_all()
        self.assertEqual(broken.state, "queued")
        self.assertEqual(served.state, "pending")
        self.assertIn(
            f"could not serve the queue of instance {broken_id}",
            "\n".join(logs.output),
        )


class TestEnqueueAnswer(_QueueCase):
    """What an endpoint that enqueues answers, for the panel's notice.

    "Started: <type>" when the job was dispatched, "Queued behind
    <type running>" when it waits, and the conflict dialog's envelope
    when it waits on an alert (``_job_response``).
    """

    def _name(self, code):
        """The visible name of the job type *code*."""
        return self._type(code).name

    def test_a_job_that_started_names_its_type(self):
        """A job dispatched at once names its type and nothing it waits behind."""
        job = self._enqueue("rebuild_instance")
        answer = _job_response(self.env, job.id)
        self.assertEqual(answer["state"], "pending")
        self.assertEqual(answer["job_type"], self._name("rebuild_instance"))
        self.assertIs(answer["queued_behind"], False)

    def test_a_queued_job_names_the_job_running(self):
        """A job that waits names the type of the job running on the instance."""
        self._job("rebuild_instance")
        restart = self._enqueue("restart_instance")
        answer = _job_response(self.env, restart.id)
        self.assertEqual(answer["state"], "queued")
        self.assertEqual(answer["job_type"], self._name("restart_instance"))
        self.assertEqual(answer["queued_behind"], self._name("rebuild_instance"))
        self.assertNotIn("blocked", answer)

    def test_a_blocked_job_answers_with_its_alert(self):
        """A job that waits on a conflict answers the alert's envelope instead."""
        alert = self.inst._raise_addon_conflict_alert(
            [{"addon": "web_sheet_full_width", "repos": ["odoo-addons", "web"]}],
        )
        deploy = self._enqueue("deploy_instance")
        self.assertEqual(deploy.blocked_alert_id, alert)
        answer = _job_response(self.env, deploy.id)
        self.assertEqual(answer["state"], "blocked")
        self.assertTrue(answer["blocked"])
        self.assertEqual(answer["alert_id"], alert.id)
        self.assertEqual(answer["alert_code"], "addon_conflict")

    def test_the_job_started_is_named_before_the_steps_waiting(self):
        """A chain's step that runs, not the step that waits for it."""
        self._job("restore_instance", state="wait_dependencies")
        self._job("deploy_instance")
        restart = self._enqueue("restart_instance")
        self.assertEqual(
            _job_response(self.env, restart.id)["queued_behind"],
            self._name("deploy_instance"),
        )

    def test_a_hidden_job_is_never_what_a_job_waits_behind(self):
        """A health probe running beside the rebuild is not what is named."""
        self._job("instance_health")
        self._job("rebuild_instance", state="pending")
        restart = self._enqueue("restart_instance")
        self.assertEqual(
            _job_response(self.env, restart.id)["queued_behind"],
            self._name("rebuild_instance"),
        )

    def test_a_held_queue_names_nothing(self):
        """Nothing runs: the job waits for the failure to be dealt with."""
        self._failed_with_alert("deploy_instance")
        restart = self._enqueue("restart_instance")
        self.assertEqual(restart.state, "queued")
        self.assertIs(_job_response(self.env, restart.id)["queued_behind"], False)

    def test_the_history_says_where_a_queued_job_is(self):
        """The history's rows carry a waiting job's place, none for a running one."""
        rebuild = self._job("rebuild_instance")
        self._enqueue("restart_instance")
        backup = self._enqueue("backup_create")
        rows = {
            row["id"]: row
            for row in self.Job.load_history({"instance_id": self.inst.id})["jobs"]
        }
        self.assertEqual(rows[backup.id]["queue_position"], 1)
        self.assertIsNone(rows[rebuild.id]["queue_position"])


@tagged("-at_install", "post_install")
class TestJobStateRoute(HttpCase):
    """``/cloud/get_job_state``: the same answer, for a job queued by id.

    Driven over real HTTP: the buttons that enqueue through the ORM read
    it, and its access gate is the point. It answers, as the job's log
    page names the job, in the language of whoever asks.
    """

    def setUp(self):
        """A rebuild running and a restart waiting; a manager in each language."""
        super().setUp()
        self.env["res.lang"]._activate_lang("es_ES")
        host = self.env["cloud.host"].create({
            "name": "state-host",
            "ip_address": "10.0.0.82",
            "user": "ubuntu",
            "wildcard_domain": "state.example.com",
        })
        project = self.env["cloud.project"].create({"name": "State"})
        inst = self.env["cloud.instance"].create({
            "name": "state-inst",
            "project_id": project.id,
            "host_id": host.id,
            "environment": "staging",
            "state": "deployed",
        })
        Type = self.env["cloud.job.type"]
        self.rebuild_type = Type.search([("code", "=", "rebuild_instance")])
        restart_type = Type.search([("code", "=", "restart_instance")])
        # Written by the test, so it holds without the shipped catalogue
        # loaded (a fresh test database has English alone).
        self.rebuild_type.update_field_translations(
            "name", {"es_ES": "Reconstruir instancia"},
        )
        restart_type.update_field_translations(
            "name", {"es_ES": "Reiniciar instancia"},
        )
        common = {"host_id": host.id, "instance_id": inst.id}
        self.running = self.env["cloud.job"].create(
            common | {"job_type_id": self.rebuild_type.id, "state": "started"},
        )
        self.waiting = self.env["cloud.job"].create(
            common | {"job_type_id": restart_type.id, "state": "queued"},
        )
        for login, group, lang in (
            ("state-manager", "incubacloud.group_cloud_manager", "en_US"),
            ("state-manager-es", "incubacloud.group_cloud_manager", "es_ES"),
            ("state-outsider", "incubacloud.group_cloud_user", "en_US"),
        ):
            new_test_user(
                self.env, login=login, password=login, lang=lang,
                groups=f"base.group_user,{group}",
            )

    def _state(self, job_id):
        """POST ``/cloud/get_job_state`` for *job_id* on the current session.

        :param job_id: what the panel passes
        :returns: the decoded ``result`` payload
        """
        payload = {
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"job_id": job_id},
        }
        return self.url_open(
            "/cloud/get_job_state",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        ).json().get("result")

    def _log_title(self, job):
        """GET the log page of *job* on the current session and return its title.

        :param job: a ``cloud.job`` record
        :returns: the text of the page's ``<title>``
        """
        response = self.url_open(f"/cloud/log/{job.id}")
        self.assertEqual(response.status_code, 200)
        title = re.search(r"<title>(.*?)</title>", response.text, re.DOTALL)
        self.assertTrue(title, "the log page has no title")
        return " ".join(title.group(1).split())

    def test_a_queued_job_says_what_it_waits_behind(self):
        """A manager reads the waiting job's state and the type running."""
        self.authenticate("state-manager", "state-manager")
        answer = self._state(self.waiting.id)
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["job_id"], self.waiting.id)
        self.assertEqual(answer["state"], "queued")
        self.assertEqual(answer["queued_behind"], self.rebuild_type.name)

    def test_a_job_the_caller_cannot_read_says_nothing(self):
        """A user outside the job's project gets ``ok: false`` alone."""
        self.authenticate("state-outsider", "state-outsider")
        self.assertEqual(self._state(self.waiting.id), {"ok": False})

    def test_a_job_that_does_not_exist_says_nothing(self):
        """An id of no job, or no id at all, gets ``ok: false`` alone."""
        self.authenticate("state-manager", "state-manager")
        self.assertEqual(self._state(0), {"ok": False})
        self.assertEqual(self._state("not an id"), {"ok": False})

    def test_a_spanish_user_reads_the_types_in_spanish(self):
        """Both type names of the answer are in the caller's language."""
        self.authenticate("state-manager-es", "state-manager-es")
        answer = self._state(self.waiting.id)
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["job_type"], "Reiniciar instancia")
        self.assertEqual(answer["queued_behind"], "Reconstruir instancia")

    def test_the_log_page_names_the_job_in_the_readers_language(self):
        """``/cloud/log/<id>`` titles the job as the lists name it."""
        self.authenticate("state-manager-es", "state-manager-es")
        self.assertIn("Reconstruir instancia", self._log_title(self.running))
        self.authenticate("state-manager", "state-manager")
        self.assertIn("Rebuild Instance", self._log_title(self.running))


@tagged("-at_install", "post_install")
class TestWhatOnlyTheServerCalls(HttpCase):
    """``call_kw`` refuses the methods of ``cloud.job`` only the server calls.

    ``execute`` above all: queue_job calls it by name, and through the
    panel's RPC anyone who reads a job would run it again, outside its
    queue and past the role gate of ``enqueue``.
    """

    def setUp(self):
        """A failed restart of an instance, and a consultant of its project."""
        super().setUp()
        host = self.env["cloud.host"].create({
            "name": "rpc-host",
            "ip_address": "10.0.0.83",
            "user": "ubuntu",
            "wildcard_domain": "rpc.example.com",
        })
        project = self.env["cloud.project"].create({"name": "RPC"})
        inst = self.env["cloud.instance"].create({
            "name": "rpc-inst",
            "project_id": project.id,
            "host_id": host.id,
            "environment": "staging",
            "state": "deployed",
        })
        self.job = self.env["cloud.job"].create({
            "name": "Restart Instance",
            "host_id": host.id,
            "instance_id": inst.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "restart_instance")], limit=1,
            ).id,
            "state": "failed",
        })
        consultant = new_test_user(
            self.env, login="rpc-consultant", password="rpc-consultant",
            groups="base.group_user,incubacloud.group_cloud_consultant",
        )
        project.member_ids = [(4, consultant.id)]
        self.authenticate("rpc-consultant", "rpc-consultant")

    def _call(self, method, args):
        """Call ``cloud.job.<method>`` through ``/web/dataset/call_kw``.

        :param str method: the method's name
        :param list args: its positional arguments, ids first for a record method
        :returns: the decoded JSON-RPC answer
        """
        payload = {
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {
                "model": "cloud.job", "method": method,
                "args": args, "kwargs": {},
            },
        }
        return self.url_open(
            f"/web/dataset/call_kw/cloud.job/{method}",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        ).json()

    def _refused(self, answer):
        """Assert *answer* is the refusal of a private method."""
        self.assertNotIn("result", answer)
        self.assertEqual(
            answer["error"]["data"]["name"], "odoo.exceptions.AccessError",
        )

    def test_a_job_is_not_run_again_through_rpc(self):
        """Refused before anything of the job is looked at."""
        Job = type(self.env["cloud.job"])
        with patch.object(Job, "_get_executor", autospec=True) as executor:
            self._refused(self._call("execute", [[self.job.id]]))
        executor.assert_not_called()
        self.assertEqual(self.job.state, "failed")

    def test_the_other_server_methods_are_refused(self):
        """The resolve endpoints, the cron and the audit route call these."""
        for method, args in (
            ("unblock_and_enqueue", [[self.job.id]]),
            ("cron_cleanup_backup_attachments", []),
            ("get_audit_log", []),
        ):
            with self.subTest(method=method):
                self._refused(self._call(method, args))

    def test_what_the_panel_calls_is_still_answered(self):
        """Positive control: the route serves the panel's own calls."""
        answer = self._call("load_jobs", [self.job.id])
        self.assertNotIn("error", answer)
        self.assertEqual(answer["result"][0]["id"], self.job.id)
