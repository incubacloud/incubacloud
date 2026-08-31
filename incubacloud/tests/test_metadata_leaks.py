"""What the panel tells a caller about the things it will not show them.

Two endpoints answered questions nobody was allowed to ask. The
preferences modal let any internal user store an arbitrary project id
in their personal mute list — browsed with sudo, so record rules never
looked — and then read the list back with sudo too, returning the
name. Write anything, read the name: an id-to-name oracle over every
project on the platform. Separately ``_capped_search`` counted the
total with sudo while listing the records without it, so the "showing N
of TOTAL" payload of ``/cloud/get_projects`` handed a stakeholder the
size of the whole platform's inventory.

Neither leaks a record, and that is the point: these tests pin the
metadata boundary, which is the one that erodes silently because
nothing breaks when it moves.
"""
import json

from odoo.tests import tagged
from odoo.tests.common import HttpCase


@tagged('-at_install', 'post_install')
class TestProjectMetadataLeaks(HttpCase):
    """Driven over real HTTP: who can reach the route is the question."""

    def setUp(self):
        super().setUp()
        Project = self.env['cloud.project']
        self.mine = Project.create({'name': 'BUG004 Mine'})
        self.theirs = Project.create({'name': 'BUG004 Theirs'})
        self.stakeholder = self._make_user(
            'bug004-stakeholder',
            ['base.group_user', 'incubacloud.group_cloud_user'],
        )
        self.mine.member_ids = [(4, self.stakeholder.id)]

    def _make_user(self, login, groups):
        """Create a user with a known password for ``authenticate``.

        :param login: login and password (same string, test-only).
        :param groups: xml ids of the groups to grant.
        :returns: the created ``res.users`` record.
        """
        return self.env['res.users'].create({
            'name': login,
            'login': login,
            'password': login,
            'group_ids': [(6, 0, [self.env.ref(g).id for g in groups])],
        })

    def _call(self, route, **params):
        """POST a JSON-RPC call to *route* on the current session.

        :param route: controller path to call.
        :param params: JSON-RPC params for the route.
        :returns: the decoded ``result`` payload.
        """
        payload = {
            'jsonrpc': '2.0', 'method': 'call', 'id': 1,
            'params': params,
        }
        return self.url_open(
            route,
            data=json.dumps(payload),
            headers={'Content-Type': 'application/json'},
        ).json().get('result')

    def _save_prefs(self, **params):
        """POST to /cloud/save_user_preferences with the required fields."""
        return self._call(
            '/cloud/save_user_preferences',
            cloud_notification_level='failures',
            cloud_notification_mode='immediate',
            **params,
        )

    # ── The mute list as an id-to-name oracle ──────────────────────────

    def test_a_project_the_caller_cannot_see_is_not_stored_as_muted(self):
        """The write side is where the oracle was fed."""
        self.authenticate('bug004-stakeholder', 'bug004-stakeholder')
        result = self._save_prefs(
            cloud_muted_project_ids=[self.mine.id, self.theirs.id],
        )
        self.assertTrue(result.get('ok'))
        self.stakeholder.invalidate_recordset()
        self.assertEqual(
            self.stakeholder.cloud_muted_project_ids, self.mine,
            'a project outside the caller\'s record rules was stored',
        )

    def test_a_muted_project_the_caller_lost_access_to_is_not_named(self):
        """Rows stored before the fix must not keep leaking on read.

        Written with sudo on purpose: this is the shape a pre-fix save —
        or a membership revoked afterwards — leaves in the database.
        """
        self.stakeholder.sudo().write({
            'cloud_muted_project_ids': [(6, 0, [self.mine.id, self.theirs.id])],
        })
        self.authenticate('bug004-stakeholder', 'bug004-stakeholder')
        result = self._call('/cloud/get_user_preferences')
        names = [p['name'] for p in result['cloud_muted_projects']]
        self.assertEqual(names, ['BUG004 Mine'])

    def test_muting_a_project_the_caller_owns_still_works(self):
        """The guard must not break the feature it protects."""
        self.authenticate('bug004-stakeholder', 'bug004-stakeholder')
        self.assertTrue(
            self._save_prefs(cloud_muted_project_ids=[self.mine.id]).get('ok')
        )
        result = self._call('/cloud/get_user_preferences')
        self.assertEqual(
            [p['id'] for p in result['cloud_muted_projects']], [self.mine.id],
        )

    def test_an_internal_user_with_no_cloud_role_can_still_save(self):
        """``_filtered_access`` filters, it does not raise.

        A plain internal user has no ``ir.model.access`` on
        ``cloud.project`` at all. Sanitising the mute list with
        ``search`` would turn their save into a 500; the rest of the
        form has nothing to do with projects.
        """
        self._make_user('bug004-plain', ['base.group_user'])
        self.authenticate('bug004-plain', 'bug004-plain')
        result = self._save_prefs(
            cloud_muted_project_ids=[self.mine.id, self.theirs.id],
        )
        self.assertTrue(result.get('ok'))

    # ── The capped-list total ──────────────────────────────────────────

    def test_the_project_total_counts_only_what_the_caller_may_see(self):
        """``total`` used to be the platform's inventory size."""
        self.assertGreater(
            self.env['cloud.project'].sudo().search_count([]), 1,
            'nothing to leak — the fixture makes this test vacuous',
        )
        self.authenticate('bug004-stakeholder', 'bug004-stakeholder')
        result = self._call('/cloud/get_projects')
        self.assertEqual([p['id'] for p in result['items']], [self.mine.id])
        self.assertEqual(result['total'], 1)
        self.assertFalse(result['truncated'])
