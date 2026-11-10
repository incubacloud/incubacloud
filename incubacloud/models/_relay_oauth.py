"""The mail relay that signs in to Microsoft 365 with OAuth2.

A production instance's ``smtp`` service is a docker-mailserver relay.
With a password it signs in to the relay host with ``RELAY_USER`` and
``RELAY_PASSWORD``; with OAuth2 it runs the image ``relay-oauth``
(``images/relay-oauth`` in this repository, pinned by
``RELAY_OAUTH_IMAGE`` in the deploy executor), whose renewer asks Entra
ID for a token with the client credentials grant and whose sasl-xoauth2
plugin signs Postfix in with it. Odoo does not change: it keeps sending
to the local relay.

What the instance, the deploy and the relay's test mail share lives
here: the fixed server, the paths the image reads, the checks of the
identifiers, and the fingerprint of the credentials.
"""
import hashlib
import json
import re

#: ``cloud.instance.smtp_relay_auth`` of a relay that signs in with a
#: password, the default and what every instance was before OAuth2.
AUTH_PASSWORD = "password"
#: ``cloud.instance.smtp_relay_auth`` of a relay that signs in to
#: Microsoft 365 with OAuth2 (client credentials).
AUTH_OAUTH2_MICROSOFT = "oauth2_microsoft"

#: The server an OAuth2 relay to Microsoft 365 always uses: Exchange
#: Online's SMTP client submission, port 587 with STARTTLS
#: (learn.microsoft.com, "authenticated-client-smtp-submission", read on
#: 2026-11-08). The panel sets it; the form shows it and does not let it
#: be changed.
MICROSOFT_SMTP_SERVER = {
    "smtp_relay_host": "smtp.office365.com",
    "smtp_relay_port": 587,
    "smtp_relay_security": "starttls",
}

#: Where the image's renewer writes the token sasl-xoauth2 reads. The
#: plugin takes the "password" of ``sasl_passwd`` as the path of its token
#: file, so this path is the copier answer ``smtp_relay_password`` of an
#: OAuth2 relay: docker-mailserver writes ``[host]:587 mailbox:<path>``.
#: Not a secret.
TOKEN_PATH = "/var/lib/ic-relay-oauth/token.json"

#: The credentials file in the instance directory, written 0600 by every
#: deploy and rebuild (``cloud.instance._extra_env_files``). ``/.docker``
#: is ignored by the template's ``.gitignore`` and left out of an export.
CREDENTIALS_FILE = ".docker/smtp-oauth.json"
#: Where the image reads it, bind-mounted read-only: a file rather than
#: environment variables, which supervisord hands to every program and
#: ``docker inspect`` shows.
CREDENTIALS_MOUNT = "/run/secrets/ic-relay-oauth.json"
#: The label of the ``smtp`` service that carries
#: :func:`fingerprint` of the credentials. Compose recreates a container whose
#: configuration changed, and a bind-mounted file's content is not part
#: of it: without the label a new secret reached the host and the running
#: relay kept reading the file it had opened, the old one.
CREDENTIALS_LABEL = "io.incubacloud.relay-oauth.credentials"

# A directory ID: a GUID, or one of the directory's domains
# (``contoso.onmicrosoft.com``), which Entra ID's token endpoint takes too.
# It goes into that endpoint's URL, so nothing else is let through.
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_DOMAIN_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
# The sender mailbox: an address, nothing a YAML scalar or the SASL
# string sasl-xoauth2 builds (``user=<mailbox>^A...``) could misread.
_MAILBOX_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def renders(inst):
    """Whether a deploy renders *inst*'s relay with OAuth2.

    A production instance with a relay whose authentication is OAuth2. A
    staging never uses the relay (its ``smtp`` is a mail catcher), and a
    record without a host has no ``smtp`` service at all. The answers,
    the snapshot, the credentials file, the override and the deploy's
    steps all ask this one question. It reads three fields and nothing
    else, so an executor's test may hand it plain data.

    :param inst: a ``cloud.instance``, or data with its three fields
    :rtype: bool
    """
    return bool(
        inst.environment == "production"
        and inst.smtp_relay_host
        and inst.smtp_relay_auth == AUTH_OAUTH2_MICROSOFT
    )


