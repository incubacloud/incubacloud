"""Unit tests for the two observability Ansible executors.

Both deploy containers on a real machine, so what matters here is the
judgement they apply *before* and *after* the playbook runs: refusing to
install agents that would push into the void, and refusing to call a
deployment successful when the backend never answered.

Built the same way as the hardening executor's tests — instantiated
without ``__init__`` so no SSH transport or job record is needed.
"""
import asyncio
import re
from unittest.mock import MagicMock

import yaml

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.metrics_acl_sync_executor import (
    MetricsAclSyncExecutor,
)
from odoo.addons.incubacloud.models.observability_agents_executor import (
    ObservabilityAgentsExecutor,
)
from odoo.addons.incubacloud.models.observability_central_executor import (
    _ACCOUNT_LOG_READ_PATHS,
    _ACCOUNT_LOG_WRITE_PATH,
    _ACCOUNT_READ_PATHS,
    _ACCOUNT_WRITE_PATH,
    _OPERATOR_DELETE_PATH,
    ObservabilityCentralExecutor,
)
from odoo.addons.incubacloud.tests.test_executor_durable_writes import (
    JobEndAssertions,
)


class ObservabilityExecutorCase(TransactionCase):

    def setUp(self):
        super().setUp()
        # The central's credentials are minted on a durable cursor; in
        # test mode that cursor rides on this test's transaction.
        self.registry_enter_test_mode()
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
        executor.job.env = self.env
        executor._log_buffer = []
        executor._sys = lambda *args, **kwargs: None
        executor._facts = {}
        # The durable cursor reads the database, not this env's cache,
        # so whatever the test set up has to be there first.
        self.env.flush_all()
        return executor

    def _user(self, cfg, username):
        """Return the vmauth user dict for *username*, or None.

        :param cfg: the parsed vmauth config.
        :param username: the account (or ``operator``) to find.
        """
        return next(
            (u for u in cfg.get("users", []) if u["username"] == username),
            None,
        )

    def _route(self, user, src_path):
        """Return the url_map entry of *user* matching *src_path*.

        :param user: a vmauth user dict.
        :param src_path: the exact ``src_paths`` regex to match.
        """
        return next(
            e for e in user["url_map"] if src_path in e["src_paths"]
        )


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
        cfg = yaml.safe_load(extra["ic_vmauth_config"])
        # The vmauth user list IS the access-control list: a user in it
        # writes and reads its own series and nothing else. The account
        # is a username.
        account = self._user(cfg, "acct_test01")
        self.assertIsNotNone(
            account, "the account is not a vmauth user, so it cannot write",
        )
        # And the label it forces on writes is derived from that user, so
        # a client cannot write as anyone else.
        write = self._route(account, _ACCOUNT_WRITE_PATH)
        self.assertIn("extra_label=ic_account=acct_test01", write["url_prefix"])

    def test_the_operator_credential_is_separate_and_generated(self):
        """It reads across accounts, so it must never be an account's own.

        Reusing the account credential here would hand every host a key
        to the unfiltered view — the exact opposite of what the split
        exists for.
        """
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        cfg = yaml.safe_load(extra["ic_vmauth_config"])
        operator = self._user(cfg, "operator")
        self.assertIsNotNone(operator)
        self.assertTrue(extra["ic_operator_plain"])
        self.assertNotEqual(extra["ic_operator_plain"], "shared-secret")
        self.assertEqual(operator["password"], extra["ic_operator_plain"])
        # The operator owns the cross-account doors and nothing else.
        paths = {sp for entry in operator["url_map"] for sp in entry["src_paths"]}
        self.assertEqual(
            paths, {"/admin-r/.*", "/gadmin/.*", _OPERATOR_DELETE_PATH},
        )

    def test_retention_falls_back_to_ninety_days(self):
        """Zero would tell VictoriaMetrics to keep nothing."""
        self.settings.metrics_retention_days = 0
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertEqual(extra["ic_retention_days"], 90)

    def test_every_data_route_forces_the_account(self):
        """The label pinning is what the whole boundary rests on.

        Measured against real VM + Loki (lab, 2026-08-10): a client that
        sends its own extra_label / extra_filters / X-Scope-OrgID has it
        dropped for colliding with the one the route forces, so it cannot
        write or read as anyone else. Pinned here so a config change that
        drops the forcing fails loudly.
        """
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        cfg = yaml.safe_load(extra["ic_vmauth_config"])
        account = self._user(cfg, "acct_test01")
        self.assertIn(
            "extra_label=ic_account=acct_test01",
            self._route(account, _ACCOUNT_WRITE_PATH)["url_prefix"],
        )
        self.assertIn(
            "acct_test01",
            self._route(account, _ACCOUNT_READ_PATHS)["url_prefix"],
        )
        for logs in (_ACCOUNT_LOG_WRITE_PATH, _ACCOUNT_LOG_READ_PATHS):
            self.assertIn(
                "X-Scope-OrgID: acct_test01",
                self._route(account, logs)["headers"],
            )

    def test_no_unauthorized_user_so_missing_credentials_are_401(self):
        """That section's ABSENCE is what makes a credential-less request
        401 rather than proxied — measured, where adding it turned the
        401 into a 503 against a dead backend."""
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        cfg = yaml.safe_load(extra["ic_vmauth_config"])
        self.assertNotIn("unauthorized_user", cfg)

    def test_every_route_uses_a_bare_root_prefix_and_drops_the_segment(self):
        """The path shape that avoids the trailing-slash 404 Loki gives.

        vmauth APPENDS the client's remaining path to url_prefix, so a
        fixed-path prefix duplicates the path or leaves a slash Loki
        rejects. Every route must therefore point at the backend root and
        drop the one-segment account prefix.
        """
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        cfg = yaml.safe_load(extra["ic_vmauth_config"])
        for user in cfg["users"]:
            for entry in user["url_map"]:
                where = f"{user['username']} {entry['src_paths']}"
                self.assertEqual(
                    entry.get("drop_src_path_prefix_parts"), 1,
                    f"{where} must drop the account segment",
                )
                root = entry["url_prefix"].split("?", 1)[0]
                self.assertTrue(
                    root.endswith("/") and root.count("/") == 3,
                    f"{where} url_prefix is not a bare root: {root}",
                )

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


