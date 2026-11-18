"""A server's known SSH keys (the design note of 17-nov-2026, Fable's review).

What an administrator pastes becomes the fingerprint sshd logs; only the
server's two methods add and remove keys, a key is never edited nor
brought back, its addition is audited, alerted and emailed at once; the
events' cron raises nothing for a sign-in with one in its window and
from where it may sign in from, and the Security view names it.
"""
import base64
import hashlib
import secrets
from datetime import datetime, timedelta
from unittest.mock import patch

import asyncssh
import psycopg2

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.service.model import get_public_method
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

from ..models import cloud_host_known_key as known
from ..models import cloud_security_events as events
from ..models.logs import LogLine
from .test_security_events import (
    MINUTE,
    OTHER_FP,
    PANEL_FP,
    SECOND,
    STRANGER_IP,
    SecurityEventsCase,
    _accepted,
    _change,
    _sudo,
)

LAPTOP_IP = "192.0.2.15"


def _random_fingerprint():
    """A SHA256 fingerprint of no key anyone holds.

    :rtype: str
    """
    return "SHA256:" + base64.b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _ed25519():
    """A real Ed25519 key's public line and its fingerprint, from asyncssh.

    :returns: ``(public line, fingerprint)``.
    :rtype: tuple
    """
    key = asyncssh.generate_private_key("ssh-ed25519", comment="ana@laptop")
    line = key.export_public_key("openssh").decode().strip()
    return line, key.get_fingerprint("sha256")


@tagged("post_install", "-at_install")
class KnownKeysCase(TransactionCase):
    """A server sending its security logs, an Administrator and a Developer.

    After every module is loaded: the users are created through the ORM,
    and on a database with ``account`` installed ``res_partner`` holds its
    NOT NULL ``autopost_bills``, whose default core's registry lacks while
    core loads alone (at install).
    """

    def setUp(self):
        """The server, its users, and the panel's key read as :data:`PANEL_FP`."""
        super().setUp()
        self.Key = self.env["cloud.host.known.key"]
        self.host = self.env["cloud.host"].sudo().create({
            "name": "KnownKeys", "ip_address": "203.0.113.91", "user": "root",
            "wildcard_domain": "knownkeys.example.com",
        })
        Host = type(self.env["cloud.host"])
        self.mocks = {}
        for name, value in (
            ("_panel_key_fingerprints", [PANEL_FP]),
            ("_security_logs_available", True),
        ):
            self.mocks[name] = self._patch(Host, name, value)
        self.admin = self._user("kk-admin", "incubacloud.group_cloud_manager")
        self.dev = self._user("kk-dev", "incubacloud.group_cloud_developer")

    def _patch(self, cls, name, value):
        """Patch *cls*.*name* to return *value* for the test.

        :returns: the started mock, stopped when the test ends.
        :rtype: unittest.mock.MagicMock
        """
        patcher = patch.object(cls, name, autospec=True, return_value=value)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def _user(self, login, group):
        """A user of *group*, with an address the notices write to.

        :rtype: res.users
        """
        return self.env["res.users"].create({
            "name": login, "login": login, "email": f"{login}@example.com",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id, self.env.ref(group).id,
            ])],
        })

    def _add(self, key=None, name="Ana's laptop", networks="", host=None):
        """Add a known key as the Administrator, by the server's method.

        :rtype: cloud.host.known.key
        """
        return (host or self.host).with_user(self.admin)._security_known_key_add(
            key or _random_fingerprint(), name, networks,
        )


