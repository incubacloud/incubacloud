"""Tests for the metric-rule cron and the shared alert API (Fase 4 / A6+A7).

The backend is always mocked: these assert the *judgement* — what the
panel does with a PromQL answer — not that VictoriaMetrics works.

The most important test here is the fail-safe: when the backend is
unreachable nothing may be resolved. Resolving on silence would turn an
outage of the metrics stack into an all-green panel.
"""
from unittest.mock import MagicMock, patch

import requests

from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models.cloud_metric_rule import (
    BACKEND_UNREACHABLE_CODE,
)

_MODULE = "odoo.addons.incubacloud.models.cloud_metric_rule"


def _promql(*samples):
    """Build a VictoriaMetrics /api/v1/query success payload."""
    return {
        "status": "success",
        "data": {
            "result": [
                {"metric": labels, "value": [0, str(value)]}
                for labels, value in samples
            ],
        },
    }


class MetricRuleCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.write({
            "metrics_enabled": True,
            "metrics_central_url": "http://vm.test:8428",
        })
        self.host = self.env["cloud.host"].create({
            "name": "H1",
            "ip_address": "10.0.0.10",
            "user": "root",
            "wildcard_domain": "h1.example.com",
        })
        # Start from a clean slate: seeded rules would also fire.
        self.env["cloud.metric.rule"].sudo().search([]).write({"active": False})
        self.rule = self.env["cloud.metric.rule"].sudo().create({
            "name": "Disk",
            "code": "test_disk",
            "expression": "disk_pct",
            "comparator": "gt",
            "threshold": 90,
            "level": "critical",
            "message": "Disk on %(host)s is %(value)s%% full.",
        })

    def _alerts(self, code, state="active"):
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", code), ("state", "=", state),
        ])

    def _run_with(self, payload=None, exc=None):
        """Evaluate the cron against a mocked backend response."""
        resp = MagicMock(spec=requests.Response)
        resp.json.return_value = payload or _promql()
        resp.raise_for_status.return_value = None
        with patch(f"{_MODULE}.requests.get", side_effect=exc,
                   return_value=resp) as mocked:
            self.env["cloud.metric.rule"].sudo()._cron_evaluate()
        return mocked


class TestSeededRules(TransactionCase):
    """Guards on the shipped rules — the watchdog got these wrong once."""

    def _rule(self, xmlid):
        return self.env.ref("incubacloud." + xmlid)

    def test_the_staleness_watchdog_can_attribute_its_alert(self):
        """The expression must carry ``host_id`` per host.

        The first version used ``absent(up{job="node"})``. Two faults:
        ``absent()`` returns a series with only its selector's equality
        matchers, so there is no ``host_id`` and ``_resolve_host`` drops
        the sample — the rule could never raise anything; and on a
        fleet-wide selector it only turns 1 when EVERY host is silent,
        which is not a per-host watchdog at all.
        """
        rule = self._rule("metric_rule_host_down")
        self.assertIn(
            "host_id", rule.expression,
            "the watchdog must group by host_id or its alerts cannot be "
            "attributed to a host, and are silently discarded",
        )
        self.assertNotRegex(
            rule.expression, r"^\s*absent\(",
            "absent() cannot carry host labels — see this test's docstring",
        )
        self.assertNotIn(
            "timestamp(last_over_time(", rule.expression,
            "the second attempt: VictoriaMetrics v1.102 returns an EMPTY "
            "result for timestamp() over last_over_time(), verified live, "
            "so this form never fires either. The working shape is the "
            "subquery last_over_time(timestamp(...)[1h:1m]).",
        )
        self.assertIn(
            "last_over_time(timestamp(", rule.expression,
            "the staleness age must survive the series going stale, which "
            "is exactly what the subquery form does and a plain "
            "timestamp(up) does not",
        )
        self.assertGreater(
            rule.threshold, 0,
            "an age-based watchdog needs a real staleness threshold",
        )

    def test_no_seeded_rule_aggregates_its_own_label_away(self):
        """A rule must not lose the label its alert is attributed by.

        Labels ride along on their own — vmagent stamps ``host``/``host_id``
        on every sample, and the relabelling adds ``instance_id`` — so a
        plain expression needs no mention of them. What *does* drop them
        is an aggregation: ``max by (x)`` keeps only ``x``. So the
        invariant is narrow and mechanical: any ``by (...)`` must include
        whichever label this rule's scope resolves through, or the
        resulting samples cannot be mapped back to anything and are
        silently discarded by the evaluator.

        Scope-aware since instance rules exist: an instance rule groups
        by ``instance_id`` and has no business carrying ``host_id``.
        """
        import re

        for rule in self.env["cloud.metric.rule"].search([]):
            if rule.scope == "instance":
                label = rule.instance_label or "instance_id"
            else:
                label = rule.host_label or "host_id"
            groups = re.findall(r"\bby\s*\(([^)]*)\)", rule.expression)
            for group in groups:
                self.assertIn(
                    label, group,
                    "rule %r (scope %s) aggregates with 'by (%s)', which "
                    "drops %r — its alerts would be unattributable and "
                    "dropped"
                    % (rule.code, rule.scope, group.strip(), label),
                )


