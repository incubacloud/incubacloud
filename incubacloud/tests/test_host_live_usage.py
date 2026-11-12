"""A host's card shows the CPU and RAM it really uses, or says why not.

The card's single CPU / RAM bar added up the limits of the host's
instances, so a nearly idle server could read 100 % and a busy one 0 %,
beside a disk bar that was real. The specs cron now reads the real CPU
and RAM from the metrics agents, together and only together, and
``/cloud/get_hosts`` gives them while they are recent; otherwise it says
why there is no figure (metrics off, agents not installed, no recent
reading) and sends none, so no stale or default 0 reads as live.
"""
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import HttpCase

from odoo.addons.incubacloud.models.cloud_host_metrics import USAGE_FRESH_SECONDS

from .test_host_metrics_handover import _GIB, HostMetricsHandoverCase

_PROMQL = "odoo.addons.incubacloud.models.cloud_host_metrics.promql_query"


class TestTheCronReadsLiveUsage(HostMetricsHandoverCase):

    def _backend(self, cpu=23.4, available_gb=3.0, total_gb=8.0):
        """A metrics backend reporting this host; ``None`` leaves a fact out.

        :param cpu: busy share of the cores, percent.
        :param available_gb: memory available, GiB.
        :param total_gb: memory installed, GiB.
        :return: a ``promql_query`` stand-in
        """
        host_id = str(self.host.id)

        def answer(_base, expression, **_kwargs):
            if "rate(" in expression:
                value = cpu
            elif "MemAvailable" in expression:
                value = None if available_gb is None else available_gb * _GIB
            elif "MemTotal" in expression:
                value = total_gb * _GIB
            elif "avail_bytes" in expression:
                value = 60 * _GIB
            elif "size_bytes" in expression:
                value = 100 * _GIB
            else:
                value = 4.0
            return [] if value is None else [({"host_id": host_id}, value)]
        return answer

    def _read(self, **backend):
        with patch(_PROMQL, autospec=True, side_effect=self._backend(**backend)):
            self._cron()

    def test_cpu_and_ram_are_written_with_their_time(self):
        self._read()
        self.assertEqual(self.host.cpu_usage, 23.4)
        self.assertEqual(self.host.ram_usage, 62.5)
        self.assertTrue(self.host.usage_last_seen)

    def test_cpu_is_asked_as_the_busy_share_of_every_core(self):
        with patch(_PROMQL, autospec=True, side_effect=self._backend()) as promql:
            self._cron()
        self.assertIn(
            '100 * (1 - avg by (host_id) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
            [call.args[1] for call in promql.call_args_list],
        )

    def test_half_a_reading_is_not_written(self):
        """A host just enrolled has no rate yet: its RAM alone must not
        stamp the pair, or the CPU's default 0 would read as live."""
        self._read(cpu=None)
        self.assertFalse(self.host.usage_last_seen)
        self.assertEqual(self.host.ram_usage, 0.0)
        self._read(available_gb=None)
        self.assertFalse(self.host.usage_last_seen)
        self.assertEqual(self.host.cpu_usage, 0.0)

    def test_the_disk_is_still_written_without_usage(self):
        self._read(cpu=None)
        self.assertEqual(self.host.disk_usage, 40.0)

    def test_figures_out_of_range_are_kept_within_it(self):
        self._read(cpu=100.4, available_gb=9.0)
        self.assertEqual(self.host.cpu_usage, 100.0)
        self.assertEqual(self.host.ram_usage, 0.0)


