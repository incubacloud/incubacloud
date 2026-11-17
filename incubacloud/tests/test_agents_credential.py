"""The agents' write-only user, the panel's addresses and its uid (plan #84, C1).

The agents write with ``<account>-agent``, which reaches ``/w/`` and
``/lw/`` alone, once the central knows it; until then, and as the way
back, with the account's credential (A2). The read credential is rotated
only once every server writes with the agents' user. The panel learns its
own addresses from what its jobs read on the servers, never from a notice,
and forgets them after 30 days or when the alert of an address change
closes (A1).
"""
import asyncio
import json
import os
import re
from datetime import timedelta
from unittest.mock import MagicMock, patch

import yaml

from odoo import fields
from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.cloud_settings_agents import credential_digest
from odoo.addons.incubacloud.models.host_hardening_executor import (
    HostHardeningExecutor,
)
from odoo.addons.incubacloud.models.observability_agents_executor import (
    AGENTS_REVISION,
    ObservabilityAgentsExecutor,
)
from odoo.addons.incubacloud.models.observability_central_executor import (
    _ACCOUNT_LOG_READ_PATHS,
    _ACCOUNT_LOG_WRITE_PATH,
    _ACCOUNT_READ_PATHS,
    _ACCOUNT_WRITE_PATH,
    CENTRAL_REVISION,
    ObservabilityCentralExecutor,
)
from odoo.addons.incubacloud.tests import _agents_playbook as playbook
from odoo.addons.incubacloud.tests.test_observability_executors import (
    ObservabilityExecutorCase,
)

ACCOUNT = "acct_test01"
AGENT = f"{ACCOUNT}-agent"


class AgentsCase(ObservabilityExecutorCase):
    """The executors' case with core's own answer on where credentials come from."""

    def setUp(self):
        """Core mints its own credentials, whatever layer is installed."""
        super().setUp()
        patcher = patch.object(
            type(self.settings), "_metrics_credentials_injected",
            autospec=True, return_value=False,
        )
        self.injected = patcher.start()
        self.addCleanup(patcher.stop)

    def _vmauth(self, cls=ObservabilityCentralExecutor):
        """Return the parsed vmauth list a run of *cls* would ship."""
        extra = self._make(cls).get_extra_vars()
        return yaml.safe_load(extra["ic_vmauth_config"])

    def _users(self, cfg, username):
        """Return every vmauth user called *username*."""
        return [u for u in cfg["users"] if u["username"] == username]

    def _paths(self, user):
        """Return every ``src_paths`` of *user*."""
        return [p for entry in user["url_map"] for p in entry["src_paths"]]

    def _agents_in_force(self, in_force=True, token="agent-secret"):
        """Record a list on the central with or without the agents' user, as a sync does.

        The users and the digests of their credentials together, so the
        state holds whether "in force" is asked by the user's name or by
        its credential's digest.

        :param bool in_force: whether the list carries the agents' user.
        :param token: the agents' credential this panel holds, ``False``
            for none; the list carries it only while *in_force*.
        """
        users = [ACCOUNT]
        digests = [credential_digest(ACCOUNT, "shared-secret")]
        if in_force:
            users.append(AGENT)
            digests.append(credential_digest(AGENT, token))
        self.settings.write({
            "metrics_agent_token": token,
            "metrics_accounts_deployed": "\n".join(users),
            "metrics_credentials_deployed": "\n".join(sorted(digests)),
        })

    def _install(self, **vals):
        """Mark this test's host as installed by the agents, with *vals*."""
        self.host.sudo().write({
            "metrics_agents_state": "installed",
            "metrics_agents_revision": 4,
            "known_hosts_key": "ssh-ed25519 AAAA",
            "traefik_deployed": True,
        } | vals)