class TestAccountRoutesNameTheirEndpoints(ObservabilityExecutorCase):
    """An account reaches the endpoints it uses, and nothing else.

    Measured (lab, 2026-09-27) against the ``/r/.*`` these replaced: an
    account wrote series labelled as ANOTHER account through
    ``/r/api/v1/import/prometheus`` and that account saw them as its own;
    it also created snapshots and forced merges. vmauth matches
    ``src_paths`` against the whole path, so ``re.fullmatch`` here is
    the same question vmauth asks.
    """

    #: What agents, the panel and Grafana 11.2 were measured using.
    _ALLOWED = (
        "/w/api/v1/write",
        "/r/api/v1/query",
        "/r/api/v1/query_range",
        "/r/api/v1/query_exemplars",
        "/r/api/v1/series",
        "/r/api/v1/labels",
        "/r/api/v1/label/__name__/values",
        "/r/api/v1/label/host/values",
        "/r/api/v1/metadata",
        "/r/api/v1/rules",
        "/r/api/v1/status/buildinfo",
        "/lw/loki/api/v1/push",
        "/lr/loki/api/v1/query",
        "/lr/loki/api/v1/query_range",
        "/lr/loki/api/v1/labels",
        "/lr/loki/api/v1/label/job/values",
        "/lr/loki/api/v1/index/stats",
    )

    #: Writes down a read route, deletions, and the backends' admin.
    _REFUSED = (
        "/r/api/v1/import/prometheus",
        "/r/api/v1/import",
        "/r/api/v1/write",
        "/r/api/v1/admin/tsdb/delete_series",
        "/r/api/v1/export",
        "/r/snapshot/create",
        "/r/snapshot/delete_all",
        "/r/internal/force_merge",
        "/r/internal/resetRollupResultCache",
        "/r/flags",
        "/r/metrics",
        "/r/api/v1/label/a/b/values",
        "/r/api/v1/query/../../api/v1/import/prometheus",
        "/w/api/v1/import/prometheus",
        "/w/api/v1/admin/tsdb/delete_series",
        "/w/api/v1/write/../import/prometheus",
        "/w/",
        "/lw/loki/api/v1/query",
        "/lr/loki/api/v1/push",
        "/lr/loki/api/v1/delete",
        "/lr/flush",
        "/lr/config",
    )

    def _account_paths(self):
        """Return every ``src_paths`` regex of this panel's account."""
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        cfg = yaml.safe_load(extra["ic_vmauth_config"])
        account = self._user(cfg, "acct_test01")
        return [sp for entry in account["url_map"] for sp in entry["src_paths"]]

    def _reachable(self, path, patterns):
        """Return True if vmauth would route *path* for this account."""
        return any(re.fullmatch(p, path) for p in patterns)

    def test_every_endpoint_in_use_is_reachable(self):
        """Cutting one of these blanks a dashboard or stops an agent."""
        patterns = self._account_paths()
        for path in self._ALLOWED:
            self.assertTrue(
                self._reachable(path, patterns),
                f"{path} is in use and no account route serves it",
            )

    def test_writes_deletions_and_admin_are_not(self):
        patterns = self._account_paths()
        for path in self._REFUSED:
            self.assertFalse(
                self._reachable(path, patterns),
                f"an account can reach {path}",
            )

    def test_no_account_route_is_a_catch_all(self):
        """A trailing ``.*`` is how the injection route existed."""
        for pattern in self._account_paths():
            self.assertNotIn(".*", pattern, pattern)


