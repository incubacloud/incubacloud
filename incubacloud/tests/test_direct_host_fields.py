"""A host's direct port: which numbers it may take, and what it changes.

The direct entrypoint (plan-trafico-interno-directo, piece 1) is a port
of the host's own for the platform's traffic. A number the platform
already publishes there would take the proxy down at the next Full
Setup, so it is refused where the operator types it. And the port joins
the host's configuration snapshot only when there is one: a new key in
every snapshot marks the whole fleet "configuration pending"
(migrations/1.0.134).
"""
import hashlib
import json

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.tests._direct import make_host


class TestTheDirectPortIsAPortOfItsOwn(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = make_host(self.env, "dp-host", "46.4.0.10", port=2222)

    def test_a_port_the_platform_uses_is_refused(self):
        """Web, mail, Traefik, the central, SSH and its rotation range."""
        for port in (
            25, 80, 143, 443, 465, 587, 993, 3000, 8080, 8082, 8428,
            2222, 22000, 25000, 29999, 1023, 65536,
        ):
            with self.subTest(port=port), self.assertRaisesRegex(
                ValidationError, "is used by the platform on this host",
            ), self.env.cr.savepoint():
                self.host.direct_port = port
                self.host.flush_recordset()

    def test_9443_is_accepted(self):
        self.host.direct_port = 9443
        self.host.flush_recordset()
        self.assertEqual(self.host.direct_port, 9443)

    def test_no_port_is_no_direct_entrypoint(self):
        self.assertEqual(self.host.direct_port, 0)

    def test_the_ssh_port_cannot_move_onto_the_direct_port(self):
        self.host.direct_port = 9443
        self.host.flush_recordset()
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.host.port = 9443
            self.host.flush_recordset()


class TestTheSnapshotCarriesAPortOnlyWhenThereIsOne(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = make_host(self.env, "dp-snap", "46.4.0.11")

    def _digest_before_the_direct_path(self):
        """The digest versions before this one computed for the host."""
        host = self.host.sudo()
        snap = {f: host[f] or "" for f in host._config_snapshot_fields()}
        snap["whitelist"] = sorted(host.whitelist_ids.mapped("hostname"))
        raw = json.dumps(snap, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    def test_a_host_without_a_port_keeps_its_digest(self):
        """Production's three hosts are not marked pending by the update."""
        self.assertNotIn("direct_port", self.host._render_config_snapshot())
        self.assertEqual(
            self.host._config_snapshot_hash(),
            self._digest_before_the_direct_path(),
        )

    def test_a_port_marks_the_host_pending_until_its_setup(self):
        self.host.write(self.host._applied_config_vals())
        self.assertFalse(self.host.config_dirty)
        self.host.direct_port = 9443
        self.host.invalidate_recordset(["config_dirty"])
        self.assertEqual(self.host._render_config_snapshot()["direct_port"], 9443)
        self.assertTrue(self.host.config_dirty)
