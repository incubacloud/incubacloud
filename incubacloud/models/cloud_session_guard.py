"""Close a web session, without asking, when it changes hands.

A stolen session cookie is a way in that skips the password and any
second factor. Nothing here asks the user anything: when a session that
was opened in one country, or with one family of browser, turns up in a
different one, the session is destroyed on the server — for whoever held
it, the owner and the thief alike — and the request that showed it is
not served. To the owner it looks like an expired session; signing in
again is what it always is. The owner's decision of 11-nov-2026: "casi
prefiero que el navegador te saque sin razón a que te esté molestando".

What a session is compared with is written into it the first time it is
seen after it is opened (:data:`PIN_KEY`): the user, the country and the
browser family of that request, and how long it may stay idle. Odoo
keeps a trace of each (platform, browser, address) a session is used
from and adds one only when the request does not match any; a country
cannot change without the address changing, nor a family without the
browser, so the comparison runs only on a request that adds a trace —
a few times per session and day — and no query is added to any other.

Three signals, each with its own switch, ``off``, ``log`` or ``close``
(``incubacloud.session_guard.<signal>``):

- **country** — the country the CDN states, believed only with the proof
  ``net/real_ip`` asks for the visitor's address, or Odoo's GeoIP where
  no CDN is configured; never both on one database. A country the CDN
  could not tell (``XX``, or no header) is logged and does not close; a
  session with a country that is then used through Tor (``T1``) or by a
  request that did not come through the CDN at all does close: both are
  how a cookie is tried away from its owner (review of 12-nov-2026, A1
  and M2).
- **browser** — the family Odoo's own user-agent parser reads (chrome,
  firefox, safari...). The version, the system and the device do not
  count. A session with a family that is then used by something the
  parser cannot name (a script, ``curl``) closes (M2).
- **inactivity** — the idle time Odoo's ``sessions.max_inactivity_seconds``
  only gives the cookie, which a thief does not have to respect, checked
  on the server on every request against the last activity of the
  session's traces, before Odoo refreshes it (M5).

Before closing, the anti-loop: a user whose sessions this closed for the
same signal three times in 24 hours — three distinct sessions, so that
the parallel requests one page makes, each loading the session before
the first one deleted it, count once (A2) — has that signal only logged
for 7 days, and an alert is raised once per user and pair of values.
While the same pair keeps showing up the 7 days start again (M4).

Every close, and every request a signal in ``log`` would have closed, is
a row of ``cloud.session.closure``, written on a cursor of its own and
committed before the request is refused: refusing it rolls the
request's transaction back (M1). Its re-entry columns say how long the
user took to sign in again and from where, which is what tells a false
close from a stolen cookie without asking anybody. Nothing of the guard
is read or written on the request's cursor. The guard's own cursor
reads what other requests committed (READ COMMITTED), and a write that
still collides with a parallel request of the same page is tried again
on a new cursor (review of the result, 12-nov-2026, A1 and A2). The
anti-loop's alert, which can page somebody over HTTP, is raised after
the hold is committed, outside the user's lock (re-review, M-a).

A close refuses the request as an expired session, except on a route
open to visitors (``auth='public'`` or ``'none'``), which is served as
a visitor, as Odoo serves a session it no longer accepts there (M1 of
the same review).

Every value, and why, is in ``docs/runbooks/RB-33-session-guard.md``.

Core alone is off by default: the module may be installed on somebody's
ERP, where signing in again is not one click. The layers that run the
platform switch it on and add what happens around a close
(:meth:`CloudSessionGuard._session_guard_closed`).

Odoo 18 has the same trace and the same ``_update_device``; it has no
session rotation and no ``STORED_SESSION_BYTES``, so nothing here reads
either (:data:`SESSION_IDENTIFIER_LENGTH`).
"""
import logging
import random
import time
from datetime import timedelta

from psycopg2 import errors as pg_errors

from odoo import SUPERUSER_ID, _, api, fields, models, sql_db
from odoo.http import GeoIP, SessionExpiredException, get_session_max_inactivity, root

from ..net import real_ip

_logger = logging.getLogger(__name__)

#: The session key holding what the session is compared with.
PIN_KEY = "_incubacloud_guard"