class TestSeriesDeletionKey(ObservabilityExecutorCase):
    """Only the operator's deletion route may carry the deletion key."""

    def _config(self, executor_cls=ObservabilityCentralExecutor):
        """Return ``(extra_vars, parsed vmauth config)`` for *executor_cls*."""
        extra = self._make(executor_cls).get_extra_vars()
        return extra, yaml.safe_load(extra["ic_vmauth_config"])

    def test_the_deployment_mints_it_and_hands_it_to_the_backend(self):
        extra, _cfg = self._config()
        self.assertTrue(extra["ic_delete_auth_key"])

    def test_the_job_reads_the_key_it_handed_to_the_backend(self):
        """The rest of the run sees the key it minted, from its own cache."""
        self.settings.metrics_delete_auth_key = False
        extra, _cfg = self._config()
        self.assertEqual(
            self.settings.metrics_delete_auth_key, extra["ic_delete_auth_key"],
        )

    def test_a_redeployment_keeps_the_recorded_key(self):
        """Rotating it on every deployment would buy nothing."""
        self.settings.metrics_delete_auth_key = "k" * 43
        extra, _cfg = self._config()
        self.assertEqual(extra["ic_delete_auth_key"], "k" * 43)

    def test_a_failed_deployment_leaves_the_key_for_the_next_one(self):
        """Minted before the playbook, so a failure cannot take it back.

        The backend may not have it yet, and purges fail until a good
        deployment starts it — with this same key, not a new one.
        """
        self.settings.metrics_delete_auth_key = False
        executor = self._make(ObservabilityCentralExecutor)
        first = executor.get_extra_vars()["ic_delete_auth_key"]
        executor._alert = MagicMock(spec=ObservabilityCentralExecutor._alert)
        asyncio.run(executor.on_failure({}, ["boom"]))
        again, _cfg = self._config()
        self.assertEqual(again["ic_delete_auth_key"], first)

    def test_the_operator_route_adds_it_and_reaches_only_delete_series(self):
        extra, cfg = self._config()
        route = self._route(self._user(cfg, "operator"), _OPERATOR_DELETE_PATH)
        self.assertEqual(
            route["url_prefix"],
            f"http://victoriametrics:8428/?authKey={extra['ic_delete_auth_key']}",
        )
        self.assertFalse(
            re.fullmatch(_OPERATOR_DELETE_PATH, "/admin-d/snapshot/create"),
        )

    def test_no_account_ever_sees_it(self):
        """With it, an account could delete any series of any account."""
        extra, cfg = self._config()
        key = extra["ic_delete_auth_key"]
        for user in cfg["users"]:
            if user["username"] == "operator":
                continue
            self.assertNotIn(key, yaml.safe_dump(user), user["username"])

    def test_the_account_sync_keeps_the_route(self):
        """The sync REPLACES the document; dropping it would lose the route."""
        self.settings.metrics_delete_auth_key = "k" * 43
        self.settings._ensure_operator_credential()
        _extra, cfg = self._config(MetricsAclSyncExecutor)
        route = self._route(self._user(cfg, "operator"), _OPERATOR_DELETE_PATH)
        self.assertTrue(route["url_prefix"].endswith("?authKey=" + "k" * 43))

    def test_the_account_sync_never_mints_it(self):
        """The backend only knows a key a deployment restarted it with.

        A route carrying a key VictoriaMetrics was never given answers
        401, so a sync on a central deployed before deletions existed
        leaves the route out rather than inventing one.
        """
        self.settings.metrics_delete_auth_key = False
        self.settings._ensure_operator_credential()
        _extra, cfg = self._config(MetricsAclSyncExecutor)
        paths = {
            sp
            for entry in self._user(cfg, "operator")["url_map"]
            for sp in entry["src_paths"]
        }
        self.assertNotIn(_OPERATOR_DELETE_PATH, paths)
        self.assertFalse(self.settings.metrics_delete_auth_key)


class TestTheWholeAccountListTravels(ObservabilityExecutorCase):
    """Grafana organisations of accounts missing from the list are deleted.

    So the list must be the whole one in both jobs — the sync narrows
    ``ic_accounts`` to the new accounts, and handing that to the
    convergence would delete every other organisation.
    """

    def test_the_deployment_names_every_account(self):
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.assertEqual(extra["ic_account_names"], ["acct_test01"])

    def test_the_sync_names_every_account_not_only_the_new_ones(self):
        self.settings.metrics_accounts_deployed = "acct_test01"
        self.settings._ensure_operator_credential()
        extra = self._make(MetricsAclSyncExecutor).get_extra_vars()
        self.assertEqual(extra["ic_accounts"], [])
        self.assertEqual(extra["ic_account_names"], ["acct_test01"])