class TestPrivilegedJobTypes(TransactionCase):
    """The observability jobs deploy containers: manager-only."""

    def test_observability_job_types_require_manager(self):
        """``enqueue`` is reachable over RPC, so the gate is the backstop.

        Both jobs deploy containers on a host — cAdvisor runs privileged
        with the root filesystem mounted. The panel endpoints check the
        manager role, but that check does not protect a direct
        ``cloud.job.enqueue`` call.
        """
        gated = self.env["cloud.job"]._get_manager_job_types()
        self.assertIn("install_observability", gated)
        self.assertIn("deploy_metrics_central", gated)


class TestRuleEvaluation(MetricRuleCase):

    def test_breach_raises_an_alert(self):
        """A sample over the threshold raises an alert for its host."""
        self._run_with(_promql(({"host_id": str(self.host.id)}, 95.5)))
        alert = self._alerts("test_disk")
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.host_id, self.host)
        self.assertEqual(alert.level, "critical")
        self.assertIn("95.5", alert.message)
        self.assertIn("H1", alert.message)

    def test_recovery_resolves_the_alert(self):
        """Once back under the threshold the alert is dismissed."""
        self._run_with(_promql(({"host_id": str(self.host.id)}, 95.0)))
        self.assertTrue(self._alerts("test_disk"))
        self._run_with(_promql(({"host_id": str(self.host.id)}, 40.0)))
        self.assertFalse(self._alerts("test_disk"))

    def test_repeated_breach_does_not_duplicate(self):
        """Re-evaluating a standing breach refreshes, never piles up."""
        for value in (95.0, 96.0, 97.0):
            self._run_with(_promql(({"host_id": str(self.host.id)}, value)))
        alerts = self._alerts("test_disk")
        self.assertEqual(len(alerts), 1)
        self.assertIn("97", alerts.message)

    def test_lt_comparator(self):
        """'<' rules fire below the threshold."""
        self.rule.write({"comparator": "lt", "threshold": 10})
        self._run_with(_promql(({"host_id": str(self.host.id)}, 5.0)))
        self.assertTrue(self._alerts("test_disk"))

    def test_sample_without_a_known_host_is_skipped(self):
        """An unattributable sample must not raise a hostless alert."""
        self._run_with(_promql(({"host_id": "99999999"}, 99.0)))
        self.assertFalse(self._alerts("test_disk"))

    def test_disabled_master_switch_does_nothing(self):
        """With observability off the cron must not even query."""
        self.settings.metrics_enabled = False
        mocked = self._run_with(_promql(({"host_id": str(self.host.id)}, 99.0)))
        mocked.assert_not_called()
        self.assertFalse(self._alerts("test_disk"))


