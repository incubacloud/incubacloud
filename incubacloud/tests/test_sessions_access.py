"""Sessions and access: the history, the notices and the page (#48, phase 2).

Driven over real HTTP where Odoo's own session check is what calls the
guard, with countries given the way production gives them, through
``net/real_ip`` and its proof (see ``test_session_guard``). Core's own
switches and hooks, whatever layer is installed beside it.
"""
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import requests

import odoo.http
from odoo.addons.base.models.res_users import Users as BaseUsers
from odoo.tests import new_test_user, tagged
from odoo.tests.common import HttpCase
from odoo.tools import config, mute_logger

from odoo.addons.incubacloud.models import cloud_session_access as access_module
from odoo.addons.incubacloud.models import cloud_session_guard as guard_module
from odoo.addons.incubacloud.net import real_ip

from .test_real_ip import EDGE, TRUSTED, _OnTheUncorrectedProxyFix
from .test_session_guard import CHROME, FIREFOX, HOME

PARAM = "incubacloud.session_guard."
NOTICE_LOGGER = "odoo.addons.incubacloud.models.cloud_session_access"


def _fail_in_the_database(notice, user, reason):
    """Stand in for the quota of a notice: a statement the database
    refuses, as a serialization failure on the rate-limit row would be.

    The transaction is left aborted unless a savepoint rolls it back.
    """
    notice.env.cr.execute("SELECT 1 / 0")


class _GuardedRequests(_OnTheUncorrectedProxyFix):
    """A user, core's switches and hooks, and requests as a browser makes them."""

    def setUp(self):
        super().setUp()
        self.user = new_test_user(
            self.env, login="seen-user", password="seen-user-pw",
            email="seen-user@example.com", groups="base.group_user",
        )
        self.ICP = self.env["ir.config_parameter"].sudo()
        Guard = type(self.env["cloud.session.guard"])
        for name in ("_session_guard_default_mode",
                     "_session_guard_inactivity_limit"):
            self.startPatcher(patch.object(
                Guard, name, getattr(guard_module.CloudSessionGuard, name),
            ))
        Notice = type(self.env["cloud.session.notice"])
        for name in ("_session_notices_default", "_session_notice_on_sign_in",
                     "_session_notice_holders", "_session_notice_page_url",
                     "_session_notice_place"):
            self.startPatcher(patch.object(
                Notice, name, getattr(access_module.CloudSessionNotice, name),
            ))
        Users = type(self.env["res.users"])
        for name in ("_ic_signed_out_everywhere", "_ic_everywhere",
                     "_ic_sessions_links"):
            self.startPatcher(patch.object(
                Users, name, getattr(access_module.ResUsers, name),
            ))
        # Logged, not closing: the history and the notices run on every
        # request a signal judges, whatever it does after.
        self.ICP.set_param(PARAM + "country", "log")
        self.ICP.set_param(PARAM + "browser", "log")

    def _behind_the_cdn(self):
        """Install the correction with the CDN's country, for this test."""
        self.assertTrue(real_ip.install({
            real_ip.HEADER_OPTION: "CF-Connecting-IP",
            real_ip.TRUSTED_OPTION: TRUSTED,
            real_ip.PROXY_OPTION: "",
            real_ip.COUNTRY_OPTION: "CF-IPCountry",
        }, odoo.http))
        self.startPatcher(patch.dict(config.options, {"proxy_mode": True}))

    def _sign_in(self):
        """Open a session of the fixture user, traced as a browser's is.

        :rtype: str
        """
        self.authenticate(
            "seen-user", "seen-user-pw", session_extra={"_trace_disable": False},
        )
        return self.session.sid

    def _call(self, sid, path, params=None, agent=CHROME, visitor=HOME, country="ES"):
        """Call the JSON route *path* with session *sid*, through the CDN.

        :return: the JSON-RPC answer, ``result`` or ``error``
        :rtype: dict
        """
        headers = {"User-Agent": agent, "Content-Type": "application/json"}
        if visitor:
            host = self.base_url().split("://", 1)[1]
            headers.update({
                "X-Forwarded-Host": host,
                "X-Forwarded-For": f"{visitor}, {EDGE}",
                "CF-Connecting-IP": visitor,
                "CF-IPCountry": country,
            })
        self.env.flush_all()
        answer = requests.post(
            self.base_url() + path,
            data=json.dumps({"jsonrpc": "2.0", "method": "call",
                             "params": params or {}}),
            headers=headers, cookies={"session_id": sid}, timeout=30,
        ).json()
        self.env.invalidate_all()
        return answer

    def _result(self, sid, path, params=None, **kwargs):
        """Call *path* as :meth:`_call` does; return its ``result``.

        :return: the result, or ``None`` on an error
        """
        return self._call(sid, path, params, **kwargs).get("result")

    def _check(self, sid, **kwargs):
        """Ask ``/web/session/check``; return whether the session was accepted."""
        return "error" not in self._call(sid, "/web/session/check", **kwargs)

    def _mails(self):
        """Return the notices queued for the fixture user."""
        return self.env["mail.mail"].sudo().search(
            [("email_to", "ilike", "seen-user@example.com")], order="id",
        )

    def _seen(self):
        """Return the fixture user's history as ``{(kind, value)}``."""
        rows = self.env["cloud.session.seen"].sudo().search(
            [("user_id", "=", self.user.id)],
        )
        return {(row.kind, row.value) for row in rows}


