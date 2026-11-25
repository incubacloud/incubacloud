import asyncio
import contextlib
import hashlib
import ipaddress
import logging
import secrets
import stat
from datetime import timedelta
from urllib.parse import urlparse

import asyncssh
import boto3
import pytz
from botocore.exceptions import BotoCoreError, ClientError

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import email_normalize
from odoo.tools.misc import hmac as keyed_hmac

from ..net.hostname import is_host_address
from ..net.outbound import OutboundError, _resolve_public
from . import sftp_dst, ssh_host_keys
from ._backup_dst import (
    access_key_is_valid,
    breaks_line,
    compose_env_breaks,
    dst_is_valid,
    endpoint_url_is_valid,
    image_tag_is_valid,
    normalize_endpoint_url,
    region_is_valid,
)
from ._backup_retention import valid_backup_retention
from .cloud_instance import _GLOBAL_BACKUP_PARAM
from .encrypted_char import EncryptedChar, EncryptedText
from .password_utils import generate_password

_logger = logging.getLogger(__name__)

# Re-alert cadence while a backend stays above its threshold. Daily
# would feel like spam; weekly is enough for the operator to react
# without being noisy.
_ALERT_DEBOUNCE_DAYS = 7

#: The comment of the key pair an SFTP destination generates: what the
#: line in the server's ``authorized_keys`` is recognised by.
SFTP_KEY_COMMENT = "incubacloud-backup"

#: The scope of the HMAC that fingerprints an SFTP password in an
#: instance's configuration snapshot (``CloudBackupBackend._sftp_snapshot``).
SFTP_PASSWORD_HMAC_SCOPE = "incubacloud.sftp_password_snapshot"

#: The shortest SFTP password accepted: the agents' scrubber masks only
#: secrets of 8 characters or more (``incubacloud_mcp``'s
#: ``MIN_SECRET_LEN``), so a shorter one could reach an agent in a log.
SFTP_PASSWORD_MIN_LEN = 8

#: How the server key's alert names a key type: as its provider and
#: ``ssh-keygen -l`` name it, not as it is written in ``known_hosts``.
_KEY_TYPE_NAMES = {
    "ssh-ed25519": "Ed25519",
    "ecdsa-sha2-nistp256": "ECDSA",
    "ecdsa-sha2-nistp384": "ECDSA",
    "ecdsa-sha2-nistp521": "ECDSA",
}

#: Fields only the server writes on an SFTP destination: the key pair it
#: generates and the server key a person confirmed, with its fingerprint.
#: Refused to any write that does not run as the platform, so a manager
#: cannot set a trusted key over ``call_kw`` without reading it first and
#: being audited, nor a fingerprint the page and the backup listing would
#: compare against.
_SFTP_SERVER_FIELDS = (
    "sftp_private_key",
    "sftp_public_key",
    "sftp_known_hosts",
    "sftp_host_key_fingerprint",
    "sftp_host_key_trusted_at",
    "sftp_host_key_trusted_by",
)

#: Files listed at most per folder walk, as an S3 listing is capped.
_SFTP_LISTING_CAP = 10000

#: Seconds a whole SFTP operation of the panel may take.
_SFTP_OPERATION_TIMEOUT = 60


class SftpUnreachable(Exception):
    """The panel could not reach or log in to an SFTP server.

    Kept apart from an answer of the server (a folder that is not there):
    a measure reports it as unreachable, never as an empty folder.
    """


#: What an SFTP destination's copies are reached by: changing one leaves
#: a copy kept on the old server out of reach (``_check_sftp_identity``).
_SFTP_IDENTITY_FIELDS = ("sftp_host", "sftp_port", "sftp_user")


