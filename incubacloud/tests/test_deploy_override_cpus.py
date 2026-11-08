"""The CPUs an instance is deployed with fit the server it runs on.

Docker refuses to create a container whose ``cpus`` is above the
daemon's ``NCPU`` ("range of CPUs is from 0.01 to 2.00, as there are
only 2 CPUs available"), and ``docker compose up -d`` had already
recreated ``db`` when it refused ``odoo``: a plan of 2.5 CPUs applied
on a server of 2 left the database ``Created``, never started, and the
panel down (job 457397, 8-nov-2026). The override written on the host
therefore asks at most the server's CPUs, read from the daemon when the
job renders it; the instance's fields keep what was sold.
"""
import asyncio
from unittest.mock import MagicMock, patch

import asyncssh
import yaml

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models import deploy_instance_executor
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport

from ._template import pin_template

#: What the job asks the daemon.
_NCPU = "docker info --format '{{.NCPU}}'"


class TestTheCpusFitTheHost(TransactionCase):

    def setUp(self):
        """An instance with a plan's CPUs on a host that has not been measured."""
        super().setUp()
        pin_template(self.env)
        self.project = self.env["cloud.project"].create({"name": "Cpu Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "cpu-host",
            "ip_address": "192.0.2.71",
            "user": "ubuntu",
            "wildcard_domain": "cpu.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "cpuinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
            "odoo_cpus": 2.5,
            "db_cpus": 1.0,
        })

    def _executor(self, executor_cls=DeployInstanceExecutor, code="deploy_instance"):
        """Build *executor_cls* around a fresh job on the instance."""
        job = self.env["cloud.job"].create({
            "name": f"Cpu {code}",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", code)], limit=1,
            ).id,
        })
        return executor_cls(job, self.host)

    @staticmethod
    def _transport(answer):
        """Return a host link whose daemon answers *answer* about its CPUs.

        :param answer: a ``CommandResult``, or an exception to raise
        """
        transport = MagicMock(spec=SSHTransport)
        if isinstance(answer, BaseException):
            transport.run.side_effect = answer
        else:
            transport.run.return_value = answer
        return transport

    def _services(self, executor, answer):
        """Read the host's CPUs through *answer*, render; return the services."""
        transport = self._transport(answer)
        asyncio.run(executor._read_host_cpus(transport))
        transport.run.assert_awaited_once()
        self.assertEqual(transport.run.await_args.args[0], _NCPU)
        return yaml.safe_load(executor._resource_override_content())["services"]

    @staticmethod
    def _log(executor):
        """Return the system lines the executor wrote."""
        return [msg for msg, _kind in executor._log_buffer]

    def test_a_plan_above_the_host_is_limited_to_its_cpus(self):
        """Agency's 2.5 on a server of 2: odoo at 2, db's 1.0 untouched."""
        executor = self._executor()
        services = self._services(executor, CommandResult("2", 0))
        self.assertEqual(services["odoo"]["cpus"], 2)
        self.assertEqual(services["db"]["cpus"], 1.0)
        self.assertIn(
            "odoo: 2.5 CPUs set, the server has 2: limited to 2.",
            self._log(executor),
        )
        self.assertEqual(executor._cpu_clamps, (("odoo", 2.5, 2),))

    def test_cpus_equal_to_the_host_are_left_alone(self):
        """Business's 2.0 on a server of 2 started (job 457396): margin 0."""
        self.instance.odoo_cpus = 2.0
        executor = self._executor()
        services = self._services(executor, CommandResult("2", 0))
        self.assertEqual(services["odoo"]["cpus"], 2.0)
        self.assertEqual(executor._cpu_clamps, ())
        self.assertFalse([m for m in self._log(executor) if "limited to" in m])

    def test_a_failed_reading_falls_back_to_the_measured_cores(self):
        """The daemon does not answer: the metrics' ``cpu_cores`` stands in."""
        self.host.cpu_cores = 1
        executor = self._executor()
        services = self._services(executor, CommandResult("Cannot connect", 1))
        self.assertEqual(services["odoo"]["cpus"], 1)
        self.assertEqual(services["db"]["cpus"], 1.0)

    def test_an_answer_that_is_no_count_falls_back_too(self):
        """An empty or odd answer is no count of CPUs."""
        self.host.cpu_cores = 2
        for answer in ("", "zero", "0", "-1"):
            with self.subTest(answer=answer):
                executor = self._executor()
                services = self._services(executor, CommandResult(answer, 0))
                self.assertEqual(services["odoo"]["cpus"], 2)

    def test_a_reading_that_runs_out_of_time_falls_back(self):
        """A daemon that hangs costs its timeout, short, and no more."""
        self.host.cpu_cores = 2
        executor = self._executor()
        services = self._services(executor, TimeoutError())
        self.assertEqual(services["odoo"]["cpus"], 2)
        timeout = deploy_instance_executor.HOST_CPUS_TIMEOUT_S
        self.assertLessEqual(timeout, 20)
        self.assertGreaterEqual(timeout, 10)

    def test_a_link_that_fails_falls_back_to_the_measured_cores(self):
        """A dropped connection or a refused channel is no reason to fail the job."""
        self.host.cpu_cores = 2
        for error in (
            asyncssh.ConnectionLost("gone"),
            asyncssh.ChannelOpenError(1, "refused"),
            ConnectionResetError(),
            OSError("broken pipe"),
        ):
            with self.subTest(error=type(error).__name__):
                executor = self._executor()
                services = self._services(executor, error)
                self.assertEqual(services["odoo"]["cpus"], 2)

    def test_the_reading_is_asked_with_a_short_timeout(self):
        executor = self._executor()
        transport = self._transport(CommandResult("4", 0))
        asyncio.run(executor._read_host_cpus(transport))
        self.assertEqual(
            transport.run.await_args.kwargs.get("timeout"),
            deploy_instance_executor.HOST_CPUS_TIMEOUT_S,
        )

    def test_nothing_to_read_limits_nothing_and_says_so(self):
        """No answer and a host never measured: today's behaviour."""
        executor = self._executor()
        services = self._services(executor, CommandResult("", 1))
        self.assertEqual(services["odoo"]["cpus"], 2.5)
        self.assertIsNone(executor._host_cpus)
        self.assertTrue([
            m for m in self._log(executor)
            if "could not be read" in m and "not limited" in m
        ])

    def test_the_daemon_wins_over_a_stale_measurement(self):
        """A server resized down an instant ago: the daemon knows first."""
        self.host.cpu_cores = 8
        executor = self._executor()
        services = self._services(executor, CommandResult("2\n", 0))
        self.assertEqual(services["odoo"]["cpus"], 2)

    def test_the_instance_keeps_what_was_sold(self):
        """Neither the fields nor the configuration's hash know the host."""
        before = self.instance._config_snapshot_hash()
        self.instance.write(self.instance._applied_config_vals())
        executor = self._executor()
        self._services(executor, CommandResult("2", 0))
        self.assertEqual(self.instance.odoo_cpus, 2.5)
        self.assertEqual(self.instance._config_snapshot_hash(), before)
        self.assertFalse(self.instance.config_dirty)

    def test_the_core_defaults_fit_a_server_of_one_cpu(self):
        """``odoo_cpus`` 2.0 by default on a VPS of 1 vCPU."""
        self.instance.write({"odoo_cpus": 2.0, "db_cpus": 1.0})
        executor = self._executor()
        services = self._services(executor, CommandResult("1", 0))
        self.assertEqual(services["odoo"]["cpus"], 1)
        self.assertEqual(services["db"]["cpus"], 1.0)
        self.assertEqual(executor._cpu_clamps, (("odoo", 2.0, 1),))

    def test_a_rebuild_renders_the_same(self):
        executor = self._executor(RebuildInstanceExecutor, "rebuild_instance")
        services = self._services(executor, CommandResult("2", 0))
        self.assertEqual(services["odoo"]["cpus"], 2)

    def test_a_render_without_a_reading_limits_nothing(self):
        """An executor built without ``__init__`` renders as before."""
        executor = DeployInstanceExecutor.__new__(DeployInstanceExecutor)
        executor.env = self.env
        job = MagicMock(spec=type(self.env["cloud.job"]))
        job.instance_id = self.instance
        executor.job = job
        self.assertIsNone(executor._host_cpus)
        self.assertEqual(executor._cpu_clamps, ())
        services = yaml.safe_load(executor._resource_override_content())["services"]
        self.assertEqual(services["odoo"]["cpus"], 2.5)

    def test_the_copier_upload_reads_the_cpus_before_rendering(self):
        """The override the job uploads is the limited one."""
        executor = self._executor()
        transport = self._transport(CommandResult("2", 0))
        with patch.object(
            DeployInstanceExecutor, "_resync_repo_requirements", autospec=True,
        ), patch.object(
            DeployInstanceExecutor, "_addons_yaml_content",
            autospec=True, return_value="{}\n",
        ), patch.object(
            DeployInstanceExecutor, "_backup_env_content",
            autospec=True, return_value=None,
        ):
            asyncio.run(executor._upload_copier_files(transport))
        uploaded = {}
        for upload in transport.upload_text_files.await_args_list:
            uploaded.update(upload.args[0])
        override = yaml.safe_load(uploaded[executor._tmp("override.yml")])
        self.assertEqual(override["services"]["odoo"]["cpus"], 2)


