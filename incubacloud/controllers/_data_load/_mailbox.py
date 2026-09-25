"""Reading a staging's captured mailbox off its host.

Every staging renders a MailHog container (``smtplocal``) and Odoo is
wired to it, so a copy of production never mails real customers. The
mailbox was there all along; what was missing was any way to look at
it. Odoo.sh calls the same thing a "Mails" tab.

Three things shape the commands below.

**The MailHog API is only reachable from inside the stack.** The
container publishes 8025 on the compose network and nothing else, and
F0 took its Traefik router away on purpose — the inbox used to be
served at ``/<domain>/smtpfake/`` with no authentication at all. So the
request is issued with ``curl`` from the instance's own ``odoo``
container, which is on that network and already carries ``curl`` (the
health probe uses it).

**The projection runs on the host, not in the container.** A staging
can be any Odoo from 7.0 up, so its container's Python is whatever that
era shipped — the same trap ``read_last_login.sh`` had to dodge. The
host is ours and has python3. Piping ``docker compose exec`` into it
costs one local copy and means only the projected JSON crosses SSH: a
mailbox of fifty messages with attachments is megabytes, and none of
those bytes are anything the panel would show.

**Nothing is stored.** These are somebody's password resets and
customer data; the panel reads them live and keeps no copy, the same
rule the proxy access log follows.
"""
import re
import shlex
from urllib.parse import quote as _url_quote

from ._helpers import _quote_remote_path

#: Host and port MailHog answers on inside the stack. The alias comes
#: from the doodba template (``test.yaml.jinja``: the ``smtp`` service
#: joins the default network as ``smtplocal``) and is the same name
#: Odoo itself is configured to send to.
_MAILHOG_ORIGIN = "http://smtplocal:8025"

#: Seconds a single API call may take before curl gives up. Generous
#: for a local request, but a hung container must not hold the SSH
#: channel — and with it a worker — open indefinitely.
_CURL_TIMEOUT = 20

#: Messages one listing may return. MailHog hands back the full body of
#: every item it lists, so this bounds what the host has to parse, not
#: just what the browser renders.
_MAX_LIMIT = 100

#: Default page size when the caller does not ask for one.
_DEFAULT_LIMIT = 50

#: A MailHog message id is base64 plus ``@`` plus the catcher's
#: hostname. It reaches a URL inside a shell command, so anything else
#: is refused rather than escaped: there is no legitimate id with a
#: quote, a space or a slash-dot in it.
_MAIL_ID_RE = re.compile(r"^[A-Za-z0-9+/=_.-]{1,512}@[A-Za-z0-9._-]{1,255}$")


def is_safe_mail_id(value):
    """Return True when *value* is shaped like a MailHog message id.

    :param value: candidate id coming from the browser
    :return: bool
    """
    return bool(isinstance(value, str) and _MAIL_ID_RE.match(value))


def clamp_limit(limit):
    """Return how many messages a listing may return.

    :param limit: requested page size, possibly ``None`` or garbage
    :return: int within ``1 .. _MAX_LIMIT``
    """
    try:
        wanted = int(limit)
    except (TypeError, ValueError):
        wanted = _DEFAULT_LIMIT
    return max(1, min(wanted or _DEFAULT_LIMIT, _MAX_LIMIT))


#: Projection that turns MailHog's listing into the few fields a list
#: view shows. Written as a quoted heredoc so the host's shell expands
#: nothing on the way through, and reading stdin so no part of it has
#: to survive shell quoting.
#:
#: Subjects arrive RFC 2047-encoded (``=?utf-8?q?...?=``) and the panel
#: must not show that; ``Created`` is MailHog's own receipt timestamp
#: and is the one field always present, where ``Date:`` is optional.
_LIST_PROGRAM = '''
import email, json, sys
from email.header import decode_header, make_header

def hdr(msg, name):
    """Return one header of *msg*, RFC 2047-decoded, never raising."""
    raw = msg.get(name)
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw

def addr(node):
    """Render one MailHog address object as ``mailbox@domain``."""
    box = (node or {}).get("Mailbox") or ""
    dom = (node or {}).get("Domain") or ""
    return ("%s@%s" % (box, dom)) if box or dom else ""

try:
    payload = json.load(sys.stdin)
except Exception:
    print(json.dumps({"ok": False, "error": "unreadable"}))
    raise SystemExit(0)

items = []
for item in payload.get("items") or []:
    try:
        msg = email.message_from_string(item.get("Raw", {}).get("Data") or "")
    except Exception:
        msg = email.message_from_string("")
    attachments = 0
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_disposition() == "attachment" or part.get_filename():
            attachments += 1
    items.append({
        "id": item.get("ID") or "",
        "created": item.get("Created") or "",
        "from": hdr(msg, "From") or addr(item.get("From")),
        "to": [addr(n) for n in (item.get("To") or [])],
        "subject": hdr(msg, "Subject"),
        "size": (item.get("Content") or {}).get("Size") or 0,
        "attachments": attachments,
    })

print(json.dumps({
    "ok": True,
    "total": payload.get("total") or 0,
    "items": items,
}))
'''

#: Longest HTML body shipped to the browser. Odoo mails with inlined
#: images get large, and the viewer is for reading a message, not for
#: recovering one.
_MAX_HTML = 256 * 1024

#: Longest plain-text body shipped to the browser.
_MAX_TEXT = 64 * 1024

#: Attachments listed for one message. Only name, type and size travel;
#: the bytes stay on the host.
_MAX_ATTACHMENTS = 50