class TestTheAgentsUser(AgentsCase):
    """vmauth's second user per account, and who gets which credential."""

    def test_vmauth_agent_user_writes_only(self):
        """``/r/`` and ``/lr/`` are not in its map; writes carry the account."""
        self.settings.metrics_agent_token = "agent-secret"
        cfg = self._vmauth()
        (agent,) = self._users(cfg, AGENT)
        self.assertEqual(agent["password"], "agent-secret")
        self.assertEqual(
            sorted(self._paths(agent)),
            sorted([_ACCOUNT_WRITE_PATH, _ACCOUNT_LOG_WRITE_PATH]),
        )
        for path in self._paths(agent):
            self.assertFalse(path.startswith(("/r/", "/lr/")), path)
        write = self._route(agent, _ACCOUNT_WRITE_PATH)
        self.assertIn(f"extra_label=ic_account={ACCOUNT}", write["url_prefix"])
        logs = self._route(agent, _ACCOUNT_LOG_WRITE_PATH)
        self.assertIn(f"X-Scope-OrgID: {ACCOUNT}", logs["headers"])
        self.assertNotIn("max_concurrent_requests", agent)
        # The account's own user keeps both until its credential rotates.
        (account,) = self._users(cfg, ACCOUNT)
        self.assertIn(_ACCOUNT_WRITE_PATH, self._paths(account))
        self.assertIn(_ACCOUNT_READ_PATHS, self._paths(account))
        self.assertNotIn("max_concurrent_requests", account)

    def test_the_deployment_mints_the_agents_user(self):
        """A central deployed before it gets one with its next deployment."""
        self.settings.metrics_agent_token = False
        cfg = self._vmauth()
        (agent,) = self._users(cfg, AGENT)
        self.assertTrue(agent["password"])
        self.assertEqual(self.settings.metrics_agent_token, agent["password"])
        self.assertNotEqual(agent["password"], "shared-secret")

    def test_agents_get_agent_token_when_central_has_it(self):
        self._agents_in_force()
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertEqual(extra["ic_remote_write_user"], AGENT)
        self.assertEqual(extra["ic_remote_write_token"], "agent-secret")
        self.assertEqual(extra["ic_write_user_kind"], "agent")
        self.assertEqual(extra["ic_account"], ACCOUNT)

    def test_agents_fall_back_to_account_token(self):
        """Minted but not on the central yet, or not minted: the account's (A2)."""
        for token in ("agent-secret", False):
            with self.subTest(token=token):
                self._agents_in_force(False, token)
                extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
                self.assertEqual(extra["ic_remote_write_user"], ACCOUNT)
                self.assertEqual(extra["ic_remote_write_token"], "shared-secret")
                self.assertEqual(extra["ic_write_user_kind"], "account")

    def test_a_layer_that_hands_it_over_is_trusted(self):
        """Handed down only once the layer's central knows it."""
        self.injected.return_value = True
        self.settings.write({
            "metrics_agent_token": "agent-secret",
            "metrics_accounts_deployed": False,
        })
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertEqual(extra["ic_remote_write_user"], AGENT)


class TestTheAgentsAreAppliedAgain(AgentsCase):
    """No server stays on the account's credential without anyone seeing it."""

    def test_agent_token_write_reapplies_agents(self):
        """Handed down by a layer: writing it re-applies every server."""
        self._install()
        self.injected.return_value = True
        self.settings.write({"metrics_agent_token": "agent-secret"})
        self.assertEqual(self.host.metrics_agents_revision, 0)

    def test_minted_here_it_waits_for_the_central(self):
        """Minted before the central knows it: applied when the list carries it."""
        self._install()
        self.settings.write({"metrics_agent_token": "agent-secret"})
        self.assertEqual(self.host.metrics_agents_revision, 4)
        self.settings._metrics_record_deployed(self.settings._vmauth_users())
        self.assertEqual(self.host.metrics_agents_revision, 0)
        self.assertIn(AGENT, self.settings.metrics_accounts_deployed.split())
        # One digest per credential: the account's and the agents'.
        digests = self.settings.metrics_credentials_deployed.split()
        self.assertEqual(len(digests), 2)
        self.assertNotIn("agent-secret", self.settings.metrics_credentials_deployed)

    def _succeed(self, **facts):
        executor = self._make(ObservabilityAgentsExecutor)
        executor.get_extra_vars()
        executor._facts = {"ic_agents_running": "3", "ic_instances_labelled": "0"} | facts
        asyncio.run(executor.on_success({}))
        return executor

    def test_write_user_installed_recorded(self):
        self._agents_in_force()
        self._succeed(ic_write_user_installed="agent")
        self.assertEqual(self.host.metrics_write_user, "agent")
        # A playbook from before says nothing: what was sent.
        self._agents_in_force(False)
        self._succeed()
        self.assertEqual(self.host.metrics_write_user, "account")

    def test_the_credential_moving_during_the_install_records_no_revision(self):
        """The agents' user came into force while the install ran."""
        self.settings.write({"metrics_agent_token": "agent-secret"})
        executor = self._make(ObservabilityAgentsExecutor)
        executor.get_extra_vars()
        self._agents_in_force()
        executor._facts = {"ic_agents_running": "3", "ic_write_user_installed": "account"}
        asyncio.run(executor.on_success({}))
        self.assertEqual(self.host.metrics_agents_revision, 0)