@tagged("-at_install", "post_install")
class TestSessionHistoryAndNotices(_GuardedRequests, HttpCase):
    """The countries and browsers seen, and the email of a new one."""

    def setUp(self):
        super().setUp()
        self._behind_the_cdn()

    def test_an_account_with_no_history_is_only_written(self):
        """Neither the first sign-in nor the first after the deploy tells."""
        self.ICP.set_param(PARAM + "notices", "on")
        self.assertTrue(self._check(self._sign_in(), country="ES"))
        self.assertEqual(self._seen(), {("country", "ES"), ("browser", "chrome")})
        self.assertFalse(self._mails())

    def test_a_new_country_is_told_once_a_day(self):
        self.ICP.set_param(PARAM + "notices", "on")
        self.assertTrue(self._check(self._sign_in(), country="ES"))
        self.assertTrue(self._check(self._sign_in(), visitor="198.51.100.30", country="FR"))
        mails = self._mails()
        self.assertEqual(len(mails), 1)
        self.assertEqual(mails.subject, "Your account was used from a new country: France")
        self.assertIn("a country it had not been used from before", mails.body_html)
        self.assertIn("/cloud/sessions", mails.body_html)
        self.assertIn("sign out everywhere", mails.body_html)
        # Another new country the same day: written, not told again.
        self.assertTrue(self._check(self._sign_in(), visitor="198.51.100.40", country="IT"))
        self.assertEqual(len(self._mails()), 1)
        self.assertEqual(
            {value for kind, value in self._seen() if kind == "country"},
            {"ES", "FR", "IT"},
        )

    def test_a_known_country_tells_nothing(self):
        self.ICP.set_param(PARAM + "notices", "on")
        self.assertTrue(self._check(self._sign_in(), country="ES"))
        self.assertTrue(self._check(self._sign_in(), visitor="203.0.113.99", country="ES"))
        self.assertFalse(self._mails())

    def test_a_new_browser_within_a_session_is_told(self):
        self.ICP.set_param(PARAM + "notices", "on")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, agent=CHROME))
        self.assertTrue(self._check(sid, agent=FIREFOX))
        mails = self._mails()
        self.assertEqual(len(mails), 1)
        self.assertEqual(mails.subject, "Your account was used from a new browser: Firefox")
        self.assertIn(("browser", "firefox"), self._seen())

    def test_unknown_countries_are_not_history(self):
        self.ICP.set_param(PARAM + "notices", "on")
        sid = self._sign_in()
        self.assertTrue(self._check(sid, country="ES"))
        for code in ("XX", "T1"):
            self._check(self._sign_in(), visitor="198.51.100.50", country=code)
        self.assertEqual(
            {value for kind, value in self._seen() if kind == "country"}, {"ES"},
        )
        self.assertFalse(self._mails())

    def test_core_alone_tells_nobody(self):
        """No parameter: the history is written, no email goes out."""
        self.assertTrue(self._check(self._sign_in(), country="ES"))
        self.assertTrue(self._check(self._sign_in(), visitor="198.51.100.30", country="FR"))
        self.assertIn(("country", "FR"), self._seen())
        self.assertFalse(self._mails())

    def test_the_guard_switched_off_writes_nothing(self):
        self.ICP.set_param(PARAM + "country", "off")
        self.ICP.set_param(PARAM + "browser", "off")
        self.assertTrue(self._check(self._sign_in(), country="ES"))
        self.assertFalse(self._seen())

    def test_a_layer_that_does_not_tell_sign_ins(self):
        """Where every sign-in comes through another database that told
        it, a new country on a session's first request is only written."""
        self.ICP.set_param(PARAM + "notices", "on")
        Notice = type(self.env["cloud.session.notice"])
        with patch.object(Notice, "_session_notice_on_sign_in", lambda self: False):
            self.assertTrue(self._check(self._sign_in(), country="ES"))
            self.assertTrue(self._check(self._sign_in(), visitor="198.51.100.30", country="FR"))
            self.assertFalse(self._mails())
            # Within a session it is told.
            sid = self._sign_in()
            self.assertTrue(self._check(sid, country="ES"))
            self.assertTrue(self._check(sid, visitor="198.51.100.31", country="PT"))
        self.assertEqual(len(self._mails()), 1)

    def test_the_trace_names_the_country_the_cdn_states(self):
        """Behind the CDN Odoo's GeoIP finds nothing: the row gets the
        CDN's country, by its English name."""
        sid = self._sign_in()
        self.assertTrue(self._check(sid, country="ES"))
        rows = self.env["res.device.log"].sudo().search(
            [("session_identifier", "=", sid[:42])],
        )
        self.assertTrue(rows)
        self.assertEqual(set(rows.mapped("country")), {"Spain"})

    def test_an_archived_user_s_history_goes(self):
        self.assertTrue(self._check(self._sign_in(), country="ES"))
        self.assertTrue(self._seen())
        self.user.active = False
        self.env["cloud.session.seen"]._gc_seen_of_archived_users()
        self.assertFalse(self._seen())


