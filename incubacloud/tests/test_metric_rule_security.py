"""The servers' security rules, and the rules' texts in the reader's language.

Plan #84, cut C5: four PromQL rules over Fluent Bit's counters, the web
login one per instance and with the addresses with the most attempts in
its text (read only when it fires, ten reads a pass at most), and the
name and message of every rule translatable, a translation with broken
markers falling back to English (review M6).

The backend is never reached: samples are handed to ``_sync_alerts``, and
the log read is an autospec of the hook that joins it.
"""
import re
from unittest.mock import patch

import polib

from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger
from odoo.tools.misc import file_path

from odoo.addons.incubacloud.models.cloud_metric_rule import (
    WEBLOGIN_CODE,
    WEBLOGIN_LOOKUPS_PER_PASS,
    WEBLOGIN_MINUTES,
    top_sources,
)

_MODULE = "odoo.addons.incubacloud.models.cloud_metric_rule"

#: The four rules of the cut, by XML id, with their level and scope.
SECURITY_RULES = {
    "metric_rule_security_ssh_bruteforce": ("security_ssh_bruteforce", "warning", "host"),
    "metric_rule_security_f2b_ineffective": ("security_f2b_ineffective", "critical", "host"),
    "metric_rule_security_weblogin_bruteforce": (WEBLOGIN_CODE, "warning", "instance"),
    "metric_rule_security_logs_silent": ("security_logs_silent", "warning", "host"),
}

#: What any rule's message may hold besides text (``_MARKERS``).
_MARKER = re.compile(r"%%|%\((?:host|value|sources)\)s")

CENTRAL = {
    "base_url": "https://central.example.com/r", "token": "t",
    "user": "acct", "verify": True,
}


class _RuleCase(TransactionCase):
    """A host, an instance on it and no other rule active."""

    def setUp(self):
        """One host and one instance; every rule archived, no alert open."""
        super().setUp()
        self.Rule = self.env["cloud.metric.rule"].sudo()
        self.Rule.search([]).write({"active": False})
        self.env["cloud.alert.suspicion"].sudo().search([]).unlink()
        self.host = self.env["cloud.host"].create({
            "name": "sec-host", "ip_address": "10.0.0.84", "user": "root",
            "wildcard_domain": "sec.example.com",
        })
        self.instance = self._instance("acme-prod")

    def _instance(self, name):
        """A production instance *name* on the case's host, its own project's."""
        project = self.env["cloud.project"].create({"name": f"P {name}"})
        return self.env["cloud.instance"].create({
            "name": name, "project_id": project.id,
            "environment": "production", "host_id": self.host.id,
            "odoo_version": "19.0",
        })

    def _shipped(self, xmlid):
        """The shipped rule *xmlid*, active and confirming on one sample."""
        rule = self.env.ref("incubacloud." + xmlid).sudo()
        rule.write({"active": True, "confirm_cycles": 1})
        return rule

    def _alerts(self, code):
        """The active alerts of *code*."""
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", code), ("state", "=", "active"),
        ])


