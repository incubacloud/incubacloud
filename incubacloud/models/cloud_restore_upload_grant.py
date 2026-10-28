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
"""
import logging
import re
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

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


class CloudRestoreUploadGrant(models.Model):
    _name = "cloud.restore.upload.grant"
    _description = "Temporary SSH access to upload one restore archive"
    _order = "id desc"

    _token_uniq = models.Constraint(
        "unique (token)",
        "Grant token must be unique.",
    )

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
    def _open(self, instance, hours=GRANT_HOURS):
        """Create a grant with a fresh keypair and return it with the key.

        :param instance: ``cloud.instance`` the archive is destined for.
        :param hours: lifetime of the grant.
        :return: ``(grant, private_key_pem)`` — the private half exists
            only in this return value.
        :raise UserError: when the instance has no host to open on.
        """
        import secrets

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        instance.ensure_one()
        if not instance.host_id:
            raise UserError(_("Instance has no host assigned."))
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
