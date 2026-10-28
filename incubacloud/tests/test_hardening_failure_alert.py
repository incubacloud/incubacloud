"""A failed hardening is one alert, not two.

On 27 October 2026 one hardening run that failed on host 2412 (job
382128) left two alerts side by side: ``hardening_failed``, critical,
saying root SSH was preserved and to check the log, and ``job_failed``,
a warning, saying what failed. The same failure, told twice, each half
of it in a different row.

The executor's own alert now carries the error, and ``job_failed`` is
not raised for a job whose type files its own failure alert
(``_failure_alert_code``) and that alert is open -- as for a critical
conflict alert. Not for a type whose failure holds its queue: that hold
reads ``job_failed``.
"""
import asyncio
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.host_hardening_executor import (
    HostHardeningExecutor,
)

_PLAYBOOK_ERROR = "Hardening playbook failed (rc=2) — see the log."


class TestOneAlertForAFailedHardening(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "failed-hardening-host",
            "ip_address": "10.0.0.46",
            "user": "ubuntu",
            "wildcard_domain": "failedhardening.example.com",
        })
        self.Alert = self.env["cloud.alert"].sudo()

    def _job_type(self, code):
        jt = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        return jt or self.env["cloud.job.type"].create({
            "name": code, "code": code, "apply_to": "host",
        })

    def _fail(self, code, uuid, own_alert=None, level="critical", dismissed=False):
        """Fail a job of *code* the way the runner does.

        :param str own_alert: code of the alert its executor raised about
            this job before failing, if any
        :param bool dismissed: that alert was closed before the job ended
        :return: the ``cloud.job``
        """
        cjob = self.env["cloud.job"].sudo().create({
            "host_id": self.host.id,
            "job_type_id": self._job_type(code).id,
            "name": f"Job {code}",
            "queue_job_uuid": uuid,
        })
        if own_alert:
            alert = self.Alert.raise_alert(
                own_alert, "the executor's own account of it",
                level=level, host=self.host, job=cjob,
            )
            if dismissed:
                alert.state = "dismissed"
        qjob = self.env["queue.job"].sudo().create({
            "uuid": uuid,
            "name": f"qj-{code}",
            "state": "pending",
            "method_name": "noop",
            "model_name": "cloud.job",
            "func_string": "noop()",
        })
        qjob.write({"state": "failed", "exc_message": _PLAYBOOK_ERROR})
        return cjob

    def _active(self, code):
        return self.Alert.search([
            ("code", "=", code),
            ("host_id", "=", self.host.id),
            ("state", "=", "active"),
        ])

    def test_a_failed_hardening_leaves_its_own_alert_only(self):
        self._fail("host_hardening", "uuid-hf1", own_alert="hardening_failed")
        self.assertFalse(self._active("job_failed"))
        alert = self._active("hardening_failed")
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.level, "critical")

    def test_a_hardening_that_failed_before_alerting_still_says_so(self):
        """Refused before the playbook ran (no trusted key, say): its
        own alert never came, so ``job_failed`` is the only account."""
        self._fail("host_hardening", "uuid-hf2")
        alert = self._active("job_failed")
        self.assertEqual(len(alert), 1)
        self.assertIn("rc=2", alert.message)

    def test_another_type_with_an_alert_of_its_own_keeps_job_failed(self):
        """Only a type that declares its failure alert is spared."""
        self._fail("sync_metrics_accounts", "uuid-hf3", own_alert="hardening_failed")
        self.assertEqual(len(self._active("job_failed")), 1)

    def test_a_type_whose_failure_holds_its_queue_keeps_job_failed(self):
        """The hold is read off ``job_failed``: sparing it would let the
        queue run on past the failure."""
        self._job_type("host_hardening").failure_holds_queue = True
        self._fail("host_hardening", "uuid-hf4", own_alert="hardening_failed")
        self.assertEqual(len(self._active("job_failed")), 1)

    def test_a_dismissed_own_alert_does_not_spare_it(self):
        """Nobody would be told: the one that said it is closed."""
        self._fail(
            "host_hardening", "uuid-hf5", own_alert="hardening_failed",
            dismissed=True,
        )
        self.assertEqual(len(self._active("job_failed")), 1)

    def _executor(self):
        """Return a hardening executor for this test's host, built without
        ``__init__`` (no job row, no SSH).

        :rtype: HostHardeningExecutor
        """
        executor = object.__new__(HostHardeningExecutor)
        executor.job = MagicMock(spec=type(self.env["cloud.job"]))
        executor.job.host_id = self.host
        executor.env = self.env
        executor._log_buffer = []
        executor._facts = {}
        return executor

    def _failure_message(self, executor, errors):
        """Run ``on_failure`` with *errors*; return the alert it files.

        :return: ``(code, message, kwargs)`` of the one ``_alert`` call
        """
        with patch.object(HostHardeningExecutor, "_sys", autospec=True), \
                patch.object(HostHardeningExecutor, "_alert", autospec=True) as alert:
            asyncio.run(executor.on_failure({}, errors))
        alert.assert_called_once()
        _self, code, message = alert.call_args.args
        return code, message, alert.call_args.kwargs

    def test_the_failure_alert_says_once_that_root_ssh_is_kept(self):
        """The playbook's error said it ("Root SSH left reachable so you
        can recover"), and the alert after it again ("Root SSH was
        preserved so you can recover")."""
        executor = self._executor()
        errors = executor.parse_results({executor._playbook: {"exit_status": 2}})
        self.assertEqual(len(errors), 1)
        _code, message, _kw = self._failure_message(executor, errors)
        self.assertIn("rc=2", message)
        self.assertEqual(message.lower().count("root ssh"), 1, message)
        self.assertEqual(message.count("recover"), 1, message)

    def test_the_failure_alert_carries_what_failed(self):
        """What ``job_failed`` said, the error, is in the one alert left."""
        code, message, kwargs = self._failure_message(
            self._executor(), [_PLAYBOOK_ERROR],
        )
        self.assertEqual(code, "hardening_failed")
        self.assertEqual(kwargs, {"level": "critical"})
        for part in ("failed-hardening-host", "rc=2", "see the log",
                     "Root SSH", "re-run"):
            with self.subTest(part=part):
                self.assertIn(part, message)
