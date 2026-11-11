"""The place a job with steps run apart keeps, and the budget of an attempt.

A step marked ``detached`` runs on its server apart from the request
(``scripts/detached_step.sh``), so what the job knows of its run must
outlive the request too: ``cloud.job.checkpoint``. Its plan and its
results are encrypted, a copy of the database has none of them, and a
job that goes leaves none behind.

How long an attempt runs before it leaves its step to the server is a
share of the server's ``limit_time_real`` (``attempt_budget_seconds``), and
a step that runs in the request must fit in what is left
(``attached_step_limit_seconds``).
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from odoo.modules.neutralize import neutralize_database
from odoo.tests.common import BaseCase, TransactionCase
from odoo.tools import mute_logger

from ..models import _job_server_limits
from ..models._job_server_limits import (
    MIN_LIMIT_TIME_REAL,
    attached_step_limit_seconds,
    attempt_budget_seconds,
    job_server_limit_problems,
)


def _job(env, name="checkpoint"):
    """Create a host and a job of it, as a dispatched job is.

    :return: the ``cloud.job``
    """
    host = env["cloud.host"].create({
        "name": f"{name}-host",
        "ip_address": "192.0.2.93",
        "user": "ubuntu",
        "wildcard_domain": f"{name}.example.com",
    })
    job_type = env["cloud.job.type"].search(
        [("code", "=", "host_probe")], limit=1,
    )
    qjob = env["queue.job"].sudo().create({
        "uuid": f"{name}-qj",
        "name": name,
        "state": "started",
        "method_name": "execute",
        "model_name": "cloud.job",
        "func_string": "execute()",
    })
    return env["cloud.job"].sudo().create({
        "host_id": host.id,
        "job_type_id": job_type.id,
        "name": name,
        "queue_job_uuid": qjob.uuid,
        "queue_job_id": qjob.id,
    })


class TestJobCheckpoint(TransactionCase):
    """The row: encrypted, one per job, gone with the job and from a copy."""

    def setUp(self):
        """A job of a host."""
        super().setUp()
        self.job = _job(self.env)
        self.Checkpoint = self.env["cloud.job.checkpoint"].sudo()

    def _create(self, data):
        """Create the job's row with *data* as its JSON."""
        return self.Checkpoint.create({
            "job_id": self.job.id,
            "queue_job_uuid": self.job.queue_job_uuid,
            "host_id": self.job.host_id.id,
            "data": json.dumps(data),
        })

    def test_the_plan_and_results_are_encrypted_and_read_back(self):
        """A command or what it printed is never left readable in the table."""
        data = {
            "plan": [["Restore", "PGPASSWORD=s3cret restore.sh", {"detached": True}]],
            "results": {"Restore": {"stdout": "token=abc", "exit_status": 0}},
            "job_dir": "/tmp/.incubacloud-job-0011223344556677",
        }
        row = self._create(data)
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT data FROM cloud_job_checkpoint WHERE id = %s", (row.id,),
        )
        raw = self.env.cr.fetchone()[0]
        self.assertTrue(raw.startswith("enc:"))
        self.assertNotIn("s3cret", raw)
        self.assertNotIn("token=abc", raw)
        row.invalidate_recordset()
        self.assertEqual(row._data(), data)

    def test_a_row_without_data_reads_empty(self):
        """``_data`` of a row created before its plan is an empty dict."""
        row = self.Checkpoint.create({"job_id": self.job.id})
        self.assertEqual(row._data(), {})
        self.assertEqual(row.next_index, 0)
        self.assertEqual(row.log_offset, 0)

    def test_one_row_per_job(self):
        """A second row for the same job is refused."""
        self._create({})
        with self.assertRaises(Exception), self.env.cr.savepoint(), \
                mute_logger("odoo.sql_db"):
            self._create({})
            self.env.flush_all()

    def test_the_row_goes_with_its_job(self):
        """Deleting the job deletes its row: nothing is left to follow."""
        row = self._create({})
        self.job.unlink()
        self.assertFalse(row.exists())

    def test_no_role_writes_it(self):
        """Only the panel's own code, as superuser or in SQL, writes it."""
        admin = self.env.ref("base.user_admin")
        Checkpoint = self.env["cloud.job.checkpoint"].with_user(admin)
        self.assertFalse(Checkpoint.has_access("create"))
        self.assertFalse(Checkpoint.has_access("write"))
        self.assertFalse(Checkpoint.has_access("unlink"))

    def test_a_neutralized_copy_has_none(self):
        """A copy must not go stop the steps of the database it came from."""
        self._create({"plan": [["Build", "build.sh", {"detached": True}]]})
        self.env.flush_all()
        neutralize_database(self.env.cr)
        self.env.cr.execute("SELECT count(*) FROM cloud_job_checkpoint")
        self.assertEqual(self.env.cr.fetchone(), (0,))

    def test_a_copy_from_before_the_table_is_neutralized(self):
        """A copy older than the table neutralizes, Odoo's part included."""
        self.env.flush_all()
        self.env.cr.execute("DROP TABLE cloud_job_checkpoint")
        neutralize_database(self.env.cr)
        self.env.cr.execute(
            "SELECT value FROM ir_config_parameter"
            " WHERE key = 'database.is_neutralized'"
        )
        self.assertEqual(self.env.cr.fetchone(), ("true",))


