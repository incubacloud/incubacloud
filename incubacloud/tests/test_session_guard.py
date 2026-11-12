"""A session that changes hands is closed, without asking anybody.

The requests are real ones, against the test server: Odoo's own session
check calls the guard, and what the guard does has to survive what Odoo
does after it — the request it refuses rolls its transaction back, and
the parallel requests of one page each load the session before the
first of them deletes it. Countries are given the way production gives
them, through ``net/real_ip`` and its proof, with the correction
installed for the test.
"""
import json
import threading
import time
from datetime import timedelta
from unittest.mock import patch

import requests

import odoo.http
from odoo import fields
from odoo.tests import tagged
from odoo.tests.common import BaseCase, HttpCase
from odoo.tools import config

from odoo.addons.incubacloud.models import cloud_session_guard as guard_module
from odoo.addons.incubacloud.models.cloud_session_guard import (
    PIN_KEY,
    judge_browser,
    judge_country,
)
from odoo.addons.incubacloud.net import real_ip

from .test_real_ip import EDGE, TRUSTED, _OnTheUncorrectedProxyFix

CHROME = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
#: Chrome again, a version later and on another system.
CHROME_ELSEWHERE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)
FIREFOX = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0"
)
CURL = "curl/8.5.0"
#: The visitors the CDN names, one per network.
HOME = "203.0.113.10"
CAFE = "203.0.113.20"
ABROAD = "198.51.100.30"
#: Two days, the value the platform's layers use.
TWO_DAYS = 2 * 24 * 3600
PARAM = "incubacloud.session_guard."


class TestJudging(BaseCase):
    """The matrix of values, without a request: only a difference
    between two values that mean something closes."""

    def test_countries(self):
        cases = [
            ("", "ES", "adopt"),
            ("", "", "none"),
            ("", "XX", "none"),
            ("", "T1", "none"),
            ("", "direct", "none"),
            ("ES", "ES", "same"),
            ("ES", "FR", "differs"),
            ("ES", "T1", "differs"),
            ("ES", "direct", "differs"),
            ("ES", "XX", "unknown"),
            ("ES", "", "unknown"),
        ]
        for pinned, seen, verdict in cases:
            with self.subTest(pinned=pinned, seen=seen):
                self.assertEqual(judge_country(pinned, seen), verdict)

    def test_browsers(self):
        cases = [
            ("", "chrome", "adopt"),
            ("", "unknown", "none"),
            ("chrome", "chrome", "same"),
            ("chrome", "firefox", "differs"),
            ("chrome", "unknown", "differs"),
        ]
        for pinned, seen, verdict in cases:
            with self.subTest(pinned=pinned, seen=seen):
                self.assertEqual(judge_browser(pinned, seen), verdict)


