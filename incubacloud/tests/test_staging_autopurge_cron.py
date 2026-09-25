"""The ladder: warn, warn again, then delete — never two rungs in a tick.

A staging has no backup. Whatever was in it when it goes is gone, so the
one property worth more than every other here is that nothing is deleted
without its owner having had a real chance to stop it.

That is not the same as "the code sends warnings". A cron that has been
down for a month comes back to find instances months past their
deadline, and the obvious implementation — evaluate the conditions, act
on whichever match — sends both warnings and deletes in a single pass
that takes under a second. Everyone was warned, technically, and nobody
had a chance. So the ladder is climbed one rung per tick, each rung
requires the one below it, and deletion additionally requires the final
warning to have been out for a full day.

These freeze time and step tick by tick, because the failure this is
guarding against only appears in the sequence, never in a single call.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.cloud_instance import (
    AUTOPURGE_FINAL_ALERT_CODE,
    AUTOPURGE_WARN_ALERT_CODE,
)

_WINDOW = 90


class _LadderCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "pg-host",
            "ip_address": "10.0.14.1",
            "user": "root",
            "wildcard_domain": "pg.example.com",
        })
        self.project = self.env["cloud.project"].create({"name": "pg-proj"})
        self.staging = self.env["cloud.instance"].create({
            "name": "pg-staging",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })
        self.settings = self.env["cloud.settings"].sudo()._get()
        self.settings.staging_autopurge_days = _WINDOW
        self.Instance = self.env["cloud.instance"]
        self.Alert = self.env["cloud.alert"].sudo()

    def _idle_for(self, days, instance=None):
        """Put the instance's clock *days* in the past."""
        inst = instance or self.staging
        inst.sudo().write({
            "last_touched_at": fields.Datetime.now() - timedelta(days=days),
        })

    def _tick(self, at=None):
        """Run one cron pass, optionally at a pretended instant."""
        deleted = []
        with patch.object(
            type(self.env["cloud.instance"]), "_autopurge_delete",
            autospec=True,
            side_effect=lambda inst, window: deleted.append(inst.id) is None,
        ):
            if at is None:
                tally = self.Instance._cron_autopurge_stagings()
            else:
                with patch.object(
                    fields.Datetime, "now", staticmethod(lambda: at),
                ):
                    tally = self.Instance._cron_autopurge_stagings()
        self.staging.invalidate_recordset([
            "autopurge_warned_at", "autopurge_final_warned_at",
        ])
        tally["deleted_ids"] = deleted
        return tally

    def _active_codes(self, instance=None):
        inst = instance or self.staging
        return set(self.Alert.search([
            ("instance_id", "=", inst.id),
            ("state", "=", "active"),
        ]).mapped("code"))


class TestTheLadderIsClimbedOneRungPerTick(_LadderCase):
    """The invariant. Everything else in this file is a corollary."""

    def test_a_long_dead_cron_warns_and_stops(self):
        """Six months past the deadline, the first tick still only warns.

        This is the scenario the whole design exists for: warning and
        deleting in the same second is notice on paper and nothing in
        practice.
        """
        self._idle_for(_WINDOW + 180)
        tally = self._tick()
        self.assertEqual(tally["warned"], 1)
        self.assertEqual(tally["deleted"], 0)
        self.assertEqual(tally["deleted_ids"], [])
        self.assertIn(AUTOPURGE_WARN_ALERT_CODE, self._active_codes())

    def test_the_second_tick_reaches_the_final_warning_and_no_further(self):
        self._idle_for(_WINDOW + 180)
        self._tick()
        tally = self._tick()
        self.assertEqual(tally["final_warned"], 1)
        self.assertEqual(tally["deleted"], 0)
        self.assertIn(AUTOPURGE_FINAL_ALERT_CODE, self._active_codes())

    def test_deletion_waits_a_full_day_after_the_final_warning(self):
        """Three ticks in one minute must not add up to notice."""
        self._idle_for(_WINDOW + 180)
        self._tick()
        self._tick()
        tally = self._tick()
        self.assertEqual(tally["deleted"], 0)
        self.assertEqual(tally["deleted_ids"], [])

    def test_the_tick_after_that_day_deletes(self):
        self._idle_for(_WINDOW + 180)
        self._tick()
        self._tick()
        tomorrow = fields.Datetime.now() + timedelta(days=1, minutes=1)
        tally = self._tick(at=tomorrow)
        self.assertEqual(tally["deleted"], 1)
        self.assertEqual(tally["deleted_ids"], [self.staging.id])