class TestLogCollectorEndpoint(ObservabilityExecutorCase):
    """Where access logs are pushed is derived, not configured twice."""

    def _url(self):
        return self._make(
            ObservabilityAgentsExecutor,
        )._logs_write_url(self.settings)

    def test_it_is_derived_from_the_metrics_endpoint(self):
        """One field fewer to fill in is one field fewer to get wrong.

        Both endpoints live under the account prefix vmauth strips: the
        write path is ``/w/api/v1/write`` and the log path is the sibling
        ``/lw/loki/api/v1/push``.
        """
        self.settings.metrics_remote_write_url = (
            "https://m.example.com/w/api/v1/write"
        )
        self.assertEqual(
            self._url(), "https://m.example.com/lw/loki/api/v1/push",
        )

    def test_a_trailing_slash_still_works(self):
        self.settings.metrics_remote_write_url = (
            "https://m.example.com/w/api/v1/write/"
        )
        self.assertEqual(
            self._url(), "https://m.example.com/lw/loki/api/v1/push",
        )

    def test_a_foreign_endpoint_disables_collection(self):
        """A self-hosted operator may point at their own backend.

        Guessing a URL there would start a collector that retries
        forever against something that does not exist — worse than not
        collecting, because it looks like a fault. A backend that is not
        ours does not carry the ``/w/`` account prefix.
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
        cfg = yaml.safe_load(extra["ic_vmauth_config"])
        real_accounts = [
            u for u in cfg["users"] if u["username"] != "operator"
        ]
        self.assertTrue(
            real_accounts,
            "the central would have come up accepting no writes at all",
        )
        self.assertTrue(extra["ic_accounts"])
        self.assertTrue(self.settings.metrics_account)


class TestTheCentralsSecretsOutliveTheJob(JobEndAssertions, ObservabilityExecutorCase):
    """What a run mints must still be there once the runner ends the job.

    ``get_extra_vars`` runs in the job's asynchronous phase. Core 1.0.135
    minted the deletion key there with a plain write: VictoriaMetrics
    started with it and the database never received it. Every secret
    below is checked by SQL after the same ``env.clear()`` the runner
    does, which is the check that release was missing.
    """

    def setUp(self):
        """Start from a fresh install: nothing minted yet."""
        super().setUp()
        # A fresh install: nothing minted yet.
        self.settings.write({
            "metrics_account": False,
            "metrics_remote_write_token": False,
            "metrics_operator_token": False,
            "grafana_admin_password": False,
            "metrics_delete_auth_key": False,
        })

    def _assert_saved(self, extra):
        """Every credential the playbook received is the one in the database."""
        (account,) = extra["ic_account_names"]
        self.assertTrue(account)
        self.assertEqual(self.stored(self.settings, "metrics_account"), account)
        passwords = {a["user"]: a["password"] for a in extra["ic_accounts"]}
        if account in passwords:
            self.assertEqual(
                self.stored(self.settings, "metrics_remote_write_token"),
                passwords[account],
            )
        self.assertEqual(
            self.stored(self.settings, "metrics_operator_token"),
            extra["ic_operator_plain"],
        )

    def test_the_deployment_saves_everything_it_ships(self):
        """
        Every secret the deployment hands the playbook is in the database.
        """
        extra = self._make(ObservabilityCentralExecutor).get_extra_vars()
        self.end_job()
        self._assert_saved(extra)
        self.assertEqual(
            self.stored(self.settings, "grafana_admin_password"),
            extra["ic_grafana_admin_password"],
        )
        self.assertEqual(
            self.stored(self.settings, "metrics_delete_auth_key"),
            extra["ic_delete_auth_key"],
        )

    def test_the_account_sync_saves_everything_it_ships(self):
        """The sync saves what it mints, and never mints the deletion key."""
        extra = self._make(MetricsAclSyncExecutor).get_extra_vars()
        self.end_job()
        self._assert_saved(extra)
        self.assertTrue(self.stored(self.settings, "grafana_admin_password"))
        self.assertFalse(self.stored(self.settings, "metrics_delete_auth_key"))

    def test_the_job_reads_what_it_minted_without_a_query(self):
        """The rest of the run resolves the panel's own account from the cache."""
        self._make(ObservabilityCentralExecutor).get_extra_vars()
        with self.assertQueryCount(0, flush=False):
            account = self.settings.metrics_account
        self.end_job()
        self.assertEqual(account, self.stored(self.settings, "metrics_account"))