@tagged("-at_install", "post_install")
class TestSessionGuard(_OnTheUncorrectedProxyFix, HttpCase):
    """Driven over real HTTP, as a browser and a thief would."""

    def setUp(self):
        super().setUp()
        self.user = self.env["res.users"].create({
            "name": "Guarded", "login": "guarded", "password": "guarded-pw",
            "group_ids": [(6, 0, [self.env.ref("base.group_user").id])],
        })
        self.ICP = self.env["ir.config_parameter"].sudo()
        self.Closure = self.env["cloud.session.closure"].sudo()
        self.Hold = self.env["cloud.session.hold"].sudo()

    # -- helpers ---------------------------------------------------------

    def _switch(self, **modes):
        """Set the switches given, e.g. ``browser="close"``."""
        for signal, mode in modes.items():
            self.ICP.set_param(PARAM + signal, mode)

    def _behind_the_cdn(self):
        """Install the correction with the CDN's country, for this test."""
        self.assertTrue(real_ip.install({
            real_ip.HEADER_OPTION: "CF-Connecting-IP",
            real_ip.TRUSTED_OPTION: TRUSTED,
            real_ip.PROXY_OPTION: "",
            real_ip.COUNTRY_OPTION: "CF-IPCountry",
        }, odoo.http))
        self.startPatcher(patch.dict(config.options.maps[0], {"proxy_mode": True}))

    def _sign_in(self, **extra):
        """Open a session of the fixture user, traced as a browser's is.

        :return: the session id
        :rtype: str
        """
        self.authenticate(
            "guarded", "guarded-pw",
            session_extra={"_trace_disable": False, **extra},
        )
        return self.session.sid

    def _headers(self, agent, visitor=None, country=None):
        """Return a request's headers: through the CDN when *visitor* is
        given, stating *country* when that is not ``None``."""
        headers = {"User-Agent": agent, "Content-Type": "application/json"}
        if visitor:
            host = self.base_url().split("://", 1)[1]
            headers.update({
                "X-Forwarded-Host": host,
                "X-Forwarded-For": f"{visitor}, {EDGE}",
                "CF-Connecting-IP": visitor,
            })
            if country is not None:
                headers["CF-IPCountry"] = country
        return headers

    def _check(self, sid, agent=CHROME, visitor=None, country=None):
        """Ask ``/web/session/check`` with session *sid*, what the test
        wrote flushed first and read afresh after.

        :return: whether the session was still accepted
        :rtype: bool
        """
        self.env.flush_all()
        with self.allow_requests(all_requests=True):
            accepted = self._ask(sid, agent, visitor, country)
        self.env.invalidate_all()
        return accepted

    def _ask(self, sid, agent, visitor=None, country=None):
        """Ask ``/web/session/check`` with session *sid*, from any thread,
        inside ``allow_requests``: the test server refuses any other.

        :return: whether the session was still accepted
        :rtype: bool
        """
        answer = requests.post(
            self.base_url() + "/web/session/check",
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {}}),
            headers=self._headers(agent, visitor, country),
            cookies={"session_id": sid},
            timeout=30,
        ).json()
        if "error" in answer:
            self.assertEqual(answer["error"]["code"], 100, answer)
            return False
        return True

    def _stored(self, sid):
        """Return the session stored under *sid*, signed in or not."""
        return odoo.http.root.session_store.get(sid)

    def _rows(self, **domain):
        """Return the fixture user's rows matching *domain*."""
        return self.Closure.search([
            ("user_id", "=", self.user.id),
            *[(key, "=", value) for key, value in domain.items()],
        ])

    # -- core alone --------------------------------------------------------

    def test_core_alone_closes_nothing(self):
        """No parameter: every switch is off, as on somebody's ERP."""
        sid = self._sign_in()
        self.assertTrue(self._check(sid, CHROME))
        self.assertTrue(self._check(sid, FIREFOX))
        self.assertTrue(self._check(sid, CURL))
        self.assertFalse(self._rows())
        self.assertNotIn(PIN_KEY, self._stored(sid))

    # -- browser -----------------------------------------------------------

    def test_another_family_closes_the_session_for_everybody(self):
        self._switch(browser="close")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, CHROME))
        self.assertEqual(self._stored(sid)[PIN_KEY]["browser"], "chrome")
        self.assertFalse(self._check(sid, FIREFOX))
        # The owner, in their own Chrome, is out too: nobody can tell
        # which of the two is the thief.
        self.assertFalse(self._check(sid, CHROME))
        self.assertIsNone(self._stored(sid).uid)
        row = self._rows(signal="browser")
        self.assertEqual(
            (row.action, row.pinned_value, row.seen_value),
            ("closed", "chrome", "firefox"),
        )
        self.assertEqual(row.session_identifier, sid[:42])

    def test_another_version_or_system_of_the_same_family_stays(self):
        self._switch(browser="close")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, CHROME))
        self.assertTrue(self._check(sid, CHROME_ELSEWHERE))
        self.assertFalse(self._rows())

    def test_a_cookie_copied_into_a_script_closes(self):
        """What a stolen-cookie checker sends (review M2)."""
        self._switch(browser="close")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, CHROME))
        self.assertFalse(self._check(sid, CURL))
        self.assertEqual(self._rows().seen_value, "unknown")

    def test_a_script_session_never_pins_a_family(self):
        """Opened by something the parser cannot name, a session has no
        family to compare and takes the first one it sees."""
        self._switch(browser="close")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, CURL))
        self.assertEqual(self._stored(sid)[PIN_KEY]["browser"], "")
        self.assertTrue(self._check(sid, FIREFOX))
        self.assertEqual(self._stored(sid)[PIN_KEY]["browser"], "firefox")
        self.assertFalse(self._check(sid, CHROME))

    def test_log_only_logs(self):
        self._switch(browser="log")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, CHROME))
        self.assertTrue(self._check(sid, FIREFOX))
        self.assertTrue(self._check(sid, CHROME))
        self.assertEqual(self._rows().action, "logged")

    def test_a_session_signed_in_as_somebody_else_is_pinned_again(self):
        """Signing in does not clear a session's keys: what another user
        left in it is not compared with (review, low)."""
        self._switch(browser="close")
        sid = self._sign_in(**{PIN_KEY: {
            "uid": self.env.ref("base.user_admin").id,
            "country": "", "browser": "chrome", "limit": 0,
        }})
        self.assertTrue(self._check(sid, FIREFOX))
        self.assertEqual(
            self._stored(sid)[PIN_KEY],
            {"uid": self.user.id, "country": "", "browser": "firefox",
             "limit": 0},
        )

    def test_an_http_route_signs_out_as_an_expired_session_does(self):
        self._switch(browser="close")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, CHROME))
        self.env.flush_all()
        with self.allow_requests(all_requests=True):
            response = requests.get(
                self.base_url() + "/web/login", headers={"User-Agent": FIREFOX},
                cookies={"session_id": sid}, allow_redirects=False, timeout=30,
            )
        self.env.invalidate_all()
        self.assertIn(response.status_code, (302, 303))
        self.assertIn("/web/login", response.headers["Location"])
        self.assertIsNone(self._stored(sid).uid)
        self.assertEqual(self._rows().action, "closed")

    # -- country -----------------------------------------------------------

    def test_another_country_closes(self):
        self._behind_the_cdn()
        self._switch(country="close")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, visitor=HOME, country="ES"))
        self.assertEqual(self._stored(sid)[PIN_KEY]["country"], "ES")
        self.assertTrue(self._check(sid, visitor=CAFE, country="ES"))
        self.assertFalse(self._check(sid, visitor=ABROAD, country="FR"))
        row = self._rows(signal="country")
        self.assertEqual((row.pinned_value, row.seen_value), ("ES", "FR"))

    def test_a_country_the_cdn_could_not_tell_is_logged(self):
        self._behind_the_cdn()
        self._switch(country="close")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, visitor=HOME, country="ES"))
        self.assertTrue(self._check(sid, visitor=CAFE, country="XX"))
        self.assertTrue(self._check(sid, visitor=ABROAD))
        self.assertEqual(set(self._rows().mapped("action")), {"logged"})

    def test_tor_closes_a_session_with_a_country(self):
        """Review M2: Tor is where a stolen cookie is tried."""
        self._behind_the_cdn()
        self._switch(country="close")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, visitor=HOME, country="ES"))
        self.assertFalse(self._check(sid, visitor=ABROAD, country="T1"))

    def test_a_request_that_skipped_the_cdn_closes(self):
        """Review A1: straight to the origin, the country is silent, and
        a silent country must not be a way around the check."""
        self._behind_the_cdn()
        self._switch(country="close")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, visitor=HOME, country="ES"))
        self.assertFalse(self._check(sid))
        self.assertEqual(self._rows().seen_value, "direct")

    def test_a_session_opened_without_a_country_takes_the_first(self):
        self._behind_the_cdn()
        self._switch(country="close")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, visitor=HOME))
        self.assertEqual(self._stored(sid)[PIN_KEY]["country"], "")
        self.assertTrue(self._check(sid))
        self.assertTrue(self._check(sid, visitor=CAFE, country="PT"))
        self.assertEqual(self._stored(sid)[PIN_KEY]["country"], "PT")
        self.assertFalse(self._check(sid, visitor=ABROAD, country="ES"))

    # -- inactivity --------------------------------------------------------

    def _idle_trace(self, idle):
        """Return a trace last active *idle* seconds ago, from elsewhere."""
        last = int(time.time()) - idle
        return [{
            "platform": "linux", "browser": "chrome", "ip_address": "192.0.2.1",
            "first_activity": last, "last_activity": last,
        }]

    def test_a_session_idle_past_the_limit_is_closed_on_the_server(self):
        """Review M5: the cookie's own expiry is the browser's to keep."""
        self._switch(inactivity="close")
        self.ICP.set_param(PARAM + "inactivity_seconds", TWO_DAYS)
        sid = self._sign_in(_trace=self._idle_trace(TWO_DAYS + 3600))
        self.assertFalse(self._check(sid))
        row = self._rows(signal="inactivity")
        self.assertEqual((row.pinned_value, row.seen_value), ("48h", "49h"))

    def test_a_session_idle_within_the_limit_stays(self):
        self._switch(inactivity="close")
        self.ICP.set_param(PARAM + "inactivity_seconds", TWO_DAYS)
        sid = self._sign_in(_trace=self._idle_trace(TWO_DAYS - 3600))
        self.assertTrue(self._check(sid))
        self.assertEqual(self._stored(sid)[PIN_KEY]["limit"], TWO_DAYS)

    def test_the_limit_is_odoo_s_when_no_parameter_sets_one(self):
        self._switch(inactivity="close")
        self.ICP.set_param("sessions.max_inactivity_seconds", 3 * 3600)
        sid = self._sign_in(_trace=self._idle_trace(4 * 3600))
        self.assertFalse(self._check(sid))

    # -- review M1: what a close writes survives its request ---------------

    def test_the_close_is_written_although_its_request_rolls_back(self):
        """Refusing the request rolls its transaction back, on a route
        that is read-only besides, where nothing could be written. The
        row, and whatever the layers do on a close, are written on a
        cursor of their own and outlive it."""
        self._switch(browser="close")
        Guard = type(self.env["cloud.session.guard"])
        original = Guard._session_guard_act
        seen_in_request = []

        def act(guard, request, *args):
            """Note whether the request's cursor could write, then act."""
            seen_in_request.append(guard.env.cr.readonly)
            return original(guard, request, *args)

        def closed(guard, user, signal, sid):
            """What a layer does on a close: write something."""
            guard.env["ir.config_parameter"].set_param(
                "test.session_guard.layer", f"{user.id}:{signal}",
            )

        sid = self._sign_in()
        self.assertTrue(self._check(sid, CHROME))
        with patch.object(Guard, "_session_guard_act", autospec=True,
                          side_effect=act), \
                patch.object(Guard, "_session_guard_closed", autospec=True,
                             side_effect=closed):
            self.assertFalse(self._check(sid, FIREFOX))
        self.assertEqual(seen_in_request, [True])
        self.assertEqual(
            self.ICP.get_param("test.session_guard.layer"),
            f"{self.user.id}:browser",
        )
        self.assertEqual(self._rows().action, "closed")

    # -- review A2: one close of one session counts once -------------------

    def test_parallel_requests_of_one_session_close_it_once(self):
        """Each loads the session before the first deletes it, and each
        sees the new browser. One row, one call to the layers, one close
        for the anti-loop."""
        self._switch(browser="close")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, CHROME))
        Guard = type(self.env["cloud.session.guard"])
        barrier = threading.Barrier(3, timeout=30)
        original_post_init = odoo.http.Request._post_init

        def post_init(request):
            """Load the session, then wait for the other two to have."""
            original_post_init(request)
            barrier.wait()

        answers = []

        def ask():
            """One of the page's parallel requests."""
            answers.append(self._ask(sid, FIREFOX))

        self.env.flush_all()
        with patch.object(Guard, "_session_guard_closed", autospec=True) as closed, \
                patch.object(odoo.http.Request, "_post_init", autospec=True,
                             side_effect=post_init), \
                self.allow_requests(all_requests=True):
            threads = [threading.Thread(target=ask) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
        self.env.invalidate_all()
        self.assertEqual(answers, [False, False, False])
        self.assertEqual(closed.call_count, 1)
        row = self._rows()
        self.assertEqual(len(row), 1)
        self.assertEqual((row.action, row.hits), ("closed", 3))

    # -- the anti-loop -----------------------------------------------------

    def _closed_before(self, count, pair=("ES", "FR")):
        """Write *count* closes of distinct sessions by country."""
        for index in range(count):
            self.Closure.create({
                "user_id": self.user.id,
                "session_identifier": f"{index:042d}",
                "signal": "country", "action": "closed",
                "pinned_value": pair[0], "seen_value": pair[1],
            })

    def test_the_fourth_close_in_a_day_only_logs_and_alerts_once(self):
        self._behind_the_cdn()
        self._switch(country="close")
        self._closed_before(3)
        sid = self._sign_in()
        self.assertTrue(self._check(sid, visitor=HOME, country="ES"))
        self.assertTrue(self._check(sid, visitor=ABROAD, country="FR"))
        self.assertEqual(self._rows(session_identifier=sid[:42]).action, "held")
        hold = self.Hold.search([("user_id", "=", self.user.id)])
        self.assertEqual(hold.pair, "ES|FR")
        alerts = self.env["cloud.alert"].search(
            [("code", "=", "session_guard_antiloop")],
        )
        self.assertEqual(len(alerts), 1)
        # The same pair again, in another session: the hold starts again
        # and nobody is alerted twice (review M4).
        hold.until = fields.Datetime.now() + timedelta(hours=1)
        other = self._sign_in()
        self.assertTrue(self._check(other, visitor=HOME, country="FR"))
        self.assertTrue(self._check(other, visitor=ABROAD, country="ES"))
        hold.invalidate_recordset()
        self.assertGreater(hold.until, fields.Datetime.now() + timedelta(days=6))
        self.assertEqual(
            len(self.env["cloud.alert"].search(
                [("code", "=", "session_guard_antiloop")],
            )), 1,
        )

    def test_three_closes_are_three_sessions_not_three_requests(self):
        """Two earlier sessions and this one: still a close."""
        self._behind_the_cdn()
        self._switch(country="close")
        self._closed_before(2)
        sid = self._sign_in()
        self.assertTrue(self._check(sid, visitor=HOME, country="ES"))
        self.assertFalse(self._check(sid, visitor=ABROAD, country="FR"))
        self.assertFalse(self.Hold.search([("user_id", "=", self.user.id)]))

    def test_an_ended_hold_lets_the_signal_close_again(self):
        self._behind_the_cdn()
        self._switch(country="close")
        self.Hold.create({
            "user_id": self.user.id, "signal": "country", "pair": "ES|FR",
            "until": fields.Datetime.now() - timedelta(minutes=1),
        })
        sid = self._sign_in()
        self.assertTrue(self._check(sid, visitor=HOME, country="ES"))
        self.assertFalse(self._check(sid, visitor=ABROAD, country="FR"))

    # -- re-entry ----------------------------------------------------------

    def test_signing_in_again_is_the_re_entry_of_the_close(self):
        self._switch(browser="close")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, CHROME))
        self.assertFalse(self._check(sid, FIREFOX))
        again = self._sign_in()
        self.assertTrue(self._check(again, FIREFOX))
        row = self._rows()
        self.assertEqual(row.reentry_browser, "firefox")
        self.assertGreaterEqual(row.reentry_seconds, 0)

    # -- the log is kept 90 days -------------------------------------------

    def test_old_rows_are_collected(self):
        self._closed_before(1)
        row = self._rows()
        self.env.cr.execute(
            "UPDATE cloud_session_closure SET create_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(days=91), row.id),
        )
        self._closed_before(1, pair=("ES", "PT"))
        self.Closure._gc_session_closures()
        self.assertEqual(self._rows().mapped("seen_value"), ["PT"])


class TestTheModuleNamesNoOdoo19Constant(BaseCase):
    """Review M6: Odoo 18 has no ``STORED_SESSION_BYTES``."""

    def test_the_identifier_length_is_odoo_s(self):
        self.assertEqual(
            guard_module.SESSION_IDENTIFIER_LENGTH,
            getattr(odoo.http, "STORED_SESSION_BYTES", 42),
        )
