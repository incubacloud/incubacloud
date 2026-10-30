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

from psycopg2 import IntegrityError
from psycopg2.errors import SerializationFailure

from odoo import fields, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import HttpCase, TransactionCase, new_test_user
from odoo.tools import mute_logger

from odoo.tools.misc import Callbacks

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
from ..models.delete_instance_executor import DeleteInstanceExecutor
from ..models.instance_health_executor import InstanceHealthExecutor
from ..models.move_cutover_executor import MoveCutoverExecutor
from ..models.move_rollback_cleanup_executor import MoveRollbackCleanupExecutor
from ..models.rebuild_instance_executor import RebuildInstanceExecutor
from ..models.registry import executor_registry
from ..models.restart_instance_executor import RestartInstanceExecutor
from ..models.restore_instance_executor import RestoreInstanceExecutor
from ..models.transport import CommandResult, SSHTransport
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

    def test_a_job_under_way_is_stopped_only_by_whoever_may_ask_for_its_type(self):
        """``cancel_job`` holds a dispatched, running or blocked job to the
        gate of its type, as it holds a waiting one: a consultant does not
        stop a restore a developer asked for, nor the delete of production,
        a developer does not stop that delete, nor a project manager a
        host's hardening."""
        consultant = self._consultant("queue-stop-consultant")
        developer = self._consultant(
            "queue-stop-developer", "incubacloud.group_cloud_developer",
        )
        pm = self._consultant(
            "queue-stop-pm", "incubacloud.group_cloud_project_manager",
        )
        prod = self._instance("queue-stop-prod", environment="production")
        pending = self._enqueue(
            "backup_restore", inst=self._instance("queue-stop-staging"),
        )
        self.assertEqual(pending.state, "pending")
        alert = self.env["cloud.alert"].sudo().raise_alert(
            "addon_conflict", "two repositories define web_x",
            level="critical", instance=prod,
        )
        delete = self._job("delete_instance", inst=prod)
        for user, job, state in (
            (consultant, self._job("backup_restore", inst=prod), "started"),
            (consultant, pending, "pending"),
            (
                consultant,
                self._job(
                    "backup_restore", state=False, inst=prod,
                    blocked_alert_id=alert.id,
                ),
                False,
            ),
            (consultant, delete, "started"),
            (developer, delete, "started"),
            (pm, self._job("host_hardening", inst=False), "started"),
        ):
            with self.subTest(
                user=user.login, job=job.job_type_id.code, state=state,
            ):
                # The role gate's refusal, not one of an access right the
                # path happens to need on the way.
                with self.assertRaisesRegex(AccessError, "group_cloud_"):
                    job.with_user(user).cancel_job()
                self.assertEqual(job.state, state)

    def test_what_a_role_may_ask_for_it_still_stops_in_any_state(self):
        """The stop buttons of the instance and host pages keep working: a
        consultant stops their restart dispatched or running, and a manager
        without superuser rights a host's hardening.

        Neither may read ``queue.job``: a request of the panel starts with
        nothing of it in the cache, and the dispatched job is stopped
        through its queue.job."""
        consultant = self._consultant("queue-stop-own")
        manager = self._consultant(
            "queue-stop-manager", "incubacloud.group_cloud_manager",
        )
        pending = self._enqueue("restart_instance")
        self.assertEqual(pending.state, "pending")
        self.env.invalidate_all()
        pending.with_user(consultant).cancel_job()
        self.assertEqual(pending.state, "cancelled")
        running = self._job("restart_instance")
        running.with_user(consultant).cancel_job()
        self.assertEqual(running.state, "cancelled")
        hardening = self._job("host_hardening", inst=False)
        hardening.with_user(manager).cancel_job()
        self.assertEqual(hardening.state, "cancelled")

    def test_a_manager_without_superuser_rights_rolls_back_a_running_move(self):
        """``rollback_move`` stops the dispatched and running steps through
        ``cancel_job`` as the manager who asks, and every type of a move is
        theirs to ask for. ``_cancel_move_chain`` swallows a ``UserError``,
        and so an ``AccessError``: only the steps' state tells."""
        manager = self._consultant(
            "queue-stop-move", "incubacloud.group_cloud_manager",
        )
        target = self.env["cloud.host"].create({
            "name": "queue-stop-target",
            "ip_address": "10.0.0.84",
            "user": "ubuntu",
            "wildcard_domain": "stop.example.com",
        })
        prod = self._instance("queue-stop-move", environment="production")
        steps = self._enqueue("stop_instance", inst=prod)
        self.assertEqual(steps.state, "pending")
        steps |= self._job("restore_instance", inst=prod, host=target)
        steps |= self._job("move_cutover", inst=prod, host=target)
        prod.write({
            "move_origin_host_id": self.host.id,
            "move_target_host_id": target.id,
            "move_chain_job_ids": ",".join(str(job_id) for job_id in steps.ids),
        })
        self.env.invalidate_all()
        prod.with_user(manager).rollback_move()
        self.assertEqual(set(steps.mapped("state")), {"cancelled"})

    def _platform_chain(self, inst, *codes):
        """``enqueue_chain`` *codes* on *inst*, as the platform asks for one.

        :param inst: the ``cloud.instance`` every step acts on
        :param codes: the job types of the steps, in order
        :return: the steps, in order
        """
        return self.Job.browse(self.Job.enqueue_chain([
            {"host_id": self.host.id, "instance_id": inst.id, "job_type_code": code}
            for code in codes
        ]))

    def test_a_cancellation_ending_a_step_above_the_caller_ends_nothing(self):
        """What a cancellation ends besides the job — the rest of its chain —
        is held to the same gate: a consultant who stops the restart heading
        a chain whose next step is a restore, dispatched or running, or the
        stop heading a move still waiting in the queue, ends nothing."""
        consultant = self._consultant("queue-stop-chain")
        dispatched = self._platform_chain(
            self.inst, "restart_instance", "backup_restore",
        )
        self.assertEqual(
            dispatched.mapped("state"), ["pending", "wait_dependencies"],
        )
        running = self._platform_chain(
            self._instance("queue-stop-chain-run"),
            "restart_instance", "backup_restore",
        )
        running[0].queue_job_id.write({"state": "started"})
        self.assertEqual(running.mapped("state"), ["started", "wait_dependencies"])
        busy = self._instance("queue-stop-chain-busy")
        self._job("rebuild_instance", inst=busy)
        move = self._platform_chain(busy, "stop_instance", "move_cutover")
        self.assertEqual(move.mapped("state"), ["queued", "queued"])
        for steps in (dispatched, running, move):
            states = steps.mapped("state")
            with self.subTest(states=states):
                self.env.invalidate_all()
                # The refusal names the step the caller may not stop.
                with self.assertRaisesRegex(
                    AccessError,
                    rf"would also end job #{steps[1].id}, .*'group_cloud_",
                ):
                    steps[0].with_user(consultant).cancel_job()
                self.assertEqual(steps.mapped("state"), states)

    def test_whoever_may_ask_for_every_step_still_ends_the_chain(self):
        """A consultant's own chain, every step theirs to ask for, still
        ends whole when they stop its head, dispatched or waiting."""
        consultant = self._consultant("queue-stop-own-chain")
        dispatched = self._platform_chain(
            self.inst, "restart_instance", "stop_instance",
        )
        self.assertEqual(
            dispatched.mapped("state"), ["pending", "wait_dependencies"],
        )
        busy = self._instance("queue-stop-own-chain-busy")
        self._job("rebuild_instance", inst=busy)
        waiting = self._platform_chain(busy, "stop_instance", "start_instance")
        self.assertEqual(waiting.mapped("state"), ["queued", "queued"])
        for steps in (dispatched, waiting):
            with self.subTest(states=steps.mapped("state")):
                self.env.invalidate_all()
                steps[0].with_user(consultant).cancel_job()
                self.assertEqual(set(steps.mapped("state")), {"cancelled"})

    def test_a_manager_and_the_watchdog_still_end_a_dispatched_move(self):
        """``rollback_move`` as a manager without superuser rights, and the
        watchdog's ``_do_rollback_move`` as the cron bot its scheduled
        action runs as, still end the whole of a dispatched move chain,
        whose steps are a manager's to ask for."""
        manager = self._consultant(
            "queue-stop-move-chain", "incubacloud.group_cloud_manager",
        )
        bot = self.env["res.users"]._get_cron_bot()
        self.assertTrue(bot)
        target = self.env["cloud.host"].create({
            "name": "queue-stop-chain-target",
            "ip_address": "10.0.0.85",
            "user": "ubuntu",
            "wildcard_domain": "stop-chain.example.com",
        })
        Instance = self.env["cloud.instance"]
        for name, rollback in (
            ("queue-stop-move-manager", lambda inst: inst.with_user(manager).rollback_move()),
            ("queue-stop-move-watchdog", lambda inst: inst.with_user(bot)._do_rollback_move()),
        ):
            with self.subTest(name=name):
                inst = self._instance(name)
                steps = self._platform_chain(
                    inst, "stop_instance", "restore_instance", "move_cutover",
                )
                self.assertEqual(
                    steps.mapped("state"),
                    ["pending", "wait_dependencies", "wait_dependencies"],
                )
                inst.write({
                    "move_origin_host_id": self.host.id,
                    "move_target_host_id": target.id,
                    "move_chain_job_ids": ",".join(str(i) for i in steps.ids),
                })
                self.env.invalidate_all()
                rollback(Instance.browse(inst.id))
                self.assertEqual(set(steps.mapped("state")), {"cancelled"})

    def test_stopping_the_last_step_of_a_running_chain_ends_it_alone(self):
        """Dispatched, a step ends only the steps after it: a consultant
        stops the restart waiting behind a restore that runs, and the
        restore runs on."""
        consultant = self._consultant("queue-stop-tail")
        chain = self._platform_chain(self.inst, "backup_restore", "restart_instance")
        chain[0].queue_job_id.write({"state": "started"})
        self.assertEqual(chain.mapped("state"), ["started", "wait_dependencies"])
        self.env.invalidate_all()
        chain[1].with_user(consultant).cancel_job()
        self.assertEqual(chain.mapped("state"), ["started", "cancelled"])

    def test_stopping_a_step_of_a_waiting_chain_would_end_all_of_it(self):
        """Waiting, a chain is cancelled whole: stopping its restart would
        end the restore before it, which the consultant may not stop."""
        consultant = self._consultant("queue-stop-whole")
        self._job("rebuild_instance")
        chain = self._platform_chain(self.inst, "backup_restore", "restart_instance")
        self.assertEqual(chain.mapped("state"), ["queued", "queued"])
        self.env.invalidate_all()
        with self.assertRaisesRegex(
            AccessError,
            rf"would also end job #{chain[0].id}, .*'group_cloud_developer'",
        ):
            chain[1].with_user(consultant).cancel_job()
        self.assertEqual(chain.mapped("state"), ["queued", "queued"])

    def test_a_refusal_ending_a_move_is_not_taken_for_a_step_already_ended(self):
        """``_cancel_move_chain`` skips a step an earlier one's cancellation
        already ended (``UserError``); an ``AccessError``, a subclass of it,
        is a refusal and is raised: the move was not stopped."""
        consultant = self._consultant("queue-stop-move-refused")
        steps = self._platform_chain(self.inst, "stop_instance", "move_cutover")
        self.assertEqual(steps.mapped("state"), ["pending", "wait_dependencies"])
        self.inst.move_chain_job_ids = ",".join(str(i) for i in steps.ids)
        self.env.invalidate_all()
        with self.assertRaisesRegex(AccessError, "'group_cloud_manager'"):
            self.inst.with_user(consultant)._cancel_move_chain()
        self.assertEqual(steps.mapped("state"), ["pending", "wait_dependencies"])

    def test_a_consultant_neither_deletes_nor_archives_over_work_above_them(self):
        """Deleting an instance empties its queue, and archiving it ends at
        dispatch what waits there: a consultant may do neither while a
        developer's restore, or a manager's move, waits on the staging. No
        delete is asked for, nothing waiting ends, the staging stays."""
        consultant = self._consultant("queue-clear-consultant")
        Job = self.Job.with_user(consultant)
        for codes in (("backup_restore",), ("stop_instance", "move_cutover")):
            inst = self._instance(f"queue-clear-{codes[-1].replace('_', '-')}")
            self._job("rebuild_instance", inst=inst)
            waiting = self._platform_chain(inst, *codes)
            self.assertEqual(set(waiting.mapped("state")), {"queued"})
            for label, attempt in (
                ("delete", lambda inst=inst: Job.enqueue(
                    self.host.id, inst.id, "delete_instance",
                )),
                ("archive", lambda inst=inst: inst.with_user(consultant).write(
                    {"active": False},
                )),
            ):
                with self.subTest(codes=codes, attempt=label):
                    jobs_before = self.Job.search_count(
                        [("instance_id", "=", inst.id)],
                    )
                    self.env.invalidate_all()
                    with self.assertRaisesRegex(
                        AccessError,
                        rf"{inst.name} has work waiting .*job #{waiting[-1].id}, "
                        r".*'group_cloud_",
                    ):
                        attempt()
                    self.assertEqual(set(waiting.mapped("state")), {"queued"})
                    self.assertEqual(
                        self.Job.search_count([("instance_id", "=", inst.id)]),
                        jobs_before,
                    )
                    self.assertTrue(inst.active)

    def test_a_consultant_does_not_remove_a_draft_over_work_above_them(self):
        """Removing an instance's record ends what waits on it, too: not
        while a developer's restore upload waits on the draft."""
        consultant = self._consultant("queue-unlink-consultant")
        draft = self._instance("queue-unlink-draft", state="draft")
        self._job("backup_create", inst=draft)
        grant = self._enqueue("grant_restore_upload_key", inst=draft)
        self.assertEqual(grant.state, "queued")
        self.env.invalidate_all()
        with self.assertRaisesRegex(
            AccessError, rf"has work waiting .*job #{grant.id}, .*'group_cloud_",
        ):
            draft.with_user(consultant).unlink()
        self.assertTrue(draft.exists())
        self.assertEqual(grant.state, "queued")

    def test_a_consultant_still_deletes_archives_and_removes_over_their_work(self):
        """What waits is the consultant's to stop: their delete empties the
        queue, their archive and their removal go through."""
        consultant = self._consultant("queue-clear-own")
        Job = self.Job.with_user(consultant)
        for label in ("delete", "archive", "remove"):
            with self.subTest(attempt=label):
                inst = self._instance(
                    f"queue-clear-own-{label}",
                    state="draft" if label == "remove" else "deployed",
                )
                self._job("rebuild_instance", inst=inst)
                restart = self.Job.browse(
                    Job.enqueue(self.host.id, inst.id, "restart_instance"),
                )
                self.assertEqual(restart.state, "queued")
                self.env.invalidate_all()
                if label == "delete":
                    delete = self.Job.browse(
                        Job.enqueue(self.host.id, inst.id, "delete_instance"),
                    )
                    self.assertEqual(delete.state, "queued")
                    self.assertEqual(restart.state, "cancelled")
                elif label == "archive":
                    inst.with_user(consultant).write({"active": False})
                    self.assertFalse(inst.active)
                else:
                    inst.with_user(consultant).unlink()
                    self.assertFalse(inst.exists())
                    self.assertEqual(restart.state, "cancelled")

    def test_a_manager_and_the_cron_bot_still_clear_whatever_waits(self):
        """A manager's delete empties the queue whole, a developer's restore
        and a move chain included, and the cron bot still archives."""
        manager = self._consultant(
            "queue-clear-manager", "incubacloud.group_cloud_manager",
        )
        bot = self.env["res.users"]._get_cron_bot()
        inst = self._instance("queue-clear-manager")
        self._job("rebuild_instance", inst=inst)
        waiting = self._platform_chain(inst, "backup_restore")
        waiting |= self._platform_chain(inst, "stop_instance", "move_cutover")
        self.env.invalidate_all()
        delete = self.Job.browse(self.Job.with_user(manager).enqueue(
            self.host.id, inst.id, "delete_instance",
        ))
        self.assertEqual(delete.state, "queued")
        self.assertEqual(set(waiting.mapped("state")), {"cancelled"})
        archived = self._instance("queue-clear-bot")
        self._job("rebuild_instance", inst=archived)
        restore = self._platform_chain(archived, "backup_restore")
        self.env.invalidate_all()
        archived.with_user(bot).write({"active": False})
        self.assertFalse(archived.active)
        self.assertEqual(restore.state, "queued")

    def _end_delete(self, job):
        """Run *job*'s ``on_success`` as its executor does, then the unlink it arms.

        The test's cursor never commits, so what the hook arms for after
        the commit (``_unlink_after_job_commit``) is gathered on callbacks
        of its own and run once the hook is done.

        :param job: a ``delete_instance`` job, in the environment it runs in
        """
        ex = DeleteInstanceExecutor(job_record=job, host_record=job.host_id)
        cr = job.env.cr
        original = cr.postcommit
        cr.postcommit = Callbacks()
        try:
            with patch.object(
                type(self.host), "refresh_observability_labels", autospec=True,
            ):
                asyncio.run(ex.on_success({}))
            job.env.flush_all()
            cr.postcommit.run()
        finally:
            cr.postcommit = original

    def test_a_consultants_delete_still_ends_over_what_was_asked_after_it(self):
        """What waits behind a delete was asked for on an instance being
        removed: the delete's own end archives the record, or removes it,
        as the platform completing what was authorised when it was asked
        for — whatever waits behind it."""
        consultant = self._consultant("queue-clear-end")
        backend = self.env["cloud.backup.backend"].create({
            "name": "queue-clear-end", "backend_type": "s3",
            "s3_bucket": "queue-clear-end",
        })
        for keep in (True, False):
            with self.subTest(keep_in_panel=keep):
                inst = self._instance(f"queue-clear-end-{'keep' if keep else 'remove'}")
                inst.backup_backend_id = backend
                delete = self._job(
                    "delete_instance", inst=inst, payload={"keep_in_panel": keep},
                )
                restore = self._platform_chain(inst, "backup_restore")
                self.assertEqual(restore.state, "queued")
                self._end_delete(delete.with_user(consultant))
                self.env.invalidate_all()
                record = self.env["cloud.instance"].with_context(
                    active_test=False,
                ).browse(inst.id)
                if keep:
                    self.assertFalse(record.active)
                    # Its copy's place, frozen as the platform's.
                    self.assertTrue(record.custom_backup_dst)
                else:
                    self.assertFalse(record.exists())
                    self.assertEqual(restore.state, "cancelled")

    def test_stopping_the_last_of_three_running_steps_ends_it_alone(self):
        """Only what comes after a step ends with it: the consultant stops
        the third step of a running chain, and the restore before it,
        theirs to stop or not, waits on."""
        consultant = self._consultant("queue-stop-tail3")
        chain = self._platform_chain(
            self.inst, "restart_instance", "backup_restore", "start_instance",
        )
        chain[0].queue_job_id.write({"state": "started"})
        self.assertEqual(
            chain.mapped("state"),
            ["started", "wait_dependencies", "wait_dependencies"],
        )
        self.env.invalidate_all()
        chain[2].with_user(consultant).cancel_job()
        self.assertEqual(
            chain.mapped("state"), ["started", "wait_dependencies", "cancelled"],
        )

    def test_archiving_ignores_what_runs_on_archived_instances(self):
        """The dispatcher leaves a job meant for archived instances to run
        (``_runs_on_archived``), so archiving does not end it and is not
        held to it: a consultant archives over a manager's purge."""
        consultant = self._consultant("queue-archive-purge")
        inst = self._instance("queue-archive-purge")
        self._job("rebuild_instance", inst=inst)
        purge = self._platform_chain(inst, "purge_archived_backups")
        self.assertEqual(purge.state, "queued")
        self.env.invalidate_all()
        inst.with_user(consultant).write({"active": False})
        self.assertFalse(inst.active)
        self.assertEqual(purge.state, "queued")

    def _target_host(self, name):
        """Another host, ready to take an instance, named *name*."""
        return self.env["cloud.host"].create({
            "name": name,
            "ip_address": "10.0.0.86",
            "user": "ubuntu",
            "wildcard_domain": f"{name}.example.com",
            "status": "compatible",
            "traefik_deployed": True,
        })

    def test_a_consultant_writes_none_of_a_moves_markers(self):
        """A move's markers say which jobs a rollback and the watchdog
        cancel and on which host the cleanup runs: only the platform writes
        them, neither a write nor a create of a consultant does."""
        consultant = self._consultant("queue-markers")
        other = self._target_host("queue-markers")
        for vals in (
            {"move_origin_host_id": self.host.id},
            {"move_target_host_id": other.id},
            {"move_chain_job_ids": "1"},
            {"move_rollback_in_progress": True},
        ):
            with self.subTest(vals=vals):
                self.env.invalidate_all()
                with self.assertRaisesRegex(AccessError, "panel's own actions"):
                    self.inst.with_user(consultant).write(vals)
                with self.assertRaisesRegex(AccessError, "panel's own actions"):
                    self.env["cloud.instance"].with_user(consultant).create({
                        "name": "queue-markers-new",
                        "project_id": self.project.id,
                        "host_id": self.host.id,
                    } | vals)
        self.assertFalse(self.inst.move_origin_host_id)
        self.assertFalse(self.inst.move_target_host_id)
        self.assertFalse(self.inst.move_chain_job_ids)

    def test_the_watchdog_ends_only_the_moves_own_jobs(self):
        """Markers that name another instance's job, as forged ones would,
        cancel nothing of it: a move's chain is its instance's jobs."""
        bot = self.env["res.users"]._get_cron_bot()
        prod = self._instance("queue-markers-prod", environment="production")
        self._job("rebuild_instance", inst=prod)
        victim = self._platform_chain(prod, "backup_restore")
        self.assertEqual(victim.state, "queued")
        self.inst.write({
            "move_origin_host_id": self.host.id,
            "move_target_host_id": self._target_host("queue-markers-wd").id,
            "move_chain_job_ids": str(victim.id),
        })
        self.assertFalse(self.inst._get_move_chain_jobs())
        self.env.invalidate_all()
        self.env["cloud.instance"].with_user(bot).cron_recover_stuck_moves()
        self.assertEqual(victim.state, "queued")

    def test_a_managers_move_and_its_steps_still_write_the_markers(self):
        """``move_to_host`` asked by a manager without superuser rights sets
        the markers, and the cutover and the rollback's cleanup, run as that
        manager, clear them."""
        manager = self._consultant(
            "queue-markers-manager", "incubacloud.group_cloud_manager",
        )
        target = self._target_host("queue-markers-move")
        Instance = self.env["cloud.instance"]
        for step, executor_cls in (
            ("move_cutover", MoveCutoverExecutor),
            ("move_rollback_cleanup", MoveRollbackCleanupExecutor),
        ):
            with self.subTest(step=step):
                inst = self._instance(f"queue-markers-{step.replace('_', '-')}")
                self.env.invalidate_all()
                Instance.with_user(manager).browse(inst.id).move_to_host(target)
                self.assertEqual(inst.move_origin_host_id, self.host)
                self.assertEqual(inst.move_target_host_id, target)
                chain = inst._get_move_chain_jobs()
                self.assertTrue(chain)
                self.assertEqual(chain.instance_id, inst)
                job = chain.filtered(lambda j, step=step: j.job_type_id.code == step)
                if not job:
                    # The rollback's cleanup is not a step of the move.
                    inst.sudo().move_rollback_in_progress = True
                    job = self._job(step, inst=inst, host=target)
                ex = executor_cls(
                    job_record=job.with_user(manager), host_record=target,
                )
                with patch.object(
                    type(self.host), "refresh_observability_labels", autospec=True,
                ):
                    asyncio.run(ex.on_success({}))
                self.env.invalidate_all()
                self.assertFalse(inst.move_origin_host_id)
                self.assertFalse(inst.move_target_host_id)
                self.assertFalse(inst.move_chain_job_ids)

    def test_an_instances_environment_is_chosen_once(self):
        """Whether an instance is production decides who may delete it,
        whether its egress is filtered and, on a customer's panel, whether
        it takes a seat of the plan: it is chosen when the instance is
        created, and nobody changes it afterwards, a manager no more than a
        consultant — a production relabelled staging was deleted, or freed
        its seat while it went on serving. The platform still may; saved as
        it is, it is no change, and creating a production is as before."""
        consultant = self._consultant("queue-env")
        manager = self._consultant("queue-env-manager", "incubacloud.group_cloud_manager")
        prod = self._instance("queue-env-prod", environment="production")
        for user in (consultant, manager):
            for inst, env in ((prod, "staging"), (self.inst, "production")):
                with self.subTest(user=user.login, instance=inst.name, environment=env):
                    before = inst.environment
                    self.env.invalidate_all()
                    with self.assertRaisesRegex(
                        AccessError, "chosen when it is created and cannot be changed",
                    ):
                        inst.with_user(user).write({"environment": env})
                    self.assertEqual(inst.environment, before)
        self.inst.with_user(consultant).write({"environment": "staging"})
        with self.assertRaises(AccessError):
            self.Job.with_user(consultant).enqueue(
                self.host.id, prod.id, "delete_instance",
            )
        other = self.env["cloud.project"].create({"name": "Queue env other"})
        created = self.env["cloud.instance"].with_user(manager).create({
            "name": "queue-env-new", "project_id": other.id,
            "environment": "production",
        })
        self.assertEqual(created.environment, "production")
        prod.write({"environment": "staging"})
        self.assertEqual(prod.environment, "staging")

    def test_what_decides_a_teardown_or_a_purge_is_not_a_consultants(self):
        """The directory a teardown removes, the place a purge empties, a
        pull request's preview the webhook deletes on close, and the stamps
        a removal and a purge go by: a consultant writes none of them; a
        manager sets the first two, and reviving still clears the stamps."""
        consultant = self._consultant("queue-guarded")
        manager = self._consultant(
            "queue-guarded-manager", "incubacloud.group_cloud_manager",
        )
        now = fields.Datetime.now()
        for vals, refusal in (
            ({"custom_remote_dir": "~/other/production"}, "Only an Administrator"),
            ({"custom_backup_dst": "s3://other/production"}, "Only an Administrator"),
            ({"pr_number": 7}, "panel's own actions"),
            ({"pr_repo": "acme/odoo"}, "panel's own actions"),
            ({"pr_head_branch": "feature"}, "panel's own actions"),
            ({"pr_comment_id": 5}, "panel's own actions"),
            ({"removal_finalized": True}, "panel's own actions"),
            ({"archived_at": now}, "panel's own actions"),
            ({"purge_cutoff_at": now}, "panel's own actions"),
        ):
            with self.subTest(vals=vals):
                self.env.invalidate_all()
                with self.assertRaisesRegex(AccessError, refusal):
                    self.inst.with_user(consultant).write(vals)
        self.assertFalse(self.inst.custom_remote_dir)
        self.assertFalse(self.inst.pr_number)
        self.assertFalse(self.inst.removal_finalized)
        self.inst.with_user(consultant).write(
            {"archived_at": False, "purge_cutoff_at": False},
        )
        self.inst.with_user(manager).write(
            {"custom_backup_dst": "s3://queue/guarded"},
        )
        self.assertEqual(self.inst.custom_backup_dst, "s3://queue/guarded")

    def test_only_a_manager_renames_a_projects_remote_folder(self):
        """A project's instances live in directories named after its remote
        folder: renamed to another project's, a teardown would remove that
        project's instances. A consultant may not rename it."""
        consultant = self._consultant("queue-folder")
        manager = self._consultant(
            "queue-folder-manager", "incubacloud.group_cloud_manager",
        )
        before = self.project.remote_folder
        self.env.invalidate_all()
        with self.assertRaisesRegex(AccessError, "remote folder"):
            self.project.with_user(consultant).write({"remote_folder": "other"})
        self.assertEqual(self.project.remote_folder, before)
        self.project.with_user(consultant).write({"remote_folder": before})
        self.project.with_user(manager).write({"remote_folder": "queue-renamed"})
        self.assertEqual(self.project.remote_folder, "queue-renamed")

    def test_only_a_manager_changes_the_host_of_an_instance_past_draft(self):
        """Past draft, an instance's stack is on its host, and the jobs
        waiting on it follow its host: changing it without moving the stack
        is ``move_to_host``'s, a manager's. A draft is anyone's to place,
        and a host saved as it is changes nothing."""
        consultant = self._consultant("queue-host")
        manager = self._consultant("queue-host-manager", "incubacloud.group_cloud_manager")
        other = self._target_host("queue-host-other")
        self.env.invalidate_all()
        with self.assertRaisesRegex(
            AccessError, rf"Only an Administrator may change the host of {self.inst.name}",
        ):
            self.inst.with_user(consultant).write({"host_id": other.id})
        self.assertEqual(self.inst.host_id, self.host)
        self.inst.with_user(consultant).write({"host_id": self.host.id})
        draft = self._instance("queue-host-draft", state="draft")
        draft.with_user(consultant).write({"host_id": other.id})
        self.assertEqual(draft.host_id, other)
        self.inst.with_user(manager).write({"host_id": other.id})
        self.assertEqual(self.inst.host_id, other)

    def test_only_a_developer_chooses_where_backups_are_kept(self):
        """Backups are a developer's: a consultant or a project manager
        chooses neither an instance's backup backend nor a project's, on a
        write or a create; saved as it is, or empty on a new project, it is
        no choice."""
        consultant = self._consultant("queue-backup")
        pm = self._consultant("queue-backup-pm", "incubacloud.group_cloud_project_manager")
        developer = self._consultant("queue-backup-dev", "incubacloud.group_cloud_developer")
        backend = self.env["cloud.backup.backend"].create({
            "name": "queue-backup", "backend_type": "s3", "s3_bucket": "queue-backup",
        })
        refusal = "Only a Developer or an Administrator may choose where backups"
        for label, attempt in (
            ("instance", lambda: self.inst.with_user(consultant).write(
                {"backup_backend_id": backend.id},
            )),
            ("project", lambda: self.project.with_user(pm).write(
                {"backup_backend_id": backend.id},
            )),
            ("new instance", lambda: self.env["cloud.instance"].with_user(consultant).create({
                "name": "queue-backup-new", "project_id": self.project.id,
                "backup_backend_id": backend.id,
            })),
            ("new project", lambda: self.env["cloud.project"].with_user(pm).create({
                "name": "Queue backup new", "backup_backend_id": backend.id,
            })),
        ):
            with self.subTest(attempt=label):
                self.env.invalidate_all()
                with self.assertRaisesRegex(AccessError, refusal):
                    attempt()
        self.assertFalse(self.inst.backup_backend_id)
        self.assertFalse(self.project.backup_backend_id)
        self.inst.with_user(consultant).write({"backup_backend_id": None})
        self.project.with_user(pm).write({"backup_backend_id": False})
        self.env["cloud.project"].with_user(pm).create(
            {"name": "Queue backup empty", "backup_backend_id": None},
        )
        self.inst.with_user(developer).write({"backup_backend_id": backend.id})
        self.project.with_user(developer).write({"backup_backend_id": backend.id})
        self.assertEqual(self.inst.backup_backend_id, backend)
        self.assertEqual(self.project.backup_backend_id, backend)

    def test_no_project_lives_in_a_platform_directory(self):
        """A project's instances live in ``~/<remote folder>/<name>``, and
        the platform keeps its own files in a few directories of the same
        home: a project named after one would deploy into, and tear down,
        the proxy's routes and certificates. No one may create or rename a
        project into one, by its folder or by the name it is derived from;
        a name that only starts like one is fine."""
        pm = self._consultant("queue-folder-pm", "incubacloud.group_cloud_project_manager")
        Project = self.env["cloud.project"]
        for label, attempt in (
            ("derived", lambda: Project.with_user(pm).create({"name": "Traefik"})),
            ("explicit", lambda: Project.with_user(pm).create(
                {"name": "Certs", "remote_folder": "traefik"},
            )),
            ("platform", lambda: Project.create({"name": "Observability central"})),
            ("renamed", lambda: self.project.write({"remote_folder": "globalwhitelist"})),
            ("re-derived", lambda: self.project.write(
                {"remote_folder": False, "name": "Incubacloud"},
            )),
        ):
            with self.subTest(attempt=label):
                with self.assertRaisesRegex(ValidationError, "reserved for the platform"):
                    attempt()
        self.assertNotIn(self.project.remote_folder, ("globalwhitelist", "incubacloud"))
        shop = Project.with_user(pm).create({"name": "Traefik Shop"})
        self.assertEqual(shop.remote_folder, "traefik-shop")

    def test_a_folder_or_a_name_ending_in_a_blank_is_refused(self):
        """A project's remote folder and an instance's name are path
        segments of the commands run on the host: the whole value has to
        have their shape, a line break or a blank at its end included,
        and a reserved folder with one is refused as malformed."""
        Project = self.env["cloud.project"]
        Instance = self.env["cloud.instance"]
        draft = self._instance("queue-blank-draft", state="draft")
        for blank in ("\n", "\r", "\t", " "):
            for label, attempt in (
                ("new folder", lambda: Project.create(
                    {"name": "Queue blank", "remote_folder": "queue-blank" + blank},
                )),
                ("reserved folder", lambda: Project.create(
                    {"name": "Queue blank", "remote_folder": "traefik" + blank},
                )),
                ("folder", lambda: self.project.write(
                    {"remote_folder": "queue-blank" + blank},
                )),
                ("new instance", lambda: Instance.create({
                    "name": "queue-blank" + blank, "project_id": self.project.id,
                    "environment": "staging",
                })),
                ("instance", lambda: draft.write({"name": "queue-blank" + blank})),
            ):
                with self.subTest(blank=blank, attempt=label):
                    # ``assertRaises``, not ``assertRaisesRegex``: Odoo's
                    # rolls back to a savepoint what the refused call wrote.
                    with self.assertRaises(ValidationError) as caught:
                        attempt()
                    self.assertIn("is invalid", str(caught.exception))
        self.assertEqual(self.project.remote_folder, "queue")
        self.assertEqual(draft.name, "queue-blank-draft")

    def test_renaming_a_project_without_a_folder_chooses_its_folder(self):
        """A project without a remote folder takes one derived from its
        name when it is renamed: that is choosing its folder, refused as
        an explicit choice is — below an administrator, and into a
        platform directory. A project that has one keeps it."""
        consultant = self._consultant("queue-derive")
        manager = self._consultant(
            "queue-derive-manager", "incubacloud.group_cloud_manager",
        )
        bare = self.env["cloud.project"].create({
            "name": "Queue bare", "remote_folder": False,
            "member_ids": [(4, consultant.id)],
        })
        self.assertFalse(bare.remote_folder)
        self.env.invalidate_all()
        with self.assertRaisesRegex(AccessError, "remote folder"):
            bare.with_user(consultant).write({"name": "Queue"})
        with self.assertRaisesRegex(ValidationError, "reserved for the platform"):
            bare.with_user(manager).write({"name": "Traefik"})
        self.env.invalidate_all()
        self.assertFalse(bare.remote_folder)
        self.project.with_user(consultant).write({"name": "Queue renamed"})
        self.assertEqual(self.project.remote_folder, "queue")
        (self.project | bare).with_user(manager).write({"name": "Queue derived"})
        self.assertEqual(self.project.remote_folder, "queue")
        self.assertEqual(bare.remote_folder, "queue-derived")

    def test_no_project_takes_another_projects_folder(self):
        """A project's instances live in ``~/<remote folder>/<name>``: two
        projects with one folder would tear down, and deploy into, each
        other's directories. A folder is refused whatever the way it is
        chosen — created or written — and whatever its case; a project the
        caller cannot see holds its folder as well. A folder derived from
        a name is no choice: it takes a number instead (see
        ``test_a_name_in_use_takes_a_numbered_folder``). Folders that
        differ stay free, any number of projects may have none, and a
        project keeps its own."""
        pm = self._consultant("queue-uniq-pm", "incubacloud.group_cloud_project_manager")
        Project = self.env["cloud.project"]
        other = Project.create({"name": "Queue other"})
        bare = Project.create({"name": "Queue bare", "remote_folder": False})
        self.assertEqual(other.remote_folder, "queue-other")
        self.assertFalse(bare.remote_folder)
        for label, attempt in (
            ("created", lambda: Project.create(
                {"name": "Queue twin", "remote_folder": "queue"},
            )),
            ("created, upper case", lambda: Project.create(
                {"name": "Queue twin", "remote_folder": "QUEUE"},
            )),
            ("written", lambda: other.write({"remote_folder": "queue"})),
            ("written, upper case", lambda: other.write({"remote_folder": "Queue"})),
            ("emptied", lambda: other.write(
                {"remote_folder": False, "name": "Queue"},
            )),
            ("one the caller cannot see", lambda: Project.with_user(pm).create(
                {"name": "Queue sneaky", "remote_folder": "queue"},
            )),
        ):
            with self.subTest(attempt=label):
                with self.assertRaises(ValidationError) as caught:
                    attempt()
                self.assertRegex(
                    str(caught.exception), "is already used by another project|is invalid",
                )
        # The message says what is wrong: the folder is taken.
        with self.assertRaisesRegex(ValidationError, "'queue' is already used by another project"):
            Project.create({"name": "Queue twin", "remote_folder": "queue"})
        # Nothing was kept.
        self.assertEqual(other.remote_folder, "queue-other")
        self.assertFalse(bare.remote_folder)
        self.assertEqual(self.project.remote_folder, "queue")
        # The database holds it too, whatever the case: a write that skips
        # the ORM's check meets the index.
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"), self.env.cr.savepoint():
            self.env.cr.execute(
                "UPDATE cloud_project SET remote_folder = 'QUEUE' WHERE id = %s",
                [other.id],
            )
        # What is free stays free.
        free = Project.create({"name": "Queue free"})
        self.assertEqual(free.remote_folder, "queue-free")
        other.write({"remote_folder": "queue-other-2"})
        bare.write({"name": "Queue bare renamed"})
        self.assertEqual(bare.remote_folder, "queue-bare-renamed")
        self.assertTrue(Project.create({"name": "Queue none", "remote_folder": False}))
        self.assertTrue(Project.create({"name": "Queue none too", "remote_folder": False}))
        # Saving a project with its own folder is no change.
        self.project.write({"remote_folder": "queue", "name": "Queue kept"})
        self.assertEqual(self.project.remote_folder, "queue")

    def test_a_name_in_use_takes_a_numbered_folder(self):
        """The folder derived from a name is the name's, not a choice: a
        project named like another one takes the first free number after
        it — ``queue``, ``queue-2``, ``queue-3`` — at a create and at the
        rename of a project without a folder, whatever the case of the
        name and whoever holds the folder. A folder chosen is refused when
        taken, as it was, and a derived one a platform directory is
        refused, not numbered: the name itself is what is reserved."""
        pm = self._consultant("queue-num-pm", "incubacloud.group_cloud_project_manager")
        manager = self._consultant(
            "queue-num-manager", "incubacloud.group_cloud_manager",
        )
        Project = self.env["cloud.project"]
        # ``self.project`` is already "Queue" and holds ``queue``.
        second = Project.create({"name": "Queue"})
        third = Project.with_user(pm).create({"name": "QUEUE"})
        self.assertEqual(
            [self.project.remote_folder, second.remote_folder, third.remote_folder],
            ["queue", "queue-2", "queue-3"],
        )
        # A gap is filled: the first free number, not the last plus one.
        second.write({"remote_folder": "queue-two"})
        self.assertEqual(Project.create({"name": "Queue"}).remote_folder, "queue-2")
        # Several in one call, each its own.
        batch = Project.create([{"name": "Batch"}, {"name": "Batch"}, {"name": "Batch"}])
        self.assertEqual(batch.mapped("remote_folder"), ["batch", "batch-2", "batch-3"])
        # A rename that derives the folder numbers it too, for one project
        # and for several at once; the Administrator's gate is as it was.
        bare = Project.create({"name": "Bare one", "remote_folder": False})
        bare_too = Project.create({"name": "Bare two", "remote_folder": False})
        consultant = self._consultant("queue-num-consultant")
        bare.member_ids = [(4, consultant.id)]
        self.env.invalidate_all()
        with self.assertRaisesRegex(AccessError, "remote folder"):
            bare.with_user(consultant).write({"name": "Queue"})
        self.assertFalse(bare.remote_folder)
        bare.with_user(manager).write({"name": "Queue"})
        self.assertEqual(bare.remote_folder, "queue-4")
        (bare_too | Project.create({"name": "Bare three", "remote_folder": False})).with_user(
            manager,
        ).write({"name": "Queue"})
        self.assertEqual(
            sorted(Project.search([("name", "=ilike", "queue")]).mapped("remote_folder")),
            ["queue", "queue-2", "queue-3", "queue-4", "queue-5", "queue-6", "queue-two"],
        )
        # A chosen folder is not numbered.
        for label, attempt in (
            ("created", lambda: Project.create({"name": "Mine", "remote_folder": "queue"})),
            ("written", lambda: second.write({"remote_folder": "queue-4"})),
        ):
            with self.subTest(attempt=label):
                with self.assertRaisesRegex(ValidationError, "is already used by another project"):
                    attempt()
        # A reserved name is refused, not numbered, taken or not.
        reserved = Project.create({"name": "Bare reserved", "remote_folder": False})
        for label, attempt in (
            ("created", lambda: Project.create({"name": "Traefik"})),
            ("renamed", lambda: reserved.with_user(manager).write({"name": "Traefik"})),
        ):
            with self.subTest(attempt=label):
                with self.assertRaisesRegex(ValidationError, "reserved for the platform"):
                    attempt()
        self.assertFalse(Project.search([("remote_folder", "=like", "traefik%")]))

    def test_a_create_that_loses_the_race_for_a_folder_takes_the_next(self):
        """Two requests creating the same name in one moment both see the
        folder free; the later one meets the unique index, and its create
        is tried again with the next number. A violation of anything else
        is not swallowed."""
        Project = self.env["cloud.project"]
        real_free = type(Project)._remote_folder_is_taken
        stale = {"queue-race"}

        def blind(model, folder):
            """What a transaction that began before the winner committed sees."""
            return folder not in stale and real_free(model, folder)

        winner = Project.create({"name": "Queue race"})
        self.assertEqual(winner.remote_folder, "queue-race")
        with patch.object(type(Project), "_remote_folder_is_taken", autospec=True, side_effect=blind):
            loser = Project.create({"name": "Queue race"})
        self.assertEqual(loser.remote_folder, "queue-race-2")
        self.assertEqual(winner.remote_folder, "queue-race")
        # Anything else that breaks the insert goes up.
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError):
            Project.create({"name": "Queue race other", "backup_backend_id": 999999999})

    def test_only_a_manager_deletes_an_archived_instance(self):
        """``delete_archived`` is public, and without a copy it removes the
        record with ``sudo``: through ``call_kw`` a consultant removed an
        archived production that way. It is a manager's, as its route is."""
        consultant = self._consultant("queue-archdel")
        manager = self._consultant("queue-archdel-manager", "incubacloud.group_cloud_manager")
        Instance = self.env["cloud.instance"].with_context(active_test=False)
        prod = self._instance("queue-archdel", environment="production", state="draft")
        prod.active = False
        self.env.invalidate_all()
        with self.assertRaises(AccessError):
            Instance.with_user(consultant).browse(prod.id).delete_archived()
        self.assertTrue(Instance.browse(prod.id).exists())
        self.assertEqual(Instance.with_user(manager).browse(prod.id).delete_archived(), [])
        self.assertFalse(Instance.browse(prod.id).exists())

    def test_only_a_manager_creates_an_instance_past_draft(self):
        """Seeding ``state`` at creation is how a running instance is
        imported, a manager's; a consultant's new record, already deployed
        on any host, skipped every check of a deploy."""
        consultant = self._consultant("queue-seed")
        manager = self._consultant("queue-seed-manager", "incubacloud.group_cloud_manager")
        Instance = self.env["cloud.instance"]
        with self.assertRaisesRegex(AccessError, "already past draft"):
            Instance.with_user(consultant).create({
                "name": "queue-seed", "project_id": self.project.id,
                "state": "deployed",
            })
        Instance.with_user(consultant).create({
            "name": "queue-seed-draft", "project_id": self.project.id,
            "state": "draft",
        })
        made = Instance.with_user(manager).create({
            "name": "queue-seed-imported", "project_id": self.project.id,
            "state": "deployed",
        })
        self.assertEqual(made.state, "deployed")

    def test_consultants_and_project_managers_create_stagings(self):
        """A staging is born with its host's egress list, which neither a
        consultant nor a project manager may read: creating one failed for
        both once nothing of the host was in the cache, and so did the
        deploy of one whose own list was empty. The list is read for them,
        and copied, as before."""
        consultant = self._consultant("queue-mk-c")
        pm = self._consultant("queue-mk-pm", "incubacloud.group_cloud_project_manager")
        self.env["cloud.host.whitelist"].create(
            {"host_id": self.host.id, "hostname": "queue.example.org"},
        )
        published = self.host.whitelist_ids.mapped("hostname")
        self.assertIn("queue.example.org", published)
        for user in (consultant, pm):
            with self.subTest(user=user.login):
                self.env.invalidate_all()
                made = self.env["cloud.instance"].with_user(user).create({
                    "name": f"queue-mk-{user.login.rsplit('-', 1)[-1]}",
                    "project_id": self.project.id,
                    "host_id": self.host.id,
                })
                self.assertEqual(made.sudo().whitelist_ids.mapped("hostname"), published)
                made.sudo().whitelist_ids.unlink()
                self.env.invalidate_all()
                # Template v10 closes a staging's egress with a gatekeeper,
                # which an empty list of its own leaves to the host's.
                self.assertEqual(
                    made.with_user(user)._egress_whitelist(template_ref="v10.0.8"),
                    published,
                )

    def test_the_instance_guards_hold_through_the_projects_commands(self):
        """A project's ``instance_ids`` commands create and write instances
        through their own ``create`` and ``write``: what a consultant may
        not write on an instance, they may not write through its project."""
        consultant = self._consultant("queue-commands")
        Project = self.project.with_user(consultant)
        for label, command, refusal in (
            ("write a marker", (1, self.inst.id, {"move_origin_host_id": self.host.id}),
             "panel's own actions"),
            ("write the environment", (1, self.inst.id, {"environment": "production"}),
             "chosen when it is created"),
            ("create a preview", (0, 0, {"name": "queue-cmd-pr", "pr_number": 9}),
             "panel's own actions"),
            ("create a running one", (0, 0, {"name": "queue-cmd-run", "state": "deployed"}),
             "already past draft"),
        ):
            with self.subTest(command=label):
                self.env.invalidate_all()
                with self.assertRaisesRegex(AccessError, refusal):
                    Project.write({"instance_ids": [command]})
        self.assertFalse(self.inst.move_origin_host_id)
        self.assertEqual(self.inst.environment, "staging")
        self.assertFalse(self.env["cloud.instance"].search(
            [("name", "in", ("queue-cmd-pr", "queue-cmd-run"))],
        ))

    def test_a_consultant_cannot_claim_another_projects_domain(self):
        """A hostname is one instance's, also across projects a consultant
        cannot see: claimed by their staging on the same host, the proxy
        would send that instance's visitors to it."""
        consultant = self._consultant("queue-domain")
        other_project = self.env["cloud.project"].create({"name": "Queue other"})
        other = self.env["cloud.instance"].create({
            "name": "queue-domain-other",
            "project_id": other_project.id,
            "host_id": self.host.id,
            "environment": "production",
            "state": "deployed",
        })
        self.env["cloud.instance.domain"].create({
            "instance_id": other.id, "hostname": "shop.queue-example.com",
        })
        self.env.invalidate_all()
        with self.assertRaisesRegex(ValidationError, "already used"):
            self.env["cloud.instance.domain"].with_user(consultant).create({
                "instance_id": self.inst.id, "hostname": "shop.queue-example.com",
            })

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

    def test_a_waiting_job_aimed_at_its_host_keeps_it_when_the_instance_moves(self):
        """The old copy's teardown after a move is aimed at the old host on
        purpose: sent where the instance lives now, it removed the live
        copy (28-oct-2026). A restart waiting beside it still follows the
        instance."""
        other = self.env["cloud.host"].create({
            "name": "queue-host-3",
            "ip_address": "10.0.0.84",
            "user": "ubuntu",
            "wildcard_domain": "queue3.example.com",
        })
        rebuild = self._job("rebuild_instance")
        restart = self._enqueue("restart_instance")
        teardown = self._enqueue("move_cleanup_source")
        self.inst.host_id = other
        rebuild.write({"state": "done"})
        _run_after_commit(self.env)
        self.assertEqual(restart.state, "pending")
        self.assertEqual(restart.host_id, other)
        self.assertEqual(teardown.state, "queued")
        self._end(restart)
        _run_after_commit(self.env)
        self.assertEqual(teardown.state, "pending")
        self.assertEqual(teardown.host_id, self.host)

    def test_the_types_aimed_at_a_host_say_so(self):
        """What tears down or reaches a copy on a host named on purpose (a
        move's, an upload grant's) keeps that host; what acts on the
        instance wherever it lives follows it."""
        for code in (
            "move_rollback_cleanup", "move_cleanup_source", "move_cutover",
            "grant_restore_upload_key", "verify_restore_upload",
            "revoke_restore_upload_key",
        ):
            with self.subTest(code=code):
                self.assertTrue(self.Job._pinned_to_host(self._job(code)))
        for code in (
            "delete_instance", "restart_instance", "start_instance",
            "stop_instance", "restore_instance", "deploy_instance",
            "backup_download", "purge_archived_backups",
        ):
            with self.subTest(code=code):
                self.assertFalse(self.Job._pinned_to_host(self._job(code)))


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

    def _move_chain(self):
        """Move the instance to the target host; return the chain's steps in order."""
        self.inst.move_to_host(self.target)
        return self.inst._get_move_chain_jobs()

    def _fail_as_the_runner(self, step):
        """Start *step* and end it failed, as the runner would, then commit."""
        step.queue_job_id.write({"state": "started"})
        step.queue_job_id.write({"state": "failed", "exc_message": "boom"})
        _run_after_commit(self.env)

    def _job_failed_alert(self, job):
        """The active ``job_failed`` alert of *job*, if any."""
        return self.env["cloud.alert"].search([
            ("job_id", "=", job.id), ("code", "=", "job_failed"),
            ("state", "=", "active"),
        ])

    def test_a_failed_source_cleanup_does_not_hold_the_queue(self):
        """The move cut over, and removing the old copy from the source
        failed: the instance lives on the destination, and its backups
        and jobs go on (owner, 27-oct-2026). The failure is still
        announced, and its alert stays."""
        *steps, cleanup = self._move_chain()
        self.assertEqual(cleanup.job_type_id.code, "move_cleanup_source")
        for step in steps:
            self._end(step)
        # What the cutover's success writes.
        self.inst.write({
            "host_id": self.target.id,
            "move_origin_host_id": False,
            "move_target_host_id": False,
            "move_chain_job_ids": False,
        })
        with patch.object(
            type(self.Job), "_broadcast_job_update", autospec=True,
        ) as told:
            self._fail_as_the_runner(cleanup)
        self.assertEqual(cleanup.state, "failed")
        self.assertIn(
            (cleanup.id, "failed"),
            [(c.args[1], c.kwargs.get("state")) for c in told.call_args_list],
        )
        self.assertTrue(self._job_failed_alert(cleanup))
        self.assertFalse(self.Job._queue_held_by(self.inst.id, None))
        backup = self.Job.browse(
            self.Job.enqueue(self.target.id, self.inst.id, "backup_create"),
        )
        self.assertEqual(backup.state, "pending")
        self.assertFalse(backup._format()[0]["queue_held"])

    def test_a_failed_source_cleanup_can_still_be_retried(self):
        """Its retry runs, and its success dismisses the alert."""
        cleanup, alert = self._failed_with_alert("move_cleanup_source")
        retry = self.Job.browse(cleanup.retry_job())
        _run_after_commit(self.env)
        self.assertEqual(retry.state, "pending")
        self.assertEqual(alert.state, "active")
        self._end(retry)
        self.assertEqual(alert.state, "dismissed")

    def _assert_a_failure_before_the_cutover_holds_the_queue(self, code):
        """Fail the move at its *code* step: that step holds the queue,
        and the source's cleanup ends without running."""
        chain = self._move_chain()
        failing = chain.filtered(lambda job: job.job_type_id.code == code)
        self.assertEqual(len(failing), 1, code)
        for step in chain[:list(chain).index(failing)]:
            self._end(step)
        self._fail_as_the_runner(failing)
        cleanup = chain[-1]
        self.assertEqual(cleanup.job_type_id.code, "move_cleanup_source")
        self.assertEqual(failing.state, "failed")
        self.assertEqual(cleanup.state, "cancelled")
        self.assertFalse(cleanup.queue_job_id.date_started)
        self.assertEqual(
            self._note(cleanup), f"Not run: step #{failing.id} ended failed.",
        )
        self.assertFalse(self._job_failed_alert(cleanup))
        self.assertEqual(
            self.Job._queue_held_by(self.inst.id, None),
            self._job_failed_alert(failing),
        )
        backup = self._enqueue("backup_create")
        self.assertEqual(backup.state, "queued")
        self.assertTrue(backup._format()[0]["queue_held"])

    def test_a_deploy_that_fails_before_the_cutover_holds_the_queue(self):
        """The first step: the destination never came up (15-oct-2026's
        ``tenant_deploy_instance`` is the SaaS manager's)."""
        self._assert_a_failure_before_the_cutover_holds_the_queue("deploy_instance")

    def test_a_restore_that_fails_before_the_cutover_holds_the_queue(self):
        """The source is stopped by then: the hold keeps everything but
        the rollback out until someone acts."""
        self._assert_a_failure_before_the_cutover_holds_the_queue("restore_instance")

    def test_a_cutover_that_fails_holds_the_queue(self):
        """The instance still lives on the source: the move did not happen."""
        self._assert_a_failure_before_the_cutover_holds_the_queue("move_cutover")

    # ── The rollback tears down the target, never the source (28-oct-2026) ──

    def _connections(self, job):
        """Run *job* as the runner would, its SSH answered here.

        :return: ``[(host, command)]``, every command the executor sent,
            in order, with the host it connected to
        """
        ran = []
        Host = type(self.env["cloud.host"])

        @contextlib.asynccontextmanager
        async def connect(host):
            """A connection to *host* that records what it is sent and says 0."""
            transport = MagicMock(spec=SSHTransport)

            def answer(command, *_args, **_kwargs):
                """Record *command* on *host*; answer as a command that worked."""
                ran.append((host, command))
                return CommandResult("", 0)

            transport.execute.side_effect = answer
            transport.run.side_effect = answer
            yield transport

        with patch.object(Host, "get_transport", autospec=True, side_effect=connect):
            job.execute()
        return ran

    def _fail_the_move_at(self, code):
        """Start the move, end its steps before *code*, fail *code* as the runner."""
        chain = self._move_chain()
        failing = chain.filtered(lambda job: job.job_type_id.code == code)
        for step in chain[:list(chain).index(failing)]:
            self._end(step)
        self._fail_as_the_runner(failing)

    def _assert_the_rollback_spares_the_source(self):
        """Serve the rollback the move left queued and run its teardown.

        The start goes to the source and the teardown to the target, and
        nothing reaches the source but the start.
        """
        start, cleanup = self._rollback_jobs()
        self.assertEqual(start.job_type_id.code, "start_instance")
        _run_after_commit(self.env)
        self.assertEqual((start.state, start.host_id), ("pending", self.host))
        self._end(start)
        _run_after_commit(self.env)
        self.assertEqual(cleanup.state, "pending")
        self.assertEqual(cleanup.host_id, self.target)
        ran = self._connections(cleanup)
        self.assertEqual({host for host, _command in ran}, {self.target})
        commands = [command for _host, command in ran]
        self.assertTrue(any(
            "compose_op.sh" in command and command.endswith(" down")
            for command in commands
        ), commands)
        self.assertIn(
            'rm -rf "$HOME"/' + self.inst.get_remote_dir().removeprefix("~/"),
            commands,
        )
        self.assertEqual(self.inst.host_id, self.host)
        self.assertFalse(self.inst.move_origin_host_id)

    def test_a_rollback_tears_down_the_target_and_never_the_source(self):
        """Its cleanup waited in the instance's queue aimed at the target,
        and the dispatcher sent it where the instance lived: the source,
        whose copy it removed (instance 2807, 28-oct-2026)."""
        self._fail_the_move_at("restore_instance")
        result = self.inst.rollback_move()
        self.assertEqual(result["job_id"], self._rollback_jobs()[1].id)
        self._assert_the_rollback_spares_the_source()

    def test_the_watchdog_tears_down_the_target_and_never_the_source(self):
        """The stuck-move watchdog queues the same rollback every five minutes."""
        self._fail_the_move_at("restore_instance")
        self.env["cloud.instance"].cron_recover_stuck_moves()
        self._assert_the_rollback_spares_the_source()

    def test_the_retry_of_a_source_cleanup_after_the_cutover_stays_on_the_source(self):
        """Sent where the instance lives now, it would remove the live copy."""
        self.inst.host_id = self.target
        cleanup, _alert = self._failed_with_alert("move_cleanup_source")
        self.assertEqual(cleanup.host_id, self.host)
        retry = self.Job.browse(cleanup.retry_job())
        _run_after_commit(self.env)
        self.assertEqual(retry.state, "pending")
        self.assertEqual(retry.host_id, self.host)

    def _end_the_move(self):
        """Clear the markers, as the cutover's success or a finished rollback does."""
        self.inst.write({
            "move_origin_host_id": False,
            "move_target_host_id": False,
            "move_chain_job_ids": False,
            "move_rollback_in_progress": False,
        })

    def test_the_retry_of_a_move_step_is_refused_once_the_move_ended(self):
        """A generic step retried alone follows the instance: a restore would
        restore the source from its own dump, a stop would stop its odoo."""
        self._fail_the_move_at("restore_instance")
        failed = self.inst._get_move_chain_jobs().filtered(
            lambda job: job.job_type_id.code == "restore_instance",
        )
        self.assertEqual(failed.state, "failed")
        self._end_the_move()
        with self.assertRaisesRegex(UserError, "no longer in progress"):
            failed.retry_job()
        self.assertFalse(self.Job.search([("retry_of_id", "=", failed.id)]))

    def test_the_retry_of_a_move_step_is_refused_after_a_rollback(self):
        """The same once the rollback cleared the markers."""
        chain = self._move_chain()
        # The destination's odoo is stopped first, then the source's.
        stops = chain.filtered(lambda job: job.job_type_id.code == "stop_instance")
        failed = stops[0]
        for step in chain[:list(chain).index(failed)]:
            self._end(step)
        self._fail_as_the_runner(failed)
        self.assertEqual(failed.state, "failed")
        self.assertEqual(failed.host_id, self.target)
        self.inst.rollback_move()
        self._end_the_move()
        with self.assertRaisesRegex(UserError, "no longer in progress"):
            failed.retry_job()

    def test_the_retry_of_a_retry_of_a_move_step_is_refused_too(self):
        """A retry is a lone job: the move it came from is looked up through
        what it retries."""
        self._fail_the_move_at("restore_instance")
        failed = self.inst._get_move_chain_jobs().filtered(
            lambda job: job.job_type_id.code == "restore_instance",
        )
        # A retry from before Retry was refused while the move went on.
        retry = self._job(
            "restore_instance", state="failed", host=failed.host_id,
            retry_of_id=failed.id, payload=failed.payload,
        )
        self._end_the_move()
        with self.assertRaisesRegex(UserError, "no longer in progress"):
            retry.retry_job()

    def test_the_retry_of_a_step_of_a_move_queued_the_old_way_is_refused(self):
        """Before 23-oct-2026 a chain had no ``queue_chain_key``: it was a
        queue_job graph, known by ``graph_uuid``. Its steps are still moves:
        a stop retried alone would stop the odoo of the host the instance
        lives on."""
        chain = self._move_chain()
        failed = chain.filtered(
            lambda job: job.job_type_id.code == "stop_instance",
        )[0]
        for step in chain[:list(chain).index(failed)]:
            self._end(step)
        self._fail_as_the_runner(failed)
        self.assertEqual(failed.state, "failed")
        self.assertTrue(failed.queue_job_id.graph_uuid)
        chain.write({"queue_chain_key": False})
        self.assertTrue(self.Job._is_a_move_step(failed))
        self._end_the_move()
        with self.assertRaisesRegex(UserError, "no longer in progress"):
            failed.retry_job()
        self.assertFalse(self.Job.search([("retry_of_id", "=", failed.id)]))

    def test_a_job_with_neither_chain_key_nor_graph_is_not_a_move_step(self):
        """The normal retry stays as it was."""
        lone = self._job("restore_instance", state="failed", host=self.host)
        self.assertFalse(lone.queue_chain_key)
        self.assertFalse(self.Job._is_a_move_step(lone))

    def test_the_retry_of_a_step_of_an_earlier_move_is_refused_in_a_new_one(self):
        """Only the steps of the move under way may be retried."""
        self._fail_the_move_at("restore_instance")
        old = self.inst._get_move_chain_jobs().filtered(
            lambda job: job.job_type_id.code == "restore_instance",
        )
        self._end_the_move()
        self.inst.move_to_host(self.target)
        self.assertTrue(self.inst.move_origin_host_id)
        with self.assertRaisesRegex(UserError, "no longer in progress"):
            old.retry_job()

    def _assert_the_retry_is_refused_while_the_move_goes_on(self, failed):
        """Retry of *failed* is refused, and no retry is queued.

        :param failed: a failed step of the move under way, or being
            rolled back
        """
        with self.assertRaisesRegex(UserError, "Roll back move"):
            failed.retry_job()
        self.assertFalse(self.Job.search([("retry_of_id", "=", failed.id)]))

    def test_the_retry_of_a_move_step_is_refused_while_the_move_goes_on(self):
        """Its retry is a lone job: it followed the instance to the source,
        where a restore aimed at the target restored the dump over the only
        live copy. The steps after it ended «Not run»: run alone, it does
        not resume the move."""
        self._fail_the_move_at("restore_instance")
        failed = self.inst._get_move_chain_jobs().filtered(
            lambda job: job.job_type_id.code == "restore_instance",
        )
        self.assertEqual(failed.host_id, self.target)
        self._assert_the_retry_is_refused_while_the_move_goes_on(failed)

    def _assert_the_retry_is_refused_during_the_rollback(self, code):
        """Fail the move at its *code* step, roll it back, and retry the step.

        The source is started again and takes writes: a retried stop aimed
        at the target would stop it, a restore would overwrite it.
        """
        self._fail_the_move_at(code)
        failed = self.inst._get_move_chain_jobs().filtered(
            lambda job: job.job_type_id.code == code,
        )
        self.inst.rollback_move()
        self.assertTrue(self.inst.move_rollback_in_progress)
        self._assert_the_retry_is_refused_while_the_move_goes_on(failed)

    def test_the_retry_of_a_restore_is_refused_during_the_rollback(self):
        """The restore aimed at the target, sent to the live source."""
        self._assert_the_retry_is_refused_during_the_rollback("restore_instance")

    def test_the_retry_of_a_cutover_is_refused_during_the_rollback(self):
        """A step aimed at its host on purpose is refused too: the
        rollback is what runs now."""
        self._assert_the_retry_is_refused_during_the_rollback("move_cutover")

    def test_a_retried_move_step_keeps_its_host(self):
        """Should a retry of a move's step ever be queued — a retry of a
        retry included — it runs where the step was asked for, never where
        the instance lives."""
        self._fail_the_move_at("restore_instance")
        failed = self.inst._get_move_chain_jobs().filtered(
            lambda job: job.job_type_id.code == "restore_instance",
        )
        self._job_failed_alert(failed).write({"state": "dismissed"})
        retry = self._job(
            "restore_instance", state="failed", host=failed.host_id,
            retry_of_id=failed.id, payload=failed.payload,
        )
        again = self._job(
            "restore_instance", state="queued", host=failed.host_id,
            retry_of_id=retry.id, payload=failed.payload, queue_head=True,
        )
        self.assertEqual(self.inst.host_id, self.host)
        self.Job._dispatch_next(self.inst.id, None)
        self.assertEqual((again.state, again.host_id), ("pending", self.target))

    def test_the_retry_of_jobs_outside_a_move_is_unchanged(self):
        """A lone restore, or a step of another chain, retried on an instance
        that moved before, runs as it always did."""
        self._fail_the_move_at("restore_instance")
        self._end_the_move()
        lone, _alert = self._failed_with_alert("restore_instance")
        retry = self.Job.browse(lone.retry_job())
        self.assertEqual(retry.retry_of_id, lone)
        self.assertEqual(retry.state, "queued")
        other = self._instance("queue-other")
        first, second = self.Job.browse(self.Job.enqueue_chain([
            {"host_id": self.host.id, "instance_id": other.id,
             "job_type_code": "stop_instance"},
            {"host_id": self.host.id, "instance_id": other.id,
             "job_type_code": "start_instance"},
        ]))
        self._fail_as_the_runner(first)
        self.assertEqual(first.state, "failed")
        again = self.Job.browse(first.retry_job())
        self.assertEqual(again.retry_of_id, first)


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

    def test_a_concurrent_update_of_the_transition_is_the_runners_to_retry(self):
        """The transition's own writes flush outside the hooks' savepoint:
        a serialization failure reaches queue_job, which postpones the job,
        instead of being logged as the hook's and failing it."""
        rebuild = self._enqueue("rebuild_instance")
        rebuild.queue_job_id.write({"state": "started"})
        conflict = SerializationFailure(
            "could not serialize access due to concurrent update"
        )
        # The test's own savepoint first: assertRaises flushes on entry.
        with (
            self.assertRaises(SerializationFailure),
            patch.object(type(self.Job), "_after_job_terminal", autospec=True) as hook,
            patch.object(
                type(self.env), "flush_all", autospec=True, side_effect=conflict,
            ),
        ):
            rebuild.queue_job_id.write({"state": "done"})
        hook.assert_not_called()

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


