"""
Tier 1 — Pure-Python unit tests for executor parse_results() methods.

Tests use object.__new__() to bypass __init__ so no Odoo ORM is needed.
All ORM/SSH interaction is excluded from parse_results() by design.
"""
import unittest

from odoo.tests.common import BaseCase

from unittest.mock import MagicMock


def _make_executor(executor_class):
    """Create an executor instance without calling __init__ or SSH setup."""
    executor = object.__new__(executor_class)
    executor.job = MagicMock()
    executor._log_buffer = []
    return executor


# ── InstanceHealthExecutor.parse_results ──────────────────────────────────────

class TestInstanceHealthParseResults(BaseCase):

    def setUp(self):
        from odoo.addons.incubacloud.models.instance_health_executor import InstanceHealthExecutor
        self.executor = _make_executor(InstanceHealthExecutor)

    def _results(self, *, container_state="", cpu_mem="0.0\t0.0",
                 http="exit:0", error_lines=""):
        return {
            "container_state":   {"stdout": container_state},
            "cpu_mem_snapshot":  {"stdout": cpu_mem},
            "http_health":       {"stdout": http},
            "error_lines":       {"stdout": error_lines},
        }

    def test_returns_empty_list_always(self):
        """parse_results never hard-fails — always returns []."""
        errors = self.executor.parse_results(self._results())
        self.assertEqual(errors, [])

    def test_container_running_when_odoo_running(self):
        results = self._results(container_state="odoo\trunning\ndb\trunning")
        self.executor.parse_results(results)
        self.assertTrue(self.executor._container_running)

    def test_container_not_running_when_odoo_exited(self):
        results = self._results(container_state="odoo\texited\ndb\trunning")
        self.executor.parse_results(results)
        self.assertFalse(self.executor._container_running)

    def test_container_not_running_when_odoo_absent(self):
        results = self._results(container_state="db\trunning")
        self.executor.parse_results(results)
        self.assertFalse(self.executor._container_running)

    def test_container_running_case_insensitive(self):
        results = self._results(container_state="odoo\tRunning")
        self.executor.parse_results(results)
        self.assertTrue(self.executor._container_running)

    def test_cpu_current_parsed(self):
        results = self._results(cpu_mem="42.5\t30.0")
        self.executor.parse_results(results)
        self.assertAlmostEqual(self.executor._cpu_current, 42.5)

    def test_mem_current_parsed(self):
        results = self._results(cpu_mem="10.0\t88.3")
        self.executor.parse_results(results)
        self.assertAlmostEqual(self.executor._mem_current, 88.3)

    def test_cpu_mem_defaults_to_zero_on_bad_data(self):
        results = self._results(cpu_mem="bad\tdata")
        self.executor.parse_results(results)
        self.assertEqual(self.executor._cpu_current, 0.0)
        self.assertEqual(self.executor._mem_current, 0.0)

    def test_cpu_mem_missing_key_defaults_to_zero(self):
        results = self._results()
        results.pop("cpu_mem_snapshot")
        self.executor.parse_results(results)
        self.assertEqual(self.executor._cpu_current, 0.0)
        self.assertEqual(self.executor._mem_current, 0.0)

    def test_http_ok_when_exit_zero(self):
        results = self._results(http="exit:0")
        self.executor.parse_results(results)
        self.assertTrue(self.executor._http_ok)

    def test_http_not_ok_when_exit_nonzero(self):
        results = self._results(http="exit:1")
        self.executor.parse_results(results)
        self.assertFalse(self.executor._http_ok)

    def test_http_not_ok_when_empty(self):
        results = self._results(http="")
        self.executor.parse_results(results)
        self.assertFalse(self.executor._http_ok)

    def test_error_lines_empty_yields_no_groups(self):
        results = self._results(error_lines="")
        self.executor.parse_results(results)
        self.assertEqual(self.executor._error_groups, [])
        self.assertEqual(self.executor._error_total, 0)

    def test_error_lines_single_line_yields_one_group(self):
        line = "2026-05-17 18:30:21,415 1 ERROR devel odoo.http: boom"
        self.executor.parse_results(self._results(error_lines=line))
        self.assertEqual(len(self.executor._error_groups), 1)
        self.assertEqual(self.executor._error_total, 1)
        self.assertEqual(self.executor._error_groups[0]['count'], 1)

    def test_error_lines_dedupe_identical_messages(self):
        line = "2026-05-17 18:30:21,415 1 ERROR devel odoo.http: boom"
        # Same message at three different timestamps + PIDs → one group.
        blob = "\n".join([
            line,
            "2026-05-17 18:30:22,415 2 ERROR devel odoo.http: boom",
            "2026-05-17 18:30:23,415 99 ERROR devel odoo.http: boom",
        ])
        self.executor.parse_results(self._results(error_lines=blob))
        self.assertEqual(len(self.executor._error_groups), 1)
        self.assertEqual(self.executor._error_groups[0]['count'], 3)
        self.assertEqual(self.executor._error_total, 3)

    def test_error_lines_dedupe_masks_numeric_ids(self):
        """Same error template with different record ids should fold together."""
        blob = "\n".join([
            "2026-05-17 18:30:21,415 1 ERROR devel mod.x: Record 42 not found",
            "2026-05-17 18:30:22,415 1 ERROR devel mod.x: Record 9123 not found",
            "2026-05-17 18:30:23,415 1 ERROR devel mod.x: Record 5500001 not found",
        ])
        self.executor.parse_results(self._results(error_lines=blob))
        self.assertEqual(len(self.executor._error_groups), 1)
        self.assertEqual(self.executor._error_groups[0]['count'], 3)

    def test_error_lines_multiple_distinct_groups(self):
        blob = "\n".join([
            "2026-05-17 18:30:21,415 1 ERROR devel mod.a: boom",
            "2026-05-17 18:30:22,415 1 ERROR devel mod.a: boom",
            "2026-05-17 18:30:23,415 1 ERROR devel mod.b: kaboom",
        ])
        self.executor.parse_results(self._results(error_lines=blob))
        self.assertEqual(len(self.executor._error_groups), 2)
        # Sorted by count desc → mod.a (2) first.
        self.assertEqual(self.executor._error_groups[0]['count'], 2)
        self.assertEqual(self.executor._error_groups[1]['count'], 1)
        self.assertEqual(self.executor._error_total, 3)

    def test_error_lines_samples_capped_per_group(self):
        line = "2026-05-17 18:30:21,415 1 ERROR devel odoo.http: boom"
        # Send 10 identical lines — samples should be capped to 3 (the
        # executor's _ERROR_SAMPLE_PER_GRP constant).
        blob = "\n".join([line] * 10)
        self.executor.parse_results(self._results(error_lines=blob))
        self.assertEqual(self.executor._error_groups[0]['count'], 10)
        self.assertLessEqual(len(self.executor._error_groups[0]['samples']), 3)

    def test_empty_results_dict_uses_defaults(self):
        self.executor.parse_results({})
        self.assertFalse(self.executor._container_running)
        self.assertEqual(self.executor._cpu_current, 0.0)
        self.assertEqual(self.executor._mem_current, 0.0)
        self.assertFalse(self.executor._http_ok)
        self.assertEqual(self.executor._error_groups, [])
        self.assertEqual(self.executor._error_total, 0)

    def test_parse_results_short_circuits_when_skipped(self):
        """When the cycle was skipped (intrusive job active), parse_results
        returns immediately without computing any health fields."""
        self.executor._skipped = True
        errors = self.executor.parse_results({"noop": {"stdout": ""}})
        self.assertEqual(errors, [])
        # No health state should have been computed.
        self.assertFalse(hasattr(self.executor, '_cpu_current'))
        self.assertFalse(hasattr(self.executor, '_error_groups'))

    def test_error_lines_command_anchors_and_strips_ansi(self):
        """The shell command for the error_lines step must:

        1. Anchor the ERROR keyword to the Odoo log-level field via
           ``grep -aP`` with a strict PCRE — otherwise INFO/DEBUG lines
           that happen to contain the substring "ERROR" (notably asyncssh
           logging the very command this step runs) are harvested as if
           they were errors and trigger a self-referential alert loop.
        2. Cover ``CRITICAL`` in addition to ``ERROR`` — a worker crash
           logs CRITICAL and would otherwise be missed.
        3. Strip ANSI color codes from output via ``sed`` so the samples
           stored in the alert payload render as plain text.
        """
        self.executor._skipped = False
        inst = MagicMock(last_health_check=False)
        self.executor._inst = MagicMock(return_value=inst)
        self.executor._inst_dir = MagicMock(return_value="~/foo/bar")
        cmds = dict(self.executor.get_commands())
        error_cmd = cmds["error_lines"]
        self.assertIn("grep -aP", error_cmd)
        self.assertIn("(ERROR|CRITICAL)", error_cmd)
        self.assertIn("sed -E", error_cmd)
        self.assertIn(r"\x1b\[[0-9;]*m", error_cmd)