class TestSecurityRulesShipped(TransactionCase):
    """The four rules load as the plan says."""

    def test_each_rule_loads_and_its_expression_carries_host_id(self):
        """Level, scope and ``host_id`` in every grouping."""
        for xmlid, (code, level, scope) in SECURITY_RULES.items():
            rule = self.env.ref("incubacloud." + xmlid)
            self.assertEqual(rule.code, code)
            self.assertEqual(rule.level, level, code)
            self.assertEqual(rule.scope, scope, code)
            self.assertEqual(rule.comparator, "gt", code)
            groups = re.findall(r"\bby\s*\(([^)]*)\)", rule.expression)
            self.assertTrue(groups, code)
            for group in groups:
                self.assertIn("host_id", group, code)

    def test_every_target_has_a_zero_to_resolve_on(self):
        """A counter that vanishes with a restart does not leave an alert open.

        The evaluator resolves only a target the answer names; each rule
        ends in a 0 for the targets seen recently.
        """
        for xmlid in SECURITY_RULES:
            rule = self.env.ref("incubacloud." + xmlid)
            self.assertRegex(rule.expression, r" or \(max by \([^)]*\) \(\w+\(.*\* 0\)+$", xmlid)

    def test_the_web_login_rule_is_per_instance_without_instance_zero(self):
        """``instance_id="0"`` (no instance identified) is never summed."""
        rule = self.env.ref("incubacloud.metric_rule_security_weblogin_bruteforce")
        self.assertIn("by (host_id, instance_id)", rule.expression)
        self.assertEqual(rule.expression.count('ic_weblogin_failed_total{instance_id!="0"}'), 2)
        self.assertIn("%(sources)s", rule.message)

    def test_the_silence_rule_sees_lines_read_and_not_delivered(self):
        """Review M3: up alone says Fluent Bit lives, not that Loki takes it."""
        expression = self.env.ref("incubacloud.metric_rule_security_logs_silent").expression
        self.assertIn('max_over_time(up{job="security"}[15m])) < bool 1', expression)
        self.assertIn(
            'increase(fluentbit_output_dropped_records_total{name=~"loki.*"}[30m])) > bool 0',
            expression,
        )
        self.assertIn(
            '(sum by (host_id) (increase(ic_security_lines_total{source=~"sshd|sudo|weblogin"}'
            '[30m])) > bool 0) * on (host_id) (max by (host_id) (increase('
            'fluentbit_output_proc_records_total{name=~"loki.*"}[30m])) == bool 0)',
            expression,
        )
        # Each condition is kept where it holds before the "or".
        self.assertEqual(expression.count(" > 0 or "), 3)


class TestSecurityRuleJudgement(_RuleCase):
    """The rules judged on one answer, as the cron hands it over."""

    def test_a_breach_raises_and_a_quiet_window_resolves(self):
        """The SSH rule over 100 raises on the host; its 0 resolves it."""
        rule = self._shipped("metric_rule_security_ssh_bruteforce")
        labels = {"host_id": str(self.host.id)}
        rule._sync_alerts([(labels, 240.0)], central=CENTRAL)
        alert = self._alerts("security_ssh_bruteforce")
        self.assertEqual(alert.host_id, self.host)
        self.assertIn("sec-host has received 240 failed SSH logins", alert.message)
        rule._sync_alerts([(labels, 0.0)], central=CENTRAL)
        self.assertFalse(self._alerts("security_ssh_bruteforce"))

    def test_only_the_servers_in_the_answer_are_judged(self):
        """With the switch off there is no counter, so no sample and no alert."""
        rule = self._shipped("metric_rule_security_ssh_bruteforce")
        other = self.env["cloud.host"].create({
            "name": "other", "ip_address": "10.0.0.85", "user": "root",
            "wildcard_domain": "other.example.com",
        })
        rule._sync_alerts([({"host_id": str(other.id)}, 500.0)], central=CENTRAL)
        alerts = self._alerts("security_ssh_bruteforce")
        self.assertEqual(alerts.host_id, other)

    def test_the_silence_rule_raises_on_its_one(self):
        """1 is a silent server, 0 one whose lines arrive."""
        rule = self._shipped("metric_rule_security_logs_silent")
        labels = {"host_id": str(self.host.id)}
        rule._sync_alerts([(labels, 1.0)])
        self.assertTrue(self._alerts("security_logs_silent"))
        rule._sync_alerts([(labels, 0.0)])
        self.assertFalse(self._alerts("security_logs_silent"))

    def test_the_silence_rule_confirms_on_a_second_reading(self):
        """As shipped, one reading of 1 raises nothing; the second does."""
        rule = self.env.ref("incubacloud.metric_rule_security_logs_silent").sudo()
        self.assertEqual(rule.confirm_cycles, 2)
        rule.active = True
        labels = {"host_id": str(self.host.id)}
        rule._sync_alerts([(labels, 1.0)])
        self.assertFalse(self._alerts("security_logs_silent"))
        rule._sync_alerts([(labels, 1.0)])
        self.assertTrue(self._alerts("security_logs_silent"))

    def test_one_target_is_judged_on_its_worst_sample(self):
        """A server renamed in the window answers twice; the breach is kept."""
        rule = self._shipped("metric_rule_security_ssh_bruteforce")
        labels = {"host_id": str(self.host.id)}
        rule._sync_alerts([(labels, 400.0), (labels, 0.0)], central=CENTRAL)
        self.assertTrue(self._alerts("security_ssh_bruteforce"))
        rule._sync_alerts([(labels, 0.0), (labels, 400.0)], central=CENTRAL)
        self.assertTrue(self._alerts("security_ssh_bruteforce"))


