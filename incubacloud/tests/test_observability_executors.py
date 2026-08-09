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
            "metrics_account": "acct_test01",
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
        """Belt and braces behind the cron's own gate.

        The reconciliation cron will not queue this job while
        observability is unconfigured, but the job stays enqueueable
        directly. Installing anyway would leave three containers running
        and pushing nowhere, which reads as healthy from the host.
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
        self.assertEqual(extra["ic_remote_write_user"], "acct_test01")
        self.assertEqual(extra["ic_account"], "acct_test01")

    def test_instances_carry_the_traefik_service_prefix(self):
        """Without it, HTTP samples cannot be attributed to an instance.

        The prefix is derived, not guessed: the panel feeds copier the
        same project name it forces into COMPOSE_PROJECT_NAME, so the
        service Traefik reports under is computable from panel state.
        """
        project = self.env["cloud.project"].create({"name": "P"})
        instance = self.env["cloud.instance"].create({
            "name": "prod", "project_id": project.id,
            "environment": "production", "host_id": self.host.id,
            "odoo_version": "19.0",
        })
        # ``state`` is only writable through the state machine.
        instance._transition("deploying")
        instance._transition("deployed")
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        labels = [
            i for i in extra["ic_instances"]
            if i["instance_id"] == str(instance.id)
        ]
        self.assertTrue(labels, "the deployed instance was not labelled")
        self.assertEqual(
            labels[0]["traefik_prefix"],
            f"{instance.doodba_project_name}-19-0-",
        )

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

    def test_extra_vars_carry_retention_and_the_account_list(self):
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertEqual(extra["ic_retention_days"], 45)
        # The htpasswd IS the access-control list: an account in it can
        # write and read its own series and nothing else. Hashed, so the
        # assertion is on the user half.
        self.assertIn("acct_test01:", extra["ic_accounts_htpasswd"])
        self.assertNotIn(
            "shared-secret", extra["ic_accounts_htpasswd"],
            "the credential was written in clear instead of hashed",
        )

    def test_the_operator_credential_is_separate_and_generated(self):
        """It reads across accounts, so it must never be an account's own.

        Reusing the account credential here would hand every host a key
        to the unfiltered view — the exact opposite of what the split
        exists for.
        """
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertIn("operator:", extra["ic_operator_htpasswd"])
        self.assertTrue(extra["ic_operator_plain"])
        self.assertNotEqual(extra["ic_operator_plain"], "shared-secret")

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


class TestLogCollectorEndpoint(ObservabilityExecutorCase):
    """Where access logs are pushed is derived, not configured twice."""

    def _url(self):
        return self._make(
            ObservabilityAgentsExecutor,
        )._logs_write_url(self.settings)

    def test_it_is_derived_from_the_metrics_endpoint(self):
        """One field fewer to fill in is one field fewer to get wrong."""
        self.settings.metrics_remote_write_url = "https://m.example.com/w/"
        self.assertEqual(self._url(), "https://m.example.com/lw/")

    def test_a_missing_trailing_slash_still_works(self):
        self.settings.metrics_remote_write_url = "https://m.example.com/w"
        self.assertEqual(self._url(), "https://m.example.com/lw/")

    def test_a_foreign_endpoint_disables_collection(self):
        """A self-hosted operator may point at their own backend.

        Guessing a URL there would start a collector that retries
        forever against something that does not exist — worse than not
        collecting, because it looks like a fault.
        """
        self.settings.metrics_remote_write_url = (
            "https://vm.example.com/api/v1/write"
        )
        self.assertEqual(self._url(), "")

    def test_the_collector_is_not_deployed_without_an_endpoint(self):
        self.settings.metrics_remote_write_url = (
            "https://vm.example.com/api/v1/write"
        )
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertEqual(extra["ic_logs_write_url"], "")


class TestFirstDeploymentIsUsable(ObservabilityExecutorCase):
    """A central that comes up accepting no writes is the worst outcome.

    It reports healthy from every angle — containers up, health endpoint
    answering — while silently refusing every agent. The account file is
    built from state that must therefore already exist when the playbook
    runs, not be created after it succeeds.
    """

    def test_the_account_exists_before_the_account_file_is_built(self):
        """Regression: the credential used to be minted in on_success.

        The first deployment then wrote an empty account file, so the
        central had to be deployed a second time before it would accept
        anything — with nothing in the job log to say so.
        """
        self.settings.write({
            "metrics_account": False,
            "metrics_remote_write_token": False,
        })
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertTrue(
            extra["ic_accounts_htpasswd"].strip(),
            "the central would have come up accepting no writes at all",
        )
        self.assertTrue(extra["ic_accounts"])
        self.assertTrue(self.settings.metrics_account)
