"""A one-use door for putting a backup archive on a host.

The panel already holds the credential that opens every host it manages;
the person restoring usually does not, and on a managed host they are not
meant to. The old rsync flow ignored that: it printed a command and
assumed the operator could authenticate, which is true on a machine they
own and false on ours.

So the platform lends its access instead of sharing it. It installs a key
the caller holds the private half of, restricted to one directory, one
command and a deadline:

* ``command="rrsync -wo -munge -no-del <dir>"`` — rsync's own restricted
  wrapper, write only, confined to that directory. No shell, no reads,
  nothing else runs; a link it receives is stored munged, so it leads
  nowhere, and nothing is deleted.
* ``restrict`` — no pty, no port or agent forwarding, no user rc.
* ``expiry-time`` — OpenSSH stops accepting the key by itself, whether or
  not anything remembers to remove it.

Three things revoke it: the restore that consumes the archive, the daily
sweep, and the operator. The first closes the key and keeps the
directory, so a failed restore can be retried without a second upload;
the sweep removes that directory three days later (``USED_GRANT_DAYS``).

The private key is generated for the caller, shown once and never stored.
Losing it costs a new grant, which is cheap; keeping it would mean the
panel custodying a credential to its own hosts for no reason.

One door per instance at a time: each one is a directory anybody holding
its key fills, on the disk the host's instances share. Opening one closes
the caller's own door of the instance, and is refused while another
person's is open. And a key opens one door ever: every key that opened
one is kept (``cloud.restore.upload.key``), when the grants themselves
are purged.
"""
import logging
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ._concurrency import read_committed_cursor

_logger = logging.getLogger(__name__)

#: Directory on the host below which every grant gets its own subdirectory.
UPLOAD_ROOT = "/tmp/incubacloud-restore"

#: The names an uploaded archive may be restored under: one path
#: component, starting with a letter or a digit — never ``.``, ``..``,
#: a hidden file or an option — and nothing a shell or a line of the
#: host's listing could read as something else. The name is the
#: uploader's, so it is checked where the listing is read and again
#: where a restore takes the file.
SAFE_UPLOAD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")

#: Where the forced command lives. The job that installs a key resolves
#: the wrapper to exactly this path first, and refuses to install a key
#: if it cannot — an unrestricted key is not a fallback.
RRSYNC = "/usr/bin/rrsync"

#: The shape of what ``_open`` writes, checked again before a job puts
#: any of it in a command: a token names a directory and a key line, the
#: public key is written into ``authorized_keys``.
GRANT_TOKEN = re.compile(r"[0-9a-f]{16}")
GRANT_PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}")

#: How long a grant stays usable. Long enough to send tens of gigabytes
#: over a domestic uplink, short enough that an unused one is gone by the
#: next working day.
GRANT_HOURS = 12

#: How long what a consumed grant left on its host stays there: the
#: archive a failed restore gave back for its retry, or what else was
#: sent. Three days lets a restore that failed on a Friday be retried on
#: the Monday; then the daily sweep revokes the grant again, directory
#: and all (``_gc_grants``), and the host's own sweep of /tmp takes a
#: directory that job never reached (``upload_dir_sweep``).
USED_GRANT_DAYS = 3

#: The advisory lock opening a door of an instance takes, next to the
#: queues' (``cloud.job``): two doors asked for at once are opened one
#: after the other.
_DOOR_LOCK_NAMESPACE = 0x0C10D10D


class CloudRestoreUploadKey(models.Model):
    """A public key that opened an upload door, kept so it never opens another.

    A door's grant is purged a month after it closed; the key it held
    is not, or a key used once would open a door again after that. Its
    fingerprint is unique in the database, so two doors asked for at
    once with one key cannot both open: the second insert waits for the
    first and inserts nothing (``_claim``). Written by the panel alone,
    read by nobody.
    """

    _name = "cloud.restore.upload.key"
    _description = "Public key that opened an upload door"

    _fingerprint_uniq = models.Constraint(
        "unique (fingerprint)",
        "That key already opened an upload door: generate a new one for "
        "each upload.",
    )

    fingerprint = fields.Char(required=True, readonly=True)

    @api.model
    def _claim(self, fingerprint):
        """Record that *fingerprint* opens a door, or refuse if it did.

        One ``INSERT … ON CONFLICT DO NOTHING``: a key recorded before
        inserts nothing, and one another transaction is recording waits
        for it — nothing when it commits, a serialization failure Odoo
        retries the request on when it was committed after this
        transaction's snapshot.

        :param str fingerprint: ``SHA256:…`` of the key
        :raises UserError: when the key already opened one
        """
        self.env.cr.execute(
            "INSERT INTO cloud_restore_upload_key"
            " (fingerprint, create_uid, write_uid, create_date, write_date)"
            " VALUES (%s, %s, %s, now() AT TIME ZONE 'UTC',"
            " now() AT TIME ZONE 'UTC')"
            " ON CONFLICT (fingerprint) DO NOTHING RETURNING id",
            (fingerprint, self.env.uid, self.env.uid),
        )
        if not self.env.cr.fetchone():
            raise UserError(_(
                "That key already opened an upload door: generate a new one "
                "for each upload."
            ))