def valid_directory_id(value):
    """Whether *value* is a directory ID Entra ID would take.

    :param str value: what was typed
    :returns: True for a GUID or a domain of at most 253 characters
    :rtype: bool
    """
    value = value or ""
    return bool(
        _GUID_RE.fullmatch(value)
        or (len(value) <= 253 and _DOMAIN_RE.fullmatch(value))
    )


def valid_client_id(value):
    """Whether *value* is an application (client) ID, a GUID.

    :param str value: what was typed
    :rtype: bool
    """
    return bool(_GUID_RE.fullmatch(value or ""))


def valid_mailbox(value):
    """Whether *value* is a mailbox address of at most 254 characters.

    :param str value: what was typed
    :rtype: bool
    """
    value = value or ""
    return len(value) <= 254 and bool(_MAILBOX_RE.fullmatch(value))


def fingerprint(instance_id, *values):
    """Return 16 hexadecimal digits that change with any of *values*.

    Salted with the instance's id, so two instances with the same secret
    do not show the same value: the configuration snapshot keeps the
    secret's, and ``compare_config`` would otherwise let anyone who reads
    two snapshots tell that they share a secret. Never a secret itself.

    :param int instance_id: the ``cloud.instance`` id
    :param values: the strings to fingerprint (empty for a missing one)
    :rtype: str
    """
    raw = json.dumps([instance_id, *(value or "" for value in values)])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


#: The renewer's key for the directory ID: Microsoft's own word for it.
CREDENTIALS_DIRECTORY_KEY = "tenant_id"


def credentials_json(directory_id, client_id, secret, mailbox):
    """Return the credentials file the image's renewer reads.

    The keys are the renewer's (``images/relay-oauth/README.md``).

    :param str directory_id: the directory ID
    :param str client_id: the application (client) ID
    :param str secret: the client secret
    :param str mailbox: the sender mailbox
    :returns: the JSON text, keys sorted
    :rtype: str
    """
    return json.dumps(
        {
            "provider": "microsoft",
            CREDENTIALS_DIRECTORY_KEY: directory_id,
            "client_id": client_id,
            "client_secret": secret,
            "mailbox": mailbox,
        },
        sort_keys=True,
    ) + "\n"


# ── Reading why the relay does not deliver ─────────────────────────────
#
# The relay hands every message to one server, its relay host, so a
# message Postfix defers is one the relay could not hand over. Postfix
# says why in the message's ``delay_reason`` (``postqueue -j``) and in
# ``mail.log``. The texts below are Postfix 3.10's in docker-mailserver
# 16.0.1, each seen on 2026-11-08 against the fake Exchange of
# ``images/relay-oauth/tests``, and the 535 5.7.139 against Microsoft 365
# itself on 2026-11-09.

#: The alert of a relay that does not deliver to its server, whatever the
#: cause: one per instance, critical (with outgoing mail stopped, the
#: owner has to act; the only level the default preference notifies).
ALERT_CODE = "smtp_relay_failing"

#: How long a message may wait in the relay's queue, deferred for a
#: reason none of the causes below names, before the relay counts as
#: stuck. Postfix tries a deferred message again after 5 minutes and then
#: backs off (``minimal_backoff_time`` 300 s, ``maximal_backoff_time``
#: 4000 s in docker-mailserver 16.0.1): 30 minutes is three tries or more
#: that all failed, which a passing glitch does not do. Chosen on
#: 2026-11-10.
STUCK_MINUTES = 30