#: The leading characters of a session id that stay the same through
#: Odoo's rotation, and that ``res.device.log`` and the session store
#: identify a session by. Odoo 19 calls it ``STORED_SESSION_BYTES``;
#: Odoo 18 writes the same 42 as a literal and has no such name, so it is
#: not imported.
SESSION_IDENTIFIER_LENGTH = 42

#: The signals, in the order they are judged.
SIGNALS = ("inactivity", "country", "browser")
#: What each switch can say.
MODES = ("off", "log", "close")
#: The prefix of every system parameter read here.
PARAM_PREFIX = "incubacloud.session_guard."

#: Values with their default; each is a system parameter under
#: :data:`PARAM_PREFIX`. Why each is what it is: RB-33 (12-nov-2026).
DEFAULTS = {
    # Closes of one user by one signal, in distinct sessions, after which
    # that signal only logs for them.
    "antiloop_closes": 3,
    # The window those closes are counted in, in hours.
    "antiloop_window_hours": 24,
    # How long the signal then only logs for them, in days; it starts
    # again while the same pair of values keeps showing up.
    "antiloop_hold_days": 7,
    # How long a sign-in after a close counts as the re-entry, in hours.
    "reentry_window_hours": 24,
    # How long a row of the log is kept, in days.
    "log_retention_days": 90,
}

#: What the CDN says when it does not know the country.
COUNTRY_UNKNOWN = "XX"
#: What the CDN says for a visitor coming out of Tor.
COUNTRY_TOR = "T1"
#: The value logged for a request that did not come through the CDN, on
#: a database whose countries come from it.
COUNTRY_DIRECT = "direct"
#: The value logged for a browser Odoo's parser cannot name.
BROWSER_UNKNOWN = "unknown"

#: The class of the advisory lock that serialises one user's closes, so
#: two sessions closing at once do not both read the count before either
#: writes: the guard's cursor reads in READ COMMITTED
#: (:data:`READ_COMMITTED`), so the count taken after the wait sees what
#: the close before it committed. Any constant: it only has to differ from
#: the other users of two-key advisory locks on the database.
LOCK_CLASS = 4801
#: How long a close waits for another close of the same user before it
#: gives up and closes without recording: the wait holds a second
#: connection. RB-33 (12-nov-2026).
LOCK_TIMEOUT = "3s"

#: Whether the guard's own cursor is switched to READ COMMITTED. Odoo's
#: cursors are REPEATABLE READ: the snapshot is taken by the first
#: statement, the request for the user's lock, before the wait, so a
#: transaction that then writes a row another committed meanwhile fails,
#: and the count the lock is for misses the close before it. In READ
#: COMMITTED each statement sees what was committed when it starts. Only
#: on a real cursor: a test's cursor is one transaction already begun.
#: RB-33 (re-review of 12-nov-2026).
READ_COMMITTED = True

#: How many cursors a write of the guard is tried on. In READ COMMITTED
#: the parallel requests of one page no longer collide on the rows they
#: write; the attempts stay for a cursor that is not switched and for a
#: deadlock. Queued on the user's lock, the n-th of a burst on a
#: REPEATABLE READ cursor can need n, and Odoo 19's web client opens a page
#: with more than three calls at once, over HTTP/2 behind the CDN, so the
#: browser does not hold them to six. RB-33 (12-nov-2026).
WRITE_ATTEMPTS = 8
#: The pause before trying again, in seconds, drawn at random between the
#: two, so the requests that collided do not collide again together.
RETRY_PAUSE = (0.01, 0.05)
#: The errors a new cursor answers.
RETRIED_ERRORS = (pg_errors.SerializationFailure, pg_errors.DeadlockDetected)

#: An attribute set on the ``SessionExpiredException`` a close raises, so
#: that a route open to visitors serves the request as one instead. The
#: exception stays Odoo's own class: the web client knows it by its name.
CLOSED_MARK = "incubacloud_session_guard_closed"