class TestKnownKeyReading(KnownKeysCase):
    """What may be pasted, and what is refused."""

    def test_a_public_key_line_gives_ssh_keygen_s_fingerprint(self):
        """A real .pub line, also behind authorized_keys' options."""
        line, fingerprint = _ed25519()
        self.assertEqual(known.read_key(self.env, line), (fingerprint, "ED25519"))
        with_options = f'from="192.0.2.0/24",no-pty {line}'
        self.assertEqual(known.read_key(self.env, with_options)[0], fingerprint)
        blob = base64.b64decode(line.split()[1])
        expected = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        self.assertEqual(fingerprint, expected)

    def test_a_fingerprint_with_or_without_prefix_padding_or_keygen_s_line(self):
        """Case is kept: a fingerprint is base64."""
        fingerprint = "SHA256:cXvJcLHKzzz4C5Y1vNtxdnADDOfisnXN3pSJXtXlAEE"
        bare = fingerprint.split(":", 1)[1]
        for text in (fingerprint, bare, bare + "=", f"  {fingerprint}\n"):
            self.assertEqual(known.read_key(self.env, text), (fingerprint, ""), text)
        line = f"256 {fingerprint} reinier@laptop (ED25519)"
        self.assertEqual(known.read_key(self.env, line), (fingerprint, "ED25519"))
        lowered = "SHA256:" + bare.lower()
        self.assertEqual(known.read_key(self.env, lowered)[0], lowered)
        self.assertNotEqual(lowered, fingerprint)

    def test_a_private_key_is_refused_without_repeating_it(self):
        """Neither its text nor a part of it reaches the error."""
        private = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()
        with self.assertRaises(ValidationError) as caught:
            known.read_key(self.env, private)
        self.assertIn("private key", str(caught.exception))
        body = private.splitlines()[1]
        self.assertNotIn(body[:20], str(caught.exception))
        with self.assertRaises(ValidationError):
            self._add(private)
        self.assertFalse(self.Key.sudo().with_context(active_test=False).search([
            ("host_id", "=", self.host.id),
        ]))

    def test_md5_a_certificate_and_garbage_are_refused(self):
        """Each with its own words."""
        cases = {
            "MD5:16:27:ac:a5:76:28:2d:36:63:1b:56:4d:eb:df:a6:48": "MD5",
            "16:27:ac:a5:76:28:2d:36:63:1b:56:4d:eb:df:a6:48": "MD5",
            "ssh-ed25519-cert-v01@openssh.com AAAAIHNzaC1lZDI1NTE5LWNlcnQ= x": "certificate",
            "ssh-ed25519 notbase64!": "not a public key",
            "hello": "not a public key",
            "": "Paste",
        }
        for text, words in cases.items():
            with self.subTest(text=text), self.assertRaises(ValidationError) as caught:
                known.read_key(self.env, text)
            self.assertIn(words, str(caught.exception))

    def test_the_panel_s_own_key_and_its_previous_one_are_refused(self):
        """A known key must never cover the panel's key from elsewhere."""
        with self.assertRaises(ValidationError):
            self._add(PANEL_FP)
        previous = _random_fingerprint()
        self.host.sudo().security_previous_key_fingerprint = previous
        with self.assertRaises(ValidationError):
            self._add(previous)

    def test_networks_are_normalized_and_bad_ones_refused(self):
        """IPv6 by its /64; /0, garbage and too many refused."""
        text, nets = known.read_networks(self.env, "192.0.2.15, 198.51.100.0/24\n2001:db8::7")
        self.assertEqual(text, "192.0.2.15/32, 198.51.100.0/24, 2001:db8::/64")
        self.assertEqual(len(nets), 3)
        self.assertEqual(known.read_networks(self.env, ""), ("", ()))
        for bad in ("0.0.0.0/0", "::/0", "192.0.2.300", "laptop"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                known.read_networks(self.env, bad)
        many = ", ".join(f"192.0.2.{n}" for n in range(1, known.NETWORKS_MAX + 2))
        with self.assertRaises(ValidationError):
            known.read_networks(self.env, many)
        with self.assertRaises(ValidationError):
            self._add(networks="0.0.0.0/0")

    def test_too_wide_networks_and_ipv4_written_as_ipv6_are_refused(self):
        """Two halves of the space are any address too; ::ffff: is IPv4."""
        for bad in ("0.0.0.0/1", "128.0.0.0/1", "10.0.0.0/7", "::/1", "2000::/16",
                    "::ffff:192.0.2.15", "::ffff:0:0/96"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                known.read_networks(self.env, bad)
        self.assertEqual(known.read_networks(self.env, "10.0.0.0/8")[0], "10.0.0.0/8")
        self.assertEqual(known.read_networks(self.env, "2001:db8::/32")[0], "2001:db8::/32")

    def test_the_reading_stops_past_the_limit(self):
        """A long list is refused at its first entry past the limit."""
        many = " ".join(["192.0.2.1"] * (known.NETWORKS_MAX + 1) + ["not-an-address"] * 1000)
        with self.assertRaises(ValidationError) as caught:
            known.read_networks(self.env, many)
        self.assertIn("at most", str(caught.exception))

    def test_a_name_is_cleaned_and_unique_among_the_active_keys(self):
        """No line breaks, control characters or angle quotes; case-blind."""
        self.assertEqual(known.clean_name(self.env, " Ana's\n«laptop»\x07 <b> "), "Ana's laptop b")
        for bad in ("", " \n ", "x" * (known.NAME_MAX + 1)):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                known.clean_name(self.env, bad)
        first = self._add(name="Laptop")
        with self.assertRaises(ValidationError):
            self._add(name="laptop")
        with self.assertRaises(ValidationError):
            self._add(first.fingerprint, name="Another")
        self.host.with_user(self.admin)._security_known_key_remove(first.id)
        again = self._add(first.fingerprint, name="Laptop")
        self.assertNotEqual(again, first)
        self.assertTrue(again.active)


class TestKnownKeyAccess(KnownKeysCase):
    """Only the server's two methods change the keys."""

    def test_nobody_creates_or_writes_a_key_over_the_orm(self):
        """Administrators included: the model is read-only to every role."""
        key = self._add()
        for user in (self.admin, self.dev):
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    self.Key.with_user(user).create({
                        "host_id": self.host.id, "name": "x",
                        "fingerprint": _random_fingerprint(),
                    })
                with self.assertRaises(AccessError):
                    key.with_user(user).write({"name": "renamed"})
                with self.assertRaises(AccessError):
                    key.with_user(user).action_archive()
                self.assertEqual(key.with_user(user).read(["name"])[0]["name"], "Ana's laptop")

    def test_the_methods_are_not_callable_remotely(self):
        """No RPC, API key or agent reaches them: they are private."""
        for name in ("_security_known_key_add", "_security_known_key_remove"):
            with self.subTest(name=name), self.assertRaises(AccessError):
                get_public_method(self.host, name)

    def test_a_developer_may_not_add_nor_remove(self):
        """The role is checked by the method itself."""
        key = self._add()
        with self.assertRaises(AccessError):
            self.host.with_user(self.dev)._security_known_key_add(_random_fingerprint(), "x")
        with self.assertRaises(AccessError):
            self.host.with_user(self.dev)._security_known_key_remove(key.id)
        self.assertTrue(key.active)

    def test_nowhere_security_logs_are_withheld(self):
        """The card and its methods exist only where the logs may be sent."""
        self.mocks["_security_logs_available"].return_value = False
        with self.assertRaises(UserError):
            self._add()

    def test_a_key_never_changes_and_never_comes_back(self):
        """Only its name; archived stays archived; never deleted."""
        key = self._add(networks="192.0.2.0/24").sudo()
        for vals in (
            {"fingerprint": _random_fingerprint()},
            {"allowed_networks": ""},
            {"host_id": self.env["cloud.host"].sudo().create({
                "name": "KK2", "ip_address": "203.0.113.92", "user": "root",
                "wildcard_domain": "kk2.example.com",
            }).id},
            {"added_at": fields.Datetime.now() - timedelta(days=1)},
            {"active": True},
        ):
            with self.subTest(vals=sorted(vals)), self.assertRaises(UserError):
                key.write(vals)
        key.write({"name": "Ana's new laptop"})
        self.assertEqual(key.name, "Ana's new laptop")
        # Whoever archives it, the date is now and the user is the one acting.
        long_ago = fields.Datetime.now() - timedelta(days=30)
        key.with_user(self.admin).sudo().write({
            "active": False, "removed_at": long_ago, "removed_by": self.dev.id,
        })
        self.assertEqual(key.removed_by, self.admin)
        self.assertGreater(key.removed_at, long_ago + timedelta(days=29))
        self.assertFalse(key.active)
        with self.assertRaises(UserError):
            key.write({"active": True})
        with self.assertRaises(UserError):
            key.action_unarchive()
        with self.assertRaises(UserError):
            key.unlink()
        with self.assertRaises(UserError):
            self.host.with_user(self.admin)._security_known_key_remove(key.id)

    def test_a_removed_key_always_has_its_date(self):
        """The database refuses an archived key without one."""
        key = self._add()
        self.env.flush_all()
        with mute_logger("odoo.sql_db"), self.assertRaises(psycopg2.errors.CheckViolation):
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    "UPDATE cloud_host_known_key SET active = false, removed_at = NULL "
                    "WHERE id = %s", (key.id,),
                )


class TestKnownKeyTold(KnownKeysCase):
    """An addition is audited, alerted and emailed at once."""

    def setUp(self):
        """Notices on, as the layers that run the platform have them."""
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.session_guard.notices", "on",
        )

    def _alerts(self):
        """The active alerts of additions on the server.

        :rtype: cloud.alert
        """
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", known.CODE_KNOWN_ADDED), ("state", "=", "active"),
            ("host_id", "=", self.host.id),
        ])

    def _mails(self, user):
        """The notices of known keys queued for *user*.

        :rtype: mail.mail
        """
        return self.env["mail.mail"].sudo().search([
            ("email_to", "ilike", user.email),
            ("subject", "ilike", "Known SSH key added"),
        ])

    def test_each_addition_is_critical_audited_and_emailed(self):
        """Two the same day: two alerts, two audit rows, two emails each."""
        other = self._user("kk-admin2", "incubacloud.group_cloud_manager")
        first = self._add(name="Laptop", networks="192.0.2.15")
        second = self._add(name="CI runner")
        alerts = self._alerts()
        self.assertEqual(len(alerts), 2)
        self.assertEqual(set(alerts.mapped("level")), {"critical"})
        self.assertNotIn(known.CODE_KNOWN_ADDED, events.EVENT_CODES)
        text = alerts.filtered(lambda a: a.payload.get("key_id") == first.id).message
        self.assertIn("kk-admin added the known SSH key «Laptop»", text)
        self.assertIn(first.fingerprint, text)
        self.assertIn("It may sign in only from some addresses.", text)
        for alert in alerts:
            self.assertNotIn("192.0.2.15", alert.message)
            self.assertNotIn("192.0.2.15", str(alert.payload))
        self.assertIs(
            alerts.filtered(lambda a: a.payload.get("key_id") == first.id).payload["only_from"],
            True,
        )
        audits = self.env["cloud.audit.log"].sudo().search([
            ("host_id", "=", self.host.id), ("action", "=", "Known SSH key added"),
        ])
        self.assertEqual(len(audits), 2)
        self.assertEqual(set(audits.mapped("user_id")), {self.admin})
        for user in (self.admin, other):
            mails = self._mails(user)
            self.assertEqual(len(mails), 2, user.login)
            self.assertIn("CI runner", " ".join(mails.mapped("subject")))
        self.assertFalse(self._mails(self.dev))
        self.assertTrue(second.active)

    def test_removing_a_key_leaves_its_alert_open_and_audits(self):
        """The alert is dismissed by whoever reads it, not by the removal."""
        key = self._add()
        self.host.with_user(self.admin)._security_known_key_remove(key.id)
        self.assertEqual(len(self._alerts()), 1)
        self.assertTrue(self.env["cloud.audit.log"].sudo().search([
            ("host_id", "=", self.host.id), ("action", "=", "Known SSH key removed"),
            ("user_id", "=", self.admin.id),
        ]))

    def test_a_key_whose_alert_cannot_be_raised_is_not_added(self):
        """A cancelled job's context: refused, and no row nor audit left."""
        fingerprint = _random_fingerprint()
        with self.assertRaises(UserError):
            self.host.with_user(self.admin).with_context(
                ic_job_cancelled=True,
            )._security_known_key_add(fingerprint, "Laptop")
        self.assertFalse(self.Key.sudo().with_context(active_test=False).search([
            ("fingerprint", "=", fingerprint),
        ]))
        self.assertFalse(self.env["cloud.audit.log"].sudo().search([
            ("host_id", "=", self.host.id), ("details", "ilike", fingerprint),
        ]))

    def test_the_card_names_addresses_to_managers_alone(self):
        """A Developer reads the keys, without their addresses or buttons."""
        self._add(networks="192.0.2.15")
        mine = self.host.with_user(self.admin)._security_known_keys_answer(True)
        theirs = self.host.with_user(self.dev)._security_known_keys_answer(False)
        self.assertTrue(mine["can_edit"])
        self.assertEqual(mine["rows"][0]["allowed_networks"], "192.0.2.15/32")
        self.assertFalse(theirs["can_edit"])
        self.assertNotIn("allowed_networks", theirs["rows"][0])
        self.assertNotIn("192.0.2.15", str(theirs))
        self.assertTrue(theirs["rows"][0]["only_from"])

    def test_the_networks_field_is_read_by_managers_alone(self):
        """Over the ORM too, not only on the card."""
        key = self._add(networks="192.0.2.15")
        self.assertEqual(key.with_user(self.admin).allowed_networks, "192.0.2.15/32")
        with self.assertRaises(AccessError):
            key.with_user(self.dev).read(["allowed_networks"])
        self.assertNotIn("allowed_networks", key.with_user(self.dev).read()[0])

    def test_removed_keys_are_listed_last_ones_first_with_their_count(self):
        """The card lists every active key and the last removed ones."""
        Remove = self.host.with_user(self.admin)._security_known_key_remove
        for n in range(known.REMOVED_SHOWN + 2):
            Remove(self._add(name=f"Old {n}").id)
        answer = self.host._security_known_keys_answer(True)
        removed = [row for row in answer["rows"] if not row["active"]]
        self.assertEqual(len(removed), known.REMOVED_SHOWN)
        self.assertEqual(answer["removed_count"], known.REMOVED_SHOWN + 2)

    def test_a_server_keeps_twenty_active_keys_at_most(self):
        """One more is refused until one is removed."""
        keys = [self._add(name=f"Key {n}") for n in range(known.ACTIVE_MAX)]
        with self.assertRaises(ValidationError):
            self._add(name="One more")
        self.host.with_user(self.admin)._security_known_key_remove(keys[0].id)
        self.assertTrue(self._add(name="One more").active)


