"""The sidebar's count badges have a route of their own.

``/cloud/get_config`` counted them once, when the panel loaded, so a
project, a host or a backup destination created or deleted left them
wrong until a reload. ``/cloud/get_nav_counts`` answers the same counts,
alone, for the panel to ask again: the same permission gates, the same
record rules.
"""
import json

from odoo.tests import tagged
from odoo.tests.common import HttpCase


@tagged('-at_install', 'post_install')
class TestNavCountsRoute(HttpCase):
    """Driven over real HTTP, as the panel asks it."""

    def setUp(self):
        super().setUp()
        for login, groups in (
            ('nav-manager', ['base.group_user', 'incubacloud.group_cloud_manager']),
            ('nav-user', ['base.group_user', 'incubacloud.group_cloud_user']),
        ):
            self.env['res.users'].create({
                'name': login, 'login': login, 'password': login,
                'group_ids': [(6, 0, [self.env.ref(g).id for g in groups])],
            })

    def _call(self, route):
        """POST *route* on the current session; its ``result``.

        :param str route: the panel's JSON route
        :rtype: dict
        """
        payload = {'jsonrpc': '2.0', 'method': 'call', 'id': 1, 'params': {}}
        return self.url_open(
            route, data=json.dumps(payload),
            headers={'Content-Type': 'application/json'},
        ).json().get('result')

    def _expected_keys(self, permissions):
        """The badges *permissions* show, as the sidebar gates its entries."""
        keys = {'projects'}
        if permissions.get('can_manage_hosts'):
            keys.add('hosts')
        if permissions.get('can_manage_settings'):
            keys.add('backups')
        return keys

    def test_the_counts_are_get_config_s_with_its_gates(self):
        for login in ('nav-manager', 'nav-user'):
            with self.subTest(login=login):
                self.authenticate(login, login)
                config = self._call('/cloud/get_config')
                counts = self._call('/cloud/get_nav_counts')['nav_counts']
                self.assertEqual(set(counts), self._expected_keys(config['permissions']))
                for key, value in counts.items():
                    self.assertEqual(value, config['nav_counts'][key], key)

    def test_a_manager_sees_hosts_and_destinations_counted(self):
        self.authenticate('nav-manager', 'nav-manager')
        counts = self._call('/cloud/get_nav_counts')['nav_counts']
        self.assertEqual(counts['hosts'], self.env['cloud.host'].search_count([]))
        self.assertEqual(
            counts['backups'], self.env['cloud.backup.backend'].search_count([]),
        )

    def test_a_project_created_is_counted_at_once(self):
        self.authenticate('nav-manager', 'nav-manager')
        before = self._call('/cloud/get_nav_counts')['nav_counts']['projects']
        self.env['cloud.project'].create({'name': 'nav-new'})
        after = self._call('/cloud/get_nav_counts')['nav_counts']['projects']
        self.assertEqual(after, before + 1)

    def test_a_visitor_gets_no_counts(self):
        self.authenticate(None, None)
        payload = {'jsonrpc': '2.0', 'method': 'call', 'id': 1, 'params': {}}
        answer = self.url_open(
            '/cloud/get_nav_counts', data=json.dumps(payload),
            headers={'Content-Type': 'application/json'},
        ).json()
        self.assertNotIn('result', answer)
