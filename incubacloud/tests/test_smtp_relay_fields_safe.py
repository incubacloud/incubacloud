"""An instance's mail relay fields hold a host, a port and a user, nothing else.

The model keeps the relay host, port and user and the admin email to
their grammar, and the copier answers render each as its own text.
"""
import yaml

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestSmtpRelayFieldsSafe(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "Relay"})
        self.inst = self.env["cloud.instance"].create({
            "name": "relaysafe",
            "project_id": self.project.id,
            "environment": "production",
        })

    def test_valid_values_are_kept(self):
        """A hostname, an IPv4 address, a port and an email user pass."""
        self.inst.write({
            "smtp_relay_host": "smtp.Example.com",
            "smtp_relay_port": 465,
            "smtp_relay_user": "mailer+odoo@example.com",
            "odoo_admin_email": "admin@example.com",
        })
        self.inst.write({"smtp_relay_host": "192.0.2.25"})
        self.inst.write({"smtp_relay_user": "plain_user-1"})
        self.assertEqual(self.inst.smtp_relay_user, "plain_user-1")

    def test_host_outside_hostname_grammar_is_refused(self):
        """Quotes, spaces, colons, braces and line breaks are no hostname."""
        for value in (
            'smtp.example.com"',
            "smtp.example.com\nx",
            "smtp example.com",
            "smtp.example.com:587",
            "{{ host }}",
            "-smtp.example.com",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.inst.write({"smtp_relay_host": value})

    def test_port_out_of_range_is_refused(self):
        """A TCP port is 1-65535; 0 keeps meaning "the default"."""
        self.inst.write({"smtp_relay_port": 0})
        for value in (-1, 65536):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.inst.write({"smtp_relay_port": value})

    def test_user_and_admin_email_refuse_control_and_quote_characters(self):
        """Line breaks, control characters and double quotes; a backslash
        in the admin email."""
        for field in ("smtp_relay_user", "odoo_admin_email"):
            for value in (
                'a@b.c"',
                "a@b.c\nx: y",
                "a@b.c\rx",
                "a@b.c\tx",
                "a@b.c\x1bx",
                "a@b.c\u2028x",
                "a@b.c\x85x",
            ):
                with self.subTest(field=field, value=value), \
                        self.assertRaises(ValidationError):
                    self.inst.write({field: value})
        with self.assertRaises(ValidationError):
            self.inst.write({"odoo_admin_email": "a@b.c\\"})

    def test_a_windows_login_is_a_valid_user_and_renders_as_itself(self):
        """``DOMAIN\\user``: the answers escape the backslash, so the
        value reads back as itself."""
        login = "CORP\\mailer"
        self.inst.write({"smtp_relay_user": login})
        self.assertEqual(self.inst.smtp_relay_user, login)
        answers = self.inst._render_copier_answers()
        self.assertEqual(answers["smtp_relay_user"], "CORP\\\\mailer")
        for key in ("smtp_relay_user", "smtp_default_from"):
            with self.subTest(key=key):
                doc = yaml.safe_load(f'K: "{answers[key]}"\n')
                self.assertEqual(doc, {"K": login})

    def test_answers_escape_a_value_stored_before_the_check(self):
        """A row saved before the check renders as its own text."""
        legacy = 'a@b.c"\nx: "y'
        self.env.cr.execute(
            "UPDATE cloud_instance SET smtp_relay_user = %s,"
            " smtp_relay_host = %s WHERE id = %s",
            (legacy, "smtp.example.com", self.inst.id),
        )
        self.inst.invalidate_recordset()
        answers = self.inst._render_copier_answers()
        for key in ("smtp_relay_user", "smtp_default_from"):
            with self.subTest(key=key):
                doc = yaml.safe_load(f'K: "{answers[key]}"\n')
                self.assertEqual(doc, {"K": legacy})
        doc = yaml.safe_load(f'K: "{answers["smtp_canonical_default"]}"\n')
        self.assertEqual(list(doc), ["K"])
