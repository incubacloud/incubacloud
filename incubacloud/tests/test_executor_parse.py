"""
Tier 1 — Pure-Python unit tests for executor parse_results() methods.

Tests use object.__new__() to bypass __init__ so no Odoo ORM is needed.
All ORM/SSH interaction is excluded from parse_results() by design.
"""
import unittest
from unittest.mock import MagicMock


def _make_executor(executor_class):
    """Create an executor instance without calling __init__ or SSH setup."""
    executor = object.__new__(executor_class)
    executor.job = MagicMock()
    executor._log_buffer = []
    return executor


# ── DockerPruneExecutor.parse_results ─────────────────────────────────────────

class TestDockerPruneParseResults(unittest.TestCase):

    def setUp(self):
        from odoo.addons.incubacloud.models.docker_prune_executor import DockerPruneExecutor
        self.executor = _make_executor(DockerPruneExecutor)

    def _results(self, exit_status=0, stdout=""):
        return {"prune": {"exit_status": exit_status, "stdout": stdout}}

    def test_success_returns_empty_errors(self):
        errors = self.executor.parse_results(self._results(exit_status=0))
        self.assertEqual(errors, [])

    def test_nonzero_exit_returns_error_message(self):
        errors = self.executor.parse_results(self._results(exit_status=1))
        self.assertEqual(len(errors), 1)
        self.assertIn("exit 1", errors[0])

    def test_exit_status_2_in_error_message(self):
        errors = self.executor.parse_results(self._results(exit_status=2))
        self.assertIn("exit 2", errors[0])

    def test_missing_prune_key_returns_empty(self):
        """If the command result is missing, default exit_status is 0 → no error."""
        errors = self.executor.parse_results({})
        self.assertEqual(errors, [])

    def test_prune_key_empty_dict_returns_empty(self):
        errors = self.executor.parse_results({"prune": {}})
        self.assertEqual(errors, [])


# ── InstanceHealthExecutor.parse_results ──────────────────────────────────────

class TestInstanceHealthParseResults(unittest.TestCase):

    def setUp(self):
        from odoo.addons.incubacloud.models.instance_health_executor import InstanceHealthExecutor
        self.executor = _make_executor(InstanceHealthExecutor)

    def _results(self, *, container_state="", cpu_mem="0.0\t0.0",
                 http="exit:0", error_count="0"):
        return {
            "container_state": {"stdout": container_state},
            "cpu_mem_peak":    {"stdout": cpu_mem},
            "http_health":     {"stdout": http},
            "error_count":     {"stdout": error_count},
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

    def test_cpu_peak_parsed(self):
        results = self._results(cpu_mem="42.5\t30.0")
        self.executor.parse_results(results)
        self.assertAlmostEqual(self.executor._cpu_peak, 42.5)

    def test_mem_peak_parsed(self):
        results = self._results(cpu_mem="10.0\t88.3")
        self.executor.parse_results(results)
        self.assertAlmostEqual(self.executor._mem_peak, 88.3)

    def test_cpu_mem_defaults_to_zero_on_bad_data(self):
        results = self._results(cpu_mem="bad\tdata")
        self.executor.parse_results(results)
        self.assertEqual(self.executor._cpu_peak, 0.0)
        self.assertEqual(self.executor._mem_peak, 0.0)

    def test_cpu_mem_missing_key_defaults_to_zero(self):
        results = self._results()
        results.pop("cpu_mem_peak")
        self.executor.parse_results(results)
        self.assertEqual(self.executor._cpu_peak, 0.0)
        self.assertEqual(self.executor._mem_peak, 0.0)

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

    def test_error_count_parsed(self):
        results = self._results(error_count="7")
        self.executor.parse_results(results)
        self.assertEqual(self.executor._error_count, 7)

    def test_error_count_zero_when_no_errors(self):
        results = self._results(error_count="0")
        self.executor.parse_results(results)
        self.assertEqual(self.executor._error_count, 0)

    def test_error_count_uses_last_line(self):
        """grep -c may prepend filename; last line is the count."""
        results = self._results(error_count="file.log:3\n3")
        self.executor.parse_results(results)
        self.assertEqual(self.executor._error_count, 3)

    def test_error_count_defaults_zero_on_invalid(self):
        results = self._results(error_count="not-a-number")
        self.executor.parse_results(results)
        self.assertEqual(self.executor._error_count, 0)

    def test_empty_results_dict_uses_defaults(self):
        self.executor.parse_results({})
        self.assertFalse(self.executor._container_running)
        self.assertEqual(self.executor._cpu_peak, 0.0)
        self.assertEqual(self.executor._mem_peak, 0.0)
        self.assertFalse(self.executor._http_ok)
        self.assertEqual(self.executor._error_count, 0)
