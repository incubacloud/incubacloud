"""Tier 1 — shared rate-limit gate for controllers (decision log P13).

The gate collapses the hit → log → shaped-deny block previously
copy-pasted across five routes. These tests bind the module's
``request`` to a plain namespace carrying the test env (attributes
only, no fake methods) and exercise the rule evaluation contract.
"""
from types import SimpleNamespace
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers import _rate_limit
from odoo.addons.incubacloud.controllers._rate_limit import Rule


class TestRateLimitGate(TransactionCase):

    def _bound(self):
        """Patch the module-level ``request`` with the test env.

        ``cloud.rate.limit.hit`` bypasses itself under ``test_enable``
        unless the ``force_rate_limit`` context flag opts back in.
        """
        env = self.env(
            context=dict(self.env.context, force_rate_limit=True),
        )
        return patch.object(
            _rate_limit, 'request', SimpleNamespace(env=env),
        )

    def _gate(self, *rules):
        with self._bound():
            return _rate_limit.rate_gate_json(*rules)

    def test_allows_under_the_cap(self):
        rule = Rule('gate:under', 'msg', max_per_window=5)
        self.assertIsNone(self._gate(rule))

    def test_denies_over_the_cap_with_the_rule_message(self):
        rule = Rule('gate:over', 'too many', max_per_window=1)
        self.assertIsNone(self._gate(rule))
        self.assertEqual(
            self._gate(rule), {'ok': False, 'error': 'too many'},
        )

    def test_rules_evaluate_in_order_and_short_circuit(self):
        """A tripped rule must stop evaluation: the later rule's window
        is not consumed by a request that was already denied."""
        a = Rule('gate:sc_a', 'A', max_per_window=1)
        b = Rule('gate:sc_b', 'B', max_per_window=2)
        self.assertIsNone(self._gate(a, b))          # a=1, b=1
        self.assertEqual(self._gate(a, b)['error'], 'A')  # a trips; b stays 1
        self.assertIsNone(self._gate(b))             # b=2 — still allowed

    def test_first_tripped_returns_the_rule_itself(self):
        rule = Rule('gate:raw', max_per_window=1, log_tag='raw test')
        with self._bound():
            self.assertIsNone(_rate_limit.first_tripped(rule))
            self.assertIs(_rate_limit.first_tripped(rule), rule)