@tagged("-at_install", "post_install")
class TestTheEnvironmentIsNotSavedLater(HttpCase):
    """``/cloud/save_instance`` says why an environment is not changed.

    The panel's form sends the instance's environment back on every save,
    and the MCP's ``update_instance`` hands an agent's values to the same
    route: a change is refused with its reason, as ``{"ok": False}``,
    which both show, rather than dropped without a word.
    """

    def setUp(self):
        """A staging in draft, and a manager signed in to the panel."""
        super().setUp()
        project = self.env["cloud.project"].create({"name": "Env route"})
        self.inst = self.env["cloud.instance"].create({
            "name": "env-route",
            "project_id": project.id,
            "environment": "staging",
            "state": "draft",
        })
        new_test_user(
            self.env, login="env-manager", password="env-manager",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        self.authenticate("env-manager", "env-manager")

    def _save(self, vals):
        """Save *vals* on the staging through ``/cloud/save_instance``.

        :param dict vals: what the form or an agent sends
        :returns: the route's answer
        """
        payload = {
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"instance_id": self.inst.id, "vals": vals},
        }
        return self.url_open(
            "/cloud/save_instance",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        ).json()["result"]

    def test_a_change_is_refused_with_its_reason(self):
        """Asked to make the staging production, the route says no, and why."""
        answer = self._save({"environment": "production"})
        self.assertFalse(answer["ok"])
        self.assertIn("chosen when it is created", answer["error"])
        self.inst.invalidate_recordset()
        self.assertEqual(self.inst.environment, "staging")

    def test_the_form_saving_it_as_it_is_still_saves(self):
        """The form's save, with the environment as it is, goes through."""
        answer = self._save({"environment": "staging", "autopurge_exempt": True})
        self.assertTrue(answer["ok"])
        self.inst.invalidate_recordset()
        self.assertTrue(self.inst.autopurge_exempt)