def judge_country(pinned, seen):
    """Return what a country seen means against the one pinned.

    ``'adopt'`` — the session has none yet and this one is a country;
    ``'same'``; ``'unknown'`` — nothing comparable was said, which is
    logged; ``'none'`` — nothing to compare or log; ``'differs'`` —
    another country, Tor, or a request that skipped the CDN.

    :param str pinned: the country written into the session, or ``''``
    :param str seen: what this request says, as :meth:`_seen_country`
        returns it
    :rtype: str
    """
    if seen in ("", COUNTRY_UNKNOWN):
        return "unknown" if pinned else "none"
    if not pinned:
        if seen in (COUNTRY_TOR, COUNTRY_DIRECT):
            return "none"
        return "adopt"
    return "same" if seen == pinned else "differs"


def judge_browser(pinned, seen):
    """Return what a browser family seen means against the one pinned.

    As :func:`judge_country`: an unnamed browser cannot be adopted, and
    against a family it differs.

    :param str pinned: the family written into the session, or ``''``
    :param str seen: this request's family, or :data:`BROWSER_UNKNOWN`
    :rtype: str
    """
    if not pinned:
        return "none" if seen == BROWSER_UNKNOWN else "adopt"
    return "same" if seen == pinned else "differs"


class CloudSessionClosure(models.Model):
    """A session closed, or that a signal in ``log`` would have closed.

    One row per session, signal, action and pair of values: the parallel
    requests of a page that all see the same change write one row and
    add to its ``hits``. Written in SQL, on a cursor of its own
    (:meth:`CloudSessionGuard._session_guard_write`).
    """

    _name = "cloud.session.closure"
    _description = "Session closed by the guard"
    _order = "id desc"

    user_id = fields.Many2one(
        "res.users", required=True, ondelete="cascade", index=True,
    )
    session_identifier = fields.Char(
        required=True,
        help="The part of the session id that survives Odoo's rotation, "
             "as res.device.log keeps it.",
    )
    signal = fields.Selection(
        [
            ("country", "Country"),
            ("browser", "Browser"),
            ("inactivity", "Inactivity"),
        ],
        required=True,
    )
    action = fields.Selection(
        [
            ("closed", "Closed"),
            ("logged", "Logged only"),
            ("held", "Logged: anti-loop"),
        ],
        required=True,
        help="Closed: the session was destroyed. Logged only: the signal "
             "is set to log. Logged: anti-loop: it would have closed, but "
             "the user had too many closes by this signal lately.",
    )
    pinned_value = fields.Char(
        required=True, default="",
        help="What the session was opened with: a country, a browser "
             "family, or the idle limit in hours.",
    )
    seen_value = fields.Char(
        required=True, default="",
        help="What the request showed: a country, a browser family, "
             "'direct' for a request that skipped the CDN, or the idle "
             "time in hours.",
    )
    hits = fields.Integer(default=1)
    last_seen = fields.Datetime()
    reentry_seconds = fields.Integer(
        help="Seconds until the user signed in again, if within the "
             "re-entry window.",
    )
    reentry_country = fields.Char()
    reentry_browser = fields.Char()

    def init(self):
        """Create the index the upsert of a row conflicts on.

        In SQL rather than as a declared index, which Odoo 18 does not
        have.
        """
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS cloud_session_closure_once
                ON cloud_session_closure
                (session_identifier, signal, action, pinned_value, seen_value)
        """)
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS cloud_session_closure_count
                ON cloud_session_closure (user_id, signal, action, create_date)
        """)

    @api.autovacuum
    def _gc_session_closures(self):
        """Delete the rows older than ``log_retention_days``, the
        anti-loop holds that ended as long ago, and both of an archived
        user.

        A deleted user's rows go with the user (``ondelete``); an
        archived user's account is closed, and the countries and
        browsers these rows name are kept no longer than it (#74).
        """
        days = self.env["cloud.session.guard"]._session_guard_value(
            "log_retention_days",
        )
        cutoff = fields.Datetime.now() - timedelta(days=days)
        self.env.cr.execute("""
            DELETE FROM cloud_session_closure
             WHERE create_date < %s
                OR user_id IN (SELECT id FROM res_users WHERE active IS NOT TRUE)
        """, (cutoff,))
        self.env.cr.execute("""
            DELETE FROM cloud_session_hold
             WHERE until < %s
                OR user_id IN (SELECT id FROM res_users WHERE active IS NOT TRUE)
        """, (cutoff,))


