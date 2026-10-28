"""Which URL each host's agents write to, and the host's own try of it.

A host is offered the central's direct name when the name works, the
host's own address is applied on the central's host, it is not marked
"Use the public path" and it did not turn the direct path down in the
last six hours. The install then tries it from the host and falls back to
the remote-write URL when it gets no answer; two refusals in a row raise
``direct_path_unreachable_from_host``. A try that fails while the name
itself is not ``ok`` is no fault of the host's.
"""
import asyncio
import json
from datetime import timedelta
from pathlib import Path

import yaml

from odoo import fields

from odoo.addons.incubacloud.models.observability_agents_executor import (
    ObservabilityAgentsExecutor,
)
from odoo.addons.incubacloud.tests._direct import make_host
from odoo.addons.incubacloud.tests.test_observability_executors import (
    ObservabilityExecutorCase,
)

DIRECT = "direct.metrics.example.test"
OFFER = f"https://{DIRECT}:9443/w/api/v1/write"
PUBLIC = "https://metrics.example.test/w/api/v1/write"
_PLAYBOOK = (
    Path(__file__).resolve().parents[1]
    / "ansible" / "playbooks" / "host_observability.yml"
)


class _OfferCase(ObservabilityExecutorCase):
    """A host, the central's host with the host admitted, and a working name."""

    def setUp(self):
        super().setUp()
        self.host.ip_address = "46.4.0.90"
        self.central = make_host(
            self.env, "ag-central", "46.4.0.91",
            direct_port=9443, direct_port_shipped=9443,
            direct_allowlist_shipped="10.0.0.0/8\n46.4.0.90/32\n46.4.0.91/32",
        )
        self.settings.write({
            "metrics_central_host_id": self.central.id,
            "metrics_central_direct_host": DIRECT,
            "metrics_remote_write_url": PUBLIC,
        })
        self.settings.metrics_central_direct_state = "ok"


class TestWhoIsOfferedTheDirectPath(_OfferCase):

    def test_everything_in_place_offers_it(self):
        self.assertEqual(self.host._direct_write_offer(), OFFER)
        self.assertEqual(self.host._metrics_remote_write_url(), OFFER)

    def test_any_condition_missing_falls_back_to_the_public_url(self):
        long_ago = fields.Datetime.now() - timedelta(hours=2)
        for change in (
            lambda: self.settings.write(
                {"metrics_central_direct_state": "failing"},
            ),
            lambda: self.host.write({"direct_path_opt_out": True}),
            lambda: self.central.write(
                {"direct_allowlist_shipped": "10.0.0.0/8\n46.4.0.91/32"},
            ),
            lambda: self.host.write({"direct_refused_at": long_ago}),
            lambda: self.central.write({"direct_port_shipped": 0}),
            lambda: self.settings.write({"metrics_central_direct_host": False}),
        ):
            with self.subTest(change=change), self.env.cr.savepoint() as sp:
                change()
                self.assertEqual(self.host._direct_write_offer(), "")
                self.assertEqual(self.host._metrics_remote_write_url(), PUBLIC)
                sp.rollback()
                self.env.invalidate_all()

    def test_a_refusal_is_forgotten_after_six_hours(self):
        self.host.direct_refused_at = fields.Datetime.now() - timedelta(hours=7)
        self.assertEqual(self.host._direct_write_offer(), OFFER)

    def test_the_playbook_gets_both_urls_and_the_checks(self):
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertEqual(extra["ic_remote_write_url"], PUBLIC)
        self.assertEqual(extra["ic_direct_write_url"], OFFER)
        self.assertEqual(
            extra["ic_direct_logs_write_url"],
            f"https://{DIRECT}:9443/lw/loki/api/v1/push",
        )
        self.assertEqual(
            extra["ic_direct_probes"],
            [{"url": OFFER, "method": "POST", "status": 401}],
        )

    def test_not_offered_the_playbook_tries_nothing(self):
        self.host.direct_path_opt_out = True
        extra = self._make(ObservabilityAgentsExecutor).get_extra_vars()
        self.assertEqual(extra["ic_direct_write_url"], "")
        self.assertEqual(extra["ic_direct_probes"], [])