@tagged("-at_install", "post_install")
class TestEveryRoleNamesTheBackupBackends(HttpCase):
    """The forms that create an instance or a project, and a project's
    settings, load the backup backends from ``/cloud/get_backup_backends``.

    It answered managers alone, so below them the panel failed to open a
    new instance's form, a new project's and a project's settings. Below a
    manager it now names them — what ``/cloud/get_project_full`` already
    tells a member — and their bucket, path and endpoint stay a manager's.
    """

    def setUp(self):
        """A backend, and a consultant and a manager of the panel."""
        super().setUp()
        self.env["cloud.backup.backend"].create({
            "name": "Roles S3", "backend_type": "s3", "s3_bucket": "roles-secret-bucket",
        })
        for login, group in (
            ("roles-consultant", "incubacloud.group_cloud_consultant"),
            ("roles-manager", "incubacloud.group_cloud_manager"),
        ):
            new_test_user(
                self.env, login=login, password=login,
                groups=f"base.group_user,{group}",
            )

    def _backends(self, login):
        """``/cloud/get_backup_backends`` as *login*.

        :param str login: who asks
        :returns: the JSON-RPC answer
        """
        self.authenticate(login, login)
        payload = {"jsonrpc": "2.0", "method": "call", "id": 1, "params": {}}
        return self.url_open(
            "/cloud/get_backup_backends",
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        ).json()

    def test_a_consultant_gets_the_names_and_nothing_else(self):
        """Named for the forms, without the bucket."""
        answer = self._backends("roles-consultant")
        self.assertNotIn("error", answer)
        item = next(i for i in answer["result"]["items"] if i["name"] == "Roles S3")
        self.assertEqual(set(item), {"id", "name"})
        self.assertNotIn("roles-secret-bucket", json.dumps(answer))

    def test_a_manager_still_gets_the_whole_list(self):
        """The manager's list, for the backends page, is as it was."""
        answer = self._backends("roles-manager")
        item = next(i for i in answer["result"]["items"] if i["name"] == "Roles S3")
        self.assertEqual(item["s3_bucket"], "roles-secret-bucket")
