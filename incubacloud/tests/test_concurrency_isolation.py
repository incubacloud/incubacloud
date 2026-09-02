"""The background writers must wait for a row, not lose it.

Odoo runs every cursor at REPEATABLE READ. That is right for request
handling and wrong for two crons that stamp disjoint columns on the
same ``cloud_instance`` rows: under snapshot isolation the second one
is aborted with ``could not serialize access due to concurrent
update``, even though nothing about the two writes actually conflicts.

It is pinned here rather than left to a comment because the failure it
prevents is invisible in normal runs and expensive when it lands:
PostgreSQL's error reaches Odoo's SQL layer, which logs ``bad query``
at ERROR before any Python can catch it, and the tenant log scraper
reports those lines straight back to the panel as an
``instance_error_logs`` alert. Seen on five separate days in August.

The behaviour of the helper is asserted directly; the two call sites
are asserted as wiring, because a test that enters test mode gets a
pseudo-cursor whose isolation level is not ours to change.
"""
import asyncio
from contextlib import contextmanager
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models._concurrency import (
    read_committed_cursor,
    try_advisory_xact_lock,
)
from odoo.addons.incubacloud.models.abstract_executor import AbstractSSHExecutor

_METRICS = "odoo.addons.incubacloud.models.cloud_instance_metrics"
_EXECUTOR = "odoo.addons.incubacloud.models.abstract_executor"


class TestReadCommittedCursor(TransactionCase):

    def test_helper_yields_a_read_committed_transaction(self):
        with read_committed_cursor(self.env.registry) as cr:
            cr.execute("SHOW transaction_isolation")
            self.assertEqual(cr.fetchone()[0], "read committed")

    def test_default_cursor_is_still_repeatable_read(self):
        """Guards the claim above: the default really is the strict one."""
        with self.env.registry.cursor() as cr:
            cr.execute("SHOW transaction_isolation")
            self.assertEqual(cr.fetchone()[0], "repeatable read")

    def test_a_borrowed_transaction_is_left_alone(self):
        """A cursor riding on someone else's transaction must not be re-declared.

        ``SET TRANSACTION`` is illegal once the transaction has run a
        query, and a pseudo-cursor always has: it wraps an open one.
        Attempting it anyway aborts the caller's transaction, which is
        a far worse outcome than inheriting an isolation level.
        """
        self.registry_enter_test_mode()
        with read_committed_cursor(self.env.registry) as cr:
            cr.execute("SELECT 1")
            self.assertEqual(cr.fetchone()[0], 1)


class TestAdvisoryTransactionLock(TransactionCase):
    """The shared advisory helper is non-blocking and transaction scoped."""

    def test_same_database_namespace_and_scope_contend(self):
        """A second real connection fails immediately on the same key."""
        with self.env.registry.cursor() as first, \
                self.env.registry.cursor() as second:
            self.assertTrue(try_advisory_xact_lock(first, "test", 7))
            self.assertFalse(try_advisory_xact_lock(second, "test", 7))
            first.rollback()
            self.assertTrue(try_advisory_xact_lock(second, "test", 7))

    def test_distinct_scopes_do_not_contend(self):
        """Different users can hold their feature locks concurrently."""
        with self.env.registry.cursor() as first, \
                self.env.registry.cursor() as second:
            self.assertTrue(try_advisory_xact_lock(first, "test", 7))
            self.assertTrue(try_advisory_xact_lock(second, "test", 8))


class TestLivenessCronIsolation(TransactionCase):
    """The metrics cron stamps through the helper, not a plain cursor."""

    def test_cron_stamps_through_the_helper(self):
        used = []

        @contextmanager
        def _spy(registry):
            used.append(registry)
            with registry.cursor() as cr:
                yield cr

        self.registry_enter_test_mode()
        settings = self.env["cloud.settings"].sudo()._get_system()
        settings.write({
            "metrics_enabled": True,
            "metrics_central_url": "http://vm.test:8428",
        })
        project = self.env["cloud.project"].create({"name": "Iso"})
        inst = self.env["cloud.instance"].create({
            "name": "isoinst", "project_id": project.id,
            "environment": "staging",
        })

        # The cron's cursor reads the database, not this env's pending
        # writes — see the note in the liveness tests.
        self.env.flush_all()
        with patch(
            f"{_METRICS}.promql_query",
            return_value=[({"instance_id": str(inst.id)}, 5)],
        ), patch(f"{_METRICS}.read_committed_cursor", _spy):
            self.env["cloud.instance"]._cron_refresh_running_from_metrics()

        self.assertTrue(
            used, "the liveness cron must stamp on a read-committed cursor",
        )
        inst.invalidate_recordset()
        self.assertTrue(inst.running)


class TestExecutorOutcomeIsolation(TransactionCase):
    """``on_success``/``on_failure`` write ``cloud_instance`` too."""

    def test_dispatch_outcome_runs_through_the_helper(self):
        used = []

        @contextmanager
        def _spy(registry):
            used.append(registry)
            with registry.cursor() as cr:
                yield cr

        self.registry_enter_test_mode()
        host = self.env["cloud.host"].create({
            "name": "iso-host",
            "ip_address": "192.0.2.65",
            "user": "ubuntu",
            "wildcard_domain": "iso.example.com",
        })
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "host_probe")], limit=1,
        ) or self.env["cloud.job.type"].create({
            "name": "host_probe", "code": "host_probe", "apply_to": "host",
        })
        job = self.env["cloud.job"].create({
            "name": "Iso probe",
            "host_id": host.id,
            "job_type_id": job_type.id,
        })

        ran = []

        class _Probe(AbstractSSHExecutor):
            _job_type = None

            def get_commands(self):
                return []

            def parse_results(self, results):
                return []

            async def on_success(self, results):
                ran.append(True)

        executor = _Probe(job, host)
        with patch(f"{_EXECUTOR}.read_committed_cursor", _spy):
            asyncio.run(executor._dispatch_outcome({}, transport=None))

        self.assertTrue(ran, "the outcome hook must have run")
        self.assertTrue(
            used, "the outcome hook must write on a read-committed cursor",
        )