class TestWebLoginAlert(_RuleCase):
    """Per instance, named by the panel, with the addresses with most attempts."""

    def setUp(self):
        """The shipped web-login rule, confirming on one sample."""
        super().setUp()
        self.rule = self._shipped("metric_rule_security_weblogin_bruteforce")
        self.model = type(self.env["cloud.metric.rule"])

    def _sample(self, instance, value):
        """A sample of the rule for *instance*."""
        return ({"host_id": str(self.host.id), "instance_id": str(instance.id)}, value)

    def _reader(self, answer=None, error=None):
        """Patch the log read with an autospec answering *answer*."""
        return patch.object(
            self.model, "_weblogin_top_sources", autospec=True,
            return_value=answer, side_effect=error,
        )

    def test_the_alert_is_raised_on_the_instance_with_its_top_addresses(self):
        """Its name, its figure and «203.0.113.5 (41)», IPv6 by /64."""
        answer = [
            ("198.51.100.7", 12), ("203.0.113.5", 41), ("2001:db8:1:2::7", 5),
            ("2001:db8:1:2:ffff::8", 4), ("192.0.2.9", 1),
        ]
        with self._reader(answer) as reader:
            self.rule._sync_alerts([self._sample(self.instance, 63.0)], central=CENTRAL)
        alert = self._alerts(WEBLOGIN_CODE)
        self.assertEqual(alert.instance_id, self.instance)
        self.assertEqual(
            alert.message,
            "acme-prod has received 63 failed web logins in 10 minutes. Most "
            "attempts: 203.0.113.5 (41), 198.51.100.7 (12), 2001:db8:1:2::/64 "
            "(9). Check that its users have strong passwords or two-factor "
            "authentication; the detail is in Metrics → Security of its server.",
        )
        reader.assert_called_once_with(
            self.rule, self.host, self.instance.id, WEBLOGIN_MINUTES, CENTRAL,
        )

    def test_the_payload_holds_addresses_and_counts_never_a_line(self):
        """What is not an address, a line an attacker shaped, is left out."""
        answer = [
            ("203.0.113.5", 41),
            ("weblogin failed ip=6.6.6.6 instance=1 t=x", 99),
            ("203.0.113.6 (1000), evil", 50),
            ("198.51.100.0/24", 30),
        ]
        with self._reader(answer):
            self.rule._sync_alerts([self._sample(self.instance, 63.0)], central=CENTRAL)
        alert = self._alerts(WEBLOGIN_CODE)
        self.assertEqual(alert.payload, {
            "sources": [{"address": "203.0.113.5", "count": 41}],
            "behind_cdn": False,
        })
        self.assertNotIn("6.6.6.6", alert.message)
        self.assertNotIn("evil", alert.message)

    def test_instance_zero_raises_nothing_and_reads_nothing(self):
        """An attempt no instance was identified for is the viewer's, not an alert's."""
        with self._reader([("203.0.113.5", 41)]) as reader:
            self.rule._sync_alerts([
                ({"host_id": str(self.host.id), "instance_id": "0"}, 500.0),
            ], central=CENTRAL)
        self.assertFalse(self._alerts(WEBLOGIN_CODE))
        reader.assert_not_called()

    def test_without_data_the_alert_goes_out_without_addresses(self):
        """The central busy or failing: the figure alone."""
        with self._reader(None):
            self.rule._sync_alerts([self._sample(self.instance, 63.0)], central=CENTRAL)
        alert = self._alerts(WEBLOGIN_CODE)
        self.assertIn("63 failed web logins in 10 minutes. Check", alert.message)
        self.assertNotIn("Most attempts", alert.message)
        self.assertEqual(alert.payload, {"sources": [], "behind_cdn": False})

    @mute_logger(_MODULE)
    def test_a_reader_that_raises_costs_the_alert_its_addresses_only(self):
        """The reader answers None rather than raise; one that raises is caught."""
        other = self._instance("acme-staging")
        with self._reader(error=ValueError("boom")):
            self.rule._sync_alerts([
                self._sample(self.instance, 63.0), self._sample(other, 70.0),
            ], central=CENTRAL)
        self.assertEqual(len(self._alerts(WEBLOGIN_CODE)), 2)

    def test_twelve_instances_over_the_threshold_read_ten_the_worst_first(self):
        """Ten reads a pass, the most attempts first; the rest without addresses."""
        instances = [self._instance(f"i{n:02d}") for n in range(12)]
        samples = [self._sample(inst, 60.0 + 10 * n) for n, inst in enumerate(instances)]
        with self._reader([("203.0.113.5", 41)]) as reader:
            self.rule._sync_alerts(list(reversed(samples[6:])) + samples[:6], central=CENTRAL)
        self.assertEqual(reader.call_count, WEBLOGIN_LOOKUPS_PER_PASS)
        read = [call.args[2] for call in reader.call_args_list]
        self.assertEqual(read, [inst.id for inst in reversed(instances[2:])])
        alerts = self._alerts(WEBLOGIN_CODE)
        self.assertEqual(len(alerts), 12)
        for alert in alerts:
            named = "203.0.113.5 (41)" in alert.message
            self.assertEqual(named, alert.instance_id not in instances[0] | instances[1])

    def test_behind_a_cdn_nothing_is_read_and_the_text_says_so(self):
        """The proxy sees the CDN's addresses: none is named, Loki is not asked."""
        self.host.behind_cdn = True
        with self._reader([("203.0.113.5", 41)]) as reader:
            self.rule._sync_alerts([self._sample(self.instance, 63.0)], central=CENTRAL)
        reader.assert_not_called()
        alert = self._alerts(WEBLOGIN_CODE)
        self.assertIn("behind a CDN", alert.message)
        self.assertNotIn("203.0.113.5", alert.message)
        self.assertEqual(alert.payload, {"sources": [], "behind_cdn": True})

    def test_a_template_without_sources_still_alerts(self):
        """An operator's text without ``%(sources)s`` goes out without addresses."""
        self.rule.message = "%(host)s: %(value)s failed web logins."
        with self._reader([("203.0.113.5", 41)]):
            self.rule._sync_alerts([self._sample(self.instance, 63.0)], central=CENTRAL)
        self.assertEqual(self._alerts(WEBLOGIN_CODE).message, "acme-prod: 63 failed web logins.")

    def test_nothing_is_read_below_the_threshold_or_without_a_central(self):
        """No read while no instance is over it; none without the central's details."""
        with self._reader([("203.0.113.5", 41)]) as reader:
            self.rule._sync_alerts([self._sample(self.instance, 12.0)], central=CENTRAL)
            self.rule._sync_alerts([self._sample(self.instance, 63.0)])
        reader.assert_not_called()
        self.assertTrue(self._alerts(WEBLOGIN_CODE))

    def test_the_other_rules_do_not_call_the_hook(self):
        """Neither a host rule nor another instance rule reads the logs."""
        ssh = self._shipped("metric_rule_security_ssh_bruteforce")
        oom = self._shipped("metric_rule_instance_oom")
        with self._reader([("203.0.113.5", 41)]) as reader:
            ssh._sync_alerts([({"host_id": str(self.host.id)}, 500.0)], central=CENTRAL)
            oom._sync_alerts([({"instance_id": str(self.instance.id)}, 3.0)], central=CENTRAL)
        reader.assert_not_called()
        self.assertFalse(self._alerts("security_ssh_bruteforce").payload)


