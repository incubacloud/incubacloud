"""The update to 1.0.214 queues the edge guard on every host already set up.

Never a Full Setup (7-nov-2026): ``push_edge_guard`` once per active
host with the panel's proxy, nothing on an archived host or one whose proxy
the panel never set up, and nothing on a fresh install.
"""
import importlib.util
import os

from odoo.modules.module import get_module_path
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.tests._direct import make_host

_VERSION = "1.0.214"
_SCRIPT = "end-migrate.py"


def _load_migration(version=_VERSION):
    """Import a version's end-migrate script as a module.

    :param str version: the migration's folder, 1.0.214 by default.
    :return: the imported module.
    """
    path = os.path.join(
        get_module_path("incubacloud"), "migrations", version, _SCRIPT,
    )
    spec = importlib.util.spec_from_file_location(
        f"ic_end_edge_guard_{version.replace('.', '_')}", path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheUpdateQueuesTheGuard(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ready = make_host(self.env, "egm-on", "46.4.0.70", traefik_deployed=True)
        self.archived = make_host(
            self.env, "egm-old", "46.4.0.71", traefik_deployed=True,
        )
        self.archived.active = False
        self.bare = make_host(self.env, "egm-bare", "46.4.0.72")

    def _guards(self, host):
        return self.env["cloud.job"].search([
            ("host_id", "=", host.id),
            ("job_type_id.code", "=", "push_edge_guard"),
        ])

    def test_one_push_per_host_already_set_up(self):
        self.env.flush_all()
        _load_migration().migrate(self.env.cr, "1.0.213")
        self.env.invalidate_all()
        self.assertEqual(len(self._guards(self.ready)), 1)
        self.assertFalse(self._guards(self.archived))
        self.assertFalse(self._guards(self.bare))
        self.assertFalse(self.env["cloud.job"].search([
            ("host_id", "in", (self.ready | self.archived | self.bare).ids),
            ("job_type_id.code", "=", "full_setup"),
        ]))

    def test_a_fresh_install_queues_nothing(self):
        self.env.flush_all()
        _load_migration().migrate(self.env.cr, None)
        self.env.invalidate_all()
        self.assertFalse(self._guards(self.ready))


class TestTheUpdateHealsHostsLeftWithoutIt(TransactionCase):
    """1.0.220 queues the guard again where 1.0.214's push never landed.

    Its job failed on a managed panel, taken by a server whose code had no
    executor for it (7-nov-2026), and nothing queued it again.
    """

    def setUp(self):
        super().setUp()
        self.missing = make_host(
            self.env, "egh-missing", "46.4.0.80", traefik_deployed=True,
        )
        self.guarded = make_host(
            self.env, "egh-guarded", "46.4.0.81", traefik_deployed=True,
            edge_guard_shipped_hash="a" * 64,
        )
        self.bare = make_host(self.env, "egh-bare", "46.4.0.82")

    def _guards(self, host):
        return self.env["cloud.job"].search([
            ("host_id", "=", host.id),
            ("job_type_id.code", "=", "push_edge_guard"),
        ])

    def test_only_the_hosts_never_guarded_get_it_again(self):
        self.env.flush_all()
        _load_migration("1.0.220").migrate(self.env.cr, "1.0.219")
        self.env.invalidate_all()
        self.assertEqual(len(self._guards(self.missing)), 1)
        self.assertFalse(self._guards(self.guarded))
        self.assertFalse(self._guards(self.bare))

    def test_a_fresh_install_queues_nothing(self):
        self.env.flush_all()
        _load_migration("1.0.220").migrate(self.env.cr, None)
        self.env.invalidate_all()
        self.assertFalse(self._guards(self.missing))