class TestAttemptBudget(BaseCase):
    """How long an attempt runs, and a step in the request may last."""

    def test_the_budget_is_a_share_of_the_limit(self):
        """0.6 of ``limit_time_real``; none without a limit."""
        table = {0: 0, 120: 72, 900: 540, 3600: 2160}
        self.assertEqual(
            {limit: attempt_budget_seconds({"limit_time_real": limit})
             for limit in table},
            table,
        )

    def test_a_step_in_the_request_fits_in_what_is_left(self):
        """0.4 of the limit: budget and step together fit in it."""
        table = {0: 0, 120: 48, 900: 360, 3600: 1440}
        self.assertEqual(
            {limit: attached_step_limit_seconds({"limit_time_real": limit})
             for limit in table},
            table,
        )
        for limit in (120, 900, 3600):
            options = {"limit_time_real": limit}
            self.assertLessEqual(
                attempt_budget_seconds(options)
                + attached_step_limit_seconds(options),
                limit,
            )

    def _conf(self, body):
        """Write an odoo.conf with *body*; return its path, removed at cleanup."""
        handle, path = tempfile.mkstemp(suffix=".conf")
        with os.fdopen(handle, "w") as conf:
            conf.write(body)
        self.addCleanup(os.unlink, path)
        return path

    def test_odoo_conf_sets_the_budget_for_tests(self):
        """``[incubacloud] attempt_budget_seconds`` wins over the limit."""
        path = self._conf(
            "[options]\nlimit_time_real = 900\n"
            "[incubacloud]\nattempt_budget_seconds = 60\n"
        )
        self.assertEqual(attempt_budget_seconds(
            {"limit_time_real": 900, "config": path},
        ), 60)

    def test_odoo_conf_is_read_again_only_once_it_changed(self):
        """Every job starts its clock: the file is not parsed every time."""
        path = self._conf("[incubacloud]\nattempt_budget_seconds = 60\n")
        options = {"limit_time_real": 900, "config": path}
        with patch.object(
            _job_server_limits, "_read_conf_section",
            wraps=_job_server_limits._read_conf_section,
        ) as read:
            self.assertEqual(attempt_budget_seconds(options), 60)
            self.assertEqual(attempt_budget_seconds(options), 60)
            self.assertEqual(read.call_count, 1)
            Path(path).write_text("[incubacloud]\nattempt_budget_seconds = 90\n")
            stat = os.stat(path)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            self.assertEqual(attempt_budget_seconds(options), 90)
            self.assertEqual(read.call_count, 2)

    def test_a_budget_that_is_not_seconds_is_ignored(self):
        """A wrong value is logged and the share of the limit applies."""
        path = self._conf("[incubacloud]\nattempt_budget_seconds = soon\n")
        with self.assertLogs(
            "odoo.addons.incubacloud.models._job_server_limits", "WARNING",
        ):
            budget = attempt_budget_seconds(
                {"limit_time_real": 900, "config": path},
            )
        self.assertEqual(budget, 540)

    def test_the_startup_warning_starts_below_odoo_sh_s_limit(self):
        """900 s is enough; below it the warning names the budget and the step."""
        modules = ["base", "web", "queue_job"]
        self.assertEqual(MIN_LIMIT_TIME_REAL, 900)
        self.assertEqual(job_server_limit_problems({
            "server_wide_modules": modules, "workers": 2,
            "limit_time_real": 900,
        }), [])
        problems = job_server_limit_problems({
            "server_wide_modules": modules, "workers": 2,
            "limit_time_real": 600,
        })
        self.assertEqual(len(problems), 1)
        self.assertIn("limit_time_real is 600 s, below the 900 s", problems[0])
        self.assertIn("runs for 360 s", problems[0])
        self.assertIn("within 240 s", problems[0])