class CloudSessionHold(models.Model):
    """A signal only logging for one user: the anti-loop.

    Keyed by user, signal and pair of values, and kept after it ends, so
    that the alert is raised once per pair and not at each new hold.
    """

    _name = "cloud.session.hold"
    _description = "Session guard anti-loop hold"
    _order = "id desc"

    user_id = fields.Many2one(
        "res.users", required=True, ondelete="cascade", index=True,
    )
    signal = fields.Selection(
        [("country", "Country"), ("browser", "Browser")], required=True,
    )
    pair = fields.Char(
        required=True,
        help="The two values the sessions went between, sorted.",
    )
    until = fields.Datetime(required=True)

    def init(self):
        """Create the index the upsert of a hold conflicts on."""
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS cloud_session_hold_once
                ON cloud_session_hold (user_id, signal, pair)
        """)


class CloudSessionGuard(models.AbstractModel):
    """The rule, the switches and what a close does.

    Called from ``res.device.log._update_device``, which Odoo calls from
    ``check_session`` on every request with a signed-in session and
    before it refreshes the session's trace.
    """

    _name = "cloud.session.guard"
    _description = "Session guard"

    # -- switches and values -------------------------------------------

    @api.model
    def _session_guard_default_mode(self, signal):
        """Return the switch of *signal* when no parameter sets it.

        ``off`` in core: see the module's docstring. Layers that run the
        platform return ``close``.

        :param str signal: one of :data:`SIGNALS`
        :rtype: str
        """
        return "off"

    @api.model
    def _session_guard_modes(self):
        """Return each signal's switch, the parameter over the default.

        An unreadable value counts as the default. Parameters are read
        from Odoo's cache: no query.

        :rtype: dict
        """
        ICP = self.env["ir.config_parameter"].sudo()
        modes = {}
        for signal in SIGNALS:
            raw = (ICP.get_param(PARAM_PREFIX + signal) or "").strip().lower()
            modes[signal] = (
                raw if raw in MODES else self._session_guard_default_mode(signal)
            )
        return modes

    @api.model
    def _session_guard_value(self, name):
        """Return the value *name* of :data:`DEFAULTS`, as a parameter
        may set it; a value that is not a positive integer counts as
        the default.

        :param str name: a key of :data:`DEFAULTS`
        :rtype: int
        """
        raw = self.env["ir.config_parameter"].sudo().get_param(
            PARAM_PREFIX + name,
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return DEFAULTS[name]
        return value if value > 0 else DEFAULTS[name]

    @api.model
    def _session_guard_inactivity_limit(self, user):
        """Return how many seconds a session of *user* may stay idle.

        ``incubacloud.session_guard.inactivity_seconds`` when set, Odoo's
        own ``sessions.max_inactivity_seconds`` otherwise. Layers lower
        it for whom they choose. Read when the session is pinned: a
        change reaches the sessions opened afterwards.

        :param user: the ``res.users`` of the session
        :rtype: int
        """
        raw = self.env["ir.config_parameter"].sudo().get_param(
            PARAM_PREFIX + "inactivity_seconds",
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else get_session_max_inactivity(self.env)

    # -- what a request shows ------------------------------------------

    @api.model
    def _seen_country(self, request):
        """Return the country *request* comes from, as the database's one
        source says it.

        Behind a CDN, the CDN's word, carried by ``net/real_ip`` with
        its proof: :data:`COUNTRY_DIRECT` when the request did not come
        through it, ``''`` when it said nothing. Where no CDN is
        configured, Odoo's GeoIP, ``''`` when it has no database or no
        answer. Never both: two sources can disagree on one address,
        and that would be a false close.

        :param request: the ``odoo.http.Request`` being authenticated
        :rtype: str
        """
        if real_ip.installed():
            if not real_ip.country_header():
                return ""
            environ = request.httprequest.environ
            if not environ.get(real_ip.VIA_CDN_KEY):
                return COUNTRY_DIRECT
            return environ.get(real_ip.COUNTRY_KEY) or ""
        try:
            code = GeoIP(request.httprequest.remote_addr).country.iso_code
        except Exception:  # noqa: BLE001 - no database is no country
            return ""
        return (code or "").upper()

    @api.model
    def _seen_browser(self, request):
        """Return the browser family of *request*, as Odoo's trace reads it.

        :param request: the ``odoo.http.Request`` being authenticated
        :rtype: str
        """
        return request.httprequest.user_agent.browser or BROWSER_UNKNOWN

    # -- the check -----------------------------------------------------

    @api.model
    def _session_guard_check(self, request):
        """Judge the session of *request*; refuse it if it has to close.

        Pins the session the first time it is seen signed in (or signed
        in as somebody else), checks its idle time, and, when the
        request is about to add a trace, compares its country and
        browser with the pinned ones.

        :param request: the ``odoo.http.Request`` (or websocket request)
            being authenticated
        :raises SessionExpiredException: when the session was closed
        """
        session = request.session
        uid = session.uid
        if not uid or session.get("_trace_disable"):
            return
        modes = self._session_guard_modes()
        if all(mode == "off" for mode in modes.values()):
            return
        traces = session.get("_trace") or []
        pin = session.get(PIN_KEY)
        if not isinstance(pin, dict) or pin.get("uid") != uid:
            pin = self._session_guard_pin(request, uid)
        now = int(time.time())
        limit = pin.get("limit") or 0
        if modes["inactivity"] != "off" and limit and traces:
            last = max(trace.get("last_activity") or 0 for trace in traces)
            if last and now - last > limit:
                self._session_guard_act(
                    request, "inactivity", modes["inactivity"],
                    f"{limit // 3600}h", f"{(now - last) // 3600}h",
                )
        httprequest = request.httprequest
        agent = httprequest.user_agent
        current = (agent.platform, agent.browser, httprequest.remote_addr)
        if any(
            (trace.get("platform"), trace.get("browser"),
             trace.get("ip_address")) == current
            for trace in traces
        ):
            return
        judged = (
            ("country", judge_country, self._seen_country),
            ("browser", judge_browser, self._seen_browser),
        )
        for signal, judge, seen_of in judged:
            if modes[signal] == "off":
                continue
            pinned = pin.get(signal) or ""
            seen = seen_of(request)
            verdict = judge(pinned, seen)
            if verdict == "adopt":
                pin = pin | {signal: seen}
                session[PIN_KEY] = pin
            elif verdict == "unknown":
                self._session_guard_act(request, signal, "log", pinned, seen)
            elif verdict == "differs":
                self._session_guard_act(
                    request, signal, modes[signal], pinned, seen,
                )

    @api.model
    def _session_guard_pin(self, request, uid):
        """Write into the session what it will be compared with.

        On the first request a signed-in session makes — Odoo's sign-in
        offers no place to hook, and a session opened before this ran
        has nothing written either — and again when the session's user
        changes, since signing in does not clear the session's keys.
        The user's last close within the re-entry window gets this
        sign-in as its re-entry.

        :param request: the request being authenticated
        :param int uid: the session's user
        :return: what was written
        :rtype: dict
        """
        modes = self._session_guard_modes()
        user = self.env["res.users"].sudo().browse(uid)
        pin = {
            "uid": uid,
            "country": "",
            "browser": "",
            "limit": (
                self._session_guard_inactivity_limit(user)
                if modes["inactivity"] != "off" else 0
            ),
        }
        country = self._seen_country(request)
        if judge_country("", country) == "adopt":
            pin["country"] = country
        browser = self._seen_browser(request)
        if judge_browser("", browser) == "adopt":
            pin["browser"] = browser
        request.session[PIN_KEY] = pin
        self._session_guard_reentry(request, uid, pin)
        return pin

    @api.model
    def _session_guard_reentry(self, request, uid, pin):
        """Record a new session of *uid* as the re-entry of its last close.

        One indexed statement per new session, on a cursor of its own.
        It is bookkeeping: a failure is logged and the check goes on.

        :param request: the request being authenticated
        :param int uid: the session's user
        :param dict pin: what the new session was pinned with
        """
        hours = self._session_guard_value("reentry_window_hours")
        now = fields.Datetime.now()
        identifier = request.session.sid[:SESSION_IDENTIFIER_LENGTH]

        def reenter(guard):
            """Complete the user's last close with this sign-in."""
            guard.env.cr.execute("""
                UPDATE cloud_session_closure
                   SET reentry_seconds = GREATEST(
                           0, EXTRACT(EPOCH FROM %s - create_date))::int,
                       reentry_country = %s, reentry_browser = %s
                 WHERE id = (
                       SELECT id FROM cloud_session_closure
                        WHERE user_id = %s AND action = 'closed'
                          AND reentry_seconds IS NULL AND create_date >= %s
                          AND session_identifier != %s
                        ORDER BY id DESC LIMIT 1)
            """, (now, pin["country"], pin["browser"], uid,
                  now - timedelta(hours=hours), identifier),
                log_exceptions=False)

        try:
            self._session_guard_own(reenter)
        except Exception:  # noqa: BLE001 - bookkeeping never stops a request
            _logger.exception(
                "[session-guard] could not record the re-entry of user %s", uid,
            )

    def _session_guard_new_cursor(self):
        """Return a new cursor that may write, apart from the request's.

        Its own method so that a test can hand out cursors on
        connections of their own.

        :rtype: odoo.sql_db.BaseCursor
        """
        return self.env.registry.cursor(readonly=False)

    def _session_guard_own(self, work):
        """Run *work* on a cursor of its own, committed on leaving, and
        again on a new one when it collides with a concurrent write.

        The request's transaction is rolled back first: nothing has been
        written in it yet at authentication, and a transaction left open
        around another would, under the tests' cursors, take the other's
        writes down with it. A real cursor is switched to READ COMMITTED
        before anything runs on it (:data:`READ_COMMITTED`). A collision
        (:data:`RETRIED_ERRORS`) is tried again up to
        :data:`WRITE_ATTEMPTS` times in all, after a pause of
        :data:`RETRY_PAUSE`, with one line of its own at INFO; the new
        cursor sees what the other request committed.

        :param work: a callable taking this guard, as superuser, on the
            new cursor
        :return: what *work* returns
        :raises Exception: what the last attempt raised
        """
        self.env.cr.rollback()
        for attempt in range(1, WRITE_ATTEMPTS + 1):
            try:
                with self._session_guard_new_cursor() as cr:
                    if READ_COMMITTED and isinstance(cr, sql_db.Cursor):
                        cr.execute(
                            "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
                        )
                    return work(
                        self.with_env(api.Environment(cr, SUPERUSER_ID, {})),
                    )
            except RETRIED_ERRORS as error:
                if attempt == WRITE_ATTEMPTS:
                    raise
                _logger.info(
                    "[session-guard] a write collided with a parallel "
                    "request (%s); trying again (%s of %s)",
                    type(error).__name__, attempt + 1, WRITE_ATTEMPTS,
                )
                time.sleep(random.uniform(*RETRY_PAUSE))

    # -- acting --------------------------------------------------------

    @api.model
    def _session_guard_act(self, request, signal, mode, pinned, seen):
        """Log, or close the session of *request*, for *signal*.

        Closing goes through the anti-loop (country and browser), then
        writes its row and runs :meth:`_session_guard_closed` on a cursor
        of its own, committed before the request is refused: refusing it
        rolls the request's transaction back. A failure to write does
        not keep the session open — ante la duda, cerrar. A failure of
        :meth:`_session_guard_closed` keeps the row, so the anti-loop
        still counts the close. A hold that starts for a new pair raises
        its alert once committed, on another cursor and outside the
        user's lock: the alert can page somebody over HTTP, and the other
        requests of the page wait for that lock. Logging, and the alert,
        are bookkeeping: a failure is logged and the check goes on.

        The exception a close raises carries :data:`CLOSED_MARK`, which
        lets a route open to visitors serve the request as one
        (``ir.http._authenticate_explicit`` in ``res_device_log``).

        :param request: the request being authenticated
        :param str signal: one of :data:`SIGNALS`
        :param str mode: ``log`` or ``close``
        :param str pinned: the value the session was opened with
        :param str seen: the value this request shows
        :raises SessionExpiredException: when the session was closed
        """
        session = request.session
        uid = session.uid
        identifier = session.sid[:SESSION_IDENTIFIER_LENGTH]
        if mode != "close":
            try:
                self._session_guard_own(lambda guard: guard._session_guard_write(
                    uid, identifier, signal, "logged", pinned, seen,
                ))
            except Exception:  # noqa: BLE001 - bookkeeping never stops a request
                _logger.exception(
                    "[session-guard] could not log a session of user %s by %s",
                    uid, signal,
                )
            return

        def record(guard):
            """Decide, write the row and run what a close does beyond
            this database; return the action and whether a hold started
            for a new pair."""
            action, new_hold = "closed", False
            if signal != "inactivity":
                action, new_hold = guard._session_guard_hold(
                    uid, identifier, signal, pinned, seen,
                )
            first = guard._session_guard_write(
                uid, identifier, signal, action, pinned, seen,
            )
            if action == "closed" and first:
                try:
                    with guard.env.cr.savepoint():
                        guard._session_guard_closed(
                            guard.env["res.users"].browse(uid), signal,
                            session.sid,
                        )
                except RETRIED_ERRORS:
                    raise
                except Exception:  # noqa: BLE001 - the row is kept
                    _logger.exception(
                        "[session-guard] what a close does beyond this "
                        "database failed for user %s by %s", uid, signal,
                    )
            return action, new_hold

        try:
            action, new_hold = self._session_guard_own(record)
        except Exception:  # noqa: BLE001 - a failed write still closes
            _logger.exception(
                "[session-guard] could not record the close of a session of "
                "user %s by %s; closing it anyway", uid, signal,
            )
            action, new_hold = "closed", False
        if new_hold:
            try:
                self._session_guard_own(lambda guard: guard._session_guard_alert(
                    uid, signal, pinned, seen,
                ))
            except Exception:  # noqa: BLE001 - the hold is kept
                _logger.exception(
                    "[session-guard] could not raise the anti-loop alert of "
                    "user %s by %s", uid, signal,
                )
        if action != "closed":
            return
        _logger.info(
            "[session-guard] closed a session of user %s: %s %s -> %s",
            uid, signal, pinned, seen,
        )
        self._session_guard_destroy(session)
        error = SessionExpiredException("Session expired")
        setattr(error, CLOSED_MARK, True)
        raise error

    @api.model
    def _session_guard_hold(self, uid, identifier, signal, pinned, seen):
        """Decide between closing and the anti-loop, on the close's cursor.

        Under a lock per user, so two sessions closing at once count in
        turn, waited for :data:`LOCK_TIMEOUT` at most; the cursor reads in
        READ COMMITTED, so the count after the wait sees the close before
        it. A hold in force for this signal logs instead of closing, and
        starts again when it is about this same pair of values.
        Otherwise, if the user's distinct sessions closed by this signal
        within the window reach ``antiloop_closes``, a hold starts. The
        statements that can collide with a parallel request do not log
        Odoo's ``bad query``: :meth:`_session_guard_own` logs the attempt.

        :param int uid: the session's user
        :param str identifier: the session's identifier
        :param str signal: ``country`` or ``browser``
        :param str pinned: the value the session was opened with
        :param str seen: the value this request shows
        :return: ``'closed'`` or ``'held'``, and whether a hold started
            for a pair never held before, whose alert the caller raises
            once this is committed (:meth:`_session_guard_alert`)
        :rtype: tuple
        """
        cr = self.env.cr
        cr.execute("SET LOCAL lock_timeout = %s", (LOCK_TIMEOUT,))
        cr.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_CLASS, uid),
            log_exceptions=False,
        )
        now = fields.Datetime.now()
        pair = "|".join(sorted((pinned, seen)))
        until = now + timedelta(
            days=self._session_guard_value("antiloop_hold_days"),
        )
        cr.execute("""
            SELECT id, pair FROM cloud_session_hold
             WHERE user_id = %s AND signal = %s AND until > %s
        """, (uid, signal, now))
        holds = cr.fetchall()
        if holds:
            for hold_id, held_pair in holds:
                if held_pair == pair:
                    cr.execute(
                        "UPDATE cloud_session_hold SET until = %s, "
                        "write_date = %s WHERE id = %s",
                        (until, now, hold_id), log_exceptions=False,
                    )
            return "held", False
        since = now - timedelta(
            hours=self._session_guard_value("antiloop_window_hours"),
        )
        cr.execute("""
            SELECT count(DISTINCT session_identifier) FROM cloud_session_closure
             WHERE user_id = %s AND signal = %s AND action = 'closed'
               AND create_date >= %s AND session_identifier != %s
        """, (uid, signal, since, identifier))
        if cr.fetchone()[0] < self._session_guard_value("antiloop_closes"):
            return "closed", False
        cr.execute("""
            INSERT INTO cloud_session_hold
                   (user_id, signal, pair, until,
                    create_uid, write_uid, create_date, write_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, signal, pair)
            DO UPDATE SET until = EXCLUDED.until, write_date = EXCLUDED.write_date
            RETURNING (xmax = 0)
        """, (uid, signal, pair, until, SUPERUSER_ID, SUPERUSER_ID, now, now),
            log_exceptions=False)
        return "held", bool(cr.fetchone()[0])

    @api.model
    def _session_guard_alert(self, uid, signal, pinned, seen):
        """Raise the alert of a new anti-loop hold.

        Called once the hold is committed, on a cursor of its own and
        outside the user's lock (:meth:`_session_guard_act`): the alert
        may page somebody over HTTP.

        :param int uid: the user
        :param str signal: ``country`` or ``browser``
        :param str pinned: one of the two values
        :param str seen: the other
        """
        user = self.env["res.users"].browse(uid)
        what = _("country") if signal == "country" else _("browser family")
        self.env["cloud.alert"].raise_alert(
            "session_guard_antiloop",
            _(
                "Sessions of %(user)s were closed too often for a change of "
                "%(signal)s (%(first)s and %(second)s): that check only logs "
                "for them for a while. Look at whether it is their connection "
                "or somebody else using their session.",
                user=user.login, signal=what, first=pinned, second=seen,
            ),
            level="warning",
            key=f"{uid}:{signal}:{'|'.join(sorted((pinned, seen)))}",
        )

    @api.model
    def _session_guard_write(self, uid, identifier, signal, action, pinned, seen):
        """Write the row of a close or a log, once per session and pair.

        :return: whether this call wrote it, rather than adding to a
            row a concurrent request of the same session wrote first
        :rtype: bool
        """
        now = fields.Datetime.now()
        self.env.cr.execute("""
            INSERT INTO cloud_session_closure
                   (user_id, session_identifier, signal, action,
                    pinned_value, seen_value, hits, last_seen,
                    create_uid, write_uid, create_date, write_date)
            VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s)
            ON CONFLICT (session_identifier, signal, action,
                         pinned_value, seen_value)
            DO UPDATE SET hits = cloud_session_closure.hits + 1,
                          last_seen = EXCLUDED.last_seen,
                          write_date = EXCLUDED.write_date
            RETURNING (xmax = 0)
        """, (uid, identifier, signal, action, pinned or "", seen or "", now,
              SUPERUSER_ID, SUPERUSER_ID, now, now), log_exceptions=False)
        return bool(self.env.cr.fetchone()[0])

    @api.model
    def _session_guard_closed(self, user, signal, sid):
        """Run what a close does beyond this database. Nothing in core.

        Called once per closed session, on the close's own cursor, as
        superuser. The master tells the panels the user may be signed in
        to; a panel makes sure the closed session cannot be written back.

        :param user: the ``res.users`` whose session closed
        :param str signal: one of :data:`SIGNALS`
        :param str sid: the closed session's full id
        """

    @api.model
    def _session_guard_destroy(self, session):
        """Delete the session's files: the current one and the ones it
        was rotated from, which share its identifier.

        Any failure is logged and the close goes on: the request is
        still refused as an expired session, which on an HTTP route Odoo
        also signs out, where a failure reaching ``_update_device`` would
        let the request through with the row saying ``closed``.

        :param session: the ``odoo.http.Session`` to destroy
        """
        try:
            root.session_store.delete_from_identifiers(
                [session.sid[:SESSION_IDENTIFIER_LENGTH]],
            )
        except Exception:  # noqa: BLE001 - the close goes on
            _logger.warning(
                "[session-guard] could not delete the files of a closed "
                "session; closing it anyway", exc_info=True,
            )