#: How old a deferred message must be before its reason counts against
#: the relay, in seconds. Postfix tries a deferred message again once
#: ``minimal_backoff_time`` (300 s) has passed, at its next scan of the
#: deferred queue (``queue_run_delay``, 300 s): up to 600 s after it was
#: deferred. A younger message may have failed only once, and two probes
#: five minutes apart can both see that one failure; past 660 s the
#: message has been tried again and failed again, whatever the probes'
#: timing. Chosen on 2026-11-10.
RETRIED_SECONDS = 660

#: A reason that ends so is the server refusing one recipient (a full or
#: unknown mailbox): the relay works, and no alert is raised for it.
RECIPIENT_ONLY = "(in reply to RCPT TO command)"

#: A reason that names the relay's own container (``127.0.0.1``, ``::1``)
#: as the other end: the hop to Amavis, never the relay host.
_LOCAL_HOP_RE = re.compile(r"\[(?:127\.\d{1,3}\.\d{1,3}\.\d{1,3}|::1)\]")

_ADDRESS_RE = re.compile(r"[^\s<>()\[\];:,\"']+@[^\s<>()\[\];:,\"']+")


def without_addresses(text):
    """Return *text* with every e-mail address replaced by ``<address>``.

    What the probe and the test mail keep of a server's answer must not
    carry who wrote to whom.

    :param str text: a ``delay_reason`` or a server's answer
    :rtype: str
    """
    return _ADDRESS_RE.sub("<address>", text or "")


def recipient_only(reason):
    """Whether *reason* is the server refusing one recipient, not the relay.

    :param str reason: a ``delay_reason``
    :rtype: bool
    """
    return (reason or "").rstrip().endswith(RECIPIENT_ONLY)


def classify_reason(reason, oauth, host):
    """Name the cause of a deferral and say what to do about it.

    :param str reason: the ``delay_reason`` Postfix gave
    :param bool oauth: whether the relay signs in with OAuth2
    :param str host: the relay host, for the message
    :returns: ``(cause, message)``, or None when *reason* is not a
        failure of the relay (a recipient refused) or not one of the
        known causes
    :rtype: tuple or None
    """
    reason = reason or ""
    if not reason or recipient_only(reason):
        return None
    host = host or "the SMTP server"
    microsoft = host.lower().endswith(".office365.com") or host.lower().endswith(
        ".outlook.com"
    )
    if _LOCAL_HOP_RE.search(reason):
        # docker-mailserver hands every mail to Amavis on 127.0.0.1:10024
        # and takes it back on :10025 before the relay host sees it: a
        # failure there is the relay's own container, not its server.
        return ("content_filter", (
            "The relay's content filter (Amavis) does not take the mail "
            "inside the relay's container. Restart the instance; if it "
            "goes on, rebuild it."
        ))
    if "5.7.139" in reason:
        return ("smtp_auth_disabled", (
            f"{host} refuses to sign the relay in: SMTP AUTH is disabled in "
            f"the Microsoft 365 organisation or for the mailbox (535 5.7.139). "
            f"Enable it for the organisation (Set-TransportConfig "
            f"-SmtpClientAuthenticationDisabled $false) and for the mailbox "
            f"(Set-CASMailbox), and turn off Entra ID's security defaults, "
            f"which keep it disabled."
        ))
    if "5.2.252" in reason or "SendAsDenied" in reason:
        return ("send_as_denied", (
            f"{host} refuses the sender (5.2.252 SendAsDenied): the From "
            f"address is not the relay's mailbox and has no Send As "
            f"permission on it. Send from the mailbox, or grant it with "
            f"Add-RecipientPermission."
        ))
    if "SASL authentication failed; server" in reason:
        if oauth:
            return ("token_refused", (
                f"{host} refuses the app's token (535 5.7.3): the app's "
                f"service principal is not registered in Exchange Online "
                f"(New-ServicePrincipal, with the Object ID of the "
                f"Enterprise application) or has no FullAccess on the "
                f"mailbox (Add-MailboxPermission)."
            ))
        if microsoft:
            return ("password_refused", (
                f"{host} refuses the relay's password: Microsoft no longer "
                f"takes passwords for SMTP. Switch the relay to OAuth2 – "
                f"Microsoft 365."
            ))
        return ("password_refused", (
            f"{host} refuses the relay's user or password. Check them in "
            f"the instance's mail relay."
        ))
    if "SASL authentication failed; cannot authenticate" in reason:
        if oauth:
            return ("no_token", (
                "The relay has no valid token from Entra ID to sign in "
                "with. Check the relay's credentials, then send a test mail."
            ))
        return ("sasl_failed", (
            f"The relay could not sign in to {host}: no authentication "
            f"method both sides support."
        ))
    if "Connection refused" in reason:
        return ("connection_refused", (
            f"{host} refuses the relay's connection. Check the SMTP host "
            f"and port."
        ))
    if "Connection timed out" in reason:
        return ("connection_timeout", (
            f"{host} does not answer the relay: a firewall drops the "
            f"connection, or the SMTP host or port is wrong."
        ))
    if "Host or domain name not found" in reason:
        return ("dns", (
            f"The SMTP host {host} does not resolve. Check its name."
        ))
    if "TLS is required, but was not offered" in reason:
        return ("no_starttls", (
            f"{host} does not offer STARTTLS on that port. Check the port "
            f"and the encryption."
        ))
    if "Cannot start TLS" in reason:
        return ("tls_failed", (
            f"The TLS handshake with {host} fails. Check the port and the "
            f"encryption."
        ))
    return None