class TestTheAgentsUserIsAppliedByState(AgentsCase):
    """A server left out of the moment the agents' user came into force.

    The reconciliation re-applies, by state, every installed server whose
    agents still write with the account's credential while the agents'
    user is in force, whatever its revision (review of 14-nov-2026, M1).
    """

    def setUp(self):
        """The agents' user in force; the other servers of the database on it."""
        super().setUp()
        self._agents_in_force()
        # A copy of production holds servers of its own: on the current
        # revision and the agents' user, they stay out of the counts.
        self.env["cloud.host"].sudo().search([("id", "!=", self.host.id)]).write({
            "metrics_agents_revision": AGENTS_REVISION, "metrics_write_user": "agent",
        })

    def _reconcile(self):
        """Run the reconciliation's revision pass and return how many it queued."""
        return self.env["cloud.host"]._reconcile_agents_revision()

    def _queued(self):
        """Return how many agents' installs wait on this test's host."""
        return self.env["cloud.job"].search_count([
            ("host_id", "=", self.host.id),
            ("job_type_id.code", "=", "install_observability"),
        ])

    def test_a_retry_on_the_account_credential_is_applied_again(self):
        """A failed server retried on the old credential as the new came in."""
        self._install(metrics_agents_state="failed", metrics_agents_attempts=1)
        self._agents_in_force(False)
        executor = self._make(ObservabilityAgentsExecutor)
        executor.get_extra_vars()
        self._agents_in_force()
        executor._facts = {"ic_agents_running": "3", "ic_write_user_installed": "account"}
        asyncio.run(executor.on_success({}))
        self.assertEqual(self.host.metrics_agents_state, "installed")
        self.assertEqual(self.host.metrics_agents_revision, AGENTS_REVISION)
        self.assertEqual(self.host.metrics_write_user, "account")
        self.assertEqual(self._reconcile(), 1)
        self.assertEqual(self._queued(), 1)

    def test_a_resumed_job_on_the_account_credential_records_no_revision(self):
        """Resumed by a worker that did not render it: the playbook's word."""
        self._install(metrics_agents_revision=0, metrics_write_user="account")
        executor = self._make(ObservabilityAgentsExecutor)
        executor._facts = {"ic_agents_running": "3", "ic_write_user_installed": "account"}
        asyncio.run(executor.on_success({}))
        self.assertEqual(self.host.metrics_agents_revision, 0)
        self.assertEqual(self._reconcile(), 1)
        # Installed with the agents' user, it records the revision.
        executor = self._make(ObservabilityAgentsExecutor)
        executor._facts = {"ic_agents_running": "3", "ic_write_user_installed": "agent"}
        asyncio.run(executor.on_success({}))
        self.assertEqual(self.host.metrics_agents_revision, AGENTS_REVISION)
        self.assertEqual(self.host.metrics_write_user, "agent")

    def test_a_layer_that_starts_handing_it_over_later(self):
        """Handed down before the layer said so: no moment, yet it converges."""
        self._agents_in_force(False, False)
        self._install(metrics_write_user="account")
        self.settings.write({"metrics_agent_token": "agent-secret"})
        self.assertEqual(self.host.metrics_agents_revision, AGENTS_REVISION)
        self.assertEqual(self._reconcile(), 0)
        # The layer is updated and now says it hands it over.
        self.injected.return_value = True
        self.assertEqual(self._reconcile(), 1)
        self.assertEqual(self._queued(), 1)

    def test_a_host_brought_back_from_the_archive(self):
        """Archived when the agents' user came into force, then restored."""
        self._agents_in_force(False)
        self._install(metrics_write_user="account", active=False)
        self.settings._metrics_record_deployed(self.settings._vmauth_users())
        self.assertTrue(self.settings._metrics_agent_in_force())
        self.assertEqual(self.host.metrics_agents_revision, AGENTS_REVISION)
        self.host.active = True
        self.assertEqual(self._reconcile(), 1)
        self.assertEqual(self._queued(), 1)

    def test_a_host_on_the_agents_user_is_left_alone(self):
        self._install(metrics_write_user="agent")
        self.assertEqual(self._reconcile(), 0)
        self.assertEqual(self._queued(), 0)

    def test_a_token_minted_again_waits_for_the_sync(self):
        """The user's name still on the list, its new token not (B1, 14-nov-2026).

        The agents keep the account's credential, and nothing is applied
        again, until a sync records the new token's digest; then every
        server moves to it.
        """
        self._install(metrics_write_user="account")
        self.settings.metrics_agent_token = "agent-secret-2"
        self.assertIn(AGENT, self.settings.metrics_accounts_deployed.split())
        self.assertFalse(self.settings._metrics_agent_in_force())
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertEqual(extra["ic_remote_write_user"], ACCOUNT)
        self.assertEqual(extra["ic_remote_write_token"], "shared-secret")
        self.assertEqual(extra["ic_write_user_kind"], "account")
        self.assertEqual(self.host.metrics_agents_revision, AGENTS_REVISION)
        self.assertEqual(self._reconcile(), 0)
        self.assertEqual(self._queued(), 0)
        # The sync puts the new token on the central.
        self.settings._metrics_record_deployed(self.settings._vmauth_users())
        self.assertTrue(self.settings._metrics_agent_in_force())
        self.assertEqual(self.host.metrics_agents_revision, 0)
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertEqual(extra["ic_remote_write_user"], AGENT)
        self.assertEqual(extra["ic_remote_write_token"], "agent-secret-2")
        self.assertEqual(extra["ic_write_user_kind"], "agent")

    def test_the_account_credential_is_right_until_it_is_in_force(self):
        """The way back (A2): nothing to re-apply while the central lacks it."""
        self._agents_in_force(False)
        self._install(metrics_write_user="account")
        self.assertEqual(self._reconcile(), 0)
        self.assertEqual(self._queued(), 0)