class TestWhatTheCardIsGiven(HostMetricsHandoverCase):

    def setUp(self):
        super().setUp()
        self.now = fields.Datetime.now()
        self.host.write({
            "cpu_usage": 23.4,
            "ram_usage": 62.5,
            "metrics_agents_state": "installed",
        })

    def _seen(self, seconds_ago):
        self.host.usage_last_seen = self.now - timedelta(seconds=seconds_ago)

    def test_a_recent_reading_is_live(self):
        self._seen(60)
        usage = self.host._usage_for_card(True, self.now)
        self.assertEqual(usage["usage_state"], "live")
        self.assertEqual(usage["cpu_usage"], 23.4)
        self.assertEqual(usage["ram_usage"], 62.5)
        self.assertTrue(usage["usage_last_seen"])

    def test_a_reading_at_the_limit_is_still_live(self):
        self._seen(USAGE_FRESH_SECONDS)
        self.assertEqual(
            self.host._usage_for_card(True, self.now)["usage_state"], "live",
        )

    def test_an_old_reading_gives_no_figures(self):
        self._seen(USAGE_FRESH_SECONDS + 1)
        usage = self.host._usage_for_card(True, self.now)
        self.assertEqual(usage["usage_state"], "stale")
        self.assertIsNone(usage["cpu_usage"])
        self.assertIsNone(usage["ram_usage"])
        self.assertTrue(usage["usage_last_seen"])

    def test_without_agents_the_card_says_so(self):
        for state in ("never", "failed"):
            with self.subTest(state=state):
                self.host.metrics_agents_state = state
                usage = self.host._usage_for_card(True, self.now)
                self.assertEqual(usage["usage_state"], "no_agents")
                self.assertIsNone(usage["cpu_usage"])
                self.assertEqual(usage["usage_last_seen"], "")

    def test_with_metrics_off_no_reading_is_shown(self):
        self._seen(60)
        usage = self.host._usage_for_card(False, self.now)
        self.assertEqual(usage["usage_state"], "off")
        self.assertIsNone(usage["cpu_usage"])
        self.assertIsNone(usage["ram_usage"])


@tagged('-at_install', 'post_install')
class TestGetHostsGivesLiveUsage(HttpCase):
    """``/cloud/get_hosts`` over real HTTP, as the hosts page asks it."""

    def setUp(self):
        super().setUp()
        self.env['res.users'].create({
            'name': 'usage-manager', 'login': 'usage-manager',
            'password': 'usage-manager',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('incubacloud.group_cloud_manager').id,
            ])],
        })
        self.env["cloud.settings"].sudo()._get_system().metrics_enabled = True
        self.host = self.env["cloud.host"].create({
            "name": "LU", "ip_address": "10.0.0.30", "user": "root",
            "wildcard_domain": "lu.example.com",
            "cpu_cores": 4, "ram_total_gb": 8.0,
        })
        self.host.sudo().write({
            "cpu_usage": 23.4, "ram_usage": 62.5,
            "metrics_agents_state": "installed",
            "usage_last_seen": fields.Datetime.now(),
        })

    def _card(self):
        """The host as ``/cloud/get_hosts`` gives it to its card."""
        self.authenticate('usage-manager', 'usage-manager')
        payload = {'jsonrpc': '2.0', 'method': 'call', 'id': 1, 'params': {}}
        hosts = self.url_open(
            '/cloud/get_hosts', data=json.dumps(payload),
            headers={'Content-Type': 'application/json'},
        ).json()['result']['hosts']
        return next(h for h in hosts if h['id'] == self.host.id)

    def test_live_usage_beside_the_allocation(self):
        card = self._card()
        self.assertEqual(card['usage_state'], 'live')
        self.assertEqual(card['cpu_usage'], 23.4)
        self.assertEqual(card['ram_usage'], 62.5)
        self.assertIn('allocated_cpus', card)
        self.assertIn('allocated_ram_gb', card)

    def test_an_old_reading_reaches_the_card_without_figures(self):
        self.host.sudo().usage_last_seen = (
            fields.Datetime.now() - timedelta(seconds=USAGE_FRESH_SECONDS + 60)
        )
        card = self._card()
        self.assertEqual(card['usage_state'], 'stale')
        self.assertIsNone(card['cpu_usage'])
        self.assertIsNone(card['ram_usage'])
