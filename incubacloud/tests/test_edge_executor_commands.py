"""Command shapes for the two jobs that change what the edge does.

Both are convergence tools: re-running one must leave the host matching
what the panel currently says, including when the answer is "carry
nothing". The removal half is the one worth pinning — a stale allowlist
rejects deliveries silently, where no allowlist merely leaves them
unfiltered, so removal has to happen whether or not anything replaces it.
"""
from odoo.tests.common import TransactionCase

from ..github.edge import EDGE_CONFIG_FILENAME
from ..models.github_webhook_edge import RANGES_PARAM
from ..models.github_webhook_edge_executor import (
    PushGitHubWebhookEdgeExecutor,
)
from ..models.push_trusted_proxies_executor import PushTrustedProxiesExecutor

RANGES = ["192.30.252.0/22"]
EDGE_PROXY = ["198.51.100.0/24"]


class EdgeExecutorCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "edge-exec-host",
            "ip_address": "10.0.0.11",
            "user": "ubuntu",
            "wildcard_domain": "edge-exec.example.com",
        })

    def _executor(self, cls, code):
        """Build *cls* against a job of type *code* for the fixture host."""
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", code)], limit=1,
        )
        self.assertTrue(job_type, f"job type {code} must be declared")
        job = self.env["cloud.job"].create({
            "host_id": self.host.id,
            "job_type_id": job_type.id,
            "name": code,
        })
        return cls(job, self.host)

    def _commands(self, cls, code):
        return dict(self._executor(cls, code).get_commands())


class TestWebhookEdgeCommands(EdgeExecutorCase):

    def _cmds(self):
        return self._commands(
            PushGitHubWebhookEdgeExecutor, "push_github_webhook_edge",
        )

    def test_a_host_with_nothing_to_publish_still_removes(self):
        commands = self._cmds()
        self.assertEqual(
            list(commands), ["Remove stale GitHub webhook allowlist"],
        )
        self.assertIn(EDGE_CONFIG_FILENAME, commands[
            "Remove stale GitHub webhook allowlist"
        ])

    def test_removal_runs_before_installation(self):
        self._publish_something()
        labels = list(self._cmds())
        self.assertEqual(labels[0], "Remove stale GitHub webhook allowlist")
        self.assertEqual(labels[1], "Install GitHub webhook allowlist")

    def test_the_document_lands_in_the_watched_directory(self):
        self._publish_something()
        install = self._cmds()["Install GitHub webhook allowlist"]
        self.assertIn(f"~/traefik/dynamic/{EDGE_CONFIG_FILENAME}", install)
        # Moved into place, never written there directly: Traefik would
        # parse a half-written document and drop the routers.
        self.assertIn("mv ", install)

    def _publish_something(self):
        """Give the host one deployed instance and a source range."""
        settings = self.env["cloud.settings"].sudo()._get_system()
        settings.github_webhook_allowlist = True
        self.env["ir.config_parameter"].sudo().set_param(
            RANGES_PARAM, "\n".join(RANGES),
        )
        project = self.env["cloud.project"].create({"name": "Edge Exec Proj"})
        instance = self.env["cloud.instance"].create({
            "name": "exec",
            "project_id": project.id,
            "environment": "production",
            "host_id": self.host.id,
            "odoo_version": "19.0",
        })
        instance._transition("deployed")
        self.assertTrue(self.host._github_webhook_document())


class TestTrustedProxyCommands(EdgeExecutorCase):

    def _cmds(self):
        return self._commands(
            PushTrustedProxiesExecutor, "push_trusted_proxies",
        )

    def test_both_documents_are_installed_together(self):
        # A host whose entrypoint names a middleware its file provider
        # does not define answers 500 on every router it serves.
        move = self._cmds()["Move Traefik configuration"]
        self.assertIn("~/traefik/traefik.yml", move)
        self.assertIn("~/traefik/config.yml", move)

    def test_the_dynamic_copy_is_refreshed(self):
        # The file provider reads config.yml from the dynamic directory
        # on a host that has been through full setup.
        refresh = self._cmds()["Refresh the dynamic configuration"]
        self.assertIn("~/traefik/dynamic/config.yml", refresh)

    def test_the_proxy_is_restarted(self):
        # trustedIPs is static configuration: Traefik reads it once at
        # start-up, so a file drop alone would change nothing.
        restart = self._cmds()["Restart Traefik"]
        self.assertIn("-p inverseproxy", restart)
        self.assertIn("restart proxy", restart)

    # ── The firewall has to believe the same list as the proxy ───────
    #
    # The proxy gets its list from this job. The firewall got its from
    # the hardening playbook, which runs on Ansible at host creation and
    # then effectively never — so a range the CDN published afterwards
    # was accepted by the proxy and dropped before reaching it, and the
    # daily job that noticed had nothing it could update.

    _FIREWALL = "Refresh the firewall allowlist"

    def _filtering_host(self, ranges="203.0.113.0/24\n2001:db8::/32"):
        self.host.write({
            "behind_cdn": True,
            "block_direct_access": True,
            "trusted_proxy_ranges": ranges,
        })

    def test_a_host_that_filters_nothing_is_left_alone(self):
        self.assertNotIn(self._FIREWALL, self._cmds())

    def test_declaring_ranges_without_refusing_direct_access_does_not(self):
        """The firewall half only exists once the proxy refuses too."""
        self.host.write({
            "behind_cdn": True,
            "trusted_proxy_ranges": "203.0.113.0/24",
        })
        self.assertNotIn(self._FIREWALL, self._cmds())

    def test_a_filtering_host_gets_todays_ranges(self):
        self._filtering_host()
        command = self._cmds()[self._FIREWALL]
        self.assertIn("203.0.113.0/24", command)
        self.assertIn("2001:db8::/32", command)

    def test_the_two_families_go_to_their_own_sets(self):
        """nftables cannot hold both in one set."""
        self._filtering_host()
        command = self._cmds()[self._FIREWALL]
        self.assertIn("ic_cdn_v4 { 203.0.113.0/24 }", command)
        self.assertIn("ic_cdn_v6 { 2001:db8::/32 }", command)

    def test_it_is_applied_as_one_transaction(self):
        """Flushing and refilling in two commands leaves a window in
        which the set is empty and the rule matches nobody."""
        self._filtering_host()
        command = self._cmds()[self._FIREWALL]
        self.assertEqual(command.count("nft -f -"), 1)
        self.assertNotIn("nft flush set", command)

    def test_it_does_nothing_on_a_host_without_the_set(self):
        """One hardened before the sets existed. Its inlined ranges keep
        working until the next hardening run replaces them."""
        self._filtering_host()
        command = self._cmds()[self._FIREWALL]
        self.assertIn("nft list set inet filter ic_cdn_v4", command)
        self.assertIn("else echo", command)

    def test_a_host_with_no_ipv4_range_is_left_alone(self):
        """What the playbook treats as "no allowlist at all". Flushing
        to empty would take the host off the network."""
        self._filtering_host(ranges="2001:db8::/32")
        self.assertNotIn(self._FIREWALL, self._cmds())

    def test_the_firewall_is_refreshed_after_the_proxy_restarts(self):
        """Order matters only in that neither half may be skipped; the
        proxy owns the connection, so it goes first."""
        self._filtering_host()
        labels = list(self._cmds())
        self.assertLess(
            labels.index("Restart Traefik"), labels.index(self._FIREWALL),
        )