class TestTheHostsOwnTry(_OfferCase):

    def _installed(self, status, msg="", installed=None):
        """Run ``on_success`` as after an install whose try answered *status*."""
        executor = self._make(ObservabilityAgentsExecutor)
        executor.get_extra_vars()
        executor._facts = {
            "ic_agents_running": "3",
            "ic_instances_labelled": "0",
            "ic_write_url_installed": installed or (
                OFFER if status == 401 else PUBLIC
            ),
            "ic_direct_report": json.dumps(
                [{"url": OFFER, "status": status, "msg": msg}],
            ),
        }
        asyncio.run(executor.on_success({}))
        self.host.invalidate_recordset()

    def _unreachable(self):
        """Return the active alert about this host not reaching the path."""
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", "direct_path_unreachable_from_host"),
            ("host_id", "=", self.host.id), ("state", "=", "active"),
        ])

    def test_a_good_try_installs_the_direct_url(self):
        self._installed(401)
        self.assertEqual(self.host.metrics_agents_write_url, OFFER)
        self.assertTrue(self.host.direct_reach_ok)
        self.assertEqual(self.host.direct_reach_urls.split(), [OFFER])

    def test_a_refusal_installs_the_public_url_and_waits_six_hours(self):
        self._installed(
            -1, msg="Request failed: <urlopen error [Errno 111] Connection refused>",
        )
        self.assertEqual(self.host.metrics_agents_write_url, PUBLIC)
        self.assertFalse(self.host.direct_reach_ok)
        self.assertEqual(self.host.direct_reach_reason, "connection")
        self.assertEqual(self.host.direct_reach_failures, 1)
        self.assertTrue(self.host.direct_refused_at)
        self.assertEqual(self.host._direct_write_offer(), "")
        self.assertFalse(self._unreachable())

    def test_two_refusals_in_a_row_alert_and_a_good_try_closes_it(self):
        self._installed(-1, msg="CERTIFICATE_VERIFY_FAILED")
        self.host.direct_refused_at = fields.Datetime.now() - timedelta(hours=7)
        self._installed(-1, msg="CERTIFICATE_VERIFY_FAILED")
        self.assertIn("(tls)", self._unreachable().message)
        self.host.direct_refused_at = fields.Datetime.now() - timedelta(hours=7)
        self._installed(401)
        self.assertFalse(self._unreachable())

    def test_a_try_against_a_name_that_is_down_is_inconclusive(self):
        self.settings.metrics_central_direct_state = "failing"
        self.host.write({"direct_refused_at": False})
        executor = self._make(ObservabilityAgentsExecutor)
        executor._direct_probes = [{
            "url": OFFER, "method": "POST", "status": 401,
            "prefix": "metrics_central_direct",
        }]
        executor._facts = {
            "ic_agents_running": "3", "ic_write_url_installed": PUBLIC,
            "ic_direct_report": json.dumps(
                [{"url": OFFER, "status": 502, "msg": ""}],
            ),
        }
        asyncio.run(executor.on_success({}))
        self.host.invalidate_recordset()
        self.assertFalse(self.host.direct_refused_at)
        self.assertTrue(self.host.direct_reach_reason.startswith("inconclusive"))
        self.assertFalse(self._unreachable())


class TestThePlaybookTriesBeforeItWrites(ObservabilityExecutorCase):

    def setUp(self):
        super().setUp()
        self.text = _PLAYBOOK.read_text()
        self.tasks = yaml.safe_load(self.text)[0]["tasks"]
        self.names = [t["name"] for t in self.tasks]

    def test_the_try_comes_before_any_configuration(self):
        tried = self.names.index("Try the direct path from this host")
        self.assertLess(
            tried, self.names.index("Write the vmagent scrape configuration"),
        )
        self.assertLess(
            tried,
            self.names.index("Write the access-log collector configuration"),
        )
        task = self.tasks[tried]["ansible.builtin.uri"]
        self.assertIs(task["validate_certs"], True)

    def test_the_agents_write_where_the_try_said(self):
        self.assertIn("-remoteWrite.url={{ ic_write_url }}", self.text)
        self.assertIn("- url: {{ ic_logs_url }}", self.text)
        self.assertNotIn("-remoteWrite.url={{ ic_remote_write_url }}", self.text)

    def test_promtail_takes_a_new_configuration(self):
        self.assertIn("Restart promtail when its configuration changed", self.names)
        self.assertIn("ic_write_url_installed", self.text)
        self.assertIn("ic_direct_report", self.text)