@tagged("post_install", "-at_install")
class TestKnownKeyCron(SecurityEventsCase):
    """The events' cron with known keys; after every module, as :class:`KnownKeysCase`."""

    def setUp(self):
        """An Administrator who adds keys; notices off."""
        super().setUp()
        self.admin = self.env["res.users"].create({
            "name": "kk-cron", "login": "kk-cron",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("incubacloud.group_cloud_manager").id,
            ])],
        })

    def _key(self, fingerprint=OTHER_FP, ago=10 * MINUTE, networks="", host=None, name="Laptop"):
        """Add a known key, added *ago* before the cron's now.

        The dates are set in SQL: the key's window is compared with the
        lines' instants, which the test moves with the cron's clock.

        :rtype: cloud.host.known.key
        """
        host = host or self.host
        key = host.with_user(self.admin)._security_known_key_add(fingerprint, name, networks)
        self._date(key, "added_at", self.now - ago)
        return key

    def _date(self, key, column, ns):
        """Write one of a key's dates in SQL, at *ns*."""
        self.env.flush_all()
        self.env.cr.execute(
            f"UPDATE cloud_host_known_key SET {column} = %s WHERE id = %s",  # noqa: S608 - a column of ours
            (events._to_datetime(ns).replace(microsecond=0), key.id),
        )
        key.invalidate_recordset()

    def _remove(self, key, ago):
        """Remove *key*, removed *ago* before the cron's now."""
        self.host.with_user(self.admin)._security_known_key_remove(key.id)
        self._date(key, "removed_at", self.now - ago)

    def _events(self):
        """The events the cron remembered on the server.

        :rtype: cloud.security.event
        """
        return self.env["cloud.security.event"].sudo().search([("host_id", "=", self.host.id)])

    def test_a_known_key_raises_nothing_and_is_not_remembered(self):
        """Recorded as used; no alert, no event, from any address."""
        key = self._key()
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP, user="root"))
        self._run()
        for code in events.EVENT_CODES:
            self.assertFalse(self._alerts(code), code)
        self.assertFalse(self._events())
        self.assertEqual(
            key.last_used_at,
            events._to_datetime(self.now - 2 * MINUTE).replace(microsecond=0),
        )
        self.probe.read.assert_not_called()

    def test_known_keys_that_cannot_be_read_leave_the_sign_in_foreign(self):
        """Fail closed: the alert is raised as if no key were known."""
        self._key()
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP))
        with patch.object(
            type(self.env["cloud.host"]), "_security_known_keys", autospec=True,
            side_effect=ValueError("broken row"),
        ), self.assertLogs(events.__name__, "ERROR"):
            self._run()
        self.assertIn("with a key that is not the panel's",
                      self._alerts(events.CODE_FOREIGN).message)

    def test_a_database_error_reading_the_keys_leaves_the_sign_in_foreign(self):
        """The read has a savepoint of its own: the alert is still written."""
        self._key()
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP))

        def broken(host):
            """Fail as a query on a missing table does."""
            host.env.cr.execute("SELECT * FROM cloud_host_known_key_missing")

        with patch.object(
            type(self.env["cloud.host"]), "_security_known_keys", autospec=True,
            side_effect=broken,
        ), mute_logger("odoo.sql_db"), self.assertLogs(events.__name__, "ERROR"):
            self._run()
        self.assertIn("with a key that is not the panel's",
                      self._alerts(events.CODE_FOREIGN).message)

    def test_a_last_use_that_cannot_be_written_keeps_the_pass(self):
        """Logged; the pass's alerts and cursors stand."""
        key = self._key()
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP, user="root"), ago=3 * MINUTE)
        self._add("accepted", _accepted("203.0.113.67", "SHA256:" + "Z" * 43))
        with patch.object(
            type(self.env["cloud.host.known.key"]), "write", autospec=True,
            side_effect=ValueError("disk full"),
        ), self.assertLogs(events.__name__, "ERROR"):
            self._run()
        self.assertTrue(self._alerts(events.CODE_FOREIGN))
        self.assertFalse(key.last_used_at)
        self.assertEqual(self._cursor("accepted"), self.now - events.SETTLE_NS)

    def test_a_known_key_of_another_server_is_still_foreign(self):
        """Each server's own list."""
        self._key(host=self._host("S2"))
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP))
        self._run()
        self.assertIn("with a key that is not the panel's",
                      self._alerts(events.CODE_FOREIGN).message)

    def test_a_sign_in_from_before_the_addition_is_foreign(self):
        """Also one read late, from a server back from a day's silence."""
        self._key(ago=MINUTE)
        self.host.write({
            "security_last_line_at": events._to_datetime(self.now - 23 * 3600 * SECOND),
        })
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP), ago=22 * 3600 * SECOND)
        self.reader.query.return_value = [({"host_id": str(self.host.id)}, 3.0)]
        self._run()
        alert = self._alerts(events.CODE_FOREIGN)
        self.assertEqual(alert.payload["count"], 1)
        self.assertEqual(alert.payload["last"]["reason"], "key")

    def test_removed_it_alerts_again_and_its_window_stays_known(self):
        """The sign-in in the window, read again, still nobody's; the next alerts."""
        key = self._key(ago=10 * MINUTE)
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP), ago=5 * MINUTE)
        self._run()
        self.assertFalse(self._alerts(events.CODE_FOREIGN))
        self.assertFalse(self._events())
        self._remove(key, ago=3 * MINUTE)
        self._add("accepted", _accepted("203.0.113.67", OTHER_FP), ago=MINUTE)
        self._run()
        alert = self._alerts(events.CODE_FOREIGN)
        self.assertEqual(alert.payload["count"], 1)
        self.assertIn("203.0.113.67", alert.message)
        self.assertEqual(len(self._events()), 1)

    def test_from_outside_its_addresses_it_is_critical_with_its_name(self):
        """Only from: inside nothing, outside a critical alert naming the key."""
        self._key(networks="192.0.2.0/24")
        self._add("accepted", _accepted(LAPTOP_IP, OTHER_FP), ago=3 * MINUTE)
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP), ago=2 * MINUTE)
        self._run()
        alert = self._alerts(events.CODE_FOREIGN)
        self.assertEqual(alert.level, "critical")
        self.assertEqual(alert.payload["count"], 1)
        self.assertIn(STRANGER_IP, alert.message)
        self.assertIn("with the known key «Laptop» from an address it may not sign in from",
                      alert.message)

    def test_on_a_password_server_a_known_key_covers_another_user(self):
        """Without the panel's key, a known key still names the sign-in."""
        self.fingerprints[self.host.id] = []
        self._key()
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP, user="ana"))
        self._run()
        self.assertFalse(self._alerts(events.CODE_FOREIGN))

    def test_the_panel_s_key_from_elsewhere_is_still_elsewhere(self):
        """A known key never covers the panel's own."""
        self._key()
        self._add("accepted", _accepted(STRANGER_IP, PANEL_FP))
        self._run()
        self.assertTrue(self._alerts(events.CODE_ELSEWHERE))

    def test_sudo_and_changes_still_alert_with_a_known_key_signed_in(self):
        """The lines carry no session to tie them to the sign-in."""
        self._key()
        self._add("accepted", _accepted(STRANGER_IP, OTHER_FP, user="probador"), ago=3 * MINUTE)
        self._add("sudo", _sudo(user="probador"))
        for line in _change(self.now - 2 * MINUTE, auid=2002):
            self._add("auditd", line)
        self._run()
        self.assertFalse(self._alerts(events.CODE_FOREIGN))
        self.assertTrue(self._alerts(events.CODE_SUDO))
        self.assertEqual(self._alerts(events.CODE_CHANGE).level, "critical")


