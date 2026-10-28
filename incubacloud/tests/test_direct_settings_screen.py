"""The host page and Settings → Monitoring read and write the direct path.

The DNS token is write-only, like the other credentials: the browser only
learns whether one is stored. A direct name is saved trimmed and in lower
case, and a refused one comes back with the reason, nothing saved.
"""
from unittest.mock import MagicMock, patch

from odoo.http import Request
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud
from odoo.addons.incubacloud.tests._direct import make_host

_WRITE_URL = "https://metrics.example.test/w/api/v1/write"


class TestTheDirectPathOnScreen(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]
        self.settings = self.env["cloud.settings"].sudo()._get()
        self.host = make_host(
            self.env, "sc-host", "46.4.0.98",
            direct_port=9443, direct_port_shipped=9443,
            direct_allowlist_shipped="10.0.0.0/8",
        )

    def _call(self, method, **kwargs):
        """Call the controller method *method* with a request on this env."""
        fake = MagicMock(spec=Request)
        fake.env = self.env
        with patch.object(_routes_crud, "request", fake):
            return getattr(self.controller, method)(**kwargs)

    def test_the_host_page_shows_the_port_its_list_and_the_agents_url(self):
        self.host.metrics_agents_write_url = _WRITE_URL
        data = self._call("cloud_get_host", host_id=self.host.id)
        self.assertEqual(data["direct_port"], 9443)
        self.assertEqual(data["direct_allowlist_shipped"], ["10.0.0.0/8"])
        self.assertTrue(data["direct_allowlist_pending"])
        self.assertEqual(data["metrics_agents_write_url"], _WRITE_URL)

    def test_the_host_page_saves_the_port_and_the_switch(self):
        self._call(
            "cloud_save_host", host_id=self.host.id,
            vals={"direct_port": 9444, "direct_path_opt_out": True},
        )
        self.assertEqual(self.host.direct_port, 9444)
        self.assertTrue(self.host.direct_path_opt_out)

    def test_the_token_is_write_only(self):
        answer = self._call(
            "cloud_save_general_settings", acme_dns_cloudflare_token=" cf-token ",
        )
        self.assertEqual(answer, {"ok": True})
        self.assertEqual(self.settings.acme_dns_cloudflare_token, "cf-token")
        data = self._call("cloud_get_general_settings")
        self.assertTrue(data["has_acme_dns_token"])
        self.assertNotIn("cf-token", str(data))
        self._call("cloud_save_general_settings", acme_dns_cloudflare_token="")
        self.assertEqual(self.settings.acme_dns_cloudflare_token, "cf-token")

    def test_the_name_is_saved_trimmed_and_its_state_read_back(self):
        answer = self._call(
            "cloud_save_general_settings",
            metrics_central_direct_host="  Direct.Metrics.Example.Test ",
        )
        self.assertEqual(answer, {"ok": True})
        self.assertEqual(
            self.settings.metrics_central_direct_host, "direct.metrics.example.test",
        )
        data = self._call("cloud_get_general_settings")
        self.assertEqual(
            data["metrics_central_direct_host"], "direct.metrics.example.test",
        )
        self.assertEqual(data["metrics_central_direct"]["state"], "unknown")

    def test_a_url_is_refused_with_the_reason(self):
        answer = self._call(
            "cloud_save_general_settings",
            metrics_central_direct_host="https://direct.metrics.example.test/",
        )
        self.assertFalse(answer["ok"])
        self.assertIn("no https://", answer["error"])