class TestTheReadCredentialRotates(AgentsCase):
    """Rotated by state: once no server holds it (A2, point 3)."""

    def setUp(self):
        """A second server, and the agents' user on the central."""
        super().setUp()
        self.other = self.env["cloud.host"].create({
            "name": "HY", "ip_address": "10.0.0.41", "user": "root",
            "wildcard_domain": "hy.example.com",
        })
        self._agents_in_force()
        self.settings.metrics_central_host_id = self.host.id

    def test_rotation_waits_for_every_host(self):
        """A server on revision 3, or with the account's credential, holds it."""
        Settings = self.env["cloud.settings"].sudo()
        self._install(metrics_write_user="agent")
        self.other.sudo().write({
            "metrics_agents_state": "installed", "metrics_agents_revision": 3,
            "metrics_write_user": False,
        })
        self.assertFalse(Settings._metrics_rotation_due())
        self.other.sudo().metrics_write_user = "account"
        self.assertFalse(Settings._metrics_rotation_due())
        self.other.sudo().metrics_write_user = "agent"
        self.assertTrue(Settings._metrics_rotation_due())
        # A server whose install failed counts, one never installed does not.
        self.other.sudo().write({"metrics_agents_state": "failed", "metrics_write_user": False})
        self.assertFalse(Settings._metrics_rotation_due())
        self.other.sudo().metrics_agents_state = "never"
        self.assertTrue(Settings._metrics_rotation_due())
        # Nor before the agents' user is in force.
        self._agents_in_force(False)
        self.assertFalse(Settings._metrics_rotation_due())

    def test_a_panel_handed_its_credentials_never_rotates_them(self):
        self._install(metrics_write_user="agent")
        self.injected.return_value = True
        self.assertFalse(self.env["cloud.settings"].sudo()._metrics_rotation_due())

    def test_the_new_credential_joins_the_old_one_before_the_panel_moves(self):
        """Two passwords for one user: no moment the panel reads with neither."""
        self._install(metrics_write_user="agent")
        Settings = self.env["cloud.settings"].sudo()
        with patch.object(
            type(self.env["cloud.job"]), "enqueue", autospec=True, return_value=99,
        ) as enqueue:
            self.assertEqual(Settings._reconcile_metrics_credentials(), 99)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.args[3], "sync_metrics_accounts")
        nxt = self.settings.metrics_read_token_next
        self.assertTrue(nxt)
        self.assertEqual(self.settings.metrics_remote_write_token, "shared-secret")
        cfg = self._vmauth()
        old, new = sorted(self._users(cfg, ACCOUNT), key=lambda u: u["password"] != "shared-secret")
        self.assertEqual(old["password"], "shared-secret")
        self.assertIn(_ACCOUNT_WRITE_PATH, self._paths(old))
        self.assertEqual(new["password"], nxt)
        self.assertEqual(sorted(self._paths(new)), sorted([_ACCOUNT_READ_PATHS, _ACCOUNT_LOG_READ_PATHS]))
        self.assertGreater(new["max_concurrent_requests"], 0)

    def test_the_panel_moves_once_the_central_carries_it(self):
        self._install(metrics_write_user="agent")
        self.settings.metrics_read_token_next = "next-secret"
        self.settings._metrics_record_deployed(self.settings._vmauth_users())
        self.assertEqual(self.settings.metrics_remote_write_token, "next-secret")
        self.assertFalse(self.settings.metrics_read_token_next)
        self.assertTrue(self.settings.metrics_read_rotated)
        # The next list drops the old password, and the account only reads.
        cfg = self._vmauth()
        (account,) = self._users(cfg, ACCOUNT)
        self.assertEqual(account["password"], "next-secret")
        self.assertNotIn(_ACCOUNT_WRITE_PATH, self._paths(account))
        self.assertNotIn(_ACCOUNT_LOG_WRITE_PATH, self._paths(account))
        self.assertGreater(account["max_concurrent_requests"], 0)
        (agent,) = self._users(cfg, AGENT)
        self.assertIn(_ACCOUNT_WRITE_PATH, self._paths(agent))

    def test_a_list_without_it_moves_nothing(self):
        self.settings.metrics_read_token_next = "next-secret"
        users = [u for u in self.settings._vmauth_users() if u["password"] != "next-secret"]
        self.settings._metrics_record_deployed(users)
        self.assertEqual(self.settings.metrics_remote_write_token, "shared-secret")
        self.assertEqual(self.settings.metrics_read_token_next, "next-secret")

    def test_a_list_in_force_queues_no_sync(self):
        # A server still holds the account's credential: no rotation yet.
        self._install(metrics_write_user="account")
        self.settings._metrics_record_deployed(self.settings._vmauth_users())
        with patch.object(type(self.env["cloud.job"]), "enqueue", autospec=True) as enqueue:
            self.assertFalse(
                self.env["cloud.settings"].sudo()._reconcile_metrics_credentials(),
            )
        enqueue.assert_not_called()