class CloudRestoreUploadGrant(models.Model):
    _name = "cloud.restore.upload.grant"
    _description = "Temporary SSH access to upload one restore archive"
    _order = "id desc"

    _sql_constraints = [
        (
            'token_uniq',
            "unique (token)",
            "Grant token must be unique."
        ),
    ]

    instance_id = fields.Many2one(
        comodel_name="cloud.instance",
        required=True,
        ondelete="cascade",
        index=True,
    )
    host_id = fields.Many2one(
        comodel_name="cloud.host",
        required=True,
        ondelete="cascade",
        index=True,
    )
    token = fields.Char(
        required=True,
        index=True,
        copy=False,
        help="Names the directory on the host and the key line that "
             "writes into it.",
    )
    public_key = fields.Char(
        required=True,
        help="The half installed on the host. The private half is shown "
             "to the caller once and never stored.",
    )
    fingerprint = fields.Char(
        help="SHA256 fingerprint, so a grant can be recognised in the "
             "host's authorized_keys without holding the key itself.",
    )
    expires_at = fields.Datetime(required=True, index=True)
    state = fields.Selection(
        [
            ("granted", "Granted"),
            ("used", "Used"),
            ("revoked", "Revoked"),
        ],
        default="granted",
        required=True,
        index=True,
    )
    received_filename = fields.Char(readonly=True)
    received_bytes = fields.Integer(readonly=True)
    received_sha256 = fields.Char(readonly=True)
    user_id = fields.Many2one(
        comodel_name="res.users",
        default=lambda self: self.env.user.id,
        readonly=True,
    )

    # ── Shape of what goes on the host ────────────────────────────────────

    def _directory(self):
        """Return the directory on the host this grant may write into."""
        self.ensure_one()
        return f"{UPLOAD_ROOT}/{self.token}"

    def _remote_path(self, filename="restore.zip"):
        """Return where an uploaded archive lands on the host."""
        self.ensure_one()
        return f"{self._directory()}/{filename}"

    def _key_comment(self):
        """Return the marker that identifies this grant's key line.

        Removal keys on it, so it must name exactly one grant and never
        collide with a key an operator put there themselves.
        """
        self.ensure_one()
        return f"ic-restore-{self.token}"

    def _check_job(self, job):
        """Refuse a grant a job may not act on, before any command is built.

        Grants are written by the panel alone (``_open``, ``sudo``), but
        a job names its grant by id in a payload its caller wrote: the
        grant must be of the job's own instance and host, and still of
        the shape ``_open`` gave it.

        :param job: the ``cloud.job`` about to act on this grant
        :raise ValueError: when the grant is not the job's, or not of
            that shape
        """
        self.ensure_one()
        if self.instance_id != job.instance_id or self.host_id != job.host_id:
            raise ValueError(
                "The upload grant is not one of this job's instance and "
                "host: nothing was done."
            )
        if not GRANT_TOKEN.fullmatch(self.token or "") \
                or not GRANT_PUBLIC_KEY.fullmatch(self.public_key or ""):
            raise ValueError(
                "The upload grant is not one the panel opened: nothing "
                "was done."
            )

    def _authorized_keys_line(self):
        """Return the restricted ``authorized_keys`` line to install.

        Every restriction is load-bearing and none of them is the only
        one: the forced command confines the session to one directory in
        write-only mode, ``restrict`` removes everything a shell would
        offer, and the expiry closes the door on its own.

        ``-munge`` and ``-no-del`` are part of the confinement. Without
        the first, one session stored a link to any directory of the
        account and the next wrote through it — its ``authorized_keys``
        among them, a shell as the account the panel connects as (root
        on the platform's hosts; 28-oct-2026). Without the second, a
        session could delete what an earlier one sent. Both exist only
        in the rrsync of rsync 3.2.4 and later, which is why the key is
        installed only where that one is (``_ensure_rrsync``).
        """
        self.ensure_one()
        stamp = fields.Datetime.to_string(
            self.expires_at,
        ).replace("-", "").replace(":", "").replace(" ", "")
        return (
            f'command="{RRSYNC} -wo -munge -no-del {self._directory()}",'
            'restrict,'
            f'expiry-time="{stamp}" {self.public_key} {self._key_comment()}'
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    @api.model
    def _parse_public_key(self, public_key):
        """Return the caller's public key in the one shape a grant installs.

        Only an ``ssh-ed25519`` key, read by ``cryptography`` and written
        back by it: what reaches ``authorized_keys`` is the key alone,
        never the caller's text — its comment, its options or a line
        break in it would be part of the key's line. Nothing of what was
        given is quoted back: a private key pasted by mistake must not
        end up in an error.

        :param str public_key: ``ssh-ed25519 AAAA… [comment]``
        :return: ``ssh-ed25519 AAAA…``
        :rtype: str
        :raise UserError: for anything that is not such a key
        """
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        refused = UserError(_(
            "That is not an ed25519 public key: give the line ssh-keygen "
            "writes to the .pub file (ssh-ed25519 AAAA…)."
        ))
        words = (public_key or "").split()
        if len(words) < 2 or words[0] != "ssh-ed25519":
            raise refused
        try:
            key = serialization.load_ssh_public_key(
                f"{words[0]} {words[1]}".encode("ascii"),
            )
        except (UnicodeEncodeError, ValueError, UnsupportedAlgorithm):
            raise refused from None
        if not isinstance(key, ed25519.Ed25519PublicKey):
            raise refused
        line = key.public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        ).decode()
        if not GRANT_PUBLIC_KEY.fullmatch(line):
            raise refused
        return line

    @api.model
    def _open(self, instance, hours=GRANT_HOURS, public_key=None):
        """Create a grant with a fresh keypair and return it with the key.

        With *public_key*, the grant installs the caller's own key and
        no keypair is made. A key opens one grant only: two lines of
        ``authorized_keys`` with the same key would have sshd pick one
        of their directories for every session, and a key that already
        held a door is no longer a one-use one (``cloud.restore.upload.key``).
        And an instance has one door open at a time
        (``_doors_to_close``): the doors that went before it are closed
        once nothing refuses this one.

        :param instance: ``cloud.instance`` the archive is destined for.
        :param hours: lifetime of the grant.
        :param str public_key: the caller's ``ssh-ed25519`` public key,
            or None to generate a keypair.
        :return: ``(grant, private_key_pem)`` — the private half exists
            only in this return value; None with *public_key*.
        :raise UserError: when the instance has no host to open on,
            another person's door of it is open, or *public_key* is not
            a key that may open one.
        """
        import secrets

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        instance.ensure_one()
        if not instance.host_id:
            raise UserError(_("Instance has no host assigned."))
        if public_key:
            public = self._parse_public_key(public_key)
            private = None
        else:
            key = ed25519.Ed25519PrivateKey.generate()
            public = key.public_key().public_bytes(
                encoding=serialization.Encoding.OpenSSH,
                format=serialization.PublicFormat.OpenSSH,
            ).decode()
            private = key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.OpenSSH,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode()
        closing = self._doors_to_close(instance)
        # Every key, whoever's and on whatever instance: a key is unique
        # on every host.
        self.env["cloud.restore.upload.key"]._claim(self._fingerprint(public))
        closing._revoke()
        # The panel's own record: people read grants, never write them
        # (``ir.model.access.csv``).
        grant = self.sudo().create({
            "instance_id": instance.id,
            "host_id": instance.host_id.id,
            "token": secrets.token_hex(8),
            "public_key": public,
            "fingerprint": self._fingerprint(public),
            "expires_at": fields.Datetime.now() + timedelta(hours=hours),
        }).with_env(self.env)
        return grant, private

    @api.model
    def _doors_to_close(self, instance):
        """Return the doors of *instance* to close before another one opens.

        Under a lock of the instance's doors, held until the transaction
        ends: two doors asked for at once open one after the other. One
        whose deadline passed is closed, as the sweep would; the
        caller's own is closed too (a key lost with the dialog that
        showed it); another person's is left open, and the new one
        refused. A door another transaction opened after this one's
        snapshot is seen by a fresh read, and refused alike.

        :param instance: the ``cloud.instance`` a door is asked for
        :return: the grants to revoke, in the caller's environment
        :raises UserError: while another person's door of it is open
        """
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            (_DOOR_LOCK_NAMESPACE, instance.id),
        )
        now = fields.Datetime.now()
        open_doors = self.sudo().search([
            ("instance_id", "=", instance.id),
            ("state", "=", "granted"),
        ])
        expired = open_doors.filtered(lambda g: g.expires_at <= now)
        others = (open_doors - expired).filtered(
            lambda g: g.user_id != self.env.user,
        )
        if others:
            raise UserError(_(
                "An upload door is already open for this instance, opened by "
                "%(person)s until %(deadline)s UTC: one door at a time. Ask "
                "them to close it, or wait until it expires.",
                person=others[0].user_id.name or "",
                deadline=fields.Datetime.to_string(others[0].expires_at),
            ))
        self.env.flush_all()
        # Not ``cr``: ``_`` reads the language through a local of that
        # name, and this cursor is closed by the time a refusal is said.
        with read_committed_cursor(self.env.registry) as fresh_cr:
            fresh_cr.execute(
                "SELECT id FROM cloud_restore_upload_grant"
                " WHERE instance_id = %s AND state = 'granted'"
                " AND expires_at > now() AT TIME ZONE 'UTC'",
                (instance.id,),
            )
            fresh = {row[0] for row in fresh_cr.fetchall()}
        if fresh - set(self.sudo().browse(fresh).exists().ids):
            raise UserError(_(
                "Another upload door was opened for this instance a moment "
                "ago: one door at a time. Close it, or wait until it expires."
            ))
        return (open_doors - others).with_env(self.env)

    @api.model
    def _fingerprint(self, public_key):
        """Return the SHA256 fingerprint OpenSSH would print for a key."""
        import base64
        import hashlib

        parts = (public_key or "").split()
        if len(parts) < 2:
            return ""
        try:
            blob = base64.b64decode(parts[1])
        except (ValueError, TypeError):
            return ""
        digest = base64.b64encode(hashlib.sha256(blob).digest()).decode()
        return "SHA256:" + digest.rstrip("=")

    def _mark_used(self):
        """Record that the archive this grant carried has been consumed."""
        return self.filtered(lambda g: g.state == "granted").sudo().write(
            {"state": "used"}
        )

    def _revoke(self, reason="revoked", states=("granted",)):
        """Enqueue removal of the key from the host and close the grant.

        Idempotent: a grant already closed enqueues nothing, so the daily
        sweep and the restore that consumed the archive can both call it.

        :param reason: state to leave behind (``revoked`` or ``used``).
        :param states: the states a grant must be in to be revoked: the
            sweep also passes ``used``, to remove what a consumed grant
            left in its directory.
        :return: ids of the jobs enqueued.
        """
        jobs = []
        for grant in self:
            if grant.state not in states:
                continue
            # An ordinary job of the instance's queue: the key stops
            # being accepted at its expiry anyway, so the revocation can
            # wait its turn behind whatever runs.
            jobs.append(self.env["cloud.job"].enqueue(
                grant.host_id.id,
                grant.instance_id.id,
                "revoke_restore_upload_key",
                payload={"grant_id": grant.id},
            ))
            grant.sudo().state = reason
        return jobs

    @api.model
    def _gc_grants(self):
        """Revoke grants whose deadline has passed, then purge old rows.

        The key on the host stops being accepted at its expiry with or
        without this — that is what ``expiry-time`` buys — but the line
        and the directory would otherwise accumulate on every host that
        ever received an upload.

        A grant a restore consumed had its key revoked then, its
        directory kept: a failed restore gives the archive back there
        for its retry, a successful one leaves whatever else was sent.
        ``USED_GRANT_DAYS`` after its last change it is revoked again,
        directory and all. Rows go a month after their last change, so a
        revocation still finds its grant.

        :return: number of grants revoked.
        """
        now = fields.Datetime.now()
        expired = self.search([
            ("state", "=", "granted"),
            ("expires_at", "<", now),
        ])
        expired._revoke()
        consumed = self.search([
            ("state", "=", "used"),
            ("write_date", "<", now - timedelta(days=USED_GRANT_DAYS)),
        ])
        consumed._revoke(states=("used",))
        cutoff = now - timedelta(days=30)
        self.search([
            ("state", "!=", "granted"),
            ("write_date", "<", cutoff),
        ]).sudo().unlink()
        if expired or consumed:
            _logger.info(
                "restore upload grants: revoked %d expired, %d consumed",
                len(expired), len(consumed),
            )
        return len(expired) + len(consumed)
