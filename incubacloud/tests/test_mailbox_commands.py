"""The mailbox commands, and the projections run for real.

The two programs that read a staging's captured mail are Python source
embedded in a shell command, and a string check would prove nothing
about either: what matters is that a quoted-printable subject comes
back as ``ñ``, that an attachment is listed without its bytes, and that
a body too big to ship is cut and says so.

So the projections are executed here — the same way the host runs them,
``python3`` reading MailHog's JSON on stdin — against messages built
with Python's own ``email`` package, which is what a real Odoo uses to
produce them. The whole pipeline is also run through ``sh`` once with
``docker compose`` and ``curl`` stubbed out, because the part that
breaks first in a command like this is its quoting, not its logic.
"""
import json
import os
import subprocess
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.controllers._data_load._mailbox import (
    _LIST_PROGRAM,
    _MAX_ATTACHMENTS,
    _MAX_HTML,
    _MAX_TEXT,
    clamp_limit,
    is_safe_mail_id,
    mailbox_clear_command,
    mailbox_list_command,
    mailbox_message_command,
)

#: A real id, as MailHog mints them: base64 plus its own hostname.
REAL_ID = "g7QLsZwC3-qJ5mZPWkc1Egpo92AsozkfpawrqkNiLf4=@mailhog.example"


def _raw(subject="Aviso de contraseña", text="Cuerpo en texto.",
         html="<p>Cuerpo en <b>HTML</b></p>", attachments=(),
         to=("someone@example.org",)):
    """Build one raw message the way an Odoo instance would send it.

    :param subject: Subject header, encoded by ``email`` if non-ASCII
    :param text: plain-text body, or ``''`` for none
    :param html: HTML body, or ``''`` for none
    :param attachments: ``(filename, mimetype, bytes)`` triples
    :param to: recipient addresses
    :return: the message as the string MailHog stores in ``Raw.Data``
    """
    msg = EmailMessage()
    msg["From"] = "Odoo <noreply@staging.example.com>"
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(text or "")
    if html:
        msg.add_alternative(html, subtype="html")
    for name, mimetype, payload in attachments:
        maintype, _slash, subtype = mimetype.partition("/")
        msg.add_attachment(
            payload, maintype=maintype, subtype=subtype, filename=name,
        )
    return msg.as_string()


def _item(raw, message_id=REAL_ID, created="2026-09-23T08:19:09.261059533Z",
          to=("someone@example.org",)):
    """Wrap a raw message in the envelope MailHog's API returns.

    :param raw: the raw message text
    :param message_id: value of the ``ID`` field
    :param created: MailHog's own receipt stamp, RFC 3339 with nanos
    :param to: recipient addresses, as MailHog splits them
    :return: dict shaped like one item of ``/api/v2/messages``
    """
    return {
        "ID": message_id,
        "Created": created,
        "From": {"Mailbox": "noreply", "Domain": "staging.example.com"},
        "To": [
            {"Mailbox": a.split("@")[0], "Domain": a.split("@")[1]}
            for a in to
        ],
        "Content": {"Size": len(raw), "Headers": {}},
        "Raw": {"Data": raw, "From": "", "To": [], "Helo": ""},
    }