class TestWebLoginReaderJoin(_RuleCase):
    """The hook reads with ``logs.weblogin_top_sources`` and the central's details."""

    def setUp(self):
        """The shipped web-login rule."""
        super().setUp()
        self.rule = self._shipped("metric_rule_security_weblogin_bruteforce")

    def _reader(self, answer):
        """Patch the log reader's function with an autospec answering *answer*."""
        return patch(
            "odoo.addons.incubacloud.models.logs.weblogin_top_sources",
            autospec=True, return_value=answer,
        )

    def test_the_reader_gets_the_central_the_server_and_the_instance(self):
        """``base_url``, the host's id, the instance's id, the window and the credential."""
        with self._reader([("203.0.113.5", 41), ("2001:db8:1:2::7", 5)]) as reader:
            self.rule._sync_alerts([
                ({"host_id": str(self.host.id), "instance_id": str(self.instance.id)}, 63.0),
            ], central=CENTRAL)
        reader.assert_called_once_with(
            CENTRAL["base_url"], self.host.id, self.instance.id,
            minutes=WEBLOGIN_MINUTES, token="t", user="acct", verify=True,
        )
        alert = self._alerts(WEBLOGIN_CODE)
        self.assertIn("Most attempts: 203.0.113.5 (41), 2001:db8:1:2::/64 (5).", alert.message)

    def test_a_reader_with_nothing_to_say_leaves_the_figure_alone(self):
        """None from the reader: the alert goes out without addresses."""
        with self._reader(None):
            self.rule._sync_alerts([
                ({"host_id": str(self.host.id), "instance_id": str(self.instance.id)}, 63.0),
            ], central=CENTRAL)
        self.assertNotIn("Most attempts", self._alerts(WEBLOGIN_CODE).message)