# ── InstanceHealthExecutor pure helpers ───────────────────────────────────────

class TestInstanceHealthFingerprint(BaseCase):
    """Direct coverage for ``_fingerprint`` — the function that maps a
    raw Odoo log line to a dedupe key. Going through parse_results
    exercises it indirectly; these tests pin the contract explicitly so
    a regression on the regex does not silently break grouping.
    """

    def setUp(self):
        from odoo.addons.incubacloud.models.instance_health_executor import InstanceHealthExecutor
        self.executor = _make_executor(InstanceHealthExecutor)

    def test_strips_odoo_log_prefix(self):
        fp = self.executor._fingerprint(
            "2026-05-17 18:30:21,415 1 ERROR devel mod.x: boom"
        )
        self.assertFalse(fp.startswith("2026"))
        self.assertIn("ERROR devel mod.x: boom", fp)

    def test_masks_pointer_like_hex(self):
        fp = self.executor._fingerprint(
            "ERROR foo: at address 0xdeadbeef in handler"
        )
        self.assertIn("<HEX>", fp)
        self.assertNotIn("0xdeadbeef", fp)

    def test_masks_digit_runs(self):
        fp_short = self.executor._fingerprint("ERROR foo: record 42 missing")
        fp_long  = self.executor._fingerprint("ERROR foo: record 999999 missing")
        self.assertEqual(fp_short, fp_long)
        self.assertIn("<N>", fp_short)

    def test_truncates_to_max_length(self):
        long_msg = "ERROR foo: " + ("x" * 500)
        fp = self.executor._fingerprint(long_msg)
        # _FINGERPRINT_MAX_LEN is 160 — anything beyond is dropped.
        self.assertLessEqual(len(fp), 160)


