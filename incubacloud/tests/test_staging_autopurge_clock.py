"""The activity clock a staging's expiry will be measured against.

Deleting a staging that nobody uses is only safe if "nobody uses it" is
measured from something a person actually did. Two signals feed the
clock and the later one wins: somebody acted on the instance from the
panel, and somebody logged into the staging's own Odoo.

What these pin is the first half — the panel signal — and the three ways
it could lie. A background probe finishing every five minutes would keep
every staging alive forever; so would a rebuild the platform bot fired
off a push, which is code arriving, not a person looking. And a clock
that starts empty would let the very first tick after the deploy find an
instance already past its deadline, which is the one thing the feature
promises never to do.
"""
import importlib.util
import os
from datetime import timedelta

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.cloud_instance import (
    AUTOPURGE_FINAL_ALERT_CODE,
    AUTOPURGE_WARN_ALERT_CODE,
)


def _load_backfill_migration():
    """Import the 1.0.130 post-migrate script as a module."""
    path = os.path.join(
        get_module_path("incubacloud"), "migrations", "1.0.130",
        "post-migrate.py",
    )
    spec = importlib.util.spec_from_file_location(
        "ic_post_migrate_1_0_130", path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ClockBase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "ap-host",
            "ip_address": "10.0.12.1",
            "user": "root",
            "wildcard_domain": "ap.example.com",
        })
        self.project = self.env["cloud.project"].create({"name": "ap-proj"})
        self.staging = self.env["cloud.instance"].create({
            "name": "ap-staging",
            "project_id": self.project.id,
            "environment": "staging",
            "host_id": self.host.id,
        })

    def _job_type(self, code, apply_to="instance"):
        jt = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        if not jt:
            jt = self.env["cloud.job.type"].create({
                "name": code, "code": code, "apply_to": apply_to,
            })
        return jt

    def _finish_job(self, code, author, state="done"):
        """Drive a cloud.job of *code* to *state* through the real bridge.

        The clock is sealed by ``queue.job.write``, not by anything the
        test could call directly, so the job has to travel the same path
        a real one does.
        """
        uuid = f"ap-uuid-{code}-{author.id}-{state}"
        # ``with_user`` resets ``su``, so the sudo has to come after it —
        # the same ordering ``as_platform`` relies on. This changes who
        # authors the job, never what it is allowed to do.
        cjob = self.env["cloud.job"].with_user(author).sudo().create({
            "instance_id": self.staging.id,
            "host_id": self.host.id,
            "job_type_id": self._job_type(code).id,
            "name": f"AP {code}",
            "queue_job_uuid": uuid,
        })
        qjob = self.env["queue.job"].sudo().create({
            "uuid": uuid,
            "name": f"ap-{code}",
            "state": "pending",
            "method_name": "noop",
            "model_name": "cloud.job",
            "func_string": "noop()",
        })
        qjob.write({"state": state})
        return cjob

    def _backdate(self, days):
        """Put the clock *days* in the past, the way real disuse would."""
        past = fields.Datetime.now() - timedelta(days=days)
        self.staging.sudo().write({"last_touched_at": past})
        return past


