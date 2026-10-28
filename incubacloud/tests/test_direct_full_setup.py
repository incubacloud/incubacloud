"""Full Setup and the proxy-settings push of a host with a direct port.

Full Setup publishes the port, ships the static file with the entrypoint
and the dynamic list, and stages the DNS token as a private file: never in
the compose file, on a command line or in the job's log. It checks the
port is free before it touches the proxy, and if Docker still refuses to
start it, the proxy comes back with its previous files. The push ships the
static file and the token only: the compose file, and with it the
published port, is Full Setup's.
"""
import asyncio
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.acme_store import AcmeStorePruneMixin
from odoo.addons.incubacloud.models.full_setup_executor import (
    _DIRECT_STAGED,
    _TOKEN_STAGED,
    FullSetupExecutor,
)
from odoo.addons.incubacloud.models.push_trusted_proxies_executor import (
    PushTrustedProxiesExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport
from odoo.addons.incubacloud.tests._direct import make_host

_TOKEN = "cf-dns-token-value"


class _DirectSetupCase(TransactionCase):

    def setUp(self):
        self.registry_enter_test_mode()
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.acme_dns_cloudflare_token = _TOKEN
        self.host = make_host(self.env, "fs-direct", "46.4.0.50", direct_port=9443)

    def _executor(self, cls, code):
        """Build *cls* over a job of type *code* on the test's host."""
        job_type = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        job = self.env["cloud.job"].create({
            "host_id": self.host.id, "job_type_id": job_type.id, "name": code,
        })
        return cls(job, self.host)

    def _before(self, executor):
        """Run ``before_execute``; return ``({path: (content, mode)}, messages)``."""
        transport = MagicMock(spec=SSHTransport)
        transport.run.return_value = CommandResult("", 0)
        with patch.object(
            AcmeStorePruneMixin, "_prepare_acme_prune",
            autospec=True, return_value=None,
        ), patch.object(type(executor), "_sys", autospec=True) as sys_:
            asyncio.run(executor.before_execute(transport))
        uploaded = {}
        for call in transport.upload_text_files.await_args_list:
            for path, content in call.args[0].items():
                uploaded[path] = (content, call.kwargs.get("mode"))
        return uploaded, [c.args[1] for c in sys_.call_args_list]


class TestFullSetupWithADirectPort(_DirectSetupCase):

    def test_the_token_is_a_private_file_and_nothing_else(self):
        executor = self._executor(FullSetupExecutor, "full_setup")
        uploaded, messages = self._before(executor)
        staged = executor._job_path(_TOKEN_STAGED)
        self.assertEqual(uploaded[staged], (_TOKEN, 0o600))
        others = [c for path, (c, _m) in uploaded.items() if path != staged]
        self.assertFalse([c for c in others if _TOKEN in c])
        commands = " ".join(c[1] for c in executor.get_commands())
        self.assertNotIn(_TOKEN, commands)
        self.assertNotIn(_TOKEN, " ".join(messages))

    def test_the_list_and_the_entrypoint_are_shipped(self):
        executor = self._executor(FullSetupExecutor, "full_setup")
        uploaded, _messages = self._before(executor)
        self.assertEqual(
            uploaded[executor._job_path(_DIRECT_STAGED)][0],
            self.host._shipped_direct_yml(),
        )
        self.assertIn(
            "direct-peers@file", uploaded[executor._job_path("traefik.yml")][0],
        )
        self.assertIn(
            "0.0.0.0:9443:9443",
            uploaded[executor._job_path("inverseproxy.yaml")][0],
        )

    def test_the_port_is_checked_before_anything_moves(self):
        executor = self._executor(FullSetupExecutor, "full_setup")
        self._before(executor)
        commands = executor.get_commands()
        labels = [c[0] for c in commands]
        check = labels.index("Check the direct port")
        self.assertLess(check, labels.index("Keep the running proxy files"))
        self.assertLess(
            labels.index("Keep the running proxy files"),
            labels.index("Move traefik config files"),
        )
        self.assertIn("direct_port_check.sh 9443", commands[check][1])
        self.assertEqual(commands[check][2], {"stop_on_failure": True})
        self.assertIn(
            "proxy_start.sh start", dict(c[:2] for c in commands)["Start Traefik"],
        )

    def test_a_busy_port_fails_the_job_and_says_who_holds_it(self):
        errors = self._executor(FullSetupExecutor, "full_setup").parse_results({
            "check:os": {"stdout": "Linux", "exit_status": 0},
            "Check the direct port": {
                "stdout": "IC_DIRECT_PORT_BUSY container incubacloud-central-grafana-1",
                "exit_status": 1,
            },
        })
        self.assertEqual(len(errors), 1)
        self.assertIn("Port 9443 is already in use on fs-direct", errors[0])
        self.assertIn("incubacloud-central-grafana-1", errors[0])

    def test_success_records_the_port_it_published(self):
        executor = self._executor(FullSetupExecutor, "full_setup")
        with patch.object(
            type(self.host), "_enqueue_observability",
            autospec=True, return_value=False,
        ):
            asyncio.run(executor.on_success({}))
        self.assertEqual(self.host.direct_port_shipped, 9443)

    def test_a_port_set_to_zero_clears_the_direct_path(self):
        self.host.write({"direct_port": 0, "direct_port_shipped": 9443})
        executor = self._executor(FullSetupExecutor, "full_setup")
        uploaded, _messages = self._before(executor)
        self.assertNotIn(executor._job_path(_TOKEN_STAGED), uploaded)
        commands = dict(c[:2] for c in executor.get_commands())
        self.assertIn(
            "direct_allowlist.sh clear 9443", commands["Clear the direct path"],
        )
        self.assertIn(
            "rm -f ~/traefik/dynamic/direct.yml",
            commands["Remove the direct list for Traefik"],
        )


class TestWithoutADirectPortNothingChanges(_DirectSetupCase):

    def setUp(self):
        super().setUp()
        self.host.direct_port = 0

    def test_full_setup_uploads_the_files_it_always_did(self):
        executor = self._executor(FullSetupExecutor, "full_setup")
        uploaded, _messages = self._before(executor)
        self.assertEqual(
            {path.rsplit("/", 1)[1] for path in uploaded},
            {
                "inverseproxy.yaml", "traefik.yml", "config.yml", "tls.yml",
                "client-auth.yml", "whitelist.yaml",
            },
        )

    def test_no_port_check_and_no_list(self):
        executor = self._executor(FullSetupExecutor, "full_setup")
        self._before(executor)
        labels = [c[0] for c in executor.get_commands()]
        self.assertNotIn("Check the direct port", labels)
        self.assertNotIn("Install the direct list for Traefik", labels)
        self.assertNotIn("Clear the direct path", labels)


class TestThePushShipsTheStaticFileAndTheToken(_DirectSetupCase):

    def test_static_file_and_token_but_no_compose(self):
        executor = self._executor(PushTrustedProxiesExecutor, "push_trusted_proxies")
        uploaded, _messages = self._before(executor)
        self.assertEqual(
            uploaded[executor._job_path(_TOKEN_STAGED)], (_TOKEN, 0o600),
        )
        self.assertIn(
            "direct-peers@file", uploaded[executor._job_path("traefik.yml")][0],
        )
        self.assertNotIn(executor._job_path("inverseproxy.yaml"), uploaded)
        labels = [c[0] for c in executor.get_commands()]
        self.assertLess(
            labels.index("Install the DNS token"), labels.index("Restart Traefik"),
        )

    def test_the_push_leaves_the_published_port_alone(self):
        executor = self._executor(PushTrustedProxiesExecutor, "push_trusted_proxies")
        asyncio.run(executor.on_success({}))
        self.assertEqual(self.host.direct_port_shipped, 0)
