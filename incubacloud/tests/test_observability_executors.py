"""Unit tests for the two observability Ansible executors.

Both deploy containers on a real machine, so what matters here is the
judgement they apply *before* and *after* the playbook runs: refusing to
install agents that would push into the void, and refusing to call a
deployment successful when the backend never answered.

Built the same way as the hardening executor's tests — instantiated
without ``__init__`` so no SSH transport or job record is needed.
"""
from unittest.mock import MagicMock

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.observability_agents_executor import (
    ObservabilityAgentsExecutor,
)
from odoo.addons.incubacloud.models.observability_central_executor import (
    ObservabilityCentralExecutor,
)


class ObservabilityExecutorCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.write({
            "metrics_enabled": True,
            "metrics_central_url": "http://vm.test:8428",
            "metrics_remote_write_url": "http://vm.test:8428/api/v1/write",
            "metrics_remote_write_token": "shared-secret",
            "metrics_retention_days": 45,
        })
        self.host = self.env["cloud.host"].create({
            "name": "HX",
            "ip_address": "10.0.0.40",
            "user": "root",
            "wildcard_domain": "hx.example.com",
        })

    def _make(self, cls):
        """Build an executor bound to this test's host.

        :param cls: executor class to instantiate.
        :return: a usable executor with no transport.
        """
        executor = object.__new__(cls)
        executor.env = self.env
        executor.job = MagicMock(spec=type(self.env["cloud.job"]))
        executor.job.host_id = self.host
        executor._log_buffer = []
        executor._sys = lambda *args, **kwargs: None
        executor._facts = {}
        return executor


class TestAgentsExecutor(ObservabilityExecutorCase):

    def test_refuses_to_install_when_observability_is_off(self):
        """The button is on every host page, so it can be pressed early.

        Installing anyway would leave three containers running and
        pushing nowhere, which reads as healthy from the host.
        """
        self.settings.metrics_enabled = False
        with self.assertRaises(UserError):
            self._make(ObservabilityAgentsExecutor).get_extra_vars()

    def test_refuses_to_install_without_a_remote_write_url(self):
        self.settings.metrics_remote_write_url = ""
        with self.assertRaises(UserError):
            self._make(ObservabilityAgentsExecutor).get_extra_vars()

    def test_extra_vars_carry_the_host_identity_and_credential(self):
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertEqual(extra["ic_host_id"], str(self.host.id))
        self.assertEqual(extra["ic_host_name"], "HX")
        self.assertEqual(extra["ic_remote_write_token"], "shared-secret")

    def test_draft_instances_are_not_labelled(self):
        """A draft instance has no containers; a label for it is noise."""
        project = self.env["cloud.project"].create({"name": "P"})
        self.env["cloud.instance"].create({
            "name": "draft-one", "project_id": project.id,
            "environment": "staging", "host_id": self.host.id,
        })
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertEqual(extra["ic_instances"], [])

    def test_a_failed_playbook_is_an_error(self):
        executor = self._make(ObservabilityAgentsExecutor)
        errors = executor.parse_results(
            {executor._playbook: {"exit_status": 2}},
        )
        self.assertTrue(errors)

    def test_partial_agents_are_an_error(self):
        """Two of three containers up is a broken install, not a success."""
        executor = self._make(ObservabilityAgentsExecutor)
        executor._facts = {"ic_agents_running": 2}
        errors = executor.parse_results(
            {executor._playbook: {"exit_status": 0}},
        )
        self.assertTrue(errors)

    def test_all_three_agents_up_is_a_success(self):
        executor = self._make(ObservabilityAgentsExecutor)
        executor._facts = {"ic_agents_running": 3}
        self.assertEqual(
            executor.parse_results({executor._playbook: {"exit_status": 0}}),
            [],
        )


class TestCentralExecutor(ObservabilityExecutorCase):

    def test_extra_vars_carry_retention_and_credential(self):
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertEqual(extra["ic_retention_days"], 45)
        self.assertEqual(extra["ic_remote_write_token"], "shared-secret")

    def test_retention_falls_back_to_ninety_days(self):
        """Zero would tell VictoriaMetrics to keep nothing."""
        self.settings.metrics_retention_days = 0
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertEqual(extra["ic_retention_days"], 90)

    def test_a_backend_that_never_answered_is_not_a_success(self):
        """rc=0 only means Ansible finished, not that the stack works.

        The playbook's own health probe is the real verdict; without
        this check a central whose container crash-looped would report
        as deployed and every later query would fail instead.
        """
        executor = self._make(ObservabilityCentralExecutor)
        executor._facts = {"ic_central_up": "False"}
        errors = executor.parse_results(
            {executor._playbook: {"exit_status": 0}},
        )
        self.assertTrue(errors)

    def test_a_healthy_backend_is_a_success(self):
        executor = self._make(ObservabilityCentralExecutor)
        executor._facts = {"ic_central_up": "True"}
        self.assertEqual(
            executor.parse_results({executor._playbook: {"exit_status": 0}}),
            [],
        )

    def test_a_failed_playbook_short_circuits(self):
        executor = self._make(ObservabilityCentralExecutor)
        errors = executor.parse_results(
            {executor._playbook: {"exit_status": 1}},
        )
        self.assertEqual(len(errors), 1)
