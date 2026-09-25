"""Asking a staging when somebody last logged into it.

The panel signal alone undercounts: somebody can work inside a staging
for weeks without ever opening the panel. This reading is the other half
of the clock, and the property it has to hold is asymmetric — a real
answer may extend an instance's life, and nothing may ever shorten it.

So the failures all have to land the same way. A stopped container, an
unreachable database, a garbled answer, a clock skewed into the future,
a restore that rolled the instance's own history backwards: every one of
them leaves ``last_login_seen_at`` exactly where it was. "We could not
ask" must never be recorded as "nobody has logged in", because the cron
downstream deletes things on the strength of the second one.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.models.read_last_login_executor import (
    ReadLastLoginExecutor,
)

_LABEL = "Read last login"


class TestParseTheAnswer(BaseCase):
    """Tier 1 — what the script said, turned into a datetime or nothing."""

    def test_a_timestamp_is_read(self):
        stamp = ReadLastLoginExecutor._parse_answer(
            "[incubacloud] reading\nLAST_LOGIN 2026-09-20T11:22:33.123456\n",
        )
        self.assertEqual(stamp, datetime(2026, 9, 20, 11, 22, 33, 123456))

    def test_none_is_not_a_date(self):
        """The table exists and is empty. That is not activity, and it
        is also not a value to write."""
        self.assertFalse(ReadLastLoginExecutor._parse_answer("LAST_LOGIN none"))

    def test_an_unparseable_answer_is_refused(self):
        self.assertFalse(
            ReadLastLoginExecutor._parse_answer("LAST_LOGIN yesterday"),
        )

    def test_output_without_the_marker_is_refused(self):
        """A script that died mid-way must not be read as an answer."""
        self.assertFalse(
            ReadLastLoginExecutor._parse_answer("Error response from daemon"),
        )

    def test_empty_output_is_refused(self):
        self.assertFalse(ReadLastLoginExecutor._parse_answer(""))

    def test_an_aware_timestamp_becomes_naive_utc(self):
        """Odoo compares naive UTC; an offset-aware value cannot be."""
        stamp = ReadLastLoginExecutor._parse_answer(
            "LAST_LOGIN 2026-09-20T13:00:00+02:00",
        )
        self.assertIsNone(stamp.tzinfo)
        self.assertEqual(stamp, datetime(2026, 9, 20, 11, 0, 0))

    def test_a_future_timestamp_is_capped_at_now(self):
        """A host with a skewed clock would otherwise buy itself years."""
        ahead = (
            datetime.now(timezone.utc) + timedelta(days=3650)
        ).replace(tzinfo=None)
        stamp = ReadLastLoginExecutor._parse_answer(
            f"LAST_LOGIN {ahead.isoformat()}",
        )
        self.assertLessEqual(stamp, fields.Datetime.now())


class _ExecutorCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "rl-host",
            "ip_address": "10.0.13.1",
            "user": "root",
            "wildcard_domain": "rl.example.com",
        })
        self.project = self.env["cloud.project"].create({"name": "rl-proj"})
        self.staging = self.env["cloud.instance"].create({
            "name": "rl-staging",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })

    def _run(self, stdout, errors=None):
        """Drive the executor's outcome hook over a fabricated answer."""
        executor = object.__new__(ReadLastLoginExecutor)
        executor.job = MagicMock(spec=type(self.env["cloud.job"]))
        executor.job.instance_id = self.staging
        executor.env = self.env
        executor._log_buffer = []
        results = {_LABEL: {"stdout": stdout, "exit_status": 0}}
        with patch.object(ReadLastLoginExecutor, "_sys"):
            if errors is None:
                asyncio.run(executor.on_success(results))
            else:
                asyncio.run(executor.on_failure(results, errors))
        self.staging.invalidate_recordset(["last_login_seen_at"])
        return executor


