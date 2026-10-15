"""A host move's steps, closed one after another on the same instance.

``test_cloud_instance_move`` pins the chain's shape, its rollback and the
watchdog, with the executors mocked away. Nothing ran the steps' own
closing hooks in order, on the one record they all share — and that is
where a move broke: the state machine (2026-07-30) refused the first
step's ``deployed`` → ``deployed``, because a move deploys a record that
is still live on its source. Every move failed at its first step until a
customer's upgrade to a paid plan hit it on 2026-10-15.

Here the chain is built by the real ``move_to_host`` and ``enqueue_chain``,
and each job's ``on_success`` runs through the executor the job runner
would pick for it. Only the SSH connection is refused.
"""
import asyncio
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from ..models.registry import executor_registry


class TestAMoveClosesEveryStep(TransactionCase):

    def setUp(self):
        """A deployed instance on one host, a second host to move it to."""
        super().setUp()
        project = self.env["cloud.project"].create({"name": "chain-proj"})
        hosts = {}
        for name, ip in (("chain-src", "192.0.2.61"), ("chain-tgt", "192.0.2.62")):
            hosts[name] = self.env["cloud.host"].create({
                "name": name, "ip_address": ip, "port": 22, "user": "root",
                "login_type": "ssh_key", "wildcard_domain": f"{name}.example.com",
                "status": "compatible", "traefik_deployed": True,
            })
        self.source, self.target = hosts["chain-src"], hosts["chain-tgt"]
        backend = self.env["cloud.backup.backend"].create({
            "name": "chain-bb", "backend_type": "s3", "s3_bucket": "chain-bucket",
        })
        self.inst = self.env["cloud.instance"].create({
            "name": "chain-movable", "project_id": project.id,
            "environment": "production", "host_id": self.source.id,
            "state": "deployed", "backup_backend_id": backend.id,
        })

    def _chain(self):
        """The move's jobs, in the order the chain runs them."""
        self.inst.move_to_host(self.target)
        ids = [int(i) for i in self.inst.move_chain_job_ids.split(",")]
        return self.env["cloud.job"].browse(ids)

    def _close(self, job):
        """Run *job*'s ``on_success`` through the executor the runner picks.

        :param job: a ``cloud.job`` of the chain
        """
        executor_cls = executor_registry.get(job.job_type_id.code)
        executor = executor_cls(job, job.host_id)
        asyncio.run(executor.on_success({}))

    def test_every_step_closes_and_the_instance_lands_on_the_target(self):
        Host = type(self.env["cloud.host"])
        with patch.object(
            Host, "get_transport", autospec=True,
            side_effect=ConnectionRefusedError("no SSH in tests"),
        ):
            for job in self._chain():
                with self.subTest(step=job.job_type_id.code):
                    self._close(job)
        self.assertEqual(self.inst.host_id, self.target)
        self.assertEqual(self.inst.state, "deployed")
        self.assertTrue(self.inst.running)
        self.assertFalse(self.inst.move_origin_host_id)
        self.assertFalse(self.inst.move_target_host_id)

    def test_the_deploy_on_the_target_leaves_the_live_record_deployed(self):
        """The step that broke: the record is live on its source the whole
        time, so the deploy on the target must not move its state."""
        deploy = self._chain()[0]
        self.assertEqual(deploy.host_id, self.target)
        self._close(deploy)
        self.assertEqual(self.inst.state, "deployed")
        self.assertEqual(self.inst.host_id, self.source)
        self.assertEqual(self.inst.move_origin_host_id, self.source)

    def test_an_undone_move_says_so_to_whoever_inherits_the_record(self):
        """A rollback leaves the instance on its source; the layer above
        (SaaS: the tenant the move put in 'provisioning') must hear it."""
        self._chain()
        cleanup_id = self.inst._do_rollback_move()
        cleanup = self.env["cloud.job"].browse(cleanup_id)
        Instance = type(self.inst)
        with patch.object(
            Instance, "_on_move_rolled_back", autospec=True,
        ) as rolled_back:
            self._close(cleanup)
        rolled_back.assert_called_once_with(self.inst)
        self.assertFalse(self.inst.move_origin_host_id)
        self.assertEqual(self.inst.host_id, self.source)

    def test_a_rollback_whose_cleanup_failed_is_undone_too(self):
        """The watchdog clears the markers of a rollback whose target
        could not be cleaned: the source runs, so the move is undone."""
        self._chain()
        self.inst._do_rollback_move()
        self.env["cloud.job"].search(
            [("instance_id", "=", self.inst.id)],
        ).write({"state": "failed"})
        Instance = type(self.inst)
        with patch.object(
            Instance, "_on_move_rolled_back", autospec=True,
        ) as rolled_back:
            self.env["cloud.instance"].cron_recover_stuck_moves()
        rolled_back.assert_called_once_with(self.inst)
        self.assertFalse(self.inst.move_origin_host_id)

    def test_a_first_deploy_still_ends_deployed(self):
        """Outside a move, the deploy still owns the transition."""
        fresh = self.env["cloud.instance"].create({
            "name": "chain-fresh", "project_id": self.inst.project_id.id,
            "environment": "staging", "host_id": self.source.id,
            "state": "deploying",
        })
        job = self.env["cloud.job"].create({
            "name": "first deploy", "host_id": self.source.id,
            "instance_id": fresh.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "deploy_instance")], limit=1,
            ).id,
        })
        self._close(job)
        self.assertEqual(fresh.state, "deployed")
