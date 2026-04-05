"""
Tier 2 — ORM integration tests for the pluggable instance actions system.

Tests cover:
  - cloud.job.type field defaults (show_as_action, action_icon, action_order)
  - The custom_actions query: filtering by show_as_action + apply_to='instance'
  - Ordering by action_order
  - Field presence in serialized output
"""
from odoo.tests.common import TransactionCase


def _job_type(env, code, apply_to='instance', **kw):
    """Create a cloud.job.type record for testing."""
    return env['cloud.job.type'].create(
        {'name': code, 'code': code, 'apply_to': apply_to} | kw
    )


def _custom_actions(env, apply_to='instance'):
    """Mirror the query used in _serialize_instance / cloud_get_host."""
    return env['cloud.job.type'].search([
        ('show_as_action', '=', True),
        ('apply_to', '=', apply_to),
    ], order='action_order, id')


class TestPluggableActionsFields(TransactionCase):

    def test_show_as_action_default_false(self):
        jt = _job_type(self.env, 'test_default_flag')
        self.assertFalse(jt.show_as_action)

    def test_action_icon_default_fa_cog(self):
        jt = _job_type(self.env, 'test_default_icon')
        self.assertEqual(jt.action_icon, 'fa-cog')

    def test_action_order_default_100(self):
        jt = _job_type(self.env, 'test_default_order')
        self.assertEqual(jt.action_order, 100)

    def test_fields_persist_when_set(self):
        jt = _job_type(
            self.env, 'test_persist',
            show_as_action=True,
            action_icon='fa-magic',
            action_order=42,
        )
        self.assertTrue(jt.show_as_action)
        self.assertEqual(jt.action_icon, 'fa-magic')
        self.assertEqual(jt.action_order, 42)


class TestCustomActionsQuery(TransactionCase):
    """
    Tests for the query that drives the custom_actions list.

    Note: _serialize_instance / cloud_get_host are controller methods that use
    request.env. We test the underlying ORM query directly using self.env,
    which exercises the same logic without an HTTP request context.
    """

    def setUp(self):
        super().setUp()
        # Disable any pre-existing show_as_action types to isolate tests
        self.env['cloud.job.type'].search([
            ('show_as_action', '=', True),
        ]).write({'show_as_action': False})

    def test_included_when_show_as_action_true_and_instance(self):
        jt = _job_type(self.env, 'ext_include', show_as_action=True)
        self.assertIn(jt, _custom_actions(self.env))

    def test_excluded_when_show_as_action_false(self):
        jt = _job_type(self.env, 'ext_exclude_flag', show_as_action=False)
        self.assertNotIn(jt, _custom_actions(self.env))

    def test_excluded_when_apply_to_host(self):
        jt = _job_type(
            self.env, 'ext_exclude_host',
            apply_to='host', show_as_action=True,
        )
        self.assertNotIn(jt, _custom_actions(self.env))

    def test_empty_when_none_registered(self):
        self.assertFalse(_custom_actions(self.env))

    def test_ordered_by_action_order(self):
        jt_low = _job_type(
            self.env, 'ext_order_low', show_as_action=True, action_order=10,
        )
        jt_high = _job_type(
            self.env, 'ext_order_high', show_as_action=True, action_order=50,
        )
        result = _custom_actions(self.env)
        low_idx = list(result.ids).index(jt_low.id)
        high_idx = list(result.ids).index(jt_high.id)
        self.assertLess(low_idx, high_idx)

    def test_multiple_instance_types_all_included(self):
        jt1 = _job_type(self.env, 'ext_multi_1', show_as_action=True)
        jt2 = _job_type(self.env, 'ext_multi_2', show_as_action=True)
        result = _custom_actions(self.env)
        self.assertIn(jt1, result)
        self.assertIn(jt2, result)

    def test_serialized_dict_has_required_keys(self):
        _job_type(
            self.env, 'ext_keys', show_as_action=True, action_icon='fa-star',
        )
        result = _custom_actions(self.env)
        serialized = [
            {
                'code': jt.code,
                'name': jt.name,
                'action_icon': jt.action_icon or 'fa-cog',
            }
            for jt in result
        ]
        self.assertTrue(serialized)
        entry = next(e for e in serialized if e['code'] == 'ext_keys')
        self.assertIn('code', entry)
        self.assertIn('name', entry)
        self.assertIn('action_icon', entry)
        self.assertEqual(entry['action_icon'], 'fa-star')

    def test_action_icon_fallback_to_fa_cog(self):
        jt = _job_type(self.env, 'ext_no_icon', show_as_action=True)
        jt.action_icon = False  # Clear it to simulate missing value
        serialized_icon = jt.action_icon or 'fa-cog'
        self.assertEqual(serialized_icon, 'fa-cog')


class TestCustomActionsHost(TransactionCase):
    """Tests for the host-scoped custom_actions query (apply_to='host')."""

    def setUp(self):
        super().setUp()
        self.env['cloud.job.type'].search([
            ('show_as_action', '=', True),
        ]).write({'show_as_action': False})

    def test_host_action_included(self):
        jt = _job_type(
            self.env, 'host_ext_include',
            apply_to='host', show_as_action=True,
        )
        self.assertIn(jt, _custom_actions(self.env, apply_to='host'))

    def test_host_action_excluded_from_instance_query(self):
        jt = _job_type(
            self.env, 'host_ext_isolation',
            apply_to='host', show_as_action=True,
        )
        self.assertNotIn(jt, _custom_actions(self.env, apply_to='instance'))

    def test_instance_action_excluded_from_host_query(self):
        jt = _job_type(
            self.env, 'inst_ext_isolation',
            apply_to='instance', show_as_action=True,
        )
        self.assertNotIn(jt, _custom_actions(self.env, apply_to='host'))

    def test_host_actions_ordered_by_action_order(self):
        jt_first = _job_type(
            self.env, 'host_order_first',
            apply_to='host', show_as_action=True, action_order=5,
        )
        jt_last = _job_type(
            self.env, 'host_order_last',
            apply_to='host', show_as_action=True, action_order=200,
        )
        result = _custom_actions(self.env, apply_to='host')
        first_idx = list(result.ids).index(jt_first.id)
        last_idx = list(result.ids).index(jt_last.id)
        self.assertLess(first_idx, last_idx)