#: What the image's renewer reports when Entra ID or its own file stops
#: it (``ic-relay-oauth status``, ``error_code``), and what to do. Codes
#: from Entra ID's reference
#: (learn.microsoft.com, "reference-error-codes", read on 2026-11-08).
_TOKEN_CAUSES = {
    "AADSTS7000215": (
        "Entra ID refuses the client secret: copy the secret's Value, not "
        "its Secret ID, into the relay."
    ),
    "AADSTS7000222": (
        "The client secret has expired: create a new one in Entra ID "
        "(Certificates & secrets) and save it in the relay."
    ),
    "AADSTS700016": (
        "Entra ID does not find the application (client) ID in that "
        "directory: check both IDs on the app registration's overview."
    ),
    "AADSTS90002": (
        "Entra ID does not find the directory ID: check it on the "
        "overview of Microsoft Entra ID."
    ),
    "config_missing": (
        "The relay's credentials file is missing on the server: rebuild "
        "the instance."
    ),
    "config_is_directory": (
        "The relay's credentials file is missing on the server: rebuild "
        "the instance."
    ),
    "config_invalid": (
        "The relay's credentials file on the server cannot be read: "
        "rebuild the instance."
    ),
}

#: How close to its expiry a token may be while the renewer reports an
#: error before the relay counts as failing. The renewer renews 20
#: minutes before expiry and keeps the old token meanwhile, so an error
#: with a token that still lasts is not yet a failure; 5 minutes leaves
#: one probe (every 5 minutes) to see it before mail is deferred.
TOKEN_GRACE_SECONDS = 300


def token_failure(status, now):
    """Name what keeps the renewer from a token the relay can use.

    :param dict status: ``ic-relay-oauth status``, or None
    :param float now: epoch seconds
    :returns: ``(cause, message)``, or None while the relay has a token
        that lasts (an error the renewer may still recover from included)
    :rtype: tuple or None
    """
    if not status or status.get("state") != "error":
        return None
    try:
        expires_at = float(status.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if expires_at - now > TOKEN_GRACE_SECONDS:
        return None
    code = str(status.get("error_code") or "")
    message = _TOKEN_CAUSES.get(code)
    if not message:
        detail = without_addresses(str(status.get("error_message") or ""))[:300]
        message = (
            f"The relay cannot get a token from Entra ID ({code or 'error'})"
            + (f": {detail}" if detail else ".")
        )
    return ("token_" + (code or "error"), message)
