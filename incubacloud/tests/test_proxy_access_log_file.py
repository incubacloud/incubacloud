"""The proxy's access log moves to its own file on the host (1.0.183).

It used to go to stdout, where Docker kept 30 MB in all and the path
changed with every recreate of the container. The file is kept 30 days
and is what the web-login jail, the panel's "Recent requests" and the
log collector read. Two stored templates change together: ``traefik.yml``
names the file and the proxy's compose file mounts its directory -- a
path with no mount would be written inside the container and lost.
"""
from pathlib import Path

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase

_LEGACY_TRAEFIK = (
    "entryPoints:\n  http:\n    address: \":80\"\n\n"
    "accessLog:\n  format: json\n  bufferingSize: 100\n"
)
_LEGACY_PROXY = (
    "services:\n  proxy:\n    image: traefik:v2.11\n    volumes:\n"
    "      - ./traefik.yml:/etc/traefik/traefik.yml:ro\n"
    "  dockersocket:\n    image: tecnativa/docker-socket-proxy\n"
    "    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock:ro\n"
)


class TestAccessLogFileRetrofit(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Host = type(self.env["cloud.host"])

    def test_the_log_block_gains_the_file(self):
        out = self.Host._add_traefik_access_log_file(_LEGACY_TRAEFIK)
        self.assertIn(
            "accessLog:\n  filePath: /var/log/traefik/access.log\n  format: json",
            out,
        )
        self.assertEqual(self.Host._add_traefik_access_log_file(out), out)

    def test_a_destination_chosen_by_hand_is_kept(self):
        own = _LEGACY_TRAEFIK.replace(
            "accessLog:\n", "accessLog:\n  filePath: /srv/logs/access.log\n",
        )
        self.assertEqual(self.Host._add_traefik_access_log_file(own), own)

    def test_a_file_with_no_access_log_is_left_alone(self):
        """Adding the block itself is ``_add_traefik_access_log``'s job,
        and it already carries the file."""
        bare = "entryPoints:\n  http:\n    address: \":80\"\n"
        self.assertEqual(self.Host._add_traefik_access_log_file(bare), bare)
        added = self.Host._add_traefik_access_log(bare)
        self.assertIn("filePath: /var/log/traefik/access.log", added)

    def test_the_proxy_mounts_the_log_directory(self):
        out = self.Host._add_proxy_access_log_volume(_LEGACY_PROXY)
        self.assertIn(
            "    volumes:\n      - /var/log/traefik:/var/log/traefik\n"
            "      - ./traefik.yml:/etc/traefik/traefik.yml:ro\n",
            out,
        )
        # Only the proxy: the socket proxy's volumes are untouched.
        self.assertEqual(out.count("/var/log/traefik:/var/log/traefik"), 1)
        self.assertEqual(self.Host._add_proxy_access_log_volume(out), out)

    def test_a_proxy_without_volumes_is_left_alone(self):
        bare = "services:\n  proxy:\n    image: traefik:v2.11\n"
        self.assertEqual(self.Host._add_proxy_access_log_volume(bare), bare)

    def test_the_shipped_templates_already_carry_both(self):
        """New hosts store them from the templates; nothing to add."""
        root = Path(get_module_path("incubacloud"), "data", "traefik")
        traefik = (root / "traefik.yml").read_text(encoding="utf-8")
        proxy = (root / "inverseproxy.yaml").read_text(encoding="utf-8")
        self.assertEqual(self.Host._add_traefik_access_log_file(traefik), traefik)
        self.assertEqual(self.Host._add_proxy_access_log_volume(proxy), proxy)
        self.assertIn("filePath: /var/log/traefik/access.log", traefik)
        self.assertIn("/var/log/traefik:/var/log/traefik", proxy)

    def test_an_upgrade_moves_a_stored_host_to_the_file(self):
        """Both stored templates change together, or neither does."""
        host = self.env["cloud.host"].create({
            "name": "accesslog-retrofit",
            "ip_address": "192.0.2.41",
            "user": "root",
            "wildcard_domain": "accesslog.example.com",
            "traefik_yml": _LEGACY_TRAEFIK,
            "traefik_inverseproxy_yaml": _LEGACY_PROXY,
        })
        self.env["cloud.host"].init_traefik_templates()
        self.assertIn("filePath: /var/log/traefik/access.log", host.traefik_yml)
        self.assertIn(
            "/var/log/traefik:/var/log/traefik", host.traefik_inverseproxy_yaml,
        )