class TestReadEndpointsNarrowed(AgentsCase):
    """``/r/`` and ``/lr/`` serve ``query`` and ``query_range`` alone (C1.6)."""

    def test_read_endpoints_narrowed(self):
        cfg = self._vmauth()
        (account,) = self._users(cfg, ACCOUNT)
        patterns = self._paths(account)
        for path in (
            "/r/api/v1/query", "/r/api/v1/query_range",
            "/lr/loki/api/v1/query", "/lr/loki/api/v1/query_range",
        ):
            self.assertTrue(any(re.fullmatch(p, path) for p in patterns), path)
        for path in (
            "/r/api/v1/series", "/r/api/v1/labels",
            "/r/api/v1/label/__name__/values", "/r/api/v1/metadata",
            "/r/api/v1/query_exemplars", "/r/api/v1/status/buildinfo",
            "/lr/loki/api/v1/series", "/lr/loki/api/v1/labels",
            "/lr/loki/api/v1/label/job/values", "/lr/loki/api/v1/index/stats",
        ):
            self.assertFalse(any(re.fullmatch(p, path) for p in patterns), path)


class TestLokiLimits(TransactionCase):
    """Loki's series per query, and a configuration that applies (C1.7)."""

    def _central_tasks(self):
        path = os.path.join(
            get_module_path("incubacloud"), "ansible", "playbooks",
            "observability_central.yml",
        )
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh)[0]

    def test_max_query_series_is_set(self):
        play = self._central_tasks()
        task = next(t for t in play["tasks"] if t.get("name") == "Write the log store configuration")
        content = playbook.environment().from_string(
            task["ansible.builtin.copy"]["content"],
        ).render(**{
            **play["vars"],
            "ic_log_retention_days": 30, "ic_security_log_retention_days": 90,
        })
        limits = yaml.safe_load(content)["limits_config"]
        self.assertEqual(limits["max_query_series"], play["vars"]["ic_loki_max_query_series"])
        self.assertGreater(limits["max_query_series"], 500)

    def test_a_week_is_split_by_day_with_room_in_the_queue(self):
        """A 7-day read is a few subqueries, and the queue holds a board's.

        Loki 2.9 splits a query every 30 minutes and queues 100 of them
        per account: one 7-day read alone was refused with 429 (QA of
        15-nov-2026).
        """
        play = self._central_tasks()
        task = next(t for t in play["tasks"] if t.get("name") == "Write the log store configuration")
        content = yaml.safe_load(playbook.environment().from_string(
            task["ansible.builtin.copy"]["content"],
        ).render(**play["vars"]))
        self.assertEqual(content["limits_config"]["split_queries_by_interval"], "24h")
        queued = content["query_scheduler"]["max_outstanding_requests_per_tenant"]
        self.assertGreaterEqual(queued, 2048)

    def test_a_new_configuration_restarts_loki(self):
        play = self._central_tasks()
        names = [t.get("name") for t in play["tasks"]]
        restart = play["tasks"][names.index("Restart Loki when its configuration changed")]
        self.assertEqual(restart["when"], "ic_loki_cfg is changed")
        self.assertGreater(
            names.index("Restart Loki when its configuration changed"),
            names.index("Bring the central stack up"),
        )

    def test_the_central_is_deployed_again(self):
        self.assertGreaterEqual(CENTRAL_REVISION, 3)