class TestKnownKeyView(KnownKeysCase):
    """The Security view's table of sign-ins."""

    def _rows(self, *lines):
        """The sign-ins table of *lines* ``(ns, fingerprint, ip)``.

        :rtype: list[dict]
        """
        read = {"lines": [
            LogLine(self.host.id, ns, _accepted(ip, fingerprint, user="root"), "sshd")
            for ns, fingerprint, ip in lines
        ]}
        return list(reversed(self.host._security_accepted(read, lambda _ip: None)["rows"]))

    def test_an_archived_key_names_the_sign_ins_of_its_window(self):
        """Before, inside and after its window; from outside its addresses."""
        fingerprint = _random_fingerprint()
        key = self._add(fingerprint, name="Laptop", networks="192.0.2.0/24").sudo()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE cloud_host_known_key SET added_at = added_at - interval '1 hour' "
            "WHERE id = %s", (key.id,),
        )
        key.invalidate_recordset()
        self.host.with_user(self.admin)._security_known_key_remove(key.id)
        added = events._to_ns(key.added_at)
        removed = events._to_ns(key.removed_at)
        rows = self._rows(
            (added - MINUTE, fingerprint, LAPTOP_IP),
            (added + MINUTE, fingerprint, LAPTOP_IP),
            (added + 2 * MINUTE, fingerprint, STRANGER_IP),
            (removed + MINUTE, fingerprint, LAPTOP_IP),
        )
        self.assertEqual(
            [(r["key"], r["attribution"], r["known_label"]) for r in rows],
            [
                ("other", "foreign_key", None),
                ("known", "known_key", "Laptop"),
                ("known", "known_key_elsewhere", "Laptop"),
                ("other", "foreign_key", None),
            ],
        )

    def test_the_window_s_edges_and_the_gap_between_two_additions(self):
        """Known from its addition, not at its removal; nothing in between."""
        t0, t1, t2 = (datetime(2026, 11, 17, h) for h in (8, 9, 10))
        fp = _random_fingerprint()
        keys = [
            known.KnownKey(1, "Laptop", fp, (), t0, t1),
            known.KnownKey(2, "Laptop", fp, (), t2, None),
        ]
        second = timedelta(seconds=1)
        self.assertIsNone(known.match_known_key(keys, fp, t0 - second))
        self.assertEqual(known.match_known_key(keys, fp, t0).id, 1)
        self.assertIsNone(known.match_known_key(keys, fp, t1))
        self.assertIsNone(known.match_known_key(keys, fp, t1 + timedelta(minutes=30)))
        self.assertEqual(known.match_known_key(keys, fp, t2).id, 2)
        self.assertIsNone(known.match_known_key(keys, _random_fingerprint(), t2))

    def test_ipv6_by_its_64(self):
        """Inside the /64 of the address given, any interface; outside, none."""
        nets = known.read_networks(self.env, "2001:db8:1:2::15")[1]
        key = known.KnownKey(1, "Laptop", "SHA256:x", nets, datetime(2026, 1, 1), None)
        self.assertTrue(key.allows("2001:db8:1:2:abcd::9"))
        self.assertFalse(key.allows("2001:db8:1:3::15"))
        self.assertFalse(key.allows("192.0.2.15"))
        self.assertFalse(key.allows("not an address"))

    def test_the_board_s_cache_changes_with_the_keys(self):
        """A key added or removed is a new stamp."""
        before = self.host._security_known_keys_stamp()
        key = self._add()
        added = self.host._security_known_keys_stamp()
        self.host.with_user(self.admin)._security_known_key_remove(key.id)
        self.assertEqual(len({before, added, self.host._security_known_keys_stamp()}), 3)