class TestInstanceHealthBlockingJobs(BaseCase):
    """Coverage for ``_is_blocked_by_active_job`` — the gate that skips
    the entire health pass when an intrusive job is running on the same
    instance. Mocks the ``cloud.job`` model directly; full ORM-level
    integration belongs in a Tier-2 test if/when one is added.
    """

    def setUp(self):
        from odoo.addons.incubacloud.models.instance_health_executor import InstanceHealthExecutor
        self.executor = _make_executor(InstanceHealthExecutor)
        # Mock job + instance + env
        self.executor.job = MagicMock()
        self.executor.job.id = 42
        self.executor.job.instance_id.id = 7
        self.JobModel = MagicMock()
        self.JobModel._active_states = ['pending', 'enqueued', 'started']
        # __getitem__ on env yields the model
        self.executor.env = MagicMock()
        self.executor.env.__getitem__.return_value = self.JobModel

    def test_blocked_when_intrusive_job_active(self):
        self.JobModel.search_count.return_value = 1
        self.assertTrue(self.executor._is_blocked_by_active_job())
        # Verify the search domain includes the right job_type codes.
        domain = self.JobModel.search_count.call_args[0][0]
        domain_dict = {tup[0]: tup for tup in domain if isinstance(tup, tuple)}
        self.assertIn('job_type_id.code', domain_dict)
        codes = domain_dict['job_type_id.code'][2]
        self.assertIn('deploy_instance', codes)
        self.assertIn('rebuild_instance', codes)
        self.assertIn('full_setup', codes)
        self.assertIn('restore_instance', codes)

    def test_not_blocked_when_no_active_job(self):
        self.JobModel.search_count.return_value = 0
        self.assertFalse(self.executor._is_blocked_by_active_job())

    def test_not_blocked_when_instance_missing(self):
        self.executor.job.instance_id = False
        self.assertFalse(self.executor._is_blocked_by_active_job())
        # search_count must not have been called when there's no instance.
        self.JobModel.search_count.assert_not_called()

    def test_excludes_own_job_id_from_search(self):
        self.JobModel.search_count.return_value = 0
        self.executor._is_blocked_by_active_job()
        domain = self.JobModel.search_count.call_args[0][0]
        # ('id', '!=', 42) must be in the domain — otherwise the executor
        # would always block itself.
        self.assertIn(('id', '!=', 42), domain)


# ── Default (base) parse_results is fail-closed ────────────────────────────────

class TestDefaultParseResultsFailClosed(BaseCase):
    """The base AbstractExecutor.parse_results must fail-closed.

    The lifecycle executors (start/stop/restart/delete_*) deliberately do
    not override parse_results, so the base default is what decides their
    success. It must flag any non-zero exit as an error; otherwise a failed
    ``docker compose up -d`` would report the job done and leave the
    instance state falsely 'ok'.
    """

    def setUp(self):
        from odoo.addons.incubacloud.models.start_instance_executor import (
            StartInstanceExecutor,
        )
        self.executor = _make_executor(StartInstanceExecutor)

    def test_all_zero_exit_returns_no_errors(self):
        results = {"Start": {"exit_status": 0, "stdout": ""}}
        self.assertEqual(self.executor.parse_results(results), [])

    def test_nonzero_exit_returns_error(self):
        results = {"Start": {"exit_status": 1, "stdout": ""}}
        errors = self.executor.parse_results(results)
        self.assertEqual(len(errors), 1)
        self.assertIn("Start", errors[0])

    def test_missing_exit_status_treated_as_failure(self):
        # A step with no recorded exit_status must not pass silently.
        results = {"Start": {"stdout": ""}}
        self.assertEqual(len(self.executor.parse_results(results)), 1)
