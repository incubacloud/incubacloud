"""The heartbeat of the scheduled actions: a ping a service outside expects.

The panel's scheduled actions often run on one thread
(``max_cron_threads = 1``): a pass that never ends holds every other one,
the sweep that would raise an alert included, and nothing inside the
panel can say so. With a heartbeat URL in Settings, each pass of the
reconciliation of lost jobs that ends well asks for that URL; the service
behind it (healthchecks.io, Better Stack, Cronitor...) warns when the asks
stop. Without a URL nothing is asked, a failed ask never fails the pass,
and a pass that fails asks for nothing.
"""
import logging
import socket
from unittest.mock import MagicMock, patch

from odoo.http import Request
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.controllers._data_load import _routes_crud
from odoo.addons.incubacloud.net import outbound
from odoo.addons.incubacloud.net.outbound import (
    OutboundError,
    _PinnedHTTPSConnection,
    ping,
)

#: The core's HTTP function, where the reconciliation reaches it.
_PING = "odoo.addons.incubacloud.models.cloud_job.ping"
_URL = "https://hc-ping.example.com/0b5e1f7a-1d1c-4c1f-9d7e-2f0c1a7e9b10"


def _resolving_to(ip):
    """Patch the outbound module's DNS so any hostname resolves to *ip*.

    :param str ip: the address the fake resolver answers with
    :return: the patcher, to use as a context manager
    """
    return patch.object(outbound, "getaddrinfo", return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443)),
    ])


class TestReconciliationHeartbeat(TransactionCase):
    """Each good pass of the reconciliation pings the URL, if there is one."""

    def setUp(self):
        """A reconciliation with nothing to close, and no URL to start."""
        super().setUp()
        self.Job = self.env["cloud.job"]
        self.settings = self.env["cloud.settings"].sudo()._get()
        self.settings.cron_heartbeat_url = False
        patcher = patch.object(
            type(self.Job), "_lost_job_ids", autospec=True, return_value=[],
        )
        self.lost = patcher.start()
        self.addCleanup(patcher.stop)

    def test_without_a_url_nothing_is_asked(self):
        """The default: no URL, no request leaves."""
        with patch(_PING, autospec=True) as sent:
            self.assertEqual(self.Job._cron_reconcile_lost_jobs(), 0)
        sent.assert_not_called()

    def test_a_good_pass_pings_once(self):
        """One GET to the URL, with a short timeout, at the end of the pass."""
        self.settings.cron_heartbeat_url = _URL
        with patch(_PING, autospec=True) as sent:
            self.Job._cron_reconcile_lost_jobs()
        sent.assert_called_once_with(
            _URL, timeout=self.Job._CRON_HEARTBEAT_TIMEOUT_S,
        )
        self.assertLessEqual(self.Job._CRON_HEARTBEAT_TIMEOUT_S, 10)

    def test_a_failed_ping_does_not_fail_the_pass(self):
        """The service down, or refused: the pass ends as it would, and says so."""
        self.settings.cron_heartbeat_url = _URL
        for error in (OutboundError("delivery to hc-ping.example.com failed"),
                      RuntimeError("anything else")):
            with self.subTest(error=type(error).__name__), patch(
                _PING, autospec=True, side_effect=error,
            ) as sent, self.assertLogs(
                "odoo.addons.incubacloud.models.cloud_job", logging.WARNING,
            ) as logged:
                self.assertEqual(self.Job._cron_reconcile_lost_jobs(), 0)
            sent.assert_called_once()
            self.assertIn("heartbeat", logged.output[0])
            self.assertNotIn(_URL, "\n".join(logged.output))

    def test_a_pass_that_fails_does_not_ping(self):
        """No ping when the pass raises: the silence is what warns."""
        self.settings.cron_heartbeat_url = _URL
        self.lost.side_effect = RuntimeError("the pass broke")
        with patch(_PING, autospec=True) as sent, \
                self.assertRaises(RuntimeError):
            self.Job._cron_reconcile_lost_jobs()
        sent.assert_not_called()