class TestTheLimitIsReportedFromTheOutcome(TransactionCase):
    """What was limited is handed to ``_report_cpu_clamps`` from the end
    hooks, on their durable cursor, never from the render."""

    def setUp(self):
        """An instance on a host."""
        super().setUp()
        pin_template(self.env)
        project = self.env["cloud.project"].create({"name": "Cpu Report"})
        self.host = self.env["cloud.host"].create({
            "name": "cpu-report-host",
            "ip_address": "192.0.2.72",
            "user": "ubuntu",
            "wildcard_domain": "cpur.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "cpureport",
            "project_id": project.id,
            "environment": "production",
            "host_id": self.host.id,
        })

    def _executor(self, executor_cls, code):
        """Build *executor_cls* around a fresh job on the instance."""
        job = self.env["cloud.job"].create({
            "name": f"Report {code}",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", code)], limit=1,
            ).id,
        })
        return executor_cls(job, self.host)

    def test_every_end_hook_reports(self):
        for cls, code in (
            (DeployInstanceExecutor, "deploy_instance"),
            (RebuildInstanceExecutor, "rebuild_instance"),
        ):
            for hook, args in (
                ("on_success", ({},)),
                ("on_failure", ({}, ["'Restart instance' exited with status 1"])),
            ):
                executor = self._executor(cls, code)
                if cls is DeployInstanceExecutor and hook == "on_success":
                    self.instance._transition("deploying")
                with self.subTest(cls=cls.__name__, hook=hook), patch.object(
                    cls, "_report_cpu_clamps", autospec=True,
                ) as report, patch.object(
                    type(self.instance), "_after_stack_rendered", autospec=True,
                ):
                    asyncio.run(getattr(executor, hook)(*args))
                    report.assert_called_once_with(executor)

    def test_core_reports_nothing_of_its_own(self):
        """A core panel's operator reads the job's log line; no alert."""
        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        executor._cpu_clamps = (("odoo", 2.0, 1),)
        alerts = self.env["cloud.alert"].search_count([])
        executor._report_cpu_clamps()
        self.assertEqual(self.env["cloud.alert"].search_count([]), alerts)