@tagged("-at_install", "post_install")
class TestSessionsPage(_GuardedRequests, HttpCase):
    """The page's calls: what it shows, Revoke and Sign out everywhere."""

    def test_the_page_shows_the_sessions_and_the_closes(self):
        self._behind_the_cdn()
        self.ICP.set_param(PARAM + "browser", "close")
        closed = self._sign_in()
        self.assertTrue(self._check(closed, agent=CHROME))
        self.assertFalse(self._check(closed, agent=FIREFOX))
        sid = self._sign_in()
        data = self._result(sid, "/cloud/sessions/get")
        self.assertTrue(data["ok"])
        current = [row for row in data["sessions"] if row["current"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["browsers"], ["Chrome"])
        self.assertEqual(current[0]["countries"], ["Spain"])
        self.assertEqual(
            [(row["reason"], row["from"], row["to"]) for row in data["closes"]],
            [("browser", "Chrome", "Firefox")],
        )
        self.assertEqual([row["name"] for row in data["seen"]["countries"]], ["Spain"])
        self.assertEqual(data["everywhere"], {"mode": "button"})

    def _another_session(self):
        """Store a second session of the fixture user, as another browser
        holds one: ``authenticate`` deletes the session it replaces.

        :return: its id
        :rtype: str
        """
        store = odoo.http.root.session_store
        other = store.new()
        other.update(dict(self.session))
        other["session_token"] = self.user._compute_session_token(other.sid)
        other["_trace"] = []
        store.save(other)
        return other.sid

    def test_sign_out_everywhere_asks_nothing_and_spares_api_keys(self):
        second = self._sign_in()
        first = self._another_session()
        key = self.env["res.users.apikeys"].with_user(self.user).sudo()._generate(
            None, "ci", datetime.now() + timedelta(days=1),
        )
        self.assertTrue(self._check(first, visitor=None))
        before = self.user.backchannel_logout_counter
        answer = self._result(second, "/cloud/sessions/sign_out_everywhere", visitor=None)
        self.assertEqual(answer, {"ok": True, "redirect": "/web/login"})
        self.assertEqual(self.user.backchannel_logout_counter, (before or 0) + 1)
        self.assertFalse(self._check(first, visitor=None))
        self.assertFalse(self._check(second, visitor=None))
        # The API key still opens the door: it is revoked one by one.
        self.assertEqual(
            self.env["res.users.apikeys"]._check_credentials(scope="rpc", key=key),
            self.user.id,
        )

    def test_moving_the_counter_changes_the_token(self):
        sid = "x" * 64
        before = self.user._compute_session_token(sid)
        self.user.sudo().backchannel_logout_counter = (
            self.user.backchannel_logout_counter or 0
        ) + 1
        self.env.flush_all()
        self.assertNotEqual(self.user._compute_session_token(sid), before)

    def test_an_empty_counter_leaves_odoo_s_token_as_it_was(self):
        """Adding the counter signed nobody out: while it is empty, the
        token is the one Odoo makes from its own fields, without it."""
        sid = "y" * 64
        self.env.cr.execute(
            "UPDATE res_users SET backchannel_logout_counter = NULL WHERE id = %s",
            (self.user.id,),
        )
        self.user.invalidate_recordset()
        self.env.registry.clear_cache()
        with_counter = BaseUsers._compute_session_token(self.user, sid)
        Users = type(self.env["res.users"])
        without = self.user._get_session_token_fields() - {"backchannel_logout_counter"}
        self.env.registry.clear_cache()
        with patch.object(Users, "_get_session_token_fields", lambda self: without):
            odoo_s = BaseUsers._compute_session_token(self.user, sid)
        self.env.registry.clear_cache()
        self.assertTrue(with_counter)
        self.assertEqual(with_counter, odoo_s)
        # A new user's counter is empty too.
        other = new_test_user(self.env, login="seen-new", groups="base.group_user")
        self.env.flush_all()
        self.env.cr.execute(
            "SELECT backchannel_logout_counter FROM res_users WHERE id = %s", (other.id,),
        )
        self.assertIsNone(self.env.cr.fetchone()[0])

    def test_revoke_an_api_key_of_one_s_own_only(self):
        Keys = self.env["res.users.apikeys"]
        Keys.with_user(self.user).sudo()._generate(
            None, "ci", datetime.now() + timedelta(days=1),
        )
        mine = Keys.sudo().search([("user_id", "=", self.user.id)])
        other = new_test_user(self.env, login="seen-other", groups="base.group_user")
        Keys.with_user(other).sudo()._generate(
            None, "theirs", datetime.now() + timedelta(days=1),
        )
        theirs = Keys.sudo().search([("user_id", "=", other.id)])
        sid = self._sign_in()
        data = self._result(sid, "/cloud/sessions/get", visitor=None)
        self.assertEqual(
            [(row["kind"], row["name"]) for row in data["doors"]], [("api_key", "ci")],
        )
        refused = self._result(sid, "/cloud/sessions/revoke",
                             {"kind": "api_key", "door_id": theirs.id}, visitor=None)
        self.assertEqual(refused, {"ok": False})
        self.assertTrue(theirs.exists())
        answer = self._result(sid, "/cloud/sessions/revoke",
                            {"kind": "api_key", "door_id": mine.id}, visitor=None)
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["doors"], [])
        self.assertFalse(Keys.sudo().search([("user_id", "=", self.user.id)]))

    def test_a_trusted_device_is_not_a_way_in(self):
        self.env["res.users.apikeys"].with_user(self.user).sudo()._generate(
            "browser", "Firefox on Linux", datetime.now() + timedelta(days=1),
        )
        self.assertEqual(self.user._ic_doors(), [])

    def _git_token(self):
        """Save a Git token on the panel, as its manager would.

        :rtype: cloud.git.credential
        """
        Provider = type(self.env["cloud.git.provider"])
        with patch.object(Provider, "_check_public_address", autospec=True):
            provider = self.env["cloud.git.provider"].create({
                "name": "GitLab.com", "base_url": "https://gitlab.com",
            })
        return self.env["cloud.git.credential"].create({
            "provider_id": provider.id, "namespace": "acme",
            "token": "glpat-0123456789abcdef",
        })

    def test_a_git_token_is_revoked_by_a_manager_only(self):
        credential = self._git_token()
        self.assertFalse(self.user._ic_revoke_door("git_token", credential.id))
        self.assertTrue(credential.exists())
        self.assertNotIn("git_token", [door["kind"] for door in self.user._ic_doors()])
        manager = new_test_user(
            self.env, login="seen-git-manager",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        doors = [door for door in manager._ic_doors() if door["kind"] == "git_token"]
        self.assertEqual([door["id"] for door in doors], [credential.id])
        # Revoke deletes the panel's copy; the token lives on at the server.
        self.assertIn("GitLab or GitHub", doors[0]["note"])
        self.assertTrue(manager._ic_revoke_door("git_token", credential.id))
        self.assertFalse(credential.exists())
        self.assertFalse(manager._ic_revoke_door("git_token", credential.id))

    def test_browsers_and_systems_have_their_proper_names(self):
        for raw, name in (
            ("macos", "macOS"), ("iphone", "iPhone"), ("ipad", "iPad"),
            ("chromeos", "ChromeOS"), ("windows", "Windows"), ("android", "Android"),
            ("linux", "Linux"), ("chrome", "Chrome"), ("firefox", "Firefox"),
            ("safari", "Safari"), ("edge", "Edge"), ("newbrowser", "Newbrowser"),
            ("", ""), (None, ""),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(access_module.proper_name(raw), name)


@tagged("-at_install", "post_install")
class TestNewWaysInAreTold(_GuardedRequests, HttpCase):
    """An API key or a Git token saved tells whoever holds it."""

    def test_a_new_api_key_is_told_once_a_day(self):
        self.ICP.set_param(PARAM + "notices", "on")
        Keys = self.env["res.users.apikeys"].with_user(self.user).sudo()
        Keys._generate(None, "ci", datetime.now() + timedelta(days=1))
        mails = self._mails()
        self.assertEqual(len(mails), 1)
        self.assertIn("New API key on ", mails.subject)
        self.assertTrue(mails.subject.endswith(": ci"))
        Keys._generate(None, "deploy", datetime.now() + timedelta(days=1))
        self.assertEqual(len(self._mails()), 1)

    def test_a_trusted_device_tells_nothing(self):
        self.ICP.set_param(PARAM + "notices", "on")
        self.env["res.users.apikeys"].with_user(self.user).sudo()._generate(
            "browser", "Firefox on Linux", datetime.now() + timedelta(days=1),
        )
        self.assertFalse(self._mails())

    def test_a_git_token_tells_who_saved_it_and_the_holder(self):
        self.ICP.set_param(PARAM + "notices", "on")
        holder = new_test_user(
            self.env, login="seen-holder", email="seen-holder@example.com",
            groups="base.group_user",
        )
        manager = new_test_user(
            self.env, login="seen-manager", email="seen-manager@example.com",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        Provider = type(self.env["cloud.git.provider"])
        # Where the provider's address is checked against the platform's
        # (a layer), the test's gitlab.com is not resolved.
        with patch.object(Provider, "_check_public_address", autospec=True):
            provider = self.env["cloud.git.provider"].create({
                "name": "GitLab.com", "base_url": "https://gitlab.com",
            })
        Notice = type(self.env["cloud.session.notice"])
        with patch.object(Notice, "_session_notice_holders", lambda self: holder):
            self.env["cloud.git.credential"].with_user(manager).create({
                "provider_id": provider.id, "namespace": "acme",
                "token": "glpat-0123456789abcdef",
            })
        told = self.env["mail.mail"].sudo().search([
            ("email_to", "ilike", "seen-"), ("subject", "like", "New Git token on"),
        ])
        self.assertEqual(
            sorted(told.mapped("email_to")),
            sorted([holder.partner_id.email_formatted, manager.partner_id.email_formatted]),
        )
        self.assertIn("GitLab.com · acme", told[0].body_html)

    def test_the_holder_is_told_whom_to_talk_to(self):
        """The holder's page does not list a member's key: their email
        names who made it instead of asking them to revoke it."""
        self.ICP.set_param(PARAM + "notices", "on")
        holder = new_test_user(
            self.env, login="seen-holder", email="seen-holder@example.com",
            groups="base.group_user",
        )
        Notice = type(self.env["cloud.session.notice"])
        with patch.object(Notice, "_session_notice_holders", lambda self: holder):
            self.env["res.users.apikeys"].with_user(self.user).sudo()._generate(
                None, "ci", datetime.now() + timedelta(days=1),
            )
        to_holder = self.env["mail.mail"].sudo().search(
            [("email_to", "ilike", "seen-holder@example.com")],
        )
        self.assertEqual(len(to_holder), 1)
        self.assertIn(f"talk to {self.user.name}", to_holder.body_html)
        self.assertIn("Users, then Remove", to_holder.body_html)
        self.assertNotIn("If it was you", to_holder.body_html)
        self.assertNotIn("sign out everywhere", to_holder.body_html)
        # Its own user is told what to do as always.
        mine = self._mails()
        self.assertIn("If it was you", mine.body_html)
        self.assertNotIn("talk to", mine.body_html)

    def test_a_name_with_a_new_line_or_markup_stays_text(self):
        self.ICP.set_param(PARAM + "notices", "on")
        self.env["res.users.apikeys"].with_user(self.user).sudo()._generate(
            None, "ci\r\nBcc: x@example.com <b>bold</b>",
            datetime.now() + timedelta(days=1),
        )
        mail = self._mails()
        self.assertEqual(len(mail), 1)
        self.assertNotIn("\n", mail.subject)
        self.assertNotIn("\r", mail.subject)
        self.assertTrue(mail.subject.endswith(": ci Bcc: x@example.com <b>bold</b>"))
        self.assertNotIn("<b>bold</b>", mail.body_html)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", mail.body_html)

    def test_a_notice_failing_in_the_database_keeps_the_key(self):
        """A database error in the notice (two keys at once and the
        holder's quota row conflicting) rolls back alone: the key is
        made and the request goes on."""
        self.ICP.set_param(PARAM + "notices", "on")
        Notice = type(self.env["cloud.session.notice"])
        with (
            patch.object(
                Notice, "_session_notice_within_quota", autospec=True,
                side_effect=_fail_in_the_database,
            ),
            mute_logger(NOTICE_LOGGER, "odoo.sql_db"),
        ):
            self.env["res.users.apikeys"].with_user(self.user).sudo()._generate(
                None, "ci", datetime.now() + timedelta(days=1),
            )
        keys = self.env["res.users.apikeys"].sudo().search([("user_id", "=", self.user.id)])
        self.assertEqual(keys.mapped("name"), ["ci"])
        self.assertFalse(self._mails())

    def test_a_notice_failing_in_the_database_keeps_the_git_token(self):
        self.ICP.set_param(PARAM + "notices", "on")
        # Saved by a person: the test's own user is the inactive superuser,
        # whom no notice is sent to, and the quota would not be reached.
        manager = new_test_user(
            self.env, login="seen-git-saver", email="seen-git-saver@example.com",
            groups="base.group_user,incubacloud.group_cloud_manager",
        )
        Provider = type(self.env["cloud.git.provider"])
        with patch.object(Provider, "_check_public_address", autospec=True):
            provider = self.env["cloud.git.provider"].create({
                "name": "GitLab.com", "base_url": "https://gitlab.com",
            })
        Notice = type(self.env["cloud.session.notice"])
        with (
            patch.object(
                Notice, "_session_notice_within_quota", autospec=True,
                side_effect=_fail_in_the_database,
            ),
            mute_logger(NOTICE_LOGGER, "odoo.sql_db"),
        ):
            self.env["cloud.git.credential"].with_user(manager).create({
                "provider_id": provider.id, "namespace": "acme",
                "token": "glpat-0123456789abcdef",
            })
        self.assertEqual(
            self.env["cloud.git.credential"].search_count([("provider_id", "=", provider.id)]),
            1,
        )

    def test_the_notice_names_the_system_properly(self):
        self.ICP.set_param(PARAM + "notices", "on")
        self.env["cloud.session.notice"]._session_notify(
            self.user, "browser", {}, origin={"browser": "safari", "platform": "iphone"},
        )
        self.assertIn("Safari on iPhone", self._mails().body_html)

    def test_nothing_is_told_while_notices_are_off(self):
        self.env["res.users.apikeys"].with_user(self.user).sudo()._generate(
            None, "ci", datetime.now() + timedelta(days=1),
        )
        self.assertFalse(self._mails())