def _project(program, payload):
    """Run one projection exactly as the host runs it, and decode it.

    :param program: ``_LIST_PROGRAM`` or ``_MESSAGE_PROGRAM``
    :param payload: what MailHog would have answered
    :return: the decoded JSON object the panel receives
    """
    proc = subprocess.run(
        [sys.executable, "-c", program],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _message_program():
    """Return the single-message projection.

    Imported through the module rather than at the top of this file so
    the underscore-prefixed name stays a private detail of one import.
    """
    from odoo.addons.incubacloud.controllers._data_load import _mailbox
    return _mailbox._MESSAGE_PROGRAM


class TestMailIdValidation(BaseCase):
    """The id reaches a URL inside a shell command.

    Two mechanisms, each doing its own job: the validator answers "is
    this shaped like an id at all", and the URL encoding answers "can
    what it contains leave the path". Neither is asked to cover for the
    other, so both are tested for what they actually do.
    """

    def test_a_real_mailhog_id_is_accepted(self):
        self.assertTrue(is_safe_mail_id(REAL_ID))

    def test_anything_shell_shaped_is_refused(self):
        for bad in (
            "a@b; rm -rf /",
            "a@b`id`",
            "a@b$(id)",
            "a@b'\"",
            "a@b with spaces",
            "a@b\nnewline",
            "no-at-sign",
            "@mailhog.example",
            "a@",
            "",
            None,
            12345,
        ):
            self.assertFalse(is_safe_mail_id(bad), bad)

    def test_an_id_too_long_to_be_one_is_refused(self):
        self.assertFalse(is_safe_mail_id("a" * 600 + "@mailhog.example"))

    def test_the_id_is_url_encoded_into_the_command(self):
        """``+`` and ``/`` are legal base64 and would break a path."""
        cmd = mailbox_message_command("~/inst", "test.yaml", "a+b/c=@mailhog")
        self.assertIn("a%2Bb%2Fc%3D%40mailhog", cmd)
        self.assertNotIn("a+b/c=@mailhog", cmd)

    def test_a_traversal_shaped_id_cannot_leave_the_path(self):
        """``/`` passes the shape check because base64 contains it.

        What stops it is the encoding, not the validator — so this is
        where that is guarded. The id reaches MailHog as one literal
        path segment and matches no message.
        """
        evil = "../../etc/passwd@mailhog"
        self.assertTrue(is_safe_mail_id(evil))
        cmd = mailbox_message_command("~/inst", "test.yaml", evil)
        self.assertNotIn("../..", cmd)
        self.assertIn("..%2F..%2Fetc%2Fpasswd%40mailhog", cmd)


class TestLimitClamping(BaseCase):
    """MailHog returns every listed message in full, so the page is a cost."""

    def test_garbage_falls_back_to_the_default(self):
        for value in (None, "", "lots", {}, []):
            self.assertEqual(clamp_limit(value), 50)

    def test_the_page_is_bounded_at_both_ends(self):
        self.assertEqual(clamp_limit(0), 50)
        self.assertEqual(clamp_limit(-10), 1)
        self.assertEqual(clamp_limit(1), 1)
        self.assertEqual(clamp_limit(25), 25)
        self.assertEqual(clamp_limit(10000), 100)

    def test_the_clamped_value_is_what_reaches_the_url(self):
        cmd = mailbox_list_command("~/inst", "test.yaml", limit=10000)
        self.assertIn("limit=100", cmd)
        self.assertNotIn("limit=10000", cmd)

    def test_a_negative_offset_never_reaches_the_url(self):
        cmd = mailbox_list_command("~/inst", "test.yaml", start=-5)
        self.assertIn("start=0", cmd)


class TestCommandShape(BaseCase):
    """What the command does before anything is parsed."""

    def test_it_reads_the_catcher_from_inside_the_stack(self):
        """The API is not published; only the compose network reaches it."""
        cmd = mailbox_list_command("~/inst", "test.yaml")
        self.assertIn("docker compose -f test.yaml exec -T odoo curl", cmd)
        self.assertIn("http://smtplocal:8025", cmd)

    def test_the_projection_runs_on_the_host(self):
        """A staging can be Odoo 7.0; its Python is not ours to assume."""
        cmd = mailbox_list_command("~/inst", "test.yaml")
        # The program is piped into the host's python3, on the right of
        # the pipe — which is the shell we SSH into, not the container.
        self.assertIn('| python3 -c "$PROG"', cmd)
        self.assertNotIn("exec -T odoo python", cmd)

    def test_the_home_directory_still_expands(self):
        cmd = mailbox_list_command("~/instances/foo", "test.yaml")
        self.assertIn('cd "$HOME"/', cmd)

    def test_a_directory_with_a_quote_in_it_is_quoted(self):
        cmd = mailbox_list_command("/srv/it's here", "test.yaml")
        self.assertNotIn("cd /srv/it's here", cmd)

    def test_clearing_asks_for_its_own_confirmation(self):
        """MailHog answers a delete with nothing at all."""
        cmd = mailbox_clear_command("~/inst", "test.yaml")
        self.assertIn("-X DELETE", cmd)
        self.assertIn("IC_MAILBOX_CLEARED", cmd)

    def test_every_call_is_time_bounded(self):
        for cmd in (
            mailbox_list_command("~/inst", "test.yaml"),
            mailbox_message_command("~/inst", "test.yaml", REAL_ID),
            mailbox_clear_command("~/inst", "test.yaml"),
        ):
            self.assertIn("--max-time", cmd)


class TestTheCommandSurvivesAShell(BaseCase):
    """Run the whole pipeline through ``sh`` with the stack stubbed out.

    The heredoc carrying a Python program into a shell command is
    exactly the shape that breaks on quoting, and a unit test of the
    projection alone would never see it.
    """

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(
            lambda: subprocess.run(["rm", "-rf", self.tmp], check=False),
        )
        # A fake ``docker`` that ignores its arguments and prints the
        # canned API answer, so the command runs end to end offline.
        raw = _raw(attachments=[("datos.csv", "text/csv", b"a,b\n1,2\n")])
        payload = {"total": 1, "count": 1, "start": 0, "items": [_item(raw)]}
        bindir = Path(self.tmp) / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text(
            "#!/bin/sh\ncat <<'IC_JSON'\n" + json.dumps(payload)
            + "\nIC_JSON\n",
        )
        (bindir / "docker").chmod(0o755)
        self.env_path = f"{bindir}:{os.environ.get('PATH', '')}"
        self.instdir = Path(self.tmp) / "inst"
        self.instdir.mkdir()

    def _run(self, command):
        """Execute *command* with the stub on PATH, returning stdout."""
        proc = subprocess.run(
            ["sh", "-c", command],
            capture_output=True, text=True, timeout=60,
            env=os.environ | {"PATH": self.env_path},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_a_listing_survives_the_trip_through_sh(self):
        out = self._run(mailbox_list_command(str(self.instdir), "test.yaml"))
        data = json.loads(out)
        self.assertTrue(data["ok"])
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["subject"], "Aviso de contraseña")
        self.assertEqual(data["items"][0]["attachments"], 1)


class TestTheListingProjection(BaseCase):
    """What the list view is given, and what it is not."""

    def test_an_encoded_subject_comes_back_readable(self):
        """``=?utf-8?q?...?=`` is not something a user should ever see."""
        payload = {"total": 1, "items": [_item(_raw(subject="Contraseña ñ"))]}
        data = _project(_LIST_PROGRAM, payload)
        self.assertEqual(data["items"][0]["subject"], "Contraseña ñ")

    def test_recipients_are_listed_from_the_envelope(self):
        raw = _raw(to=("a@x.org", "b@y.org"))
        payload = {"total": 1, "items": [_item(raw, to=("a@x.org", "b@y.org"))]}
        data = _project(_LIST_PROGRAM, payload)
        self.assertEqual(data["items"][0]["to"], ["a@x.org", "b@y.org"])

    def test_attachments_are_counted_not_carried(self):
        raw = _raw(attachments=[
            ("one.pdf", "application/pdf", b"%PDF-" + b"x" * 4096),
            ("two.csv", "text/csv", b"a,b\n"),
        ])
        data = _project(_LIST_PROGRAM, {"total": 1, "items": [_item(raw)]})
        item = data["items"][0]
        self.assertEqual(item["attachments"], 2)
        self.assertNotIn("%PDF-", json.dumps(data))

    def test_no_body_travels_with_the_listing(self):
        """The whole point of projecting on the host."""
        raw = _raw(text="SECRET BODY LINE", html="<p>SECRET BODY LINE</p>")
        data = _project(_LIST_PROGRAM, {"total": 1, "items": [_item(raw)]})
        self.assertNotIn("SECRET BODY LINE", json.dumps(data))

    def test_an_empty_mailbox_is_not_an_error(self):
        data = _project(_LIST_PROGRAM, {"total": 0, "count": 0, "items": []})
        self.assertTrue(data["ok"])
        self.assertEqual(data["items"], [])

    def test_a_message_it_cannot_parse_still_appears(self):
        """A listing that drops rows is worse than one with a blank cell."""
        broken = _item("this is not a message at all")
        data = _project(_LIST_PROGRAM, {"total": 1, "items": [broken]})
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["id"], REAL_ID)

    def test_garbage_on_stdin_is_reported_not_crashed(self):
        proc = subprocess.run(
            [sys.executable, "-c", _LIST_PROGRAM],
            input="<html>502 Bad Gateway</html>",
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(json.loads(proc.stdout)["ok"])


class TestTheMessageProjection(BaseCase):
    """What the reader sees when one message is opened."""

    def test_both_bodies_are_decoded(self):
        raw = _raw(text="Hola ñandú", html="<p>Hola <b>ñandú</b></p>")
        data = _project(_message_program(), _item(raw))
        self.assertTrue(data["ok"])
        self.assertIn("Hola ñandú", data["text"])
        self.assertIn("<b>ñandú</b>", data["html"])

    def test_attachments_are_described_and_left_behind(self):
        blob = b"x" * 5000
        raw = _raw(attachments=[("factura.pdf", "application/pdf", blob)])
        data = _project(_message_program(), _item(raw))
        self.assertEqual(len(data["attachments"]), 1)
        att = data["attachments"][0]
        self.assertEqual(att["filename"], "factura.pdf")
        self.assertEqual(att["type"], "application/pdf")
        self.assertEqual(att["size"], len(blob))
        # The bytes are the thing that must not have travelled.
        self.assertNotIn("xxxxxxxxxx", json.dumps(data))

    def test_an_attachment_is_never_mistaken_for_the_body(self):
        raw = _raw(
            text="the real body",
            attachments=[("note.txt", "text/plain", b"attached text")],
        )
        data = _project(_message_program(), _item(raw))
        self.assertIn("the real body", data["text"])
        self.assertNotIn("attached text", data["text"])

    def test_a_huge_body_is_cut_and_says_so(self):
        big = "<p>" + ("a" * (_MAX_HTML + 5000)) + "</p>"
        data = _project(_message_program(), _item(_raw(html=big)))
        self.assertTrue(data["html_truncated"])
        self.assertEqual(len(data["html"]), _MAX_HTML)

    def test_a_huge_text_body_is_cut_and_says_so(self):
        big = "a" * (_MAX_TEXT + 5000)
        data = _project(_message_program(), _item(_raw(text=big, html="")))
        self.assertTrue(data["text_truncated"])
        self.assertEqual(len(data["text"]), _MAX_TEXT)

    def test_an_ordinary_message_is_not_marked_truncated(self):
        data = _project(_message_program(), _item(_raw()))
        self.assertFalse(data["html_truncated"])
        self.assertFalse(data["text_truncated"])

    def test_the_attachment_list_is_bounded(self):
        raw = _raw(attachments=[
            (f"f{i}.txt", "text/plain", b"x") for i in range(_MAX_ATTACHMENTS + 10)
        ])
        data = _project(_message_program(), _item(raw))
        self.assertEqual(len(data["attachments"]), _MAX_ATTACHMENTS)

    def test_a_text_only_message_has_no_html(self):
        data = _project(_message_program(), _item(_raw(html="")))
        self.assertEqual(data["html"], "")
        self.assertIn("Cuerpo en texto.", data["text"])

    def test_the_receipt_stamp_is_carried_verbatim(self):
        """The panel renders it; nothing here should reshape it."""
        data = _project(_message_program(), _item(_raw()))
        self.assertEqual(data["created"], "2026-09-23T08:19:09.261059533Z")

    def test_an_unparseable_message_is_reported_not_crashed(self):
        data = _project(_message_program(), {"ID": "x", "Raw": {"Data": ""}})
        # An empty message parses to an empty message; what must not
        # happen is a traceback reaching the panel as "no answer".
        self.assertTrue(data["ok"])
        self.assertEqual(data["text"], "")

    def test_garbage_on_stdin_is_reported_not_crashed(self):
        proc = subprocess.run(
            [sys.executable, "-c", _message_program()],
            input="not json",
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(json.loads(proc.stdout)["ok"])