class TestHeartbeatSetting(TransactionCase):
    """The URL travels through Settings › Logs & retention."""

    def setUp(self):
        """The general settings' controller, as a manager calls it."""
        super().setUp()
        self.controller = _routes_crud.CrudMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]
        self.settings = self.env["cloud.settings"].sudo()._get()

    def _call(self, method, **values):
        """Call the controller's *method* with *values*, in a fake request.

        :param str method: the name of the route's method
        :return: what the route answered
        """
        fake = MagicMock(spec=Request)
        fake.env = self.env
        with patch.object(_routes_crud, "request", fake):
            return getattr(self.controller, method)(**values)

    def test_empty_by_default(self):
        """A new panel pings nothing."""
        field = self.env["cloud.settings"]._fields["cron_heartbeat_url"]
        self.assertFalse(field.default)
        self.settings.cron_heartbeat_url = False
        data = self._call("cloud_get_general_settings")
        self.assertEqual(data["cron_heartbeat_url"], "")

    def test_saved_trimmed_and_read_back(self):
        """A public https URL is kept, without the spaces around it."""
        with _resolving_to("93.184.216.34"):
            result = self._call(
                "cloud_save_general_settings", cron_heartbeat_url=f"  {_URL} ",
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.settings.cron_heartbeat_url, _URL)
        data = self._call("cloud_get_general_settings")
        self.assertEqual(data["cron_heartbeat_url"], _URL)

    def test_emptied_turns_it_off(self):
        """An empty value clears it."""
        self.settings.cron_heartbeat_url = _URL
        result = self._call("cloud_save_general_settings", cron_heartbeat_url="")
        self.assertTrue(result["ok"], result)
        self.assertFalse(self.settings.cron_heartbeat_url)

    def test_a_url_the_panel_would_not_dial_is_refused_under_its_field(self):
        """Plain http, or a private address: refused, and nothing is saved."""
        self.settings.write({"cron_heartbeat_url": _URL, "job_retention_days": 180})
        for url, ip in (("http://hc-ping.example.com/x", "93.184.216.34"),
                        ("https://status.internal.example.com/x", "10.0.0.5")):
            with self.subTest(url=url), _resolving_to(ip):
                result = self._call(
                    "cloud_save_general_settings", cron_heartbeat_url=url,
                    job_retention_days=12,
                )
            self.assertFalse(result["ok"])
            self.assertEqual(result["field"], "cron_heartbeat_url")
            self.assertEqual(self.settings.cron_heartbeat_url, _URL)
            self.assertEqual(self.settings.job_retention_days, 180)


class TestPing(BaseCase):
    """``ping``: a GET through the same door as the webhooks."""

    def test_a_get_to_the_validated_address_whose_answer_is_dropped(self):
        """GET with the path and query, a bounded read, the socket closed."""
        fake = MagicMock(spec=_PinnedHTTPSConnection)
        with _resolving_to("93.184.216.34"), patch.object(
            outbound, "_PinnedHTTPSConnection", return_value=fake,
        ) as conn:
            self.assertIsNone(ping("https://hc-ping.example.com/abc?rid=1", timeout=5))
        self.assertEqual(conn.call_args.args[:2], ("hc-ping.example.com", "93.184.216.34"))
        self.assertEqual(conn.call_args.kwargs["timeout"], 5)
        method, path = fake.request.call_args.args[:2]
        self.assertEqual((method, path), ("GET", "/abc?rid=1"))
        self.assertGreater(fake.getresponse.return_value.read.call_args.args[0], 0)
        fake.close.assert_called_once()

    def test_a_refused_destination_never_opens_a_socket(self):
        """The metadata service, by name: refused before any connection."""
        with _resolving_to("169.254.169.254"), patch.object(
            outbound, "_PinnedHTTPSConnection",
        ) as conn, self.assertRaises(OutboundError):
            ping("https://metadata.example.com/x")
        conn.assert_not_called()

    def test_a_failed_exchange_is_an_outbound_error(self):
        """A timeout or a reset comes back as ``OutboundError``, nothing more."""
        fake = MagicMock(spec=_PinnedHTTPSConnection)
        fake.request.side_effect = TimeoutError("timed out")
        with _resolving_to("93.184.216.34"), patch.object(
            outbound, "_PinnedHTTPSConnection", return_value=fake,
        ), self.assertRaises(OutboundError):
            ping("https://hc-ping.example.com/abc")
        fake.close.assert_called_once()