class TestWhenEachRungIsDue(_LadderCase):

    def test_nothing_happens_before_the_first_warning_is_due(self):
        self._idle_for(_WINDOW - 30)
        tally = self._tick()
        self.assertEqual(tally["warned"], 0)
        self.assertFalse(self._active_codes())

    def test_the_first_warning_lands_fourteen_days_out(self):
        self._idle_for(_WINDOW - 14)
        self.assertEqual(self._tick()["warned"], 1)

    def test_the_final_warning_waits_until_three_days_out(self):
        """Warned early, the second rung is not due yet."""
        self._idle_for(_WINDOW - 14)
        self._tick()
        tally = self._tick()
        self.assertEqual(tally["final_warned"], 0)
        self.assertNotIn(AUTOPURGE_FINAL_ALERT_CODE, self._active_codes())

    def test_the_final_warning_is_critical(self):
        """A ``warning`` reaches nobody on the default preference."""
        self._idle_for(_WINDOW + 180)
        self._tick()
        self._tick()
        final = self.Alert.search([
            ("instance_id", "=", self.staging.id),
            ("code", "=", AUTOPURGE_FINAL_ALERT_CODE),
        ], limit=1)
        self.assertEqual(final.level, "critical")


class TestWhoIsNeverTouched(_LadderCase):

    def test_an_exempt_staging_is_left_alone(self):
        self._idle_for(_WINDOW + 180)
        self.staging.sudo().write({"autopurge_exempt": True})
        tally = self._tick()
        self.assertEqual(tally["warned"], 0)
        self.assertFalse(self._active_codes())

    def test_production_is_left_alone(self):
        prod = self.env["cloud.instance"].create({
            "name": "pg-prod",
            "project_id": self.env["cloud.project"].create(
                {"name": "pg-proj-2"},
            ).id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self._idle_for(_WINDOW + 180, instance=prod)
        self._tick()
        self.assertFalse(self._active_codes(instance=prod))

    def test_an_archived_staging_is_left_alone(self):
        """It has already been dealt with, and its copy has its own retention."""
        self._idle_for(_WINDOW + 180)
        self.staging.sudo().write({"active": False})
        tally = self._tick()
        self.assertEqual(tally["warned"], 0)

    def test_nothing_happens_while_the_window_is_off(self):
        self._idle_for(_WINDOW + 180)
        self.settings.staging_autopurge_days = 0
        tally = self._tick()
        self.assertEqual(
            (tally["warned"], tally["final_warned"], tally["deleted"]),
            (0, 0, 0),
        )
        self.assertFalse(self._active_codes())

    def test_an_instance_without_a_clock_is_skipped_not_deleted(self):
        """No clock is "we do not know", which is never grounds to delete."""
        self.staging.sudo().write({
            "last_touched_at": False, "last_login_seen_at": False,
        })
        tally = self._tick()
        self.assertEqual(tally["skipped"], 1)
        self.assertEqual(tally["warned"], 0)


class TestComingBackToLifeResetsEverything(_LadderCase):

    def test_touching_a_warned_staging_clears_the_ladder(self):
        self._idle_for(_WINDOW + 180)
        self._tick()
        self._tick()
        self.staging._touch_autopurge_clock()
        self.assertFalse(self.staging.autopurge_warned_at)
        self.assertFalse(self.staging.autopurge_final_warned_at)
        self.assertFalse(self._active_codes())

    def test_a_revived_staging_starts_the_whole_notice_over(self):
        """Not "three days left and no warning showing" — the full window."""
        self._idle_for(_WINDOW + 180)
        self._tick()
        self._tick()
        self.staging._touch_autopurge_clock()
        tally = self._tick()
        self.assertEqual(
            (tally["warned"], tally["final_warned"], tally["deleted"]),
            (0, 0, 0),
        )

    def test_a_login_inside_the_staging_postpones_it(self):
        """The panel clock is stale but somebody is living in there."""
        self._idle_for(_WINDOW + 180)
        self.staging.sudo().write({
            "last_login_seen_at": fields.Datetime.now() - timedelta(days=1),
        })
        tally = self._tick()
        self.assertEqual(tally["warned"], 0)


class TestOneFailureDoesNotStopTheRest(_LadderCase):

    def setUp(self):
        super().setUp()
        self.other = self.env["cloud.instance"].create({
            "name": "pg-staging-2",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })
        self._idle_for(_WINDOW + 180)
        self._idle_for(_WINDOW + 180, instance=self.other)

    def test_a_raising_instance_is_skipped_and_the_others_proceed(self):
        real_step = type(self.env["cloud.instance"])._autopurge_step
        failing_id = self.staging.id

        def _step(inst, now, deadline, window):
            if inst.id == failing_id:
                raise ValueError("host is on fire")
            return real_step(inst, now, deadline, window)

        with patch.object(
            type(self.env["cloud.instance"]), "_autopurge_step",
            autospec=True, side_effect=_step,
        ):
            tally = self.Instance._cron_autopurge_stagings()
        self.assertEqual(tally["skipped"], 1)
        self.assertEqual(tally["warned"], 1)


class TestDeletionGoesThroughTheOrdinaryPath(_LadderCase):

    def _expire_to_the_brink(self):
        """Climb both warning rungs and put the final one a day back."""
        self._idle_for(_WINDOW + 180)
        self.staging.sudo().write({
            "autopurge_warned_at": fields.Datetime.now() - timedelta(days=20),
            "autopurge_final_warned_at": (
                fields.Datetime.now() - timedelta(days=2)
            ),
        })

    def test_a_deployed_staging_is_torn_down_by_a_job(self):
        self._expire_to_the_brink()
        self.staging.sudo()._transition("deployed")
        enqueued = []
        with patch.object(
            type(self.env["cloud.job"]), "enqueue",
            side_effect=lambda h, i, code, **kw: enqueued.append((i, code)),
        ):
            self.Instance._cron_autopurge_stagings()
        self.assertIn((self.staging.id, "delete_instance"), enqueued)
        self.assertTrue(
            self.staging.exists(),
            "the record goes when the teardown job says so, not before",
        )

    def test_an_undeployed_staging_is_unlinked_directly(self):
        self._expire_to_the_brink()
        inst_id = self.staging.id
        self.Instance._cron_autopurge_stagings()
        self.assertFalse(self.env["cloud.instance"].browse(inst_id).exists())

    def test_an_undeployed_staging_a_job_still_owns_is_not_unlinked(self):
        """``deploying`` refuses the unlink; that is "busy", not a crash.

        It has to land on the same answer a refused enqueue gets —
        stuck alert, retry tomorrow — instead of a traceback in the
        cron log and an instance that quietly stops being considered.
        """
        self._expire_to_the_brink()
        self.staging.sudo()._transition("deploying")
        tally = self.Instance._cron_autopurge_stagings()
        self.assertEqual(tally["deleted"], 0)
        self.assertTrue(self.staging.exists())
        self.assertIn("staging_autopurge_stuck", self._active_codes())

    def test_a_refused_deletion_writes_no_audit_row(self):
        """The trail must not claim a purge that never happened."""
        self._expire_to_the_brink()
        self.staging.sudo()._transition("deployed")
        with patch.object(
            type(self.env["cloud.job"]), "enqueue",
            side_effect=UserError("another job is running"),
        ):
            self.Instance._cron_autopurge_stagings()
        self.assertFalse(self.env["cloud.audit.log"].sudo().search([
            ("action", "=", "Autopurge"),
            ("instance_id", "=", self.staging.id),
        ]))

    def test_the_audit_log_records_why_it_went(self):
        self._expire_to_the_brink()
        self.Instance._cron_autopurge_stagings()
        entry = self.env["cloud.audit.log"].sudo().search([
            ("action", "=", "Autopurge"),
        ], limit=1)
        self.assertTrue(entry)
        self.assertIn("pg-staging", entry.details)
        self.assertIn(str(_WINDOW), entry.details)

    def test_a_blocked_deletion_raises_stuck_and_keeps_the_instance(self):
        """A job already running is normal; tomorrow's tick retries."""
        self._expire_to_the_brink()
        self.staging.sudo()._transition("deployed")
        with patch.object(
            type(self.env["cloud.job"]), "enqueue",
            side_effect=UserError("another job is running"),
        ):
            tally = self.Instance._cron_autopurge_stagings()
        self.assertEqual(tally["deleted"], 0)
        self.assertTrue(self.staging.exists())
        self.assertIn("staging_autopurge_stuck", self._active_codes())
