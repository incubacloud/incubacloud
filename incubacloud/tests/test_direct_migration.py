"""The update records the URL every installed host's agents use, and moves no one.

Without it the first reconciliation would compare an empty URL with the
one each host should have and re-apply the whole fleet, the master's and
every tenant panel's.
"""
import importlib.util
import os

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.tests._direct import make_host

_VERSION = "1.0.200"
_SCRIPT = "post-direct-path.py"
_WRITE_URL = "https://metrics.example.test/w/api/v1/write"


def _load_migration():
    """Import this version's direct-path post-migrate script as a module."""
    path = os.path.join(
        get_module_path("incubacloud"), "migrations", _VERSION, _SCRIPT,
    )
    spec = importlib.util.spec_from_file_location("ic_post_direct_path", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheUpdateMovesNoAgents(TransactionCase):

    def test_installed_hosts_keep_the_url_they_use(self):
        settings = self.env["cloud.settings"].sudo()._get_system()
        settings.write({
            "metrics_enabled": True, "metrics_remote_write_url": _WRITE_URL,
        })
        settings.metrics_account = settings.metrics_account or "acct_mig"
        prepared = {
            "metrics_agents_state": "installed",
            "known_hosts_key": "mg.example.com ssh-ed25519 AAAA",
            "traefik_deployed": True,
        }
        installed = make_host(self.env, "mg-on", "46.4.0.95", **prepared)
        archived = make_host(self.env, "mg-old", "46.4.0.96", **prepared)
        archived.active = False
        never = make_host(self.env, "mg-never", "46.4.0.97")
        self.env.flush_all()
        _load_migration().migrate(self.env.cr, "1.0.199")
        self.env.invalidate_all()
        for host in (installed, archived):
            self.assertEqual(host.metrics_agents_write_url, _WRITE_URL)
        self.assertFalse(never.metrics_agents_write_url)
        self.env["cloud.host"]._reconcile_direct_agents()
        self.assertFalse(self.env["cloud.job"].search([
            ("host_id", "=", installed.id),
            ("job_type_id.code", "=", "install_observability"),
        ]))

    def test_the_new_cron_goes_to_the_cron_bot(self):
        cron = self.env.ref("incubacloud.cron_sync_direct_path")
        bot = self.env["res.users"]._get_cron_bot()
        cron.user_id = self.env.ref("base.user_root")
        self.env.flush_all()
        _load_migration().migrate(self.env.cr, "1.0.199")
        self.env.invalidate_all()
        self.assertEqual(cron.user_id, bot)
