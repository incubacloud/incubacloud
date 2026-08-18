"""The ``instance_error_logs`` payload must survive the container.

An alert whose payload holds only the ERROR headline is undiagnosable
once the instance has been rebuilt and its logs are gone — which is
exactly what happened on 2026-08-16, when three
``Exception during request handling`` groups were left without a stack
by the next rebuild. The probe now carries the traceback lines that
follow each header, bounded so a log stuck in a loop cannot inflate the
serialized payload.
"""
import asyncio

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.instance_health_executor import (
    _ERROR_CONTEXT_CHARS,
    InstanceHealthExecutor,
)

_HEADER = (
    "2026-08-16 17:50:03,123 7 ERROR prod odoo.http: "
    "Exception during request handling."
)
_SECOND_HEADER = (
    "2026-08-16 17:51:10,000 7 ERROR prod odoo.sql_db: "
    "bad query"
)
_TRACEBACK = [
    "Traceback (most recent call last):",
    '  File "/usr/lib/python3/dist-packages/odoo/http.py", line 1, in dispatch',
    "    return self._do_it()",
    "ValueError: boom",
]


class TestErrorLogContext(TransactionCase):

    def setUp(self):
        super().setUp()
        # The probe writes alerts on a cursor of its own; test mode is
        # what lets that cursor see the records created here.
        self.registry_enter_test_mode()
        self.project = self.env["cloud.project"].create({"name": "Ctx Proj"})
        self.host = self.env["cloud.host"].create({
            "name": "ctx-host",
            "ip_address": "192.0.2.64",
            "user": "ubuntu",
            "wildcard_domain": "ctx.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "ctxinst",
            "project_id": self.project.id,
            "environment": "production",
            "host_id": self.host.id,
        })
        self.job_type = self.env["cloud.job.type"].search(
            [("code", "=", "instance_health")], limit=1,
        )

    def _executor(self):
        job = self.env["cloud.job"].create({
            "name": "Health",
            "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.job_type.id,
        })
        executor = InstanceHealthExecutor(job, self.host)
        executor._skipped = False
        return executor

    def _groups(self, raw):
        return self._executor()._dedupe_error_lines(raw)

    def test_traceback_is_filed_under_its_header(self):
        groups = self._groups("\n".join([_HEADER, *_TRACEBACK]))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 1, "context is not an ERROR line")
        self.assertIn("ValueError: boom", "\n".join(groups[0]["context"]))

    def test_grep_separator_closes_the_block(self):
        """A ``--`` between blocks must not leak context across groups."""
        groups = self._groups("\n".join([
            _HEADER, *_TRACEBACK, "--", _SECOND_HEADER,
        ]))
        by_fp = {g["fingerprint"]: g for g in groups}
        self.assertEqual(len(by_fp), 2)
        empty = [g for g in groups if not g["context"]]
        self.assertEqual(len(empty), 1, "second group must start clean")

    def test_only_the_first_occurrence_carries_context(self):
        """Repeats share the stack — storing it again is dead weight."""
        groups = self._groups("\n".join([
            _HEADER, *_TRACEBACK, "--", _HEADER, *_TRACEBACK,
        ]))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(groups[0]["context"].count("ValueError: boom"), 1)

    def test_runaway_context_is_capped(self):
        """A log loop must not inflate the serialized payload."""
        noise = ["x" * 500] * 40
        groups = self._groups("\n".join([_HEADER, *noise]))
        stored = sum(len(line) for line in groups[0]["context"])
        self.assertLessEqual(stored, _ERROR_CONTEXT_CHARS)

    def test_alert_payload_ships_the_context(self):
        executor = self._executor()
        results = {
            "container_state": {"stdout": "\n".join(
                f"{svc}\trunning" for svc in self.instance.expected_services()
            )},
            "cpu_mem_snapshot": {"stdout": "0.0\t0.0"},
            "http_health": {"stdout": "exit:0"},
            "error_lines": {"stdout": "\n".join([_HEADER, *_TRACEBACK])},
        }
        executor.parse_results(results)
        asyncio.run(executor.on_success(results))
        alert = self.env["cloud.alert"].search([
            ("instance_id", "=", self.instance.id),
            ("code", "=", "instance_error_logs"),
            ("state", "=", "active"),
        ])
        self.assertEqual(len(alert), 1)
        self.assertIn(
            "ValueError: boom", "\n".join(alert.payload[0]["context"]),
        )