class CloudBackupBackend(models.Model):
    _name = "cloud.backup.backend"
    _inherit = ["cloud.security.mixin"]
    _description = "Backup Backend"

    name = fields.Char(required=True)
    backend_type = fields.Selection(
        selection=[("s3", "S3 Compatible"), ("sftp", "SFTP")],
        string="Type",
        required=True,
        default="s3",
    )

    # ── S3 / compatible storage ─────────────────────────────────────────────

    s3_bucket = fields.Char(string="Bucket")
    s3_path = fields.Char(
        string="Path",
        default="backups",
        help="Object prefix inside the bucket (e.g. backups or clients/acme).",
    )
    s3_endpoint_url = fields.Char(
        string="Endpoint URL",
        help="Leave empty for AWS S3. Set for MinIO, Wasabi, Backblaze, etc.",
    )
    s3_region = fields.Char(
        string="Region",
        help="The bucket's region, as the provider names it (e.g. "
        "eu-west-par). Continuous recovery signs its requests with it. "
        "Leave empty for Cloudflare R2, or for AWS S3 in us-east-1.",
    )
    s3_access_key_id = fields.Char(
        string="Access Key ID",
        groups="incubacloud.group_cloud_manager",
    )
    s3_secret_access_key = EncryptedChar(
        string="Secret Access Key",
        groups="incubacloud.group_cloud_manager",
    )

    # ── SFTP ────────────────────────────────────────────────────────────────
    # The panel generates the key pair; the person adds the public half to
    # the server and confirms the server's own key (``sftp_known_hosts``).
    # A server that accepts no key logs in with a password instead
    # (``sftp_auth_method``): the server key is trusted the same way.

    sftp_host = fields.Char(string="SFTP Server")
    sftp_port = fields.Integer(string="SFTP Port", default=22)
    sftp_user = fields.Char(string="SFTP User")
    sftp_path = fields.Char(
        string="SFTP Path",
        default="backups",
        help="Folder under the user's home (e.g. backups). Start with / "
        "for an absolute path.",
    )
    sftp_auth_method = fields.Selection(
        [("key", "Key (recommended)"), ("password", "Password")],
        string="SFTP Authentication",
        default="key",
        required=True,
        help="How the panel and the backups log in to the SFTP server: "
        "with a key the panel generates, or with a password for a server "
        "that accepts no key.",
    )
    sftp_password = EncryptedChar(
        string="SFTP Password",
        groups="incubacloud.group_cloud_manager",
        copy=False,
    )
    sftp_private_key = EncryptedText(
        string="SFTP Private Key",
        groups="base.group_system",
        copy=False,
    )
    sftp_public_key = fields.Char(
        string="SFTP Public Key",
        readonly=True,
        copy=False,
        help="Add this line to the server's authorized_keys.",
    )
    sftp_known_hosts = fields.Text(
        string="Trusted Server Key",
        readonly=True,
        copy=False,
        groups="incubacloud.group_cloud_manager",
    )
    sftp_host_key_fingerprint = fields.Char(
        string="Server Key Fingerprint",
        compute="_compute_sftp_host_key_fingerprint",
        store=True,
    )
    sftp_host_key_trusted_at = fields.Datetime(readonly=True, copy=False)
    sftp_host_key_trusted_by = fields.Many2one(
        "res.users", readonly=True, copy=False,
    )

    # ── Encryption ─────────────────────────────────────────────────────────

    passphrase = EncryptedChar(
        string="Encryption Passphrase",
        groups="incubacloud.group_cloud_manager",
        help="Auto-generated if left blank. Used as PASSPHRASE inside the backup container.",
    )

    # ── Docker image ────────────────────────────────────────────────────────

    backup_image_version = fields.Char(
        string="Image Version",
        default="latest",
        help="Tag of the docker-duplicity image (e.g. latest, 4.0.0).",
    )

    # ── Notifications ───────────────────────────────────────────────────────

    email_from = fields.Char(string="Notification From")
    email_to = fields.Char(string="Notification To")
    smtp_report_success = fields.Boolean(
        string="Report on Success",
        default=True,
    )

    # ── Retention ─────────────────────────────────────────────────────────────

    backup_retention = fields.Char(
        string="Retention Period",
        default="3M",
        help="How long to keep old backups (e.g. 3M = 3 months, 30D = 30 days, 1Y = 1 year).",
    )

    @api.constrains("backup_retention")
    def _check_backup_retention(self):
        """Refuse a retention the backup container must not run.

        It reaches a shell inside the container as duplicity's
        ``remove-older-than`` argument (``_backup_retention``): a count
        of at least one and ``D``, ``W``, ``M`` or ``Y``, upper case.

        :raises ValidationError: for any other value, empty included
        """
        for backend in self:
            if not valid_backup_retention(backend.backup_retention):
                raise ValidationError(_(
                    "The retention period must be a number followed by D, "
                    "W, M or Y in capitals (e.g. 30D, 3M, 1Y)."
                ))

    # ── Advanced ────────────────────────────────────────────────────────────

    retention_owner = fields.Selection(
        [
            ("cron", "The backup service (Duplicity)"),
            ("lifecycle", "A lifecycle rule on the bucket"),
            ("none", "Nobody: keep everything"),
        ],
        string="Who deletes old backups",
        default="cron",
        required=True,
        help="Who deletes old backups: the daily deletion cron inside "
        "the container, a bucket lifecycle policy configured "
        "outside IncubaCloud, or nobody (backups are kept forever).",
    )
    retention_owner_declared = fields.Boolean(
        default=True,
        help="False marks an ambiguous, un-confirmed retention state "
        "(migrated from the old on/off flag with the flag off, "
        "with no lifecycle explicitly declared). Triggers a "
        "warning alert until the operator picks an explicit "
        "retention_owner value.",
    )
    backup_tz = fields.Char(
        string="Timezone",
        default="UTC",
    )

    # ── Computed destination URL ────────────────────────────────────────────

    backup_dst = fields.Char(
        string="Backup Path",
        compute="_compute_backup_dst",
        store=True,
        help="Computed duplicity DST URL (e.g. boto3+s3://bucket/path).",
    )

    @api.depends(
        "backend_type", "s3_bucket", "s3_path",
        "sftp_host", "sftp_port", "sftp_user", "sftp_path",
    )
    def _compute_backup_dst(self):
        for rec in self:
            if rec.backend_type == "s3" and rec.s3_bucket:
                path = (rec.s3_path or "").strip("/")
                if path:
                    rec.backup_dst = f"boto3+s3://{rec.s3_bucket}/{path}"
                else:
                    rec.backup_dst = f"boto3+s3://{rec.s3_bucket}"
            elif rec.backend_type == "sftp" and rec.sftp_host and rec.sftp_user:
                try:
                    rec.backup_dst = sftp_dst.build(
                        rec.sftp_user, rec.sftp_host, rec.sftp_port,
                        rec.sftp_path,
                    )
                except sftp_dst.DstError:
                    # The constraint refuses it on save; computed before.
                    rec.backup_dst = ""
            else:
                rec.backup_dst = ""

    @api.depends("sftp_known_hosts")
    def _compute_sftp_host_key_fingerprint(self):
        for rec in self:
            rec.sftp_host_key_fingerprint = ssh_host_keys.fingerprint(
                rec.sudo().sftp_known_hosts,
            ) or False

    # ── SFTP server key ─────────────────────────────────────────────────────

    #: Raised when the backups would refuse the SFTP server's key: another
    #: key than the trusted one, or a trusted RSA key the server still
    #: presents while the backups get another type (``_sftp_alert_on_other_key``).
    #: One code for both: either way the destination has one alert about its
    #: server key, trusting a key resolves it, and passing from one case to
    #: the other rewrites its message instead of leaving two.
    _SFTP_HOST_KEY_ALERT_CODE = "backup_sftp_host_key_changed"

    @api.model
    def _sftp_network_policy(self):
        """Return where this panel may connect to an SFTP server.

        ``'any'`` in the core: a self-hosted panel reaches a NAS or a
        server on its private network. A layer may answer ``'public'``.

        :rtype: str
        """
        return "any"

    def _sftp_address(self):
        """Return the address the panel dials for this destination.

        Always the destination's server, never one read from a DST. With
        the ``'public'`` policy every address of the name must be public
        and the one checked is returned, so the connection does not
        resolve the name a second time. On the hosts the name is resolved
        again, which is safe: with the server key pinned, a name that
        turned inward only reaches a key exchange that is refused.

        :rtype: str
        :raise UserError: when the policy refuses the server
        """
        self.ensure_one()
        host = (self.sftp_host or "").strip()
        if self._sftp_network_policy() != "public":
            return host
        try:
            return _resolve_public(host, self.sftp_port or 22, version=4)
        except OutboundError as exc:
            raise UserError(_(
                "%(host)s must resolve to public addresses only: this panel "
                "connects only to SFTP servers on a public address.",
                host=host,
            )) from exc

    def _sftp_presented_line(
        self, server_host_key_algs=ssh_host_keys.PARAMIKO_HOST_KEY_ORDER,
    ):
        """Read the key the server presents, filed under the URL's name.

        The line is labelled with the server as the URL names it
        (``host`` on 22, ``[host]:port`` otherwise), which is what
        duplicity's paramiko looks up, whatever address was dialled. The
        key is read in the order paramiko asks for one, so it is the key
        the backups get (``ssh_host_keys.PARAMIKO_HOST_KEY_ORDER``).

        :param server_host_key_algs: the key types to ask for, in order
        :return: the ``known_hosts`` line
        :rtype: str
        :raise UserError: when the server cannot be reached, presents no
            key, or a key type paramiko does not read, and on a
            ``ValueError`` of asyncssh (``KeyImportError`` among them)
        """
        self.ensure_one()
        address = self._sftp_key_address()
        try:
            key = ssh_host_keys.presented_host_key(
                address, self.sftp_port,
                server_host_key_algs=server_host_key_algs,
            )
        except (
            asyncssh.Error, OSError, TimeoutError, asyncio.TimeoutError,
            ValueError,
        ) as exc:
            raise self._sftp_key_read_error(exc) from exc
        return self._sftp_presented_key_line(key)

    async def _sftp_presented_line_async(
        self, server_host_key_algs=ssh_host_keys.PARAMIKO_HOST_KEY_ORDER,
    ):
        """:meth:`_sftp_presented_line` for a caller inside an event loop.

        The ``backup_list`` job reads the key from its own running loop,
        where the synchronous form cannot start another one.

        :param server_host_key_algs: the key types to ask for, in order
        :return: the ``known_hosts`` line
        :rtype: str
        :raise UserError: as :meth:`_sftp_presented_line`
        """
        self.ensure_one()
        address = self._sftp_key_address()
        try:
            key = await ssh_host_keys.presented_host_key_async(
                address, self.sftp_port,
                server_host_key_algs=server_host_key_algs,
            )
        except (
            asyncssh.Error, OSError, TimeoutError, asyncio.TimeoutError,
            ValueError,
        ) as exc:
            raise self._sftp_key_read_error(exc) from exc
        return self._sftp_presented_key_line(key)

    def _sftp_key_address(self):
        """Return the address the server key is read at, SFTP only.

        :rtype: str
        :raise UserError: on another type, or a server the policy refuses
        """
        if self.backend_type != "sftp":
            raise UserError(_("Only an SFTP destination has a server key."))
        return self._sftp_address()

    def _sftp_key_read_error(self, exc):
        """Return the error a failed reading of the server key is told by.

        :param Exception exc: what the connection raised
        :rtype: UserError
        """
        return UserError(_(
            "Could not read the key of %(host)s:%(port)s: %(error)s",
            host=self.sftp_host, port=self.sftp_port,
            error=str(exc) or type(exc).__name__,
        ))

    def _sftp_presented_key_line(self, key):
        """Return the ``known_hosts`` line of the key the server presented.

        :param key: the ``asyncssh`` key, or None when none was presented
        :rtype: str
        :raise UserError: without a key, or with a type paramiko does not read
        """
        if not key:
            raise UserError(_("The server presented no key."))
        line = ssh_host_keys.known_hosts_line(
            self.sftp_host, self.sftp_port, key,
        )
        kind = ssh_host_keys.key_type(line)
        if kind not in ssh_host_keys.ACCEPTED_KEY_TYPES:
            raise UserError(_(
                "The server presents a %(type)s key, which the backups "
                "cannot check. Offer an Ed25519, ECDSA or RSA host key.",
                type=kind,
            ))
        return line

    def _sftp_read_host_key(self):
        """Report the key the server presents now, trusting nothing.

        :return: ``fingerprint`` and ``key_type`` presented now, and
            ``trusted_fingerprint`` (``''`` when none is trusted)
        :rtype: dict
        """
        self.ensure_one()
        line = self._sftp_presented_line()
        return {
            "fingerprint": ssh_host_keys.fingerprint(line),
            "key_type": ssh_host_keys.key_type(line),
            "trusted_fingerprint": self.sftp_host_key_fingerprint or "",
        }

    def _sftp_trust_host_key(self, expected_fingerprint):
        """Trust the server key a person saw, if the server still presents it.

        The key is read again and compared with *expected_fingerprint*,
        the one the person approved: a server that presents another one in
        between is refused, logged and audited, and nothing is stored.

        :param str expected_fingerprint: the ``SHA256:…`` the person saw
        :return: the fingerprint now trusted
        :rtype: str
        :raise UserError: when the presented key is not the approved one
        """
        self.ensure_one()
        line = self._sftp_presented_line()
        found = ssh_host_keys.fingerprint(line)
        Audit = self.env["cloud.audit.log"].sudo()
        if not expected_fingerprint or found != expected_fingerprint:
            _logger.warning(
                "[sftp %s] refused to trust %s: %s was approved",
                self.name, found, expected_fingerprint,
            )
            Audit.create({
                "action": "SFTP server key refused",
                "details": (
                    f"backup destination {self.name} (id {self.id}): "
                    f"presented {found}, approved {expected_fingerprint}"
                ),
            })
            raise UserError(_(
                "The server now presents %(found)s, not the key you "
                "approved (%(approved)s). Read the key again and compare it "
                "with the one your provider shows.",
                found=found, approved=expected_fingerprint or "-",
            ))
        self.sudo().write({
            "sftp_known_hosts": line,
            "sftp_host_key_trusted_at": fields.Datetime.now(),
            "sftp_host_key_trusted_by": self.env.user.id,
        })
        Audit.create({
            "action": "SFTP server key trusted",
            "details": f"backup destination {self.name} (id {self.id}): {found}",
        })
        self.env["cloud.alert"].sudo().resolve_alert(
            self._SFTP_HOST_KEY_ALERT_CODE, key=self._alert_key(),
        )
        return found

    # ── SFTP sessions: test, measure, sizes ─────────────────────────────────

    def _sftp_connect_kwargs(self):
        """Return how the panel logs in to this destination's server.

        The destination's own server at the address the network policy
        checked (``_sftp_address``), its key or its password in memory
        (never written to disk or to the log), and the trusted server key
        as the only key accepted: passed as a key, not by host pattern, so
        dialling a checked address rather than the name changes nothing.
        With a password ``client_keys`` is None, which turns public key
        authentication off: an empty list would load ``~/.ssh``'s keys.

        :rtype: dict
        :raise UserError: without a trusted server key, or a readable key
            or password
        """
        self.ensure_one()
        bb = self.sudo()
        if not bb.sftp_known_hosts or not bb.sftp_host_key_fingerprint:
            raise UserError(_(
                "Read and trust the SFTP server's key first."
            ))
        try:
            kind, data = bb.sftp_known_hosts.split()[1:3]
            trusted = asyncssh.import_public_key(f"{kind} {data}")
        except (ValueError, asyncssh.KeyImportError) as exc:
            raise UserError(_(
                "Read and trust the SFTP server's key first."
            )) from exc
        client_keys, password = self._sftp_credentials()
        return {
            "host": self._sftp_address(),
            "port": self.sftp_port or 22,
            "username": self.sftp_user,
            "client_keys": client_keys,
            "known_hosts": ([trusted], [], []),
            "agent_path": None,
            "password": password,
            "connect_timeout": ssh_host_keys.CONNECT_TIMEOUT,
            "login_timeout": ssh_host_keys.TOTAL_TIMEOUT,
        }

    def _sftp_credentials(self):
        """Return what the panel logs in to this destination's server with.

        :return: ``(client_keys, password)``: the key and None, or None
            and the password
        :rtype: tuple
        :raise UserError: when the key or the password cannot be read
        """
        self.ensure_one()
        bb = self.sudo()
        if bb.sftp_auth_method == "password":
            try:
                password = bb.sftp_password or ""
            except ValueError:
                password = ""
            if not password:
                raise UserError(_(
                    "The SFTP password of %(name)s cannot be read: enter it "
                    "again.",
                    name=self.name,
                ))
            return None, password
        try:
            client_key = asyncssh.import_private_key(bb.sftp_private_key or "")
        except (ValueError, asyncssh.KeyImportError) as exc:
            raise UserError(_(
                "The SFTP key of %(name)s cannot be read: generate a new one.",
                name=self.name,
            )) from exc
        return [client_key], None

    @contextlib.asynccontextmanager
    async def _sftp_client(self, kwargs):
        """Open an SFTP client on this destination's server.

        :param dict kwargs: :meth:`_sftp_connect_kwargs`, read before the
            loop runs (it reads the database)
        :raise SftpUnreachable: when the connection or the login fails
        """
        try:
            conn = await asyncssh.connect(**kwargs)
        except (asyncssh.Error, OSError, TimeoutError, asyncio.TimeoutError) as exc:
            raise SftpUnreachable(exc) from exc
        try:
            async with conn.start_sftp_client() as sftp:
                yield sftp
        finally:
            conn.close()

    def _sftp_run(self, coro_factory):
        """Run ``coro_factory(sftp)`` on a fresh loop, within the time limit.

        For a caller outside an event loop (a route, a cron). One inside a
        loop (``backup_list``'s ``on_success``) awaits the async form.

        :param coro_factory: ``async def f(sftp)``
        :return: what it returns
        """
        self.ensure_one()
        kwargs = self._sftp_connect_kwargs()

        async def _go():
            async with self._sftp_client(kwargs) as sftp:
                return await coro_factory(sftp)

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                asyncio.wait_for(_go(), _SFTP_OPERATION_TIMEOUT),
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise SftpUnreachable(exc) from exc
        finally:
            loop.close()

    @staticmethod
    async def _sftp_walk(sftp, path, cap=None):
        """Return ``(relative path, size)`` of every file under *path*.

        Links are neither followed nor counted (``lstat`` semantics of a
        directory listing): a link out of the folder must not make
        another folder's files this one's.

        :param sftp: an open ``asyncssh.SFTPClient``
        :param str path: the folder (``''`` for the home)
        :param int cap: stop after this many files (``None``: no cap)
        :return: the files found; empty when *path* does not exist
        :rtype: list
        """
        cap = cap or _SFTP_LISTING_CAP
        found = []
        pending = [path or "."]
        while pending and len(found) < cap:
            current = pending.pop()
            try:
                names = await sftp.readdir(current)
            except asyncssh.SFTPNoSuchFile:
                if current == (path or "."):
                    return []
                continue
            for entry in names:
                name = entry.filename
                if name in (".", ".."):
                    continue
                full = f"{current}/{name}"
                kind = entry.attrs.type
                if kind == asyncssh.FILEXFER_TYPE_DIRECTORY or (
                    kind is None and entry.attrs.permissions is not None
                    and stat.S_ISDIR(entry.attrs.permissions)
                ):
                    pending.append(full)
                elif kind == asyncssh.FILEXFER_TYPE_REGULAR or (
                    kind is None and entry.attrs.permissions is not None
                    and stat.S_ISREG(entry.attrs.permissions)
                ):
                    found.append((full, entry.attrs.size or 0))
                    if len(found) >= cap:
                        break
        if len(found) >= cap:
            _logger.warning(
                "[sftp] %s: stopped at %d files; sizes and usage under it "
                "count those only", path or ".", cap,
            )
        return found

    def _sftp_scan(self, path):
        """Synchronous :meth:`_sftp_walk` of *path* on this destination.

        :param str path: the folder (``''`` for the home)
        :rtype: list
        :raise SftpUnreachable: when the server cannot be reached
        """
        self.ensure_one()
        return self._sftp_run(lambda sftp: self._sftp_walk(sftp, path))

    def _sftp_measure_prefix(self, dst):
        """Return ``(bytes, files)`` of an instance's folder on this server.

        The folder is read off *dst*, which must name this destination's
        server (``sftp_dst.check_dst``) and one instance's folder: either
        error raises, so the copy is reported unreachable, never missing.

        :param str dst: the frozen ``sftp://`` path
        :rtype: tuple
        """
        self.ensure_one()
        path = sftp_dst.check_dst(
            self.sftp_user, self.sftp_host, self.sftp_port, dst,
        )
        if not sftp_dst.instance_path(path):
            raise sftp_dst.DstError(f"{dst} is not one instance's folder")
        files = self._sftp_scan(path)
        return float(sum(size for _p, size in files)), len(files)

    def _sftp_test(self):
        """Log in, create the folder, write and delete a file, read the space.

        :return: ``free_bytes`` (None when the server does not say)
        :rtype: dict
        :raise SftpUnreachable: when the connection or the login fails
        :raise asyncssh.SFTPError: when the folder cannot be written
        """
        self.ensure_one()
        path = sftp_dst.check_path(self.sftp_path) or "."
        probe = f"{path}/.incubacloud-probe-{secrets.token_hex(8)}"

        async def _probe(sftp):
            if path != ".":
                await sftp.makedirs(path, exist_ok=True)
            async with sftp.open(probe, "w") as fh:
                await fh.write("incubacloud\n")
            await sftp.remove(probe)
            try:
                vfs = await sftp.statvfs(path)
            except asyncssh.SFTPError:
                return {"free_bytes": None}
            return {"free_bytes": vfs.bavail * vfs.frsize}

        return self._sftp_run(_probe)

    def _sftp_check_presented_key(self):
        """Raise the server key's alert if the backups would refuse its key.

        Compares fingerprints read with the key exchange alone, rather
        than reading the failure's type: a server that stopped offering
        the trusted key's algorithm fails the negotiation, not the key
        check. The key is read as the backups ask for it
        (``_sftp_backups_order``), and a trusted RSA key is read again
        with RSA alone when the server answers with another type
        (``_sftp_reads_rsa_again``).
        Nothing is trusted here; the person confirms a new key on the
        destination's page.

        :return: ``(outcome, fingerprint)``, as
            :meth:`_sftp_alert_on_other_key`; ``("", "")`` when the server
            cannot be reached now
        :rtype: tuple
        """
        self.ensure_one()
        if not self.sftp_host_key_fingerprint:
            return ("", "")
        try:
            line = self._sftp_presented_line(self._sftp_backups_order())
        except UserError:
            # Not reachable now: nothing to compare, nothing to say.
            return ("", "")
        rsa_line = ""
        if self._sftp_reads_rsa_again(line):
            with contextlib.suppress(UserError):
                rsa_line = self._sftp_presented_line(
                    ssh_host_keys.RSA_HOST_KEY_ORDER,
                )
        return self._sftp_alert_on_other_key(line, rsa_line)

    async def _sftp_check_presented_key_async(self):
        """:meth:`_sftp_check_presented_key` inside a running event loop.

        :return: ``(outcome, fingerprint)``, as
            :meth:`_sftp_alert_on_other_key`
        :rtype: tuple
        """
        self.ensure_one()
        if not self.sftp_host_key_fingerprint:
            return ("", "")
        try:
            line = await self._sftp_presented_line_async(
                self._sftp_backups_order(),
            )
        except UserError:
            return ("", "")
        rsa_line = ""
        if self._sftp_reads_rsa_again(line):
            with contextlib.suppress(UserError):
                rsa_line = await self._sftp_presented_line_async(
                    ssh_host_keys.RSA_HOST_KEY_ORDER,
                )
        return self._sftp_alert_on_other_key(line, rsa_line)

    def _sftp_backups_order(self):
        """Return the order the backups ask the server for its key in.

        paramiko's, with the trusted line's type first
        (``ssh_host_keys.paramiko_order``): a server that still offers it
        answers with the key the backups compare, so a destination whose
        backups work raises nothing.

        :rtype: tuple
        """
        return ssh_host_keys.paramiko_order(
            ssh_host_keys.key_type(self.sudo().sftp_known_hosts or ""),
        )

    def _sftp_reads_rsa_again(self, line):
        """Tell whether the trusted key must be looked for with RSA alone.

        True when the destination trusts an RSA key and the server answers
        paramiko's order with another key type. A server on OpenSSH 8.8 or
        later no longer offers ``ssh-rsa``, the type paramiko asks first
        for an RSA line, so the backups get its Ed25519 or ECDSA key: the
        RSA key may well be unchanged.

        :param str line: the ``known_hosts`` line the server presents now
        :rtype: bool
        """
        trusted_line = self.sudo().sftp_known_hosts or ""
        return (
            ssh_host_keys.key_type(trusted_line) == "ssh-rsa"
            and ssh_host_keys.key_type(line) != "ssh-rsa"
        )

    def _sftp_alert_on_other_key(self, line, rsa_line=""):
        """Raise the server key's alert when the backups refuse *line*.

        Two cases, one alert code (``_SFTP_HOST_KEY_ALERT_CODE``):

        - the server still presents the trusted RSA key (*rsa_line*) and
          answers paramiko's order with another type: the key did not
          change, the backups need that other key trusted, and the alert
          says so without calling it a change;
        - otherwise the server presents another key than the trusted one:
          the changed-key alert.

        :param str line: the ``known_hosts`` line the server presents now,
            in paramiko's order
        :param str rsa_line: the line read with RSA alone, when it was read
            (``_sftp_reads_rsa_again``), else ``''``
        :return: ``("", "")`` when *line* is the trusted key (or none is
            trusted), ``("key_type", fingerprint)`` in the first case,
            ``("changed", fingerprint)`` in the second, with the
            fingerprint of *line*
        :rtype: tuple
        """
        trusted = self.sftp_host_key_fingerprint or ""
        presented = ssh_host_keys.fingerprint(line)
        if not trusted or presented == trusted:
            return ("", "")
        if rsa_line and ssh_host_keys.fingerprint(rsa_line) == trusted:
            kind = ssh_host_keys.key_type(line)
            self.env["cloud.alert"].sudo().raise_alert(
                self._SFTP_HOST_KEY_ALERT_CODE,
                _(
                    "The backups to the SFTP server of the backup destination "
                    "%(name)s check its %(type)s key, while the destination "
                    "trusts its RSA key, which the server still presents. Read "
                    "the server's key on the destination's page, trust it, "
                    "then rebuild the instances on the destination: backups "
                    "to it fail until then. Before you trust the key, compare "
                    "its fingerprint with the one your provider shows.",
                    name=self.name, type=_KEY_TYPE_NAMES.get(kind, kind),
                ),
                level="critical",
                key=self._alert_key(),
                payload={"backup_backend_id": self.id},
            )
            return ("key_type", presented)
        self.env["cloud.alert"].sudo().raise_alert(
            self._SFTP_HOST_KEY_ALERT_CODE,
            _(
                "The SFTP server of the backup destination %(name)s presents "
                "the key %(presented)s, not the trusted %(trusted)s. Backups "
                "to it fail until the key is checked and trusted again.",
                name=self.name, presented=presented, trusted=trusted,
            ),
            level="critical",
            key=self._alert_key(),
            payload={"backup_backend_id": self.id},
        )
        return ("changed", presented)

    # ── SFTP validation ─────────────────────────────────────────────────────

    @api.constrains(
        "backend_type", "sftp_host", "sftp_port", "sftp_user", "sftp_path",
        "retention_owner",
    )
    def _check_sftp_fields(self):
        """Refuse an SFTP destination its values cannot safely reach.

        The values end up in a URL, the backup container's environment and
        copier's answers: a host is a DNS name or an IPv4 address (an IPv6
        literal would need brackets duplicity does not write), the user
        and the path are limited to characters nothing reads twice
        (``sftp_dst``). A server has no bucket to hold a lifecycle rule, so
        only the backup service or nobody can delete old backups.

        :raise ValidationError: on the first value refused
        """
        for rec in self.filtered(lambda r: r.backend_type == "sftp"):
            host = (rec.sftp_host or "").strip()
            if not host or not is_host_address(host):
                raise ValidationError(_(
                    "Enter the SFTP server as a host name or an IPv4 address."
                ))
            with contextlib.suppress(ValueError):
                if ipaddress.ip_address(host).version == 6:
                    raise ValidationError(_(
                        "IPv6 addresses are not supported for SFTP yet: "
                        "enter a host name instead."
                    ))
            if not 1 <= (rec.sftp_port or 0) <= 65535:
                raise ValidationError(_("The SFTP port goes from 1 to 65535."))
            if not sftp_dst.USER_RE.match(rec.sftp_user or ""):
                raise ValidationError(_(
                    "The SFTP user may hold letters, digits, dots, dashes "
                    "and underscores, up to 100 characters."
                ))
            try:
                sftp_dst.check_path(rec.sftp_path)
            except sftp_dst.DstError as exc:
                raise ValidationError(_(
                    "The SFTP path may hold letters, digits, dots, dashes "
                    "and underscores between slashes, without empty, . or "
                    ".. parts."
                )) from exc
            if rec.retention_owner == "lifecycle":
                raise ValidationError(_(
                    "An SFTP server has no bucket for a lifecycle rule: let "
                    "the backup service (Duplicity) delete old backups, or "
                    "choose Nobody."
                ))

    @api.constrains("backend_type", "sftp_auth_method", "sftp_password")
    def _check_sftp_password(self):
        """Require the password of an SFTP destination that logs in with one.

        It reaches the backup container as a line of ``backup.env``
        (``BACKEND_PASSWORD``), which compose reads: a value that holds
        what compose reads as more than the value would log in with
        something else, so it is refused here, as the passphrase is
        (``compose_env_breaks``).

        It is at least ``SFTP_PASSWORD_MIN_LEN`` characters long, so the
        agents' scrubber, which masks secrets of that length or more,
        always masks it in what an agent reads.

        :raises ValidationError: without a password, with a shorter one,
            or with one compose would not read as written
        """
        for rec in self.filtered(
            lambda r: r.backend_type == "sftp" and r.sftp_auth_method == "password"
        ):
            # sudo: the password is a manager's field; the check is not.
            password = rec.sudo().sftp_password
            if not password:
                raise ValidationError(_(
                    "Enter the password %(name)s logs in to the SFTP server "
                    "with.",
                    name=rec.name,
                ))
            if len(password) < SFTP_PASSWORD_MIN_LEN:
                raise ValidationError(_(
                    "The SFTP password of %(name)s must be at least %(min)s "
                    "characters long.",
                    name=rec.name, min=SFTP_PASSWORD_MIN_LEN,
                ))
            if compose_env_breaks(password):
                raise ValidationError(_(
                    "The SFTP password of %(name)s may not hold quotes, "
                    "backquotes, $, \\, #, line breaks or control "
                    "characters, nor start or end with a space.",
                    name=rec.name,
                ))

    @api.constrains("backend_type", "s3_bucket", "s3_path")
    def _check_backup_dst_shape(self):
        """Keep the bucket and the path to the grammar of a backup path.

        Every instance's path is computed from them
        (``cloud.instance._compute_instance_backup_dst``) and frozen into
        ``custom_backup_dst`` when the instance is archived, which holds
        that field's grammar (``_backup_dst.dst_is_valid``): a bucket or a
        path outside it would reach the hosts' files and commands. Checked
        when they are written; a destination saved before goes on as it
        is.

        :raises ValidationError: for a bucket or a path outside it
        """
        for rec in self:
            if rec.backup_dst and not dst_is_valid(rec.backup_dst):
                raise ValidationError(
                    _(
                        "The bucket and the path of %(name)s may hold only "
                        "letters, digits, dots, hyphens and underscores, with "
                        "a / between the parts of the path. No '..', spaces "
                        "or shell metacharacters.",
                        name=rec.name,
                    )
                )

    @api.constrains(
        "s3_endpoint_url", "s3_region", "s3_access_key_id",
        "s3_secret_access_key", "passphrase",
    )
    def _check_s3_access_shape(self):
        """Keep the endpoint and the secrets to one line of an environment file.

        Each is written as a line of the backup container's and the purges'
        environment files (``_backup_dst.env_line``), where a line break
        starts a variable of its own and the last of two wins: an endpoint
        could name another ``DST``. ``backup.env`` and ``pitr.env`` are
        read by compose, which also closes a value that opens with a quote
        and reads the rest of the line as another variable, interpolates
        ``$`` and cuts at `` #`` (``_backup_dst.compose_env_line``): a
        passphrase would be another one, and lost to a restore. The
        endpoint is an ``http``/``https`` URL of a host
        (``endpoint_url_is_valid``), the region a provider's name of one
        (``region_is_valid``), the keys hold letters, digits and
        ``/ + = . _ -`` only (``access_key_is_valid``), and the passphrase
        none of what compose reads as more (``compose_env_breaks``); the
        one the panel generates holds none. Checked when they are written;
        one saved before is refused by the file it would break, before the
        host is reached.

        :raises ValidationError: for a value outside that shape
        """
        for rec in self:
            # sudo: the secrets are a manager's fields; the check is not.
            secrets = rec.sudo()
            url = rec.s3_endpoint_url
            if url and not endpoint_url_is_valid(url):
                raise ValidationError(_(
                    "The endpoint URL of %(name)s must be an http:// or "
                    "https:// address of a host, with an optional port and "
                    "path, and no spaces.",
                    name=rec.name,
                ))
            if rec.s3_region and not region_is_valid(rec.s3_region):
                raise ValidationError(_(
                    "The region of %(name)s may hold only letters, digits "
                    "and the characters . _ -, as the provider names it "
                    "(e.g. eu-west-par).",
                    name=rec.name,
                ))
            for key in (secrets.s3_access_key_id, secrets.s3_secret_access_key):
                if key and not access_key_is_valid(key):
                    raise ValidationError(_(
                        "The access keys of %(name)s may hold only letters, "
                        "digits and the characters / + = . _ -.",
                        name=rec.name,
                    ))
            if secrets.passphrase and compose_env_breaks(secrets.passphrase):
                raise ValidationError(_(
                    "The encryption passphrase of %(name)s may not hold "
                    "quotes, backquotes, $, \\, #, line breaks or control "
                    "characters, nor start or end with a space.",
                    name=rec.name,
                ))

    @api.constrains("backup_image_version", "backup_tz", "email_from", "email_to")
    def _check_backup_answers_shape(self):
        """Keep the answers the template renders into the compose file to their shape.

        The image tag is written after ``image: …:`` unquoted, and the time
        zone and the addresses between double quotes of the backup
        service's environment (``cloud.instance._render_copier_answers``):
        a line break would add YAML of its own to the service, such as
        ``privileged: true``. A docker tag, a time zone ``pytz`` knows and
        one address each; empty takes the template's default. Checked
        when they are written, an import's included.

        :raises ValidationError: for a value outside that shape
        """
        for rec in self:
            tag = rec.backup_image_version
            if tag and not image_tag_is_valid(tag):
                raise ValidationError(_(
                    "The image version of %(name)s must be a docker tag: "
                    "letters, digits, dots, hyphens and underscores, such as "
                    "latest or 4.0.0.",
                    name=rec.name,
                ))
            if rec.backup_tz and rec.backup_tz not in pytz.all_timezones_set:
                raise ValidationError(_(
                    "The timezone of %(name)s must be a timezone name, such "
                    "as UTC or Europe/Madrid.",
                    name=rec.name,
                ))
            for address in (rec.email_from, rec.email_to):
                if address and (
                    breaks_line(address) or not email_normalize(address)
                ):
                    raise ValidationError(_(
                        "The notification addresses of %(name)s must be one "
                        "email address each.",
                        name=rec.name,
                    ))

    # ── Deletion guard ──────────────────────────────────────────────────────

    def _deletion_blockers(self):
        """Return everything that would lose its storage with this backend.

        An instance resolves its backend as ``instance.backup_backend_id
        or project.backup_backend_id or <global default>``, so asking only
        about the direct assignment misses two entire routes: a project
        default, and the global default that every unassigned instance in
        the fleet inherits.

        Archived instances count. Their chains are still in the bucket —
        keeping them is the whole point of archiving rather than deleting
        — and they are precisely what an ``active_test`` search cannot
        see, so they are what a naive guard silently deletes the storage
        out from under.

        :return: dict with ``instances`` (recordset, archived included),
            ``projects`` (recordset that defaults to this backend) and
            ``is_global_default`` (bool).
        """
        self.ensure_one()
        instances = (
            self.env["cloud.instance"]
            .sudo()
            .with_context(active_test=False)
            .search([("backup_backend_id", "=", self.id)])
        )
        projects = (
            self.env["cloud.project"]
            .sudo()
            .search([("backup_backend_id", "=", self.id)])
        )
        global_id = int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(_GLOBAL_BACKUP_PARAM, 0)
            or 0
        )
        return {
            "instances": instances,
            "projects": projects,
            "is_global_default": global_id == self.id,
        }

    # ── Usage tracking + quota alerts ──────────────────────────────────────
    # Universal feature: applies to every backend, BYO or managed. Any
    # panel built on this module gets it, so any backend configured there
    # benefits.

    quota_gb = fields.Float(
        string="Quota (GB)",
        default=0.0,
        help="Soft cap used for alert calculations. Set to 0 to disable "
        "quota tracking (usage is still measured for display).",
    )
    last_measured_gb = fields.Float(
        string="Last Measured (GB)",
        readonly=True,
    )
    last_measured_at = fields.Datetime(
        string="Last Measured At",
        readonly=True,
    )
    alert_threshold_pct = fields.Integer(
        string="Alert Threshold (%)",
        default=0,
        help="Per-backend override of the system-wide default alert "
        "threshold. 0 = inherit from settings; -1 = disabled; "
        "1–100 = explicit percentage.",
    )
    alert_email = fields.Char(
        string="Alert Email",
        help="Optional override of the recipient for usage alerts. "
        "Falls back to ``Notification To`` if empty.",
    )
    last_alert_sent_at = fields.Datetime(
        string="Last Alert Sent At",
        readonly=True,
    )

    # ── Externally-managed backend association (opaque hook field) ────────
    # When non-empty, this backend is managed by an external module that
    # sets this opaque id and overrides ``_on_managed_backend_delete``.
    # Core stores the id but never interprets it and knows nothing about
    # the managing module — empty for self-managed (BYO) backends.

    managed_reservation_id = fields.Char(
        string="Managed Reservation ID",
        readonly=True,
        copy=False,
        help="Opaque id set by an external module that manages this "
        "backend. Core does not interpret it. Empty for BYO backends.",
    )
    archived_until = fields.Datetime(
        string="Archived Until",
        readonly=True,
        copy=False,
        help="When set, this backend is in retention period — the bucket "
        "still works for backup_download but new backups should not "
        "be written here. The module that manages the backend unlinks "
        "the record once the bucket has been purged.",
    )

    # ── Password handling ───────────────────────────────────────────────────

    _PASSWORD_FIELDS = (
        "s3_secret_access_key", "passphrase", "sftp_private_key", "sftp_password",
    )

    def _check_server_fields(self, vals):
        """Refuse the fields only the server writes, unless it is writing.

        :param dict vals: values of a ``create`` or ``write``
        :raise UserError: when *vals* holds one of them outside ``sudo``
        """
        if self.env.su:
            return
        refused = sorted(set(vals) & set(_SFTP_SERVER_FIELDS))
        if refused:
            raise UserError(_(
                "%(fields)s cannot be set by hand: the panel generates the "
                "key and records the server key you confirm.",
                fields=", ".join(refused),
            ))

    def _sftp_frozen_instances(self):
        """Return the instances whose copy is pinned on this destination.

        An archived instance, or an imported one, carries its copy's
        place in ``custom_backup_dst``: a path on this destination's
        server, read with its key and its trusted server key. Archived
        ones included (``active_test``), as ``_deletion_blockers`` counts
        them.

        :rtype: cloud.instance
        """
        self.ensure_one()
        return (
            self.env["cloud.instance"]
            .sudo()
            .with_context(active_test=False)
            .search([("custom_backup_dst", "!=", False)])
            .filtered(lambda i: i.effective_backup_backend == self)
        )

    def _check_sftp_identity(self, vals):
        """Refuse moving an SFTP destination that holds pinned copies.

        The user, the server and the port are the destination's, never a
        DST's (``sftp_dst``): an archived or imported instance's copy
        stays on the server it was written to, and a destination that
        moved would read and purge it with the wrong key and server key,
        or not at all.

        :param dict vals: values of a ``write``
        :raise UserError: when one changes and such an instance exists
        """
        for rec in self.filtered(lambda r: r.backend_type == "sftp"):
            moved = [
                f for f in _SFTP_IDENTITY_FIELDS
                if f in vals and vals[f] != rec[f]
            ]
            if not moved:
                continue
            pinned = rec._sftp_frozen_instances()
            if pinned:
                raise UserError(_(
                    "The server, port and user of %(name)s cannot change: "
                    "%(instances)s keep their copy on the current server. "
                    "Create a new destination for the other server.",
                    name=rec.name,
                    instances=", ".join(pinned.mapped("name")),
                ))

    @api.model
    def _normalize_sftp_vals(self, vals):
        """Write the SFTP server and port of *vals* as they are compared.

        The server in lower case, without the spaces around it: duplicity's
        paramiko looks the server key up under the URL's host as
        ``urlparse`` lowers it and compares the labels exactly, so a key
        trusted under ``Backup.example.com`` would read as an unknown
        server on every copy and every purge. The port as an integer: the
        page may send it as text, and ``"22" != 22`` would read as another
        port, dropping the trusted key or refusing a move that is none.

        :param dict vals: values of a ``create`` or ``write``, changed in
            place
        :raise ValidationError: when the port is not a number
        """
        if isinstance(vals.get("sftp_host"), str):
            vals["sftp_host"] = vals["sftp_host"].strip().lower()
        if isinstance(vals.get("sftp_port"), str):
            try:
                vals["sftp_port"] = int(vals["sftp_port"].strip() or 0)
            except ValueError as exc:
                raise ValidationError(_(
                    "The SFTP port goes from 1 to 65535."
                )) from exc

    def write(self, vals):
        self._check_can_manage_settings()
        self._check_server_fields(vals)
        self._normalize_sftp_vals(vals)
        # An empty secret from the page means "untouched"; the platform
        # dropping one it has no use for says so (``_sftp_settle_login``).
        dropping = self.env.su and self.env.context.get("ic_sftp_drop_secret")
        for field in self._PASSWORD_FIELDS:
            if field in vals and not vals[field] and not dropping:
                del vals[field]
        if vals.get("s3_endpoint_url"):
            vals["s3_endpoint_url"] = normalize_endpoint_url(vals["s3_endpoint_url"])
        if isinstance(vals.get("s3_region"), str):
            vals["s3_region"] = vals["s3_region"].strip()
        if "backend_type" in vals and any(
            rec.backend_type != vals["backend_type"] for rec in self
        ):
            raise UserError(_(
                "A backup destination keeps its type: create a new "
                "destination for the other type."
            ))
        self._check_sftp_identity(vals)
        endpoint_moved = self.filtered(
            lambda r: r.backend_type == "sftp" and any(
                f in vals and vals[f] != r[f] for f in ("sftp_host", "sftp_port")
            )
        )
        login_changed = self.filtered(
            lambda r: r.backend_type == "sftp" and "sftp_auth_method" in vals
            and vals["sftp_auth_method"] != r.sftp_auth_method
        )
        password_written = bool(vals.get("sftp_password"))
        res = super().write(vals)
        self._sftp_settle_login(login_changed, password_written)
        if endpoint_moved:
            # Another server, or another port of it: the key confirmed
            # was the old endpoint's, and must be confirmed again.
            endpoint_moved.sudo().write({
                "sftp_known_hosts": False,
                "sftp_host_key_trusted_at": False,
                "sftp_host_key_trusted_by": False,
            })
        # An explicit user choice of retention_owner resolves the
        # ambiguous state and its warning alert. Migrations bypass the
        # ORM (raw SQL), so this only fires for real operator edits.
        if "retention_owner" in vals and "retention_owner_declared" not in vals:
            self.write({"retention_owner_declared": True})
            self._resolve_retention_undeclared_alert()
        return res

    def unlink(self):
        self._check_can_manage_settings()
        return super().unlink()

    def _on_managed_backend_delete(self):
        """Hook: convert a delete of an externally-managed backend into the
        managing module's own teardown.

        Called by the delete endpoint when ``managed_reservation_id`` is
        set. The managing module (which set that id) overrides this to run
        its teardown and returns a JSON-RPC result dict. Core has no
        managed-backup concept, so by default such a backend cannot be
        deleted here.
        """
        self.ensure_one()
        return {
            "ok": False,
            "error": _("This managed destination cannot be deleted from this database."),
        }

    @api.model_create_multi
    def create(self, vals_list):
        self._check_can_manage_settings()
        private_keys = []
        for vals in vals_list:
            self._check_server_fields(vals)
            self._normalize_sftp_vals(vals)
            if not vals.get("passphrase"):
                vals["passphrase"] = generate_password()
            if vals.get("s3_endpoint_url"):
                vals["s3_endpoint_url"] = normalize_endpoint_url(
                    vals["s3_endpoint_url"],
                )
            if isinstance(vals.get("s3_region"), str):
                vals["s3_region"] = vals["s3_region"].strip()
            private = None
            if vals.get("sftp_auth_method", "key") == "key":
                # A key destination keeps no password, a password one no key.
                vals.pop("sftp_password", None)
                if vals.get("backend_type") == "sftp":
                    pair = self._sftp_key_pair()
                    private = pair.pop("sftp_private_key")
                    vals.update(pair)
            private_keys.append(private)
        records = super().create(vals_list)
        # The private half is a field of the platform (``base.group_system``),
        # which Odoo refuses in the values of a manager who is not an Odoo
        # administrator: the server writes it, as on a regeneration.
        for rec, private in zip(records, private_keys):
            if private:
                rec.sudo().write({"sftp_private_key": private})
        return records

    @api.model
    def _sftp_key_pair(self):
        """Generate the Ed25519 key pair an SFTP destination logs in with.

        The private half stays in the panel, encrypted and readable by the
        platform only; the public half is what the person adds to the
        server's ``authorized_keys``.

        :return: ``sftp_private_key`` (OpenSSH PEM) and ``sftp_public_key``
            (one ``authorized_keys`` line)
        :rtype: dict
        """
        key = asyncssh.generate_private_key(
            "ssh-ed25519", comment=SFTP_KEY_COMMENT,
        )
        kind, data = key.export_public_key("openssh").decode().split()[:2]
        return {
            "sftp_private_key": key.export_private_key("openssh").decode(),
            "sftp_public_key": f"{kind} {data} {SFTP_KEY_COMMENT}",
        }

    def _sftp_settle_login(self, switched, password_written):
        """Keep the secret of the way each SFTP destination logs in, only.

        A destination switched to a password loses its key pair, and one
        switched to a key its password and gets a new pair: the other
        secret has no use left, and nothing should hold it. The instances
        get the change on their next build (``_sftp_snapshot``); until
        then they log in as before. A password written to a destination
        that logs in with a key is dropped.

        :param switched: the destinations whose ``sftp_auth_method`` this
            write changed
        :param bool password_written: whether the write set a password
        """
        platform = self.sudo().with_context(ic_sftp_drop_secret=True)
        for rec in switched:
            if rec.sftp_auth_method == "password":
                platform.browse(rec.id).write({
                    "sftp_private_key": False, "sftp_public_key": False,
                })
            else:
                platform.browse(rec.id).write({
                    "sftp_password": False, **self._sftp_key_pair(),
                })
            self.env["cloud.audit.log"].sudo().create({
                "action": "SFTP login changed",
                "details": (
                    f"backup destination {rec.name} (id {rec.id}): "
                    f"now with a {rec.sftp_auth_method}"
                ),
            })
        if password_written:
            keyed = (self - switched).filtered(
                lambda r: r.backend_type == "sftp" and r.sftp_auth_method == "key"
            )
            if keyed:
                platform.browse(keyed.ids).write({"sftp_password": False})

    def _sftp_regenerate_key(self):
        """Replace the key pair; the instances get it on their next build.

        The new public key has to be on the server before then: until each
        instance is rebuilt it keeps logging in with the old one, so the
        old line stays in ``authorized_keys`` until all are.
        """
        self.ensure_one()
        if self.backend_type != "sftp":
            raise UserError(_("Only an SFTP destination has a key."))
        if self.sftp_auth_method == "password":
            raise UserError(_(
                "%(name)s logs in with a password: switch it to a key first.",
                name=self.name,
            ))
        self.sudo().write(self._sftp_key_pair())
        self.env["cloud.audit.log"].sudo().create({
            "action": "SFTP backup key regenerated",
            "details": f"backup destination {self.name} (id {self.id})",
        })

    def _sftp_snapshot(self):
        """Return what an instance's configuration snapshot keeps of SFTP.

        Fingerprints only, never a key: a new key pair or a newly trusted
        server key leaves the instances on this destination with changes
        to apply, because the files that carry them reach a host on a
        build only. So does a new password, or a switch between the two,
        with ``auth`` and the password's fingerprint; a destination on a
        key has neither, so the instances on one hash as before.

        The snapshot is stored on the instance, which every member of its
        project reads: the password's fingerprint is an HMAC keyed by the
        database's ``database.secret`` (``odoo.tools.misc.hmac``), which
        only the platform reads, so nobody can check a guessed password
        against it. It is a function of the password itself, not of its
        encrypted form: Fernet encrypts the same password differently
        each time, so saving the same one again would leave changes to
        apply that change nothing on the hosts, and a rotated encryption
        key would leave them on every instance. Keyed so, it changes
        exactly when the password does.

        :return: ``public`` (SHA-256 of the public key) and ``host_key``
            (the trusted server key's fingerprint); with a password,
            ``auth`` and ``password`` too
        :rtype: dict
        """
        self.ensure_one()
        public = self.sftp_public_key or ""
        snapshot = {
            "public": hashlib.sha256(public.encode()).hexdigest() if public else "",
            "host_key": self.sftp_host_key_fingerprint or "",
        }
        if self.sftp_auth_method == "password":
            fingerprint = keyed_hmac(
                self.env(su=True), SFTP_PASSWORD_HMAC_SCOPE,
                (self.id, self.sudo().sftp_password or ""),
            )
            snapshot.update({"auth": "password", "password": fingerprint[:16]})
        return snapshot

    # ── Effective alert threshold ──────────────────────────────────────────

    def _effective_alert_threshold(self):
        """Resolve the threshold % that applies to *self*.

        Returns ``None`` when alerts are disabled (own field is -1, or
        own field is 0 and the settings default is also 0/-1, or no
        quota is set on the backend). Otherwise returns the percentage
        in the 1–100 range to compare ``last_measured_gb / quota_gb``
        against.
        """
        self.ensure_one()
        if not self.quota_gb or self.quota_gb <= 0:
            return None
        own = self.alert_threshold_pct
        if own == -1:
            return None
        if own > 0:
            return min(own, 100)
        # Inherit
        default = (
            self.env["cloud.settings"]
            .sudo()
            ._get_system()
            .default_backup_alert_threshold_pct
        )
        if default <= 0:
            return None
        return min(default, 100)

    # ── Usage measurement (boto3 ListObjectsV2 + sum) ──────────────────────

    def _check_backend_type_supported(self, what):
        """Raise on a type *what* does not handle, instead of answering empty.

        An empty answer reads as an empty destination: an archived copy
        reported lost, a quota at zero. Not knowing has to say so.

        :param str what: the operation, for the message
        :raise UserError: on a type other than S3
        """
        self.ensure_one()
        if self.backend_type not in ("s3", "sftp"):
            raise UserError(_(
                "%(what)s does not handle %(type)s destinations.",
                what=what, type=self.backend_type,
            ))

    def _measure_prefix(self, dst):
        """Return ``(bytes, object_count)`` under the duplicity DST *dst*.

        Used to tell an archived copy that is still there from one a
        provider lifecycle rule or a manual delete removed — a
        distinction ``test_backup_backend`` cannot make, because
        credentials and bucket stay valid over an emptied prefix.

        Takes the *frozen* path rather than recomputing one: the
        computed path derives from the project and the instance name,
        and an archived instance can lose its project, at which point
        the derivation points at a shared fallback prefix and would
        report someone else's objects as this instance's copy.

        Errors are raised, not swallowed: the caller has to be able to
        tell "the prefix is empty" from "the provider did not answer",
        and returning zero for both would report a network blip as data
        loss.

        :param str dst: ``boto3+s3://bucket/path/...`` as stored in
            ``custom_backup_dst``.
        :return: tuple of total size in bytes and number of objects.
        """
        self.ensure_one()
        self._check_backend_type_supported("_measure_prefix")
        if not dst:
            return 0.0, 0
        if self.backend_type == "sftp":
            return self._sftp_measure_prefix(dst)
        parsed = urlparse(dst.replace("boto3+s3://", "s3://"))
        bucket = parsed.netloc
        prefix = parsed.path.strip("/")
        if not bucket or not prefix:
            # A bare bucket would measure every instance that shares the
            # destination and report the total as this one's copy.
            return 0.0, 0
        kwargs = {
            "aws_access_key_id": self.s3_access_key_id,
            "aws_secret_access_key": self.s3_secret_access_key,
        }
        if self.s3_endpoint_url:
            kwargs["endpoint_url"] = self.s3_endpoint_url
        if self.s3_region:
            kwargs["region_name"] = self.s3_region
        client = boto3.client("s3", **kwargs)
        total = 0
        count = 0
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
            for obj in page.get("Contents") or ():
                total += obj.get("Size") or 0
                count += 1
        return float(total), count

    def _measure_usage(self):
        """Refresh ``last_measured_gb`` / ``last_measured_at`` for *self*.

        Paginated ``ListObjectsV2`` summing ``Size``. S3-compatible ops
        are free on Cloudflare R2 and most providers; AWS S3 charges
        per-1k LIST but a daily cron at small object counts stays in
        the cents.

        Raises ``BotoCoreError`` / ``ClientError`` upwards — the caller
        (cron or on-demand endpoint) decides whether to swallow.
        """
        self.ensure_one()
        self._check_backend_type_supported("_measure_usage")
        if self.backend_type == "sftp":
            total = sum(
                size for _path, size in self._sftp_scan(
                    sftp_dst.check_path(self.sftp_path),
                )
            )
            self.sudo().write({
                "last_measured_gb": total / (1024**3),
                "last_measured_at": fields.Datetime.now(),
            })
            return
        if not self.s3_bucket:
            return
        if not self.s3_access_key_id or not self.s3_secret_access_key:
            return
        kwargs = {
            "aws_access_key_id": self.s3_access_key_id,
            "aws_secret_access_key": self.s3_secret_access_key,
        }
        if self.s3_endpoint_url:
            kwargs["endpoint_url"] = self.s3_endpoint_url
        if self.s3_region:
            kwargs["region_name"] = self.s3_region
        client = boto3.client("s3", **kwargs)
        prefix = (self.s3_path or "").strip("/")
        total = 0
        paginator = client.get_paginator("list_objects_v2")
        page_kwargs = {"Bucket": self.s3_bucket}
        if prefix:
            page_kwargs["Prefix"] = f"{prefix}/"
        for page in paginator.paginate(**page_kwargs):
            for obj in page.get("Contents") or ():
                total += obj.get("Size") or 0
        # 1 GB = 1024**3 bytes. Backups are typically reported in IEC.
        gb = total / (1024**3)
        self.sudo().write(
            {
                "last_measured_gb": gb,
                "last_measured_at": fields.Datetime.now(),
            }
        )

    def _maybe_send_threshold_alert(self):
        """Send the usage-alert email if *self* is above its threshold
        and the 7-day debounce has elapsed.

        Idempotent: callable from any cron pass without re-firing.
        """
        self.ensure_one()
        threshold_pct = self._effective_alert_threshold()
        if threshold_pct is None:
            return
        if not self.last_measured_gb or self.last_measured_gb <= 0:
            return
        pct_used = (self.last_measured_gb / self.quota_gb) * 100
        if pct_used < threshold_pct:
            return
        # Debounce.
        if self.last_alert_sent_at:
            elapsed = fields.Datetime.now() - self.last_alert_sent_at
            if elapsed < timedelta(days=_ALERT_DEBOUNCE_DAYS):
                return
        recipient = (self.alert_email or self.email_to or "").strip()
        if not recipient:
            _logger.warning(
                "Backup backend %s reached %.1f%% usage but has no "
                "alert recipient (alert_email/email_to both empty). "
                "Set one to receive alerts.",
                self.display_name,
                pct_used,
            )
            return
        template = self.env.ref(
            "incubacloud.mail_template_backup_usage_alert",
            raise_if_not_found=False,
        )
        if not template:
            _logger.error(
                "Cannot send backup usage alert for %s: mail template "
                "'incubacloud.mail_template_backup_usage_alert' is missing.",
                self.display_name,
            )
            return
        template.sudo().with_context(
            backup_alert_pct_used=round(pct_used, 1),
            backup_alert_threshold_pct=threshold_pct,
        ).send_mail(
            self.id,
            force_send=False,
            email_values={
                "email_to": recipient,
            },
        )
        self.sudo().write({"last_alert_sent_at": fields.Datetime.now()})

    @api.model
    def _cron_measure_backup_usage(self):
        """Daily cron: measure usage for every S3 backend and alert on threshold.

        Per-backend try/except — one failing backend (bad creds, network,
        missing bucket) must not abort the loop.
        """
        backends = self.sudo().search(
            [
                "|",
                "&", "&",
                ("backend_type", "=", "s3"),
                ("s3_bucket", "!=", False),
                ("s3_access_key_id", "!=", False),
                "&",
                ("backend_type", "=", "sftp"),
                ("sftp_host_key_fingerprint", "!=", False),
            ]
        )
        ok, failed = 0, 0
        for backend in backends:
            try:
                backend._measure_usage()
                backend._maybe_send_threshold_alert()
                ok += 1
            except SftpUnreachable as exc:
                failed += 1
                _logger.warning(
                    "[backup-usage-cron] %s: measurement failed: %s",
                    backend.display_name, exc,
                )
                backend._sftp_check_presented_key()
            except (BotoCoreError, ClientError) as exc:
                failed += 1
                _logger.warning(
                    "[backup-usage-cron] %s: measurement failed: %s",
                    backend.display_name,
                    exc,
                )
            except Exception:  # noqa: BLE001
                failed += 1
                _logger.exception(
                    "[backup-usage-cron] %s: unexpected failure",
                    backend.display_name,
                )
        _logger.info(
            "[backup-usage-cron] measured %s backend(s), %s failed",
            ok,
            failed,
        )
        return {"ok": ok, "failed": failed}

    # ── Retention-owner ambiguous-state alert ───────────────────────────────
    # cloud.alert has no backup_backend_id column: a backend's alerts have
    # no host/instance/project target, and the backend is their key.

    _RETENTION_ALERT_CODE = "backup_retention_undeclared"

    def _alert_key(self):
        """Return what identifies this backend's alerts (``dedup_key``).

        :rtype: str
        """
        self.ensure_one()
        return f"backup_backend:{self.id}"

    def _resolve_retention_undeclared_alert(self):
        """Dismiss this backend's undeclared-retention alert, if open."""
        self.ensure_one()
        self.env["cloud.alert"].resolve_alert(
            self._RETENTION_ALERT_CODE, key=self._alert_key(),
        )

    @api.model
    def _cron_check_retention_declared(self):
        """Daily cron: warn on backends whose retention owner is still
        ambiguous (migrated from the old on/off flag with the flag off,
        or otherwise never explicitly confirmed by the operator).

        Declaring an owner via ``write()`` resolves the alert; this
        cron never auto-resolves anything, only creates/refreshes.
        """
        Alert = self.env["cloud.alert"]
        undeclared = self.search([("retention_owner_declared", "=", False)])
        for backend in undeclared:
            Alert.raise_alert(
                self._RETENTION_ALERT_CODE,
                f"Backup backend '{backend.display_name}' has an "
                f"unconfirmed retention policy — old backups may "
                f"never be deleted. Open it and pick who deletes old "
                f"backups (the backup service (Duplicity) / a lifecycle "
                f"rule on the bucket / nobody).",
                key=backend._alert_key(),
                payload={"backup_backend_id": backend.id},
            )
        return {"flagged": len(undeclared)}

    # ── Managed backend orphan alert (P15 #9b) ──────────────────────────────
    # Core only knows ``managed_reservation_id`` is non-empty ("this backend
    # is managed by some external module"); it never interprets the id
    # itself, so this check stays valid regardless of who manages it.

    _MANAGED_UNUSED_ALERT_CODE = "managed_backend_unused"

    @api.model
    def _cron_check_managed_unused(self):
        """Daily cron: warn on managed backends assigned to nothing —
        not an instance, not a project default, not the global default.

        Never auto-resolves for backends that vanish from the search
        (assigning one dismisses its own alert instead, see below).
        """
        Alert = self.env["cloud.alert"].sudo()
        managed = self.search([("managed_reservation_id", "!=", False)])
        if not managed:
            return {"flagged": 0}

        used_ids = set(
            self.env["cloud.instance"]
            .sudo()
            .search(
                [
                    ("backup_backend_id", "in", managed.ids),
                ]
            )
            .mapped("backup_backend_id")
            .ids
        )
        used_ids |= set(
            self.env["cloud.project"]
            .sudo()
            .search(
                [
                    ("backup_backend_id", "in", managed.ids),
                ]
            )
            .mapped("backup_backend_id")
            .ids
        )
        global_id = int(
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(
                _GLOBAL_BACKUP_PARAM,
                0,
            )
            or 0
        )
        if global_id:
            used_ids.add(global_id)

        flagged = 0
        for backend in managed:
            if backend.id in used_ids:
                Alert.resolve_alert(
                    self._MANAGED_UNUSED_ALERT_CODE, key=backend._alert_key(),
                )
                continue
            flagged += 1
            Alert.raise_alert(
                self._MANAGED_UNUSED_ALERT_CODE,
                f"Managed backup backend '{backend.display_name}' "
                f"is not assigned to any instance, project or the "
                f"global default — it's being paid for and unused.",
                key=backend._alert_key(),
                payload={"backup_backend_id": backend.id},
            )
        return {"flagged": flagged}