class TestClockIsStampedAtBirth(_ClockBase):
    """A row without a clock is a row the cron cannot reason about."""

    def test_create_stamps_the_clock(self):
        self.assertTrue(
            self.staging.last_touched_at,
            "a new staging must start its own window, not an expired one",
        )

    def test_copy_gets_a_fresh_clock(self):
        """``copy=False`` drops the stamp; ``create`` must put one back.

        Otherwise duplicating an instance produces the one shape the
        cron cannot handle — a staging with no clock at all.
        """
        copy = self.staging.copy({"name": "ap-staging-copy"})
        self.assertTrue(copy.last_touched_at)

    def test_production_is_stamped_too(self):
        """Environment can be flipped later; the stamp must not be a hole."""
        prod = self.env["cloud.instance"].create({
            "name": "ap-prod",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self.assertTrue(prod.last_touched_at)


class TestTouchResetsTheWindow(_ClockBase):
    """Touching is the whole escape hatch: it must undo every warning."""

    def test_touch_moves_the_clock_forward(self):
        past = self._backdate(60)
        self.staging._touch_autopurge_clock()
        self.assertGreater(self.staging.last_touched_at, past)

    def test_touch_clears_both_warning_stamps(self):
        self.staging.sudo().write({
            "autopurge_warned_at": fields.Datetime.now(),
            "autopurge_final_warned_at": fields.Datetime.now(),
        })
        self.staging._touch_autopurge_clock()
        self.assertFalse(self.staging.autopurge_warned_at)
        self.assertFalse(self.staging.autopurge_final_warned_at)

    def test_touch_resolves_both_alerts(self):
        Alert = self.env["cloud.alert"].sudo()
        Alert.raise_alert(
            AUTOPURGE_WARN_ALERT_CODE, "expiring", instance=self.staging,
        )
        Alert.raise_alert(
            AUTOPURGE_FINAL_ALERT_CODE, "last call", level="critical",
            instance=self.staging,
        )
        self.staging._touch_autopurge_clock()
        still_active = Alert.search([
            ("instance_id", "=", self.staging.id),
            ("state", "=", "active"),
            ("code", "in", [
                AUTOPURGE_WARN_ALERT_CODE, AUTOPURGE_FINAL_ALERT_CODE,
            ]),
        ])
        self.assertFalse(
            still_active,
            "a warning that outlives the touch tells the user to act twice",
        )

    def test_production_is_not_touched(self):
        """Production has no expiry, so it has no clock to move."""
        prod = self.env["cloud.instance"].create({
            "name": "ap-prod-2",
            "project_id": self.env["cloud.project"].create(
                {"name": "ap-proj-2"},
            ).id,
            "environment": "production",
            "host_id": self.host.id,
        })
        before = fields.Datetime.now() - timedelta(days=30)
        prod.sudo().write({"last_touched_at": before})
        prod._touch_autopurge_clock()
        self.assertEqual(prod.last_touched_at, before)


class TestEffectiveClock(_ClockBase):
    """The clock is the later of the two signals, not either one alone."""

    def test_login_wins_when_it_is_the_later_one(self):
        panel = fields.Datetime.now() - timedelta(days=40)
        login = fields.Datetime.now() - timedelta(days=2)
        self.staging.sudo().write({
            "last_touched_at": panel, "last_login_seen_at": login,
        })
        self.assertEqual(self.staging._autopurge_clock(), login)

    def test_panel_wins_when_it_is_the_later_one(self):
        panel = fields.Datetime.now() - timedelta(days=1)
        login = fields.Datetime.now() - timedelta(days=50)
        self.staging.sudo().write({
            "last_touched_at": panel, "last_login_seen_at": login,
        })
        self.assertEqual(self.staging._autopurge_clock(), panel)

    def test_a_missing_login_reading_does_not_shorten_the_clock(self):
        """No reading is "we do not know", never "nobody logged in"."""
        panel = fields.Datetime.now() - timedelta(days=10)
        self.staging.sudo().write({
            "last_touched_at": panel, "last_login_seen_at": False,
        })
        self.assertEqual(self.staging._autopurge_clock(), panel)


class TestOnlyHumanVisibleWorkSealsTheClock(_ClockBase):
    """What counts as "somebody used it" is narrower than "a job ran"."""

    def setUp(self):
        super().setUp()
        # An existing internal user rather than ``new_test_user``: that
        # helper creates a partner, and some optional addons add NOT NULL
        # columns to ``res_partner`` with no server default. Working
        # around that means an ``ALTER TABLE`` on a table every other
        # test reads, which deadlocked this suite against the HTTP cases
        # running beside it. Nothing here needs a *new* user — only one
        # that is internal and is not the bot.
        self.human = self.env.ref("base.user_admin")
        self.bot = self.env["res.users"]._get_cron_bot()

    def test_a_humans_visible_job_seals_the_clock(self):
        past = self._backdate(60)
        self._finish_job("restart_instance", self.human)
        self.assertGreater(self.staging.last_touched_at, past)

    def test_a_humans_visible_job_clears_the_warnings(self):
        self._backdate(60)
        self.staging.sudo().write({
            "autopurge_warned_at": fields.Datetime.now(),
        })
        self._finish_job("restart_instance", self.human)
        self.assertFalse(self.staging.autopurge_warned_at)

    def test_a_hidden_job_does_not_seal_the_clock(self):
        """The health probe runs every five minutes on its own.

        If it counted, no staging would ever expire and the feature
        would be dead code that looks alive.
        """
        past = self._backdate(60)
        self._finish_job("instance_health", self.human)
        self.assertEqual(self.staging.last_touched_at, past)

    def test_a_bot_job_does_not_seal_the_clock(self):
        """A rebuild off a push is code arriving, not a person looking."""
        past = self._backdate(60)
        self._finish_job("rebuild_instance", self.bot)
        self.assertEqual(self.staging.last_touched_at, past)

    def test_a_failed_job_does_not_seal_the_clock(self):
        past = self._backdate(60)
        self._finish_job("restart_instance", self.human, state="failed")
        self.assertEqual(self.staging.last_touched_at, past)


class TestWindowSetting(_ClockBase):
    """The window is the operator's to set, within bounds that make sense."""

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get()

    def test_default_is_ninety_days(self):
        self.assertEqual(
            self.env["cloud.settings"].sudo()
            ._get().staging_autopurge_days,
            90,
        )

    def test_zero_turns_it_off(self):
        self.settings.staging_autopurge_days = 0
        self.assertEqual(self.settings.staging_autopurge_days, 0)

    def test_a_window_shorter_than_the_warnings_is_refused(self):
        """14 and 3 days of notice do not fit inside a 10-day window."""
        with self.assertRaises(ValidationError):
            self.settings.staging_autopurge_days = 10

    def test_a_negative_window_is_refused(self):
        with self.assertRaises(ValidationError):
            self.settings.staging_autopurge_days = -1


class TestBackfillMigration(_ClockBase):
    """Existing rows must receive the whole window, not what is left of it."""

    def test_backfill_stamps_now_not_create_date(self):
        """``create_date`` would expire the fleet on the first tick.

        Every staging older than the window would already be past its
        deadline the moment the release lands — the feature would open
        by doing the one thing it promises never to do.
        """
        old = fields.Datetime.now() - timedelta(days=400)
        self.env.cr.execute(
            "UPDATE cloud_instance SET last_touched_at = NULL,"
            " create_date = %s WHERE id = %s",
            (old, self.staging.id),
        )
        self.staging.invalidate_recordset(["last_touched_at"])

        _load_backfill_migration().migrate(self.env.cr, "1.0.129")
        self.staging.invalidate_recordset(["last_touched_at"])

        self.assertTrue(self.staging.last_touched_at)
        self.assertGreater(
            self.staging.last_touched_at,
            fields.Datetime.now() - timedelta(days=1),
        )

    def test_backfill_leaves_a_stamped_row_alone(self):
        mine = fields.Datetime.now() - timedelta(days=5)
        self.staging.sudo().write({"last_touched_at": mine})

        _load_backfill_migration().migrate(self.env.cr, "1.0.129")
        self.staging.invalidate_recordset(["last_touched_at"])

        self.assertEqual(self.staging.last_touched_at, mine)
