"""A one-use door for putting a backup archive on a host.

The panel already holds the credential that opens every host it manages;
the person restoring usually does not, and on a managed host they are not
meant to. The old rsync flow ignored that: it printed a command and
assumed the operator could authenticate, which is true on a machine they
own and false on ours.

So the platform lends its access instead of sharing it. It installs a key
the caller holds the private half of, restricted to one directory, one
command and a deadline:

* ``command="rrsync -wo <dir>"`` — rsync's own restricted wrapper, write
  only, confined to that directory. No shell, no reads, nothing else runs.
* ``restrict`` — no pty, no port or agent forwarding, no user rc.
* ``expiry-time`` — OpenSSH stops accepting the key by itself, whether or
  not anything remembers to remove it.

Three things revoke it: the restore that consumes the archive, the daily
sweep, and the operator. The first is what makes the common case leave
nothing behind.

The private key is generated for the caller, shown once and never stored.
Losing it costs a new grant, which is cheap; keeping it would mean the
panel custodying a credential to its own hosts for no reason.
"""
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

#: Directory on the host below which every grant gets its own subdirectory.
UPLOAD_ROOT = "/tmp/incubacloud-restore"

#: Where the forced command lives. The job that installs a key resolves
#: the wrapper to exactly this path first, and refuses to install a key
#: if it cannot — an unrestricted key is not a fallback.
RRSYNC = "/usr/bin/rrsync"

#: How long a grant stays usable. Long enough to send tens of gigabytes
#: over a domestic uplink, short enough that an unused one is gone by the
#: next working day.
GRANT_HOURS = 12


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

    def _authorized_keys_line(self):
        """Return the restricted ``authorized_keys`` line to install.

        Every restriction is load-bearing and none of them is the only
        one: the forced command confines the session to one directory in
        write-only mode, ``restrict`` removes everything a shell would
        offer, and the expiry closes the door on its own.
        """
        self.ensure_one()
        stamp = fields.Datetime.to_string(
            self.expires_at,
        ).replace("-", "").replace(":", "").replace(" ", "")
        return (
            f'command="{RRSYNC} -wo {self._directory()}",restrict,'
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
        grant = self.create({
            "instance_id": instance.id,
            "host_id": instance.host_id.id,
            "token": secrets.token_hex(8),
            "public_key": public,
            "fingerprint": self._fingerprint(public),
            "expires_at": fields.Datetime.now() + timedelta(hours=hours),
        })
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
        return self.filtered(lambda g: g.state == "granted").write(
            {"state": "used"}
        )

    def _revoke(self, reason="revoked"):
        """Enqueue removal of the key from the host and close the grant.

        Idempotent: a grant already closed enqueues nothing, so the daily
        sweep and the restore that consumed the archive can both call it.

        :param reason: state to leave behind (``revoked`` or ``used``).
        :return: ids of the jobs enqueued.
        """
        jobs = []
        for grant in self:
            if grant.state != "granted":
                continue
            jobs.append(self.env["cloud.job"].enqueue(
                grant.host_id.id,
                grant.instance_id.id,
                "revoke_restore_upload_key",
                payload={"grant_id": grant.id},
                bypass_running_check=True,
            ))
            grant.state = reason
        return jobs

    @api.model
    def _gc_grants(self):
        """Revoke grants whose deadline has passed, then purge old rows.

        The key on the host stops being accepted at its expiry with or
        without this — that is what ``expiry-time`` buys — but the line
        and the directory would otherwise accumulate on every host that
        ever received an upload.

        :return: number of grants revoked.
        """
        expired = self.search([
            ("state", "=", "granted"),
            ("expires_at", "<", fields.Datetime.now()),
        ])
        expired._revoke()
        cutoff = fields.Datetime.now() - timedelta(days=30)
        self.search([
            ("state", "!=", "granted"),
            ("create_date", "<", cutoff),
        ]).unlink()
        if expired:
            _logger.info(
                "restore upload grants: revoked %d expired", len(expired),
            )
        return len(expired)
