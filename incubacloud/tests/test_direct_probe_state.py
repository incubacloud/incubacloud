"""When the platform's hosts and panels leave the CDN, and come back.

A direct name starts ``unknown``; one good probe makes it ``ok``. Two
failed probes in a row make it ``failing``; one failed DNS check does at
once, and only the job's complete probe (which reads DNS) clears that.
From ``failing`` it takes two good probes and 30 minutes. The state is
written on a cursor of its own and only when it moves; a state that holds
writes no column of the settings at all: the time of the last probe lives
in a system parameter, so that a probe every few minutes never competes
with a save of the Settings screen for the one settings row.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.models import _direct_probe, cloud_settings_direct
from odoo.addons.incubacloud.models._concurrency import read_committed_cursor
from odoo.addons.incubacloud.tests._direct import make_host

_T0 = datetime(2026, 10, 27, 12, 0, 0)
_OK = {"ok": True, "layer": None, "source": "job"}
_LIGHT_OK = {"ok": True, "layer": None, "source": "light"}
_TLS = {"ok": False, "layer": "tls", "source": "light"}
_DNS = {"ok": False, "layer": "dns", "source": "job"}
_PREFIX = "metrics_central_direct"


def _run(outcomes, start="unknown", step=timedelta(minutes=3)):
    """Feed *outcomes* to ``advance`` from *start*; return every state."""
    current = {"state": start, "failures": 0, "successes": 0,
               "failing_since": False, "dns_bad": False}
    states = []
    for index, outcome in enumerate(outcomes):
        current = _direct_probe.advance(current, outcome, _T0 + index * step)
        states.append(current["state"])
    return states


class TestTheStateMachine(BaseCase):

    def test_one_good_probe_takes_an_unknown_name_to_ok(self):
        self.assertEqual(_run([_OK]), ["ok"])

    def test_one_failure_is_not_enough_two_are(self):
        self.assertEqual(_run([_OK, _TLS, _TLS]), ["ok", "ok", "failing"])

    def test_a_dns_failure_fails_at_once(self):
        self.assertEqual(_run([_OK, _DNS]), ["ok", "failing"])

    def test_light_successes_do_not_clear_a_dns_failure(self):
        states = _run([_OK, _DNS] + [_LIGHT_OK] * 20, step=timedelta(minutes=5))
        self.assertEqual(states[-1], "failing")

    def test_the_job_clears_dns_and_two_successes_after_30_minutes_bring_it_back(self):
        states = _run([_OK, _DNS] + [_OK] * 12, step=timedelta(minutes=3))
        # Failing at 3 min; two successes at 6 and 9, but not 30 minutes yet.
        self.assertEqual(states[2:4], ["failing", "failing"])
        self.assertEqual(states[-1], "ok")
        self.assertEqual(states.index("ok", 2), 11)  # 33 min after the start

    def test_back_from_failing_needs_two_successes_in_a_row(self):
        states = _run([_OK, _TLS, _TLS, _OK, _TLS, _OK], step=timedelta(minutes=40))
        self.assertEqual(states[-1], "failing")

    def test_a_state_that_holds_moves_no_counter(self):
        current = {"state": "ok", "failures": 0, "successes": 2,
                   "failing_since": False, "dns_bad": False}
        self.assertEqual(_direct_probe.advance(current, _OK, _T0), current)


class TestTheProbeOutput(BaseCase):

    def test_a_good_probe(self):
        outcome = _direct_probe.parse_probe(
            "IC_DIRECT_DNS ok 46.4.0.60\nIC_DIRECT_TLS ok\n"
            "IC_DIRECT_CERT 2027-01-25T10:00:00Z\n"
            "IC_DIRECT_ROUTE POST /w/api/v1/write 401 ok\n"
            "IC_DIRECT_ROUTE GET /flags 403 ok\n"
        )
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["cert_expires"], datetime(2027, 1, 25, 10, 0, 0))
        self.assertEqual(outcome["source"], "job")

    def test_each_layer_is_named(self):
        for output, layer in (
            ("IC_DIRECT_DNS fail resolves to 104.21.0.1\nIC_DIRECT_TLS ok\n", "dns"),
            ("IC_DIRECT_DNS ok 46.4.0.60\nIC_DIRECT_TLS fail tls curl 60\n", "tls"),
            ("IC_DIRECT_DNS ok 46.4.0.60\nIC_DIRECT_TLS fail connect curl 7\n",
             "connect"),
            (
                (
                    "IC_DIRECT_DNS ok 46.4.0.60\nIC_DIRECT_TLS ok\n"
                    "IC_DIRECT_ROUTE GET /flags 200 fail\n"
                ),
                "route",
            ),
            ("", "connect"),
        ):
            with self.subTest(layer=layer):
                outcome = _direct_probe.parse_probe(output)
                self.assertFalse(outcome["ok"])
                self.assertEqual(outcome["layer"], layer)

    def test_the_list_is_compared_by_what_it_covers(self):
        sent = ["10.0.0.0/8", "46.4.0.20/32", "46.4.0.21/32"]
        expected = _direct_probe.covered(sent)
        # What a host prints back depends on its nft: 1.0.2 and 1.1.3 print
        # CIDR (``46.4.0.20/31``), an older one a range.
        for printed in (
            "IC_DIRECT_ALLOW 10.0.0.0/8, 46.4.0.20/31",
            "IC_DIRECT_ALLOW 10.0.0.0/8, 46.4.0.20-46.4.0.21",
            "IC_DIRECT_ALLOW 10.0.0.0/8, 46.4.0.20, 46.4.0.21",
        ):
            with self.subTest(printed=printed):
                read = _direct_probe.allow_entries(f"x\n{printed}\n")
                self.assertEqual(_direct_probe.covered(read), expected)
        self.assertIsNone(_direct_probe.allow_entries("nothing"))


class TestTheSettingsKeepTheState(TransactionCase):

    def setUp(self):
        self.registry_enter_test_mode()
        super().setUp()
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.write({
            "metrics_central_direct_host": "direct.metrics.example.test",
            **_direct_probe.reset_values(_PREFIX),
        })
        self.Alert = self.env["cloud.alert"].sudo()
        # The cron a state change triggers: a copy of production has it off,
        # and an inactive cron takes no trigger for "now".
        self.env.ref("incubacloud.cron_reconcile_observability").sudo().active = True

    def _apply(self, outcome, cert=None):
        """Apply *outcome* as a job would: from another transaction.

        What the test wrote is flushed first for the job's cursor to see
        it, and the test's cache dropped after, as a later request would
        have none.
        """
        self.env.flush_all()
        result = self.settings._direct_probe_apply(
            _PREFIX, dict(outcome, cert_expires=cert, detail="x"),
        )
        self.env.invalidate_all()
        return result

    def _down(self):
        return self.Alert.search([
            ("code", "=", "direct_path_down"), ("state", "=", "active"),
            ("dedup_key", "=", "direct.metrics.example.test"),
        ])

    def test_failing_raises_a_critical_alert_and_ok_closes_it(self):
        self._apply(_OK)
        self._apply(_TLS)
        self._apply(_TLS)
        self.settings.invalidate_recordset()
        self.assertEqual(self.settings.metrics_central_direct_state, "failing")
        self.assertEqual(self._down().level, "critical")
        self.assertIn("(tls)", self._down().message)
        self.settings.metrics_central_direct_failing_since = (
            fields.Datetime.now() - timedelta(hours=1)
        )
        self._apply(_OK)
        self._apply(_OK)
        self.settings.invalidate_recordset()
        self.assertEqual(self.settings.metrics_central_direct_state, "ok")
        self.assertFalse(self._down())

    def test_a_state_that_holds_writes_no_settings_column(self):
        self._apply(_OK)
        self._apply(_OK)
        cls = type(self.settings)
        with patch.object(cls, "write", autospec=True, side_effect=cls.write) as write:
            self._apply(_OK)
        write.assert_not_called()

    def test_the_time_of_the_probe_is_a_system_parameter(self):
        """Not a column: it advances at every probe, and a settings row
        written that often would clash with a save of the Settings screen."""
        field = self.env["cloud.settings"]._fields[f"{_PREFIX}_checked_at"]
        self.assertFalse(field.store)
        self.assertTrue(field.compute)
        self.assertTrue(field.readonly)
        key = f"incubacloud.direct_checked_at.{_PREFIX}"
        params = self.env["ir.config_parameter"].sudo()
        params.set_param(key, "2026-01-01 00:00:00")
        self._apply(_OK)
        self.settings.invalidate_recordset()
        stamp = fields.Datetime.to_datetime(params.get_param(key))
        self.assertGreater(stamp, datetime(2026, 10, 1))
        self.assertEqual(self.settings.metrics_central_direct_checked_at, stamp)

    def test_it_writes_on_a_read_committed_cursor(self):
        with patch.object(
            cloud_settings_direct, "read_committed_cursor", wraps=read_committed_cursor,
        ) as cursor:
            self._apply(_OK)
        cursor.assert_called_once()

    def test_a_certificate_with_less_than_21_days_warns(self):
        now = fields.Datetime.now()
        expiring = [
            ("code", "=", "direct_certificate_expiring"), ("state", "=", "active"),
        ]
        self._apply(_OK, cert=now + timedelta(days=22))
        self.assertFalse(self.Alert.search(expiring))
        self._apply(_OK, cert=now + timedelta(days=20))
        self.assertTrue(self.Alert.search(expiring))

    def test_a_state_change_converges_at_once(self):
        cron = self.env.ref("incubacloud.cron_reconcile_observability")
        triggers = self.env["ir.cron.trigger"].sudo()
        before = triggers.search_count([("cron_id", "=", cron.id)])
        self._apply(_OK)
        self.assertGreater(triggers.search_count([("cron_id", "=", cron.id)]), before)

    def test_a_new_name_starts_over(self):
        self._apply(_OK)
        self._apply(_TLS)
        self._apply(_TLS)
        self.settings.write(
            {"metrics_central_direct_host": "direct2.metrics.example.test"},
        )
        self.assertEqual(self.settings.metrics_central_direct_state, "unknown")
        self.assertEqual(self.settings.metrics_central_direct_failures, 0)
        self.assertFalse(self._down())
        self.settings.invalidate_recordset()
        self.assertFalse(self.settings.metrics_central_direct_checked_at)

    def test_saving_the_same_name_keeps_the_state(self):
        self._apply(_OK)
        self.settings.write(
            {"metrics_central_direct_host": "direct.metrics.example.test"},
        )
        self.assertEqual(self.settings.metrics_central_direct_state, "ok")


class TestADirectNameIsTwoLevelsAndUncovered(TransactionCase):

    def test_a_url_is_refused(self):
        settings = self.env["cloud.settings"].sudo()._get_system()
        with self.assertRaisesRegex(ValidationError, "no https://"), \
                self.env.cr.savepoint():
            settings.metrics_central_direct_host = (
                "https://direct.metrics.example.test/"
            )
            settings.flush_recordset()

    def test_a_name_the_default_certificate_covers_is_refused(self):
        make_host(self.env, "dn-host", "46.4.0.61", direct_port=9443)
        settings = self.env["cloud.settings"].sudo()._get_system()
        with patch.object(
            type(self.env["cloud.host"]), "_tls_default_covers", autospec=True,
            side_effect=lambda host, name: name == "metrics.example.test",
        ), self.assertRaisesRegex(
            ValidationError, "covered by the host's default certificate",
        ), self.env.cr.savepoint():
            settings.metrics_central_direct_host = "metrics.example.test"
            settings.flush_recordset()