class TestTheReadingOnlyEverMovesForward(_ExecutorCase):

    def test_a_reading_is_recorded(self):
        seen = (fields.Datetime.now() - timedelta(hours=2)).replace(
            microsecond=0,
        )
        self._run(f"LAST_LOGIN {seen.isoformat()}")
        self.assertEqual(self.staging.last_login_seen_at, seen)

    def test_a_newer_reading_replaces_an_older_one(self):
        old = fields.Datetime.now() - timedelta(days=30)
        self.staging.sudo().write({"last_login_seen_at": old})
        newer = (fields.Datetime.now() - timedelta(days=1)).replace(
            microsecond=0,
        )
        self._run(f"LAST_LOGIN {newer.isoformat()}")
        self.assertEqual(self.staging.last_login_seen_at, newer)

    def test_an_older_reading_is_ignored(self):
        """A staging restored from an old backup reports an old max.

        Writing it would shorten the instance's own remaining life on
        the strength of a restore nobody meant as abandonment.
        """
        held = fields.Datetime.now() - timedelta(days=2)
        self.staging.sudo().write({"last_login_seen_at": held})
        stale = fields.Datetime.now() - timedelta(days=200)
        self._run(f"LAST_LOGIN {stale.isoformat()}")
        self.assertEqual(self.staging.last_login_seen_at, held)

    def test_none_does_not_blank_a_previous_reading(self):
        held = fields.Datetime.now() - timedelta(days=2)
        self.staging.sudo().write({"last_login_seen_at": held})
        self._run("LAST_LOGIN none")
        self.assertEqual(self.staging.last_login_seen_at, held)

    def test_a_failed_reading_leaves_the_clock_alone(self):
        """The whole point: not being able to ask is not an answer."""
        held = fields.Datetime.now() - timedelta(days=2)
        self.staging.sudo().write({"last_login_seen_at": held})
        self._run("", errors=["'Read last login' exited with status 1"])
        self.assertEqual(self.staging.last_login_seen_at, held)

    def test_a_failed_reading_on_a_virgin_instance_writes_nothing(self):
        self.staging.sudo().write({"last_login_seen_at": False})
        self._run("", errors=["host unreachable"])
        self.assertFalse(self.staging.last_login_seen_at)


class TestTheReadingIsInvisible(_ExecutorCase):
    """It runs daily on every staging: it must not look like user work."""

    def test_the_job_type_is_hidden(self):
        self.assertIn(
            "read_last_login",
            self.env["cloud.job"]._get_hidden_job_types(),
            "a daily probe in the job drawer buries the real operations, "
            "and a hidden type is also what keeps it out of the audit log "
            "and stops it blocking the instance",
        )


class TestWhichStagingsGetAsked(_ExecutorCase):

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get()
        self.settings.staging_autopurge_days = 90
        self.staging.sudo().write({"running": True})
        self.staging.sudo()._transition("deployed")

    def _queued_instance_ids(self):
        """Run the cron with the queue stubbed and report who it asked."""
        enqueued = []
        Job = type(self.env["cloud.job"])
        with patch.object(
            Job, "enqueue",
            side_effect=lambda h, i, code, **kw: enqueued.append((i, code)),
        ):
            self.env["cloud.instance"].cron_read_staging_logins()
        return [
            inst_id for inst_id, code in enqueued
            if code == "read_last_login"
        ]

    def test_a_running_staging_is_asked(self):
        self.assertIn(self.staging.id, self._queued_instance_ids())

    def test_an_exempt_staging_is_not_asked(self):
        """Its answer could never change an outcome."""
        self.staging.sudo().write({"autopurge_exempt": True})
        self.assertNotIn(self.staging.id, self._queued_instance_ids())

    def test_a_stopped_staging_is_not_asked(self):
        """It cannot answer, and being stopped for months *is* the disuse."""
        self.staging.sudo().write({"running": False})
        self.assertNotIn(self.staging.id, self._queued_instance_ids())

    def test_production_is_not_asked(self):
        prod = self.env["cloud.instance"].create({
            "name": "rl-prod",
            "project_id": self.env["cloud.project"].create(
                {"name": "rl-proj-2"},
            ).id,
            "environment": "production",
            "host_id": self.host.id,
            "state": "deployed",
        })
        prod.sudo().write({"running": True})
        self.assertNotIn(prod.id, self._queued_instance_ids())

    def test_nothing_is_asked_while_the_window_is_off(self):
        self.settings.staging_autopurge_days = 0
        self.assertEqual(self._queued_instance_ids(), [])
