"""The startup warning about Odoo limits that cut the panel's jobs short.

A self-hosted panel with ``workers = 0`` and Odoo's defaults reloaded its
whole server two minutes into every long deploy, and the deploys ended
``JobFoundDead`` after three attempts (issue #4 of the public repository,
8-nov-2026). The panel now says so at startup, in the log and in an alert.
"""
from unittest.mock import MagicMock, patch

from odoo import cli as odoo_cli
from odoo.service import server as odoo_server
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.models._job_server_limits import (
    JOB_LIMITS_ALERT_CODE,
    job_server_limit_problems,
)

_GIB = 1024 * 1024 * 1024


def _options(**overrides):
    """Return a configuration that runs jobs, with Odoo 19's defaults."""
    options = {
        "server_wide_modules": ["base", "rpc", "web", "queue_job"],
        "stop_after_init": False,
        "workers": 0,
        "limit_time_real": 120,
        "limit_memory_soft": 2 * _GIB,
        "limit_memory_hard": 2560 * 1024 * 1024,
    }
    options.update(overrides)
    return options


class TestJobServerLimitProblems(BaseCase):
    """Which configurations are judged, and what is found wrong in them."""

    def test_odoo_defaults_with_no_workers_are_three_problems(self):
        """Odoo's defaults in a threaded server: the time and both memories."""
        problems = job_server_limit_problems(_options())
        self.assertEqual(len(problems), 3)
        self.assertIn("limit_time_real is 120 s", problems[0])
        self.assertIn("the whole server reloads", problems[0])
        self.assertIn("limit_memory_soft is 2048 MiB", problems[1])
        self.assertIn("limit_memory_hard is 2560 MiB", problems[2])

    def test_a_server_sized_for_jobs_has_none(self):
        """No limit, or limits above what a job needs, find nothing wrong."""
        self.assertEqual(job_server_limit_problems(_options(
            limit_time_real=3600, limit_memory_soft=0, limit_memory_hard=0,
        )), [])
        # Odoo.sh's limit: the long steps run apart from the request.
        self.assertEqual(job_server_limit_problems(_options(
            limit_time_real=900, limit_memory_soft=0, limit_memory_hard=0,
        )), [])
        self.assertEqual(job_server_limit_problems(_options(
            limit_time_real=0,
            limit_memory_soft=6 * _GIB,
            limit_memory_hard=8 * _GIB,
        )), [])

    def test_with_workers_only_the_time_counts(self):
        """In prefork the memory limits are per worker, as Odoo means them."""
        problems = job_server_limit_problems(_options(workers=5))
        self.assertEqual(len(problems), 1)
        self.assertIn("its worker is killed", problems[0])
        self.assertEqual(job_server_limit_problems(_options(
            workers=9, limit_time_real=3600,
        )), [])

    def test_a_server_that_runs_no_jobs_is_not_judged(self):
        """The web server beside a dedicated runner loads ``--load=web``."""
        self.assertIsNone(job_server_limit_problems(_options(
            server_wide_modules=["base", "web"],
        )))
        self.assertIsNone(job_server_limit_problems(_options(
            stop_after_init=True,
        )))

    def test_modules_written_as_in_the_configuration_file(self):
        """``server_wide_modules`` read from a file is a comma-separated text."""
        self.assertIsNone(job_server_limit_problems(_options(
            server_wide_modules="base,web",
        )))
        self.assertEqual(len(job_server_limit_problems(_options(
            server_wide_modules="base, web, queue_job",
        ))), 3)


class TestJobServerLimitAlert(TransactionCase):
    """The startup check logs each problem and keeps the alert in step."""

    def setUp(self):
        """Run each test as a server started by ``odoo server`` would."""
        super().setUp()
        self.startPatcher(patch.object(
            odoo_server, "server", MagicMock(spec=odoo_server.ThreadedServer),
        ))
        self.startPatcher(patch.object(odoo_cli, "COMMAND", "server"))

    def _active(self):
        """Return the active alert about the job server's limits."""
        return self.env["cloud.alert"].search([
            ("code", "=", JOB_LIMITS_ALERT_CODE), ("state", "=", "active"),
        ])

    def test_raised_with_the_problems_then_resolved(self):
        """Each problem is logged and raises the alert; good limits close it."""
        Settings = self.env["cloud.settings"]
        with self.assertLogs(
            "odoo.addons.incubacloud.models.cloud_settings", "WARNING",
        ) as logs:
            Settings._warn_job_server_limits(_options())
        self.assertEqual(len(logs.records), 3)
        alert = self._active()
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.audience, "provider")
        self.assertIn("limit_time_real is 120 s", alert.message)

        Settings._warn_job_server_limits(_options(
            limit_time_real=3600, limit_memory_soft=0, limit_memory_hard=0,
        ))
        self.assertFalse(self._active())

    def test_a_web_only_server_leaves_the_alert_alone(self):
        """A server that runs no jobs neither raises nor closes the alert."""
        Settings = self.env["cloud.settings"]
        Settings._warn_job_server_limits(_options())
        Settings._warn_job_server_limits(_options(
            server_wide_modules=["base", "web"], limit_time_real=1200,
        ))
        self.assertEqual(len(self._active()), 1)

    def test_a_registry_loaded_without_a_server_is_not_judged(self):
        """``click-odoo-update`` and scripts load the registry with no server.

        They read the configuration file, whose ``limit_time_real`` is the
        web's, and would raise the alert on every deploy.
        """
        Settings = self.env["cloud.settings"]
        with patch.object(odoo_server, "server", None), \
                patch.object(odoo_cli, "COMMAND", None):
            Settings._warn_job_server_limits(_options())
        self.assertFalse(self._active())

    def test_the_shell_is_not_judged(self):
        """``odoo shell`` starts a server and stops it before loading."""
        Settings = self.env["cloud.settings"]
        with patch.object(odoo_cli, "COMMAND", "shell"):
            Settings._warn_job_server_limits(_options())
        self.assertFalse(self._active())