class TestBackendFailSafe(MetricRuleCase):
    """The property that matters most: silence is not health."""

    def test_unreachable_backend_raises_its_own_alert(self):
        self._run_with(exc=requests.ConnectionError("boom"))
        self.assertTrue(self._alerts(BACKEND_UNREACHABLE_CODE))

    def test_unreachable_backend_does_not_resolve_existing_alerts(self):
        """A dead backend must never clear standing alerts."""
        self._run_with(_promql(({"host_id": str(self.host.id)}, 99.0)))
        self.assertTrue(self._alerts("test_disk"))
        self._run_with(exc=requests.ConnectionError("boom"))
        self.assertTrue(
            self._alerts("test_disk"),
            "an unreachable backend silently cleared a real alert",
        )

    def test_backend_recovery_clears_the_backend_alert(self):
        self._run_with(exc=requests.ConnectionError("boom"))
        self.assertTrue(self._alerts(BACKEND_UNREACHABLE_CODE))
        self._run_with(_promql())
        self.assertFalse(self._alerts(BACKEND_UNREACHABLE_CODE))

    def test_promql_error_status_is_treated_as_unreachable(self):
        """A malformed query is a backend problem, not 'all clear'."""
        self._run_with({"status": "error", "error": "parse failure"})
        self.assertTrue(self._alerts(BACKEND_UNREACHABLE_CODE))


class TestAlertApi(TransactionCase):
    """The shared dedup rule behind both producers (executors and cron)."""

    def setUp(self):
        super().setUp()
        self.Alert = self.env["cloud.alert"].sudo()
        self.host = self.env["cloud.host"].create({
            "name": "H", "ip_address": "10.0.0.2", "user": "root",
            "wildcard_domain": "h.example.com",
        })

    def test_raise_is_idempotent(self):
        first = self.Alert.raise_alert("c", "one", host=self.host)
        second = self.Alert.raise_alert("c", "two", host=self.host)
        self.assertEqual(first, second)
        self.assertEqual(first.message, "two")

    def test_resolve_is_idempotent(self):
        self.Alert.raise_alert("c", "m", host=self.host)
        self.Alert.resolve_alert("c", host=self.host)
        # Resolving again must not raise, and must stay resolved.
        self.Alert.resolve_alert("c", host=self.host)
        self.assertFalse(self.Alert.search([
            ("code", "=", "c"), ("state", "=", "active"),
        ]))

    def test_host_and_instance_alerts_are_distinct(self):
        """Same code, different target = different alert."""
        project = self.env["cloud.project"].create({"name": "P"})
        inst = self.env["cloud.instance"].create({
            "name": "i1", "project_id": project.id, "environment": "staging",
        })
        self.Alert.raise_alert("c", "host one", host=self.host)
        self.Alert.raise_alert("c", "instance one", instance=inst)
        self.assertEqual(len(self.Alert.search([
            ("code", "=", "c"), ("state", "=", "active"),
        ])), 2)

    def test_resolving_one_target_leaves_the_other(self):
        project = self.env["cloud.project"].create({"name": "P2"})
        inst = self.env["cloud.instance"].create({
            "name": "i2", "project_id": project.id, "environment": "staging",
        })
        self.Alert.raise_alert("c", "h", host=self.host)
        self.Alert.raise_alert("c", "i", instance=inst)
        self.Alert.resolve_alert("c", host=self.host)
        remaining = self.Alert.search([
            ("code", "=", "c"), ("state", "=", "active"),
        ])
        self.assertEqual(remaining.instance_id, inst)


