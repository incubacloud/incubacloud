"""What a health check leaves in its job log.

The probe runs every five minutes for every deployed instance. Written
in full, the log of a check that found nothing was 83% of the panel's
job log rows (19-oct-2026), and said nothing the instance did not
already show. The log is now held until the check ends: a cycle that
passed keeps its verdict, any other cycle keeps every line.
"""
import asyncio
from contextlib import suppress
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.abstract_executor import AbstractExecutor
from odoo.addons.incubacloud.models.instance_health_executor import (
    InstanceHealthExecutor,
)


class TestHealthCheckLog(TransactionCase):

    def setUp(self):
        super().setUp()
        # Alerts and log lines are written on the executor's own cursor;
        # test mode makes it ride on this transaction, so both are
        # really written and really rolled back.
        self.registry_enter_test_mode()
        self.project = self.env["cloud.project"].create({"name": "HLog Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "hlog-host",
            "ip_address": "192.0.2.66",
            "user": "ubuntu",
            "wildcard_domain": "hlog.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "hloginst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self.job = self.env["cloud.job"].create({
            "name": "Health",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "instance_health")], limit=1,
            ).id,
        })
        self.executor = InstanceHealthExecutor(self.job, self.host)
        self.executor._skipped = False

    def _results(self, http_exit=0, **states):
        """Return the command results of one cycle.

        Every service the instance expects is ``running`` unless
        *states* says otherwise.

        :param int http_exit: exit status of the HTTP probe
        :returns: results as ``parse_results`` receives them
        """
        listing = dict.fromkeys(self.instance.expected_services(), "running")
        listing.update(states)
        return {
            "container_state": {
                "stdout": "\n".join(f"{s}\t{v}" for s, v in listing.items()),
            },
            "cpu_mem_snapshot": {"stdout": "1.0\t20.0"},
            "http_health": {"stdout": f"exit:{http_exit}"},
            "error_lines": {"stdout": ""},
        }

    def _check(self, results, then_raise=None):
        """Run one cycle without SSH and flush its log as ``run()`` does.

        :param dict results: what the commands answered
        :param Exception then_raise: raised once ``on_success`` is done,
            as a failing cleanup step would
        :returns: the contents of the job's log, in order
        """
        async def entry(executor):
            executor._sys("Connection established.")
            executor._sys("Running: container_state")
            if not executor.parse_results(results):
                await executor.on_success(results)
            if then_raise:
                raise then_raise

        with patch.object(
            AbstractExecutor, "_async_entry", autospec=True, side_effect=entry,
        ):
            with suppress(RuntimeError):
                asyncio.run(self.executor._async_entry())
        self.executor._flush_logs()
        return self._log()

    def _log(self):
        """Return the contents of the job's log lines, in order."""
        return self.env["cloud.job.log.chunk"].search(
            [("job_id", "=", self.job.id)], order="id",
        ).mapped("content")

    def test_nothing_is_written_while_the_check_runs(self):
        self.executor._log_settled = False  # what ``run`` does first
        self.executor._sys("Checking health of 'hloginst'…")
        self.assertEqual(self.executor._flush_logs(), 0)
        self.assertEqual(self._log(), [])

    def test_a_check_refused_before_it_ran_keeps_its_log(self):
        """Refused before ``run`` (a guard, a lost lock), nothing holds it."""
        self.executor._sys("✗ Refused before running.")
        self.executor._flush_logs()
        self.assertEqual(self._log(), ["✗ Refused before running."])

    def test_a_cycle_that_passed_keeps_only_its_verdict(self):
        log = self._check(self._results())
        self.assertEqual(self.instance.status, "ok")
        self.assertEqual(log, [self.executor._verdict])
        self.assertIn("healthy", log[0])

    def test_a_skipped_cycle_keeps_why_it_was_skipped(self):
        self.executor._skipped = True
        log = self._check({"noop": {"stdout": "", "exit_status": 0}})
        self.assertEqual(len(log), 1)
        self.assertIn("intrusive job", log[0])

    def test_a_cycle_that_reported_something_keeps_its_whole_log(self):
        """A companion down goes through ``_inst_alert``: the first
        sighting is only a suspicion, and that already counts."""
        log = self._check(self._results(db="exited"))
        self.assertEqual(self.instance.status, "warning")
        self.assertIn("Connection established.", log)
        self.assertIn("Running: container_state", log)
        self.assertGreater(len(log), 2)

    def test_a_cycle_that_did_not_pass_keeps_its_whole_log(self):
        """One failed HTTP probe sets the status without an alert yet:
        no verdict, so every line stays."""
        log = self._check(self._results(http_exit=7))
        self.assertEqual(self.instance.status, "error")
        self.assertIsNone(self.executor._verdict)
        self.assertIn("Connection established.", log)
        self.assertGreater(len(log), 2)

    def test_a_cycle_that_raised_keeps_its_whole_log(self):
        log = self._check(self._results(), then_raise=RuntimeError("cleanup"))
        self.assertIsNone(self.executor._verdict)
        self.assertIn("Connection established.", log)
        self.assertGreater(len(log), 2)