class TestARebuildFailureMayLeaveTheInstanceUp(TransactionCase):
    """``_failure_leaves_error``: a layer whose failure put the stack back
    as it was says so, and the instance is not marked in error."""

    def setUp(self):
        """An instance on a host, healthy."""
        super().setUp()
        project = self.env["cloud.project"].create({"name": "Cpu Fail"})
        self.host = self.env["cloud.host"].create({
            "name": "cpu-fail-host",
            "ip_address": "192.0.2.73",
            "user": "ubuntu",
            "wildcard_domain": "cpuf.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "cpufail",
            "project_id": project.id,
            "environment": "production",
            "host_id": self.host.id,
            "status": "ok",
        })
        job = self.env["cloud.job"].create({
            "name": "Fail rebuild",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "rebuild_instance")], limit=1,
            ).id,
        })
        self.executor = RebuildInstanceExecutor(job, self.host)

    def test_a_failure_marks_the_instance_in_error_by_default(self):
        results = {"Restart instance": {"stdout": "", "exit_status": 1}}
        self.assertTrue(self.executor._failure_leaves_error(results))
        asyncio.run(self.executor.on_failure(results, ["failed"]))
        self.assertEqual(self.instance.status, "error")

    def test_a_layer_that_put_it_back_keeps_its_status(self):
        results = {"Restart instance": {"stdout": "", "exit_status": 76}}
        with patch.object(
            RebuildInstanceExecutor, "_failure_leaves_error",
            autospec=True, return_value=False,
        ) as leaves:
            asyncio.run(self.executor.on_failure(results, ["rolled back"]))
        leaves.assert_called_once_with(self.executor, results)
        self.assertEqual(self.instance.status, "ok")
