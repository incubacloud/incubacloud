"""How a job's steps run, and why a run that skipped steps cannot succeed.

Every SSH-backed job is a list of steps run in order. ``stop_on_failure``
cuts the rest off when a step fails; ``continue_on`` lists exit codes a
step uses to say something other than "failed", which never stop the
run. Whatever ``parse_results`` decides afterwards, a run that skipped
steps did not do what the job promised.

Production, 2026-09-29: the delete forgave its purge's "already empty"
(exit 10) in ``parse_results``, after ``stop_on_failure`` had already
cut the teardown off. Nine deletes ended "done" with "removed from
host" and left every warm stack running on Tenants1.
"""
import asyncio
from unittest.mock import MagicMock, patch

from odoo.addons.queue_job.exception import RetryableJobError
from odoo.tests.common import BaseCase

from ..models.abstract_executor import AbstractExecutor
from ..models.transport import CommandResult, SSHTransport


class _Steps(AbstractExecutor):
    """A bare executor: the step loop and the outcome rules, nothing else.

    No ``_job_type``, so it never enters the executor registry.
    """

    def get_commands(self):
        """Unused: the tests hand ``_run_steps`` their own steps."""
        return []


def _executor():
    """Build a ``_Steps`` without a job, as the loop needs none."""
    ex = object.__new__(_Steps)
    ex._log_buffer = []
    return ex


def _run(ex, steps, exits):
    """Run *steps* through *ex*'s loop against a mocked host.

    :param list steps: ``(label, command[, opts])`` tuples.
    :param dict exits: ``{command: exit_status}``; absent means 0.
    :return: ``(results, skipped)`` as ``_run_steps`` returns them.
    """
    transport = MagicMock(spec=SSHTransport)
    transport.execute.side_effect = (
        lambda command, *_handlers: CommandResult("", exits.get(command, 0))
    )
    return asyncio.run(ex._run_steps(transport, steps))


class TestRunSteps(BaseCase):

    def test_every_step_runs_when_all_succeed(self):
        results, skipped = _run(
            _executor(), [("A", "a"), ("B", "b")], {},
        )
        self.assertEqual(list(results), ["A", "B"])
        self.assertEqual(skipped, [])

    def test_a_failing_step_without_stop_does_not_stop_the_run(self):
        results, skipped = _run(
            _executor(), [("A", "a"), ("B", "b")], {"a": 1},
        )
        self.assertEqual(list(results), ["A", "B"])
        self.assertEqual(skipped, [])

    def test_stop_on_failure_cuts_the_rest_off_and_names_it(self):
        results, skipped = _run(
            _executor(),
            [("A", "a", {"stop_on_failure": True}), ("B", "b"), ("C", "c")],
            {"a": 2},
        )
        self.assertEqual(list(results), ["A"])
        self.assertEqual(skipped, ["B", "C"])

    def test_a_continue_on_code_never_stops_the_run(self):
        opts = {"stop_on_failure": True, "continue_on": (10,)}
        results, skipped = _run(
            _executor(), [("A", "a", opts), ("B", "b")], {"a": 10},
        )
        self.assertEqual(list(results), ["A", "B"])
        self.assertEqual(results["A"]["exit_status"], 10)
        self.assertEqual(skipped, [])

    def test_other_codes_still_stop_a_step_with_continue_on(self):
        opts = {"stop_on_failure": True, "continue_on": (10,)}
        results, skipped = _run(
            _executor(), [("A", "a", opts), ("B", "b")], {"a": 22},
        )
        self.assertEqual(list(results), ["A"])
        self.assertEqual(skipped, ["B"])


class TestOutcomeErrors(BaseCase):

    def test_skipped_steps_fail_a_run_parse_results_forgave(self):
        """The delete's bug, in general form."""
        ex = _executor()
        with patch.object(ex, "parse_results", return_value=[]):
            errors = ex._outcome_errors(
                {"Purge": {"stdout": "", "exit_status": 10}},
                ["Teardown", "Remove directory"],
            )
        self.assertEqual(len(errors), 1)
        self.assertIn("'Purge' stopped the run", errors[0])
        self.assertIn("'Teardown', 'Remove directory'", errors[0])

    def test_an_abort_on_the_last_step_skips_nothing(self):
        """Purge-archived's single step forgives its "already empty":
        nothing was cut off, so success stays success."""
        ex = _executor()
        with patch.object(ex, "parse_results", return_value=[]):
            self.assertEqual(
                ex._outcome_errors(
                    {"Purge archived": {"stdout": "", "exit_status": 10}}, [],
                ),
                [],
            )

    def test_parse_results_errors_are_kept_as_they_are(self):
        ex = _executor()
        with patch.object(ex, "parse_results", return_value=["boom"]):
            self.assertEqual(
                ex._outcome_errors(
                    {"A": {"stdout": "", "exit_status": 1}}, ["B"],
                ),
                ["boom"],
            )

    def test_a_retry_is_still_a_retry(self):
        """The rebuild's exit 75 raises ``RetryableJobError`` from
        ``parse_results`` with steps skipped. That must reach queue_job
        as a retry, not turn into a failure."""
        ex = _executor()
        with patch.object(
            ex, "parse_results", side_effect=RetryableJobError("again"),
        ), self.assertRaises(RetryableJobError):
            ex._outcome_errors(
                {"Update": {"stdout": "", "exit_status": 75}}, ["Hand back"],
            )

    def test_an_empty_run_is_a_success(self):
        """A delete re-run with nothing left to do returns no steps."""
        self.assertEqual(_executor()._outcome_errors({}, []), [])