class TestThePanelsAddresses(AgentsCase):
    """Learned on the servers, forgotten after 30 days or with the change (A1)."""

    def _sources(self, host=None):
        return [item["address"] for item in (host or self.host)._panel_sources_kept()]

    def test_panel_source_recorded_from_facts(self):
        executor = self._make(ObservabilityAgentsExecutor)
        executor.get_extra_vars()
        executor._facts = {
            "ic_agents_running": "3",
            "ic_panel_source": "198.51.100.7",
            "ic_panel_uid": "1000",
        }
        asyncio.run(executor.on_success({}))
        self.assertTrue(self.host._panel_source_known("198.51.100.7"))
        self.assertFalse(self.host._panel_source_known("198.51.100.8"))

    def test_panel_source_recorded_from_the_hardening(self):
        executor = object.__new__(HostHardeningExecutor)
        executor.job = MagicMock(spec=type(self.env["cloud.job"]))
        executor.job.host_id = self.host
        executor.env = self.env
        executor._log_buffer = []
        executor._facts = {"ic_panel_source": "203.0.113.20"}
        with patch.object(HostHardeningExecutor, "_sys", autospec=True), \
                patch.object(HostHardeningExecutor, "_resolve_alert", autospec=True), \
                patch.object(HostHardeningExecutor, "_alert", autospec=True), \
                patch.object(
                    HostHardeningExecutor, "_open_edge_firewall_port",
                    autospec=True, return_value=None,
                ), \
                patch.object(
                    HostHardeningExecutor, "_finalize_disable_root",
                    autospec=True, return_value=True,
                ):
            asyncio.run(executor.on_success({}))
        self.assertEqual(self._sources(), ["203.0.113.20"])

    def test_the_playbooks_read_it_without_become(self):
        """sudo clears SSH_CONNECTION: both reads run as the connecting user.

        The hardening's recipe does not hand it to the panel yet (review
        of 14-nov-2026, M4): its executor records it once it does.
        """
        agents = playbook.task("Read the address the panel connects from")
        self.assertIs(agents["become"], False)
        self.assertEqual(agents["ansible.builtin.command"], "printenv SSH_CONNECTION")
        path = os.path.join(
            get_module_path("incubacloud"), "ansible", "playbooks", "host_hardening.yml",
        )
        with open(path, encoding="utf-8") as fh:
            tasks = yaml.safe_load(fh)[0]["tasks"]
        read = next(
            t for t in tasks if t.get("name") == "Determine the address this play is connecting from"
        )
        self.assertIs(read["become"], False)

    def test_panel_source_expires_after_30_days(self):
        self.host._record_panel_source("198.51.100.7")
        old = fields.Datetime.now() - timedelta(days=31)
        self.host.sudo().security_panel_sources = json.dumps([
            {"address": "198.51.100.7", "seen": fields.Datetime.to_string(old)},
            {"address": "198.51.100.9", "seen": fields.Datetime.to_string(fields.Datetime.now())},
        ])
        self.assertFalse(self.host._panel_source_known("198.51.100.7"))
        self.assertTrue(self.host._panel_source_known("198.51.100.9"))

    def test_five_are_kept_the_newest(self):
        for n in range(7):
            self.host._record_panel_source(f"198.51.100.{n}")
        self.assertEqual(len(self._sources()), 5)
        self.assertTrue(self.host._panel_source_known("198.51.100.6"))
        self.assertFalse(self.host._panel_source_known("198.51.100.0"))

    def test_panel_source_ipv6_by_64(self):
        self.host._record_panel_source("2001:db8:1:2::10")
        self.assertTrue(self.host._panel_source_known("2001:db8:1:2:ffff::1"))
        self.assertFalse(self.host._panel_source_known("2001:db8:1:3::10"))
        # Another address of the same /64 replaces it, not a new slot.
        self.host._record_panel_source("2001:db8:1:2::11")
        self.assertEqual(self._sources(), ["2001:db8:1:2::11"])

    def test_what_is_not_an_address_is_not_recorded(self):
        for value in ("", "not-an-ip", None, "999.1.1.1"):
            self.assertFalse(self.host._record_panel_source(value))
        self.assertEqual(self._sources(), [])

    def test_panel_address_change_drops_old_source(self):
        """When the change's alert closes, the address left is forgotten.

        With no hardened server at risk it closes at once.
        """
        self.host._record_panel_source("198.51.100.7")
        self.host._record_panel_source("198.51.100.9")
        self.assertTrue(
            self.env["cloud.host"]._panel_address_changed("198.51.100.7", "203.0.113.50"),
        )
        self.assertEqual(self._sources(), ["198.51.100.9"])

    def test_the_notice_teaches_no_address(self):
        self.env["cloud.host"]._panel_address_changed("198.51.100.7", "203.0.113.50")
        self.assertFalse(self.host._panel_source_known("203.0.113.50"))

    def test_the_addresses_are_for_host_managers_only(self):
        field = self.env["cloud.host"]._fields["security_panel_sources"]
        self.assertEqual(field.groups, "incubacloud.group_cloud_manager")

    def test_panel_uid_recorded(self):
        executor = self._make(ObservabilityAgentsExecutor)
        executor.get_extra_vars()
        executor._facts = {"ic_agents_running": "3", "ic_panel_uid": "1001"}
        asyncio.run(executor.on_success({}))
        self.assertEqual(self.host.security_panel_uid, "1001")
        executor._facts = {"ic_agents_running": "3", "ic_panel_uid": "x; rm"}
        asyncio.run(executor.on_success({}))
        self.assertEqual(self.host.security_panel_uid, "1001")
        export = playbook.task("Export the agent facts")["ansible.builtin.set_stats"]["data"]
        self.assertIn("ansible_user_uid", export["ic_panel_uid"])


class TestThePanelLua(TransactionCase):
    """What the classification learns of the instances (C1.9)."""

    def test_panel_lua_lists_instance_prefixes(self):
        """A prefix per instance and the exact names a layer adds, as data."""
        lua = playbook.fluent_bit_files(ic_instances=[
            {"instance_id": "42", "traefik_prefix": "acme-19-0-", "traefik_extra": []},
            {"instance_id": "45", "traefik_prefix": "x-19-0-",
             "traefik_extra": ["shop-svc@file"]},
        ])["panel.lua"]
        self.assertIn('{id = "42", prefix = "acme-19-0-", extra = {', lua)
        self.assertIn('["shop-svc@file"] = true,', lua)
        self.assertIn('host_id = "7",', lua)
        self.assertIn("weblogin_per_minute = 600,", lua)