#: Projection for one message. Same contract as ``_LIST_PROGRAM``:
#: reads MailHog's JSON on stdin, prints one JSON object on stdout.
#:
#: The body is rebuilt from ``Raw.Data`` rather than from MailHog's own
#: ``MIME.Parts``, because the raw message is the only representation
#: that carries the transfer encoding and charset of each part — which
#: is what turns a quoted-printable ``=C3=B1`` back into an ``ñ``.
_MESSAGE_PROGRAM = '''
import email, json, sys
from email.header import decode_header, make_header

MAX_HTML = %(max_html)d
MAX_TEXT = %(max_text)d
MAX_ATTACHMENTS = %(max_attachments)d

def hdr(msg, name):
    """Return one header of *msg*, RFC 2047-decoded, never raising."""
    raw = msg.get(name)
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw

def body(part):
    """Return one part's payload as text, guessing nothing silently."""
    data = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return data.decode(charset, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")

try:
    item = json.load(sys.stdin)
except Exception:
    print(json.dumps({"ok": False, "error": "unreadable"}))
    raise SystemExit(0)

try:
    msg = email.message_from_string(item.get("Raw", {}).get("Data") or "")
except Exception:
    print(json.dumps({"ok": False, "error": "unparseable"}))
    raise SystemExit(0)

text = ""
html = ""
attachments = []
for part in msg.walk():
    if part.get_content_maintype() == "multipart":
        continue
    ctype = part.get_content_type()
    filename = part.get_filename()
    if part.get_content_disposition() == "attachment" or filename:
        if len(attachments) < MAX_ATTACHMENTS:
            attachments.append({
                "filename": filename or "",
                "type": ctype,
                "size": len(part.get_payload(decode=True) or b""),
            })
        continue
    if ctype == "text/plain" and not text:
        text = body(part)
    elif ctype == "text/html" and not html:
        html = body(part)

headers = []
for name in ("Date", "Message-ID", "Return-Path", "Content-Type"):
    value = hdr(msg, name)
    if value:
        headers.append({"name": name, "value": value})

print(json.dumps({
    "ok": True,
    "id": item.get("ID") or "",
    "created": item.get("Created") or "",
    "from": hdr(msg, "From"),
    "to": hdr(msg, "To"),
    "cc": hdr(msg, "Cc"),
    "subject": hdr(msg, "Subject"),
    "text": text[:MAX_TEXT],
    "text_truncated": len(text) > MAX_TEXT,
    "html": html[:MAX_HTML],
    "html_truncated": len(html) > MAX_HTML,
    "attachments": attachments,
    "headers": headers,
}))
''' % {
    "max_html": _MAX_HTML,
    "max_text": _MAX_TEXT,
    "max_attachments": _MAX_ATTACHMENTS,
}


def _piped(inst_dir, yaml_file, path, program):
    """Return the command that curls *path* and projects it on the host.

    The program is assigned from a quoted heredoc first and passed with
    ``python3 -c "$PROG"`` rather than piped into ``python3 -`` — the
    pipe already carries the API response on stdin, so the program has
    to arrive by another door.

    :param str inst_dir: remote instance directory (may start with ~/)
    :param str yaml_file: compose file of the instance's environment
    :param str path: MailHog API path, already URL-safe
    :param str program: the projection to run on the host
    :return: the shell command
    """
    qdir = _quote_remote_path(inst_dir)
    return (
        "PROG=$(cat <<'IC_MAILBOX_PY'\n"
        f"{program}\n"
        "IC_MAILBOX_PY\n"
        ")\n"
        f"cd {qdir} && docker compose -f {shlex.quote(yaml_file)} "
        f"exec -T odoo curl -sf --max-time {_CURL_TIMEOUT} "
        f"{shlex.quote(_MAILHOG_ORIGIN + path)} "
        '| python3 -c "$PROG"'
    )


def mailbox_list_command(inst_dir, yaml_file, limit=None, start=0):
    """Return the command that lists a staging's captured mailbox.

    Newest first, which is MailHog's own order.

    :param str inst_dir: remote instance directory (may start with ~/)
    :param str yaml_file: compose file of the instance's environment
    :param limit: how many messages to return (clamped)
    :param start: offset into the mailbox, for paging
    :return: the shell command
    """
    n = clamp_limit(limit)
    try:
        offset = max(0, int(start))
    except (TypeError, ValueError):
        offset = 0
    path = f"/api/v2/messages?start={offset}&limit={n}"
    return _piped(inst_dir, yaml_file, path, _LIST_PROGRAM)


def mailbox_message_command(inst_dir, yaml_file, message_id):
    """Return the command that reads one captured message in full.

    :param str inst_dir: remote instance directory (may start with ~/)
    :param str yaml_file: compose file of the instance's environment
    :param str message_id: MailHog id, already passed ``is_safe_mail_id``
    :return: the shell command
    """
    path = "/api/v1/messages/" + _url_quote(message_id, safe="")
    return _piped(inst_dir, yaml_file, path, _MESSAGE_PROGRAM)


def mailbox_clear_command(inst_dir, yaml_file):
    """Return the command that empties a staging's captured mailbox.

    MailHog answers a successful delete with an empty body, so the
    command prints its own marker: an empty stdout would otherwise be
    indistinguishable from the container not being there at all.

    :param str inst_dir: remote instance directory (may start with ~/)
    :param str yaml_file: compose file of the instance's environment
    :return: the shell command
    """
    qdir = _quote_remote_path(inst_dir)
    url = shlex.quote(_MAILHOG_ORIGIN + "/api/v1/messages")
    return (
        f"cd {qdir} && docker compose -f {shlex.quote(yaml_file)} "
        f"exec -T odoo curl -sf -X DELETE --max-time {_CURL_TIMEOUT} "
        f"{url} && echo IC_MAILBOX_CLEARED"
    )
