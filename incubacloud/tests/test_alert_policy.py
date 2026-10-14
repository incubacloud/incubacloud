"""The confirmation policy: which codes are measurements, and how many
sightings in a row make one an alert.

A producer that measures (a probe, a reading) raises its code and says
nothing about confirmation; ``cloud.alert.policy`` holds, per code, how
many sightings the alert needs and how fresh they must be. An event (a
job that failed, a key that changed) has no row and raises on the spot.
The producers do not carry the rule, so adding a measurement is one
data row, and reviewing which alerts confirm is reading one table.
"""
from datetime import timedelta

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class PolicyCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Alert = self.env["cloud.alert"].sudo()
        self.Policy = self.env["cloud.alert.policy"].sudo()
        self.Suspicion = self.env["cloud.alert.suspicion"].sudo()
        self.host = self.env["cloud.host"].create({
            "name": "HP", "ip_address": "10.0.0.4", "user": "root",
            "wildcard_domain": "hp.example.com",
        })

    def _active(self, code):
        """Return the active alerts with *code*."""
        return self.Alert.search([
            ("code", "=", code), ("state", "=", "active"),
        ])

    def _suspicions(self, code):
        """Return the suspicions with *code*."""
        return self.Suspicion.search([("code", "=", code)])


class TestTheTable(PolicyCase):

    def test_an_unknown_code_confirms_on_the_spot(self):
        self.assertEqual(self.Policy._for_code("job_failed"), (1, 900))

    def test_a_row_says_how_many_and_how_fresh(self):
        self.Policy.create({
            "code": "probe_x", "confirm_after": 3, "confirm_window": 600,
        })
        self.assertEqual(self.Policy._for_code("probe_x"), (3, 600))

    def test_a_pattern_covers_a_family_of_codes(self):
        self.Policy.create({
            "code": "probe_svc_*_lost", "confirm_after": 3,
            "confirm_window": 600,
        })
        self.assertEqual(self.Policy._for_code("probe_svc_db_lost"), (3, 600))
        self.assertEqual(self.Policy._for_code("probe_svc_lost"), (1, 900))

    def test_an_exact_row_beats_a_pattern(self):
        self.Policy.create({
            "code": "probe_svc_*_lost", "confirm_after": 3,
            "confirm_window": 600,
        })
        self.Policy.create({
            "code": "probe_svc_backup_lost", "confirm_after": 4,
            "confirm_window": 900,
        })
        self.assertEqual(
            self.Policy._for_code("probe_svc_backup_lost"), (4, 900),
        )

    def test_the_shipped_family_covers_every_companion(self):
        self.assertEqual(
            self.Policy._for_code("instance_service_backup_down"), (2, 900),
        )

    def test_a_code_has_one_row(self):
        self.Policy.create({"code": "probe_y", "confirm_after": 2})
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self.Policy.create({"code": "probe_y", "confirm_after": 3})

    def test_less_than_one_sighting_is_refused(self):
        with self.assertRaises(ValidationError):
            self.Policy.create({"code": "probe_z", "confirm_after": 0})

    def test_the_shipped_measurements_are_listed(self):
        """What 1.0.181 moved out of the producers' code."""
        for code in (
            "metrics_backend_unreachable", "instance_down", "disk_critical",
            "instance_service_db_down",
        ):
            with self.subTest(code=code):
                self.assertEqual(self.Policy._for_code(code)[0], 2)


class TestRaiseAlertReadsIt(PolicyCase):

    def setUp(self):
        super().setUp()
        self.Policy.create({
            "code": "probe_p", "confirm_after": 2, "confirm_window": 900,
        })

    def test_a_listed_code_is_a_suspicion_first(self):
        result = self.Alert.raise_alert("probe_p", "m", host=self.host)
        self.assertFalse(result)
        self.assertFalse(self._active("probe_p"))
        self.assertEqual(len(self._suspicions("probe_p")), 1)

    def test_the_second_sighting_raises(self):
        self.Alert.raise_alert("probe_p", "m", host=self.host)
        alert = self.Alert.raise_alert("probe_p", "m", host=self.host)
        self.assertTrue(alert)
        self.assertEqual(self._active("probe_p"), alert)
        self.assertFalse(self._suspicions("probe_p"))

    def test_an_unlisted_code_raises_on_the_spot(self):
        self.assertTrue(self.Alert.raise_alert("event_e", "m", host=self.host))

    def test_a_caller_that_says_so_wins_over_the_table(self):
        """Metric rules carry their own count per rule."""
        self.assertTrue(self.Alert.raise_alert(
            "probe_p", "m", host=self.host, confirm_after=1,
        ))

    def test_a_caller_window_wins_over_the_table(self):
        self.Alert.raise_alert(
            "probe_p", "m", host=self.host, confirm_window=60,
        )
        row = self._suspicions("probe_p")
        row.last_seen_at = fields.Datetime.now() - timedelta(seconds=120)
        self.Alert.raise_alert(
            "probe_p", "m", host=self.host, confirm_window=60,
        )
        self.assertFalse(self._active("probe_p"), "a stale sighting counted")
        self.assertEqual(self._suspicions("probe_p").occurrences, 1)

    def test_a_pattern_row_applies_through_raise_alert(self):
        self.Policy.create({
            "code": "probe_*_q", "confirm_after": 2, "confirm_window": 900,
        })
        self.assertFalse(self.Alert.raise_alert("probe_db_q", "m", host=self.host))
        self.assertTrue(self.Alert.raise_alert("probe_db_q", "m", host=self.host))

    def test_the_disk_probe_confirms_before_paging(self):
        """``disk_critical`` is raised by the SSH probe with no count of
        its own; the table gives it one."""
        self.assertFalse(self.Alert.raise_alert(
            "disk_critical", "Disk at 99%", level="critical", host=self.host,
        ))
        self.assertTrue(self.Alert.raise_alert(
            "disk_critical", "Disk at 99%", level="critical", host=self.host,
        ))
