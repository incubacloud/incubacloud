"""Backup backend CRUD and connectivity test.

Mixed into ``CloudDataLoadController`` in ``data_load.py``.
"""

import asyncio

import asyncssh
import boto3
from botocore.exceptions import ClientError, ParamValidationError

from odoo import _, http
from odoo.exceptions import UserError
from odoo.http import request

from ...models.cloud_backup_backend import SftpUnreachable
from .._safe_error import safe_error_response
from ._helpers import _capped_search, _has_encrypted


class BackendsMixin:
    """Backup backend CRUD."""

    # ── Backup backend endpoints ──────────────────────────────────────────────

    _BACKUP_BACKEND_ALLOWED = {
        "name",
        "backend_type",
        "s3_bucket",
        "s3_path",
        "s3_endpoint_url",
        "s3_region",
        "s3_access_key_id",
        "s3_secret_access_key",
        "passphrase",
        "backup_image_version",
        "email_from",
        "email_to",
        "smtp_report_success",
        "backup_retention",
        "retention_owner",
        "backup_tz",
        # Usage / quota / alert (every backend has them)
        "quota_gb",
        "alert_threshold_pct",
        "alert_email",
        # SFTP: where, and how to log in. The key pair and the trusted
        # server key are the server's to write (``_SFTP_SERVER_FIELDS``),
        # never the SPA's; a password is the person's, as an S3 secret.
        "sftp_host",
        "sftp_port",
        "sftp_user",
        "sftp_path",
        "sftp_auth_method",
        "sftp_password",
    }
    # NOTE: ``managed_reservation_id`` and ``archived_until`` are NOT in the
    # allowlist. They are written exclusively by server-side flows
    # (provisioning + delete intercept) and must never be settable from the
    # SPA — a user who could clear ``archived_until`` would skip
    # retention; one who could set ``managed_reservation_id`` could orphan
    # a real bucket. Keep them out of the mass-assignment surface.

    @http.route(["/cloud/get_backup_backends"], type="jsonrpc", auth="user")
    def cloud_get_backup_backends(self):
        # The forms that choose a backend — a new instance's, a new
        # project's, a project's settings — load it from here, and failed
        # to open below a manager. Below one, the backends are named and
        # nothing else, as ``/cloud/get_project_full`` names them to a
        # member: their bucket, path and endpoint stay a manager's, as
        # every other backend endpoint keeps them (SEC-009).
        if not self._sec()._has_cloud_group('group_cloud_manager'):
            self._sec()._check_cloud_group('group_cloud_user')
            backends, total, truncated, limit = _capped_search(
                request.env["cloud.backup.backend"],
                order="name",
            )
            return {
                "items": [{"id": b.id, "name": b.name} for b in backends],
                "total": total,
                "truncated": truncated,
                "limit": limit,
            }
        self._sec()._check_can_manage_settings()
        backends, total, truncated, limit = _capped_search(
            request.env["cloud.backup.backend"],
            order="name",
        )
        # Count instances per backend. We cap the instance scan too to
        # protect the dashboard when the system has tens of thousands of
        # instances — usage counts become approximate above the cap, but
        # the alternative is blowing up the worker.
        instances, _itotal, _itrunc, _ilim = _capped_search(
            request.env["cloud.instance"],
        )
        usage = {}
        for inst in instances:
            bid = inst.effective_backup_backend.id
            if bid:
                usage[bid] = usage.get(bid, 0) + 1
        return {
            "items": [
                {
                    "id": b.id,
                    "name": b.name,
                    "backend_type": b.backend_type,
                    "s3_bucket": b.s3_bucket or "",
                    "s3_path": b.s3_path or "",
                    "s3_endpoint_url": b.s3_endpoint_url or "",
                    "sftp_host": b.sftp_host or "",
                    "backup_dst": b.backup_dst or "",
                    "instance_count": usage.get(b.id, 0),
                    "quota_gb": b.quota_gb or 0.0,
                    "last_measured_gb": b.last_measured_gb or 0.0,
                    "last_measured_at": (
                        b.last_measured_at.isoformat() if b.last_measured_at else ""
                    ),
                    "managed_reservation_id": b.managed_reservation_id or "",
                    "archived_until": (
                        b.archived_until.isoformat() if b.archived_until else ""
                    ),
                }
                for b in backends
            ],
            "total": total,
            "truncated": truncated,
            "limit": limit,
        }

    @http.route(["/cloud/get_backup_backend"], type="jsonrpc", auth="user")
    def cloud_get_backup_backend(self, backend_id):
        # Manager-gated like its save/delete siblings: this returns
        # s3_access_key_id in the clear, which the ACL alone would expose
        # to any stakeholder.
        self._sec()._check_can_manage_settings()
        b = request.env["cloud.backup.backend"].browse(backend_id)
        if not b.exists():
            return {"ok": False, "error": _("Backend not found")}
        return {
            "id": b.id,
            "name": b.name,
            "backend_type": b.backend_type,
            "s3_bucket": b.s3_bucket or "",
            "s3_path": b.s3_path or "",
            "s3_endpoint_url": b.s3_endpoint_url or "",
            "s3_region": b.s3_region or "",
            "s3_access_key_id": b.s3_access_key_id or "",
            "has_s3_secret_access_key": _has_encrypted(b, "s3_secret_access_key"),
            "has_passphrase": _has_encrypted(b, "passphrase"),
            # SFTP: the public half and the trusted server key's
            # fingerprint. Never the private key nor the password: no
            # route reads them.
            "sftp_host": b.sftp_host or "",
            "sftp_port": b.sftp_port or 22,
            "sftp_user": b.sftp_user or "",
            "sftp_path": b.sftp_path or "",
            "sftp_auth_method": b.sftp_auth_method or "key",
            "has_sftp_password": _has_encrypted(b, "sftp_password"),
            "sftp_public_key": b.sftp_public_key or "",
            "sftp_host_key_fingerprint": b.sftp_host_key_fingerprint or "",
            "sftp_host_key_trusted": bool(b.sftp_host_key_fingerprint),
            "sftp_host_key_trusted_at": (
                b.sftp_host_key_trusted_at.isoformat()
                if b.sftp_host_key_trusted_at else ""
            ),
            "sftp_host_key_trusted_by": b.sftp_host_key_trusted_by.name or "",
            "backup_image_version": b.backup_image_version or "latest",
            "email_from": b.email_from or "",
            "email_to": b.email_to or "",
            "smtp_report_success": b.smtp_report_success,
            "backup_retention": b.backup_retention or "3M",
            "retention_owner": b.retention_owner,
            "retention_owner_declared": b.retention_owner_declared,
            "backup_tz": b.backup_tz or "UTC",
            "backup_dst": b.backup_dst or "",
            "quota_gb": b.quota_gb or 0.0,
            "alert_threshold_pct": b.alert_threshold_pct,
            "alert_email": b.alert_email or "",
            "last_measured_gb": b.last_measured_gb or 0.0,
            "last_measured_at": (
                b.last_measured_at.isoformat() if b.last_measured_at else ""
            ),
            "last_alert_sent_at": (
                b.last_alert_sent_at.isoformat() if b.last_alert_sent_at else ""
            ),
            "managed_reservation_id": b.managed_reservation_id or "",
            "archived_until": (
                b.archived_until.isoformat() if b.archived_until else ""
            ),
        }

    @http.route(["/cloud/create_backup_backend"], type="jsonrpc", auth="user")
    def cloud_create_backup_backend(self, vals):
        self._sec()._check_can_manage_settings()
        safe = {k: v for k, v in vals.items() if k in self._BACKUP_BACKEND_ALLOWED}
        b = request.env["cloud.backup.backend"].create(safe)
        return {"id": b.id, "name": b.name}

    @http.route(["/cloud/save_backup_backend"], type="jsonrpc", auth="user")
    def cloud_save_backup_backend(self, backend_id, vals):
        self._sec()._check_can_manage_settings()
        b = request.env["cloud.backup.backend"].browse(backend_id)
        if not b.exists():
            return {"ok": False, "error": _("Backend not found")}
        safe = {k: v for k, v in vals.items() if k in self._BACKUP_BACKEND_ALLOWED}
        b.write(safe)
        # Writing the retention owner confirms it (see the model's write):
        # the page drops its «Unconfirmed» note on this answer.
        return {"ok": True, "retention_owner_declared": b.retention_owner_declared}

    #: Connections to SFTP servers a user may open from the panel per hour
    #: (reading the key, trusting it, testing, measuring), as the relay test.
    _SFTP_CONNECTIONS_PER_HOUR = 20

    def _sftp_rate_limited(self):
        """Count one connection to an SFTP server against the user's hour.

        :return: the deny dict when over the limit, else None
        :rtype: dict or None
        """
        uid = request.env.uid
        allowed = request.env["cloud.rate.limit"].sudo().hit(
            f"sftp_backend:{uid}",
            max_per_window=self._SFTP_CONNECTIONS_PER_HOUR,
            window_seconds=3600,
        )
        if allowed:
            return None
        return {
            "ok": False,
            "error": _(
                "Too many connections to SFTP servers in the last hour. "
                "Try again later."
            ),
        }

    def _sftp_backend(self, backend_id):
        """Return the SFTP destination *backend_id*, or the error dict.

        :return: ``(backend, None)`` or ``(None, {'ok': False, ...})``
        :rtype: tuple
        """
        self._sec()._check_can_manage_settings()
        b = request.env["cloud.backup.backend"].browse(backend_id)
        if not b.exists():
            return None, {"ok": False, "error": _("Backend not found")}
        if b.backend_type != "sftp":
            return None, {
                "ok": False, "error": _("This destination is not SFTP."),
            }
        return b, None

    @http.route(["/cloud/sftp_backend_read_host_key"], type="jsonrpc", auth="user")
    def cloud_sftp_backend_read_host_key(self, backend_id):
        """Read the key the SFTP server presents; trust nothing.

        Only the key exchange runs: no credential is sent.
        """
        b, error = self._sftp_backend(backend_id)
        if error:
            return error
        limited = self._sftp_rate_limited()
        if limited:
            return limited
        try:
            return {"ok": True, **b._sftp_read_host_key()}
        except UserError as exc:
            return {"ok": False, "error": str(exc)}

    @http.route(["/cloud/sftp_backend_trust_host_key"], type="jsonrpc", auth="user")
    def cloud_sftp_backend_trust_host_key(self, backend_id, expected_fingerprint):
        """Trust the server key the person saw, if it is still the one presented."""
        b, error = self._sftp_backend(backend_id)
        if error:
            return error
        limited = self._sftp_rate_limited()
        if limited:
            return limited
        try:
            found = b._sftp_trust_host_key(expected_fingerprint)
        except UserError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "sftp_host_key_fingerprint": found,
            "sftp_host_key_trusted_at": (
                b.sftp_host_key_trusted_at.isoformat()
                if b.sftp_host_key_trusted_at else ""
            ),
            "sftp_host_key_trusted_by": b.sftp_host_key_trusted_by.name or "",
        }

    @http.route(["/cloud/sftp_backend_regenerate_key"], type="jsonrpc", auth="user")
    def cloud_sftp_backend_regenerate_key(self, backend_id):
        """Replace an SFTP destination's key pair and return the new public key.

        The instances log in with the old key until they are rebuilt: the
        page says to keep both lines in ``authorized_keys`` until then.
        """
        self._sec()._check_can_manage_settings()
        b = request.env["cloud.backup.backend"].browse(backend_id)
        if not b.exists():
            return {"ok": False, "error": _("Backend not found")}
        b._sftp_regenerate_key()
        return {"ok": True, "sftp_public_key": b.sftp_public_key or ""}

    @http.route(["/cloud/delete_backup_backend"], type="jsonrpc", auth="user")
    def cloud_delete_backup_backend(self, backend_id):
        self._sec()._check_can_manage_settings()
        b = request.env["cloud.backup.backend"].browse(backend_id)
        if not b.exists():
            return {"ok": False, "error": _("Backend not found")}
        # Every route an instance can reach this backend by, not just the
        # direct assignment: a project default and the global default are
        # backends too, and archived instances still have chains in the
        # bucket. See ``_deletion_blockers``.
        blockers = b._deletion_blockers()
        reasons = []
        instances = blockers["instances"]
        if instances:
            reasons.append(
                _(
                    "%(count)d instance(s) point at it directly: "
                    "%(names)s.",
                    count=len(instances),
                    names=", ".join(
                        inst.name if inst.active
                        else _("%s (archived)", inst.name)
                        for inst in instances
                    ),
                )
            )
        projects = blockers["projects"]
        if projects:
            reasons.append(
                _(
                    "%(count)d project(s) use it as their default: "
                    "%(names)s.",
                    count=len(projects),
                    names=", ".join(projects.mapped("name")),
                )
            )
        if blockers["is_global_default"]:
            reasons.append(
                _(
                    "It is the global default backend, inherited by every "
                    "instance without an assignment of its own."
                )
            )
        if reasons:
            return {
                "ok": False,
                "error": _(
                    "Cannot delete: this backend is still the backup "
                    "destination for existing records. %(reasons)s Clear "
                    "those assignments first. Deleting the backend does "
                    "not delete the backups it holds — it only makes them "
                    "unreachable from the panel.",
                    reasons=" ".join(reasons),
                ),
            }
        # Externally-managed backend: hand the delete to the managing
        # module's teardown hook (it converts it into "enter retention").
        # The managing module returns the result dict; core stays oblivious
        # to what a managed backup is. Fail-closed: the hook does not unlink
        # unless its own teardown succeeded.
        if b.managed_reservation_id:
            return b._on_managed_backend_delete()
        b.unlink()
        return {"ok": True}

    @http.route(["/cloud/test_backup_backend"], type="jsonrpc", auth="user")
    def cloud_test_backup_backend(self, backend_id):
        # Manager-gated, consistent with measure/save: this fires an
        # outbound S3 call using the stored credentials.
        self._sec()._check_can_manage_settings()
        b = request.env["cloud.backup.backend"].browse(backend_id)
        if not b.exists():
            return {"ok": False, "error": _("Backend not found.")}
        if b.backend_type == "sftp":
            limited = self._sftp_rate_limited()
            if limited:
                return limited
            return self._sftp_test_answer(b)
        if b.backend_type != "s3":
            return {
                "ok": False,
                "error": _("Testing this type of destination is not supported."),
            }
        if not b.s3_bucket:
            return {"ok": False, "error": _("No bucket configured.")}

        kwargs = {
            "aws_access_key_id": b.s3_access_key_id or None,
            "aws_secret_access_key": b.s3_secret_access_key or None,
        }
        if b.s3_endpoint_url:
            kwargs["endpoint_url"] = b.s3_endpoint_url
        if b.s3_region:
            kwargs["region_name"] = b.s3_region

        try:
            client = boto3.client("s3", **kwargs)
            client.head_bucket(Bucket=b.s3_bucket)
            return {"ok": True}
        except ParamValidationError:
            return {
                "ok": False,
                "error": _(
                    'Invalid bucket name "%s": '
                    "bucket names cannot contain slashes. "
                    "Put only the bucket name here and the prefix in the Path field."
                )
                % b.s3_bucket,
            }
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            msg = e.response.get("Error", {}).get("Message", str(e))
            if code in ("301", "PermanentRedirect"):
                # The bucket exists but is served from a different
                # endpoint/region/jurisdiction than the one configured.
                # This is NOT a healthy connection: duplicity will fail
                # (it does not follow the redirect) — so report it as an
                # error instead of a false green. This was masking an
                # endpoint/jurisdiction mismatch (e.g. an EU endpoint
                # pointing at a default-jurisdiction bucket).
                return {
                    "ok": False,
                    "error": _(
                        "Endpoint mismatch (301 redirect): the bucket "
                        "exists but is served from a different "
                        "endpoint/region/jurisdiction than configured. "
                        "Check the S3 endpoint URL."
                    ),
                }
            if code == "403":
                return {
                    "ok": False,
                    "error": _(
                        "Access denied (403): check your credentials and bucket policy."
                    ),
                }
            if code == "404":
                return {
                    "ok": False,
                    "error": _('Bucket "%s" not found (404).') % b.s3_bucket,
                }
            return {
                "ok": False,
                "error": _("S3 error %(code)s: %(msg)s") % {"code": code, "msg": msg},
            }
        except Exception as exc:
            return safe_error_response(exc, _("Backend connection test failed"))

    def _sftp_failure(self, b, exc):
        """Say why the panel could not use an SFTP destination.

        A failed key exchange is told apart by reading the key the server
        presents now and comparing fingerprints
        (``_sftp_check_presented_key``, which raises the alert), not by
        the error's type. A trusted RSA key the server still presents is
        no change: the panel logs in with it, so its failure has another
        cause, told below, and the alert asks for the key the backups get.

        :param b: the ``cloud.backup.backend``
        :param exc: what was raised
        :rtype: dict
        """
        if isinstance(exc, UserError):
            return {"ok": False, "error": str(exc)}
        cause = exc.__cause__ if isinstance(exc, SftpUnreachable) else exc
        outcome, presented = b._sftp_check_presented_key()
        if outcome == "changed":
            return {"ok": False, "error": _(
                "The server presents the key %(presented)s, not the trusted "
                "%(trusted)s. Check it with your provider before trusting it.",
                presented=presented, trusted=b.sftp_host_key_fingerprint,
            )}
        if isinstance(cause, asyncssh.PermissionDenied) and (
            b.sftp_auth_method == "password"
        ):
            return {"ok": False, "error": _(
                "The server refused the user or the password. Check both "
                "with your provider and enter the password again."
            )}
        if isinstance(cause, asyncssh.PermissionDenied):
            return {"ok": False, "error": _(
                "The server refused the key. Add this line to the user's "
                "authorized_keys: %(key)s",
                key=b.sftp_public_key,
            )}
        if isinstance(cause, asyncssh.SFTPPermissionDenied):
            return {"ok": False, "error": _(
                "The user may not write in %(path)s.", path=b.sftp_path or "~",
            )}
        if isinstance(cause, (TimeoutError, asyncio.TimeoutError)):
            return {"ok": False, "error": _("The server did not answer in time.")}
        return {"ok": False, "error": _(
            "Could not use the SFTP server: %(error)s",
            error=str(cause) or type(cause).__name__,
        )}

    def _sftp_test_answer(self, b):
        """Run the SFTP connection test of *b* and shape the answer."""
        try:
            result = b._sftp_test()
        except (UserError, SftpUnreachable, asyncssh.Error, OSError,
                TimeoutError, asyncio.TimeoutError) as exc:
            return self._sftp_failure(b, exc)
        return {"ok": True, **result}

    @http.route(["/cloud/measure_backup_usage"], type="jsonrpc", auth="user")
    def cloud_measure_backup_usage(self, backend_id):
        """Run an on-demand usage measurement for *backend_id*.

        Manager-gated. Synchronous because the SPA gauge expects fresh
        numbers immediately after the user clicks "Refresh now". The
        daily cron handles the unattended path.
        """
        self._sec()._check_can_manage_settings()
        b = request.env["cloud.backup.backend"].browse(backend_id)
        if not b.exists():
            return {"ok": False, "error": _("Backend not found.")}
        if b.backend_type == "sftp":
            limited = self._sftp_rate_limited()
            if limited:
                return limited
            try:
                b.sudo()._measure_usage()
            except (UserError, SftpUnreachable, asyncssh.Error, OSError,
                    TimeoutError, asyncio.TimeoutError) as exc:
                return self._sftp_failure(b, exc)
            return {
                "ok": True,
                "last_measured_gb": b.last_measured_gb or 0.0,
                "last_measured_at": (
                    b.last_measured_at.isoformat() if b.last_measured_at else ""
                ),
            }
        if not b.s3_bucket or not b.s3_access_key_id:
            return {
                "ok": False,
                "error": _("Backend is missing bucket or credentials."),
            }
        try:
            b.sudo()._measure_usage()
        except (ClientError, ParamValidationError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return safe_error_response(exc, _("Usage measurement failed"))
        return {
            "ok": True,
            "last_measured_gb": b.last_measured_gb or 0.0,
            "last_measured_at": (
                b.last_measured_at.isoformat() if b.last_measured_at else ""
            ),
        }