class TestBackendCredential(MetricRuleCase):
    """The panel must present the credential the central enforces."""

    def test_the_query_authenticates_as_this_panel_account(self):
        """Without this the panel 401s against its own backend.

        The user half is not cosmetic: the central derives the label
        filter it forces on every query from whoever authenticated, so
        querying as the wrong user returns another account's series or
        none at all. A 401, from the cron's point of view, is
        indistinguishable from an outage — it raises the unreachable
        alert and stops resolving.
        """
        self.settings.metrics_account = "acct_deadbeef"
        self.settings.metrics_remote_write_token = "s3cr3t"
        mocked = self._run_with(_promql())
        self.assertEqual(
            mocked.call_args.kwargs.get("auth"), ("acct_deadbeef", "s3cr3t"),
        )

    def test_no_credential_is_sent_when_none_is_configured(self):
        """A central deployed without a token accepts anonymous reads."""
        self.settings.metrics_remote_write_token = ""
        mocked = self._run_with(_promql())
        self.assertIsNone(mocked.call_args.kwargs.get("auth"))

    def test_a_token_without_an_account_sends_nothing(self):
        """Half a credential must fail loudly, not guess a username.

        Guessing would resurrect the old fixed ``incubacloud`` user and
        authenticate as somebody else's account on a shared central —
        the exact confusion per-account credentials exist to remove.
        """
        self.settings.metrics_account = False
        self.settings.metrics_remote_write_token = "s3cr3t"
        mocked = self._run_with(_promql())
        self.assertIsNone(mocked.call_args.kwargs.get("auth"))


class TestInstanceScopedRules(MetricRuleCase):
    """Rules that describe an instance, not the host it happens to be on."""

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "HInst", "ip_address": "10.0.0.70", "user": "root",
            "wildcard_domain": "hinst.example.com",
        })
        project = self.env["cloud.project"].create({"name": "PInst"})
        self.instance = self.env["cloud.instance"].create({
            "name": "prod", "project_id": project.id,
            "environment": "production", "host_id": self.host.id,
            "odoo_version": "19.0",
        })
        self.rule = self.env["cloud.metric.rule"].create({
            "name": "Instance down", "code": "test_instance_down",
            "scope": "instance",
            "expression": "irrelevant", "comparator": "gt",
            "threshold": 300.0, "level": "critical",
            "message": "Instance %(host)s is silent (%(value)s s).",
        })

    def _alerts(self):
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", "test_instance_down"),
            ("state", "=", "active"),
        ])

    def _sample(self, age):
        return [({"instance_id": str(self.instance.id)}, age)]

    def test_a_breach_raises_against_the_instance(self):
        self.rule._sync_alerts(self._sample(600.0))
        alerts = self._alerts()
        self.assertTrue(alerts)
        self.assertEqual(alerts[0].instance_id, self.instance)

    def test_a_healthy_instance_resolves(self):
        self.rule._sync_alerts(self._sample(600.0))
        self.rule._sync_alerts(self._sample(10.0))
        self.assertFalse(self._alerts())

    def test_a_sample_for_an_unknown_instance_is_skipped(self):
        """An unattributable alert is worse than no alert."""
        self.rule._sync_alerts([({"instance_id": "999999999"}, 600.0)])
        self.assertFalse(self._alerts())

    def test_a_suppressed_instance_never_alerts(self):
        """The sleeping-instance trap.

        An instance a plan puts to sleep stops every container on
        purpose, so cAdvisor goes quiet and a naive rule concludes it is
        down. Every Free instance would then raise a critical alert
        nightly, and operators would learn to ignore the one that
        matters. Core asks the instance; the SaaS layer is what knows
        about sleeping.
        """
        self.patch(
            type(self.instance), "_metric_alerts_suppressed",
            lambda inst: True,
        )
        self.rule._sync_alerts(self._sample(600.0))
        self.assertFalse(self._alerts())

    def test_suppression_also_clears_an_alert_already_raised(self):
        """Going to sleep must clear the alert, not freeze it on screen."""
        self.rule._sync_alerts(self._sample(600.0))
        self.assertTrue(self._alerts())
        self.patch(
            type(self.instance), "_metric_alerts_suppressed",
            lambda inst: True,
        )
        self.rule._sync_alerts(self._sample(600.0))
        self.assertFalse(self._alerts())