class TestTopSources(TransactionCase):
    """The addresses an alert names, from what the reader counted."""

    def test_ipv6_is_grouped_by_64_and_the_top_three_kept(self):
        """Ranked by count, a tie by address; IPv4 seen through IPv6 as IPv4."""
        self.assertEqual(
            top_sources([
                ("2001:db8:a:b::1", 3), ("2001:db8:a:b::2", 3),
                ("::ffff:192.0.2.1", 6), ("198.51.100.9", 6), ("203.0.113.1", 1),
            ]),
            [("192.0.2.1", 6), ("198.51.100.9", 6), ("2001:db8:a:b::/64", 6)],
        )

    def test_what_is_not_an_address_or_a_count_is_left_out(self):
        """Networks wider than one address or a /64, and counts not positive."""
        self.assertEqual(
            top_sources([
                ("10.0.0.0/8", 9), ("2001:db8::/48", 9), ("x", 9),
                ("192.0.2.1", 0), ("192.0.2.2", True), ("192.0.2.3", 2.5),
                ("192.0.2.4",), None,
            ]),
            [],
        )
        self.assertEqual(top_sources(None), [])


class TestRuleTexts(_RuleCase):
    """Name and message translatable; the text in the env's language (M6)."""

    _SPANISH = "El disco de %(host)s está al %(value)s%% de su capacidad."

    def setUp(self):
        """Spanish active; a disk rule of the case's own."""
        super().setUp()
        self.env["res.lang"]._activate_lang("es_ES")
        self.rule = self.Rule.create({
            "name": "Disk", "code": "test_disk_text", "expression": "disk_pct",
            "threshold": 90, "message": "Disk on %(host)s is %(value)s%% full.",
            "confirm_cycles": 1,
        })
        self.labels = {"host_id": str(self.host.id)}

    def _message(self, lang):
        """The alert's message after one breach judged in *lang*."""
        self.rule.with_context(lang=lang)._sync_alerts([(self.labels, 95.0)])
        return self._alerts("test_disk_text").message

    def test_name_and_message_are_translatable(self):
        """Both fields, the ones the panel shows."""
        fields_ = self.env["cloud.metric.rule"]._fields
        self.assertTrue(fields_["name"].translate)
        self.assertTrue(fields_["message"].translate)

    def test_in_spanish_the_message_comes_out_in_spanish(self):
        """The cron's language is the alert's."""
        self.rule.update_field_translations("message", {"es_ES": self._SPANISH})
        self.assertEqual(self._message("es_ES"), "El disco de sec-host está al 95% de su capacidad.")

    def test_without_a_language_the_message_is_english(self):
        """A cron runs without one: English."""
        self.rule.update_field_translations("message", {"es_ES": self._SPANISH})
        self.assertEqual(self._message(None), "Disk on sec-host is 95% full.")

    def test_a_translation_with_broken_markers_falls_back_to_english(self):
        """An unknown marker, a lone %, a marker without its type."""
        for broken in (
            "El disco de %(hots)s está lleno.",
            "El disco de %(host)s está al %(value)s% de su capacidad.",
            "El disco de %(host) está lleno.",
            "El disco de %s está lleno.",
        ):
            self.rule.update_field_translations("message", {"es_ES": broken})
            self.assertEqual(self._message("es_ES"), "Disk on sec-host is 95% full.", broken)

    @mute_logger(_MODULE)
    def test_an_english_text_broken_by_an_edit_still_alerts(self):
        """The markers it knows are filled; the pass is not lost."""
        self.rule.with_context(lang="en_US").message = "Disk on %(host)s: %(what)s"
        self.assertEqual(self._message(None), "Disk on sec-host: %(what)s")

    def test_the_web_login_fragment_is_in_the_env_s_language(self):
        """The addresses' sentence goes through the catalogue too."""
        rule = self._shipped("metric_rule_security_weblogin_bruteforce")
        with patch.object(
            type(self.env["cloud.metric.rule"]), "_weblogin_top_sources",
            autospec=True, return_value=[("203.0.113.5", 41)],
        ):
            sources, _payload = rule.with_context(lang="es_ES")._weblogin_extra(
                self.instance, {"host_id": str(self.host.id)}, CENTRAL,
                {"lookups": 0},
            )
        self.assertEqual(sources, " Más intentos: 203.0.113.5 (41).")

    def test_the_columns_keep_their_english(self):
        """Stored as translations, every rule still reads its English text."""
        self.env.cr.execute("""
            SELECT column_name, data_type FROM information_schema.columns
             WHERE table_name = 'cloud_metric_rule'
               AND column_name IN ('name', 'message')
        """)
        self.assertEqual(dict(self.env.cr.fetchall()), {"name": "jsonb", "message": "jsonb"})
        for rule in self.Rule.with_context(active_test=False, lang="en_US").search([]):
            self.assertTrue(rule.name, rule.code)
            self.assertTrue(rule.message, rule.code)

    def test_every_shipped_rule_is_translated_with_its_markers(self):
        """es.po carries each rule's name and message, markers intact."""
        catalogue = polib.pofile(file_path("incubacloud/i18n/es.po"))
        translations = {}
        for entry in catalogue:
            if entry.obsolete or "fuzzy" in entry.flags or not entry.msgstr:
                continue
            for occurrence, _line in entry.occurrences:
                if occurrence.startswith("model:cloud.metric.rule,"):
                    translations[occurrence.removeprefix("model:cloud.metric.rule,")] = entry
        shipped = self.env["ir.model.data"].search([
            ("model", "=", "cloud.metric.rule"), ("module", "=", "incubacloud"),
        ])
        self.assertGreaterEqual(len(shipped), 17)
        for xmlid in shipped:
            rule = self.Rule.with_context(lang="en_US", active_test=False).browse(xmlid.res_id)
            for field in ("name", "message"):
                key = f"{field}:incubacloud.{xmlid.name}"
                self.assertIn(key, translations)
                entry = translations[key]
                self.assertEqual(entry.msgid, rule[field], key)
                self.assertEqual(
                    sorted(_MARKER.findall(entry.msgstr)),
                    sorted(_MARKER.findall(entry.msgid)), key,
                )
                self.assertTrue(rule._markers_ok(entry.msgstr), key)
