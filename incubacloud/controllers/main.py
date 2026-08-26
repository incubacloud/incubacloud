# Part of Odoo. See LICENSE file for full copyright and licensing details.

import json
import logging
import os
import re
from contextlib import suppress
from pathlib import Path

import asyncssh

from odoo import http
from odoo.http import Controller, request

from odoo.addons.bus.websocket import WebsocketConnectionHandler

from ..restore_staging import new_upload_path
from ._data_load._helpers import (
    is_safe_log_archive,
    log_archive_download_command,
)
from ._rate_limit import Rule, first_tripped
from .async_utils import run_async

_logger = logging.getLogger(__name__)


class CloudController(Controller):
    @http.route(
        ["/cloud", "/cloud/<path:subpath>"], auth="user", type="http", methods=["GET"]
    )
    def cloud_ui(self, subpath="", **k):
        """
        Render the main cloud management UI
        """
        is_internal_user = request.env.user._is_internal()
        if not is_internal_user:
            return request.not_found()

        company = request.env.user.company_id
        session_info = request.env["ir.http"].session_info()
        session_info["user_context"]["allowed_company_ids"] = company.ids
        session_info["user_companies"] = {
            "current_company": company.id,
            "allowed_companies": {
                company.id: session_info["user_companies"]["allowed_companies"][
                    company.id
                ]
            },
        }
        context = {
            "from_backend": 1,
            "session_info": session_info,
        }
        response = request.render("incubacloud.index", context)
        response.headers["Cache-Control"] = "no-store"
        return response

    @http.route(["/cloud/log/<int:job_id>"], auth="user", type="http")
    def cloud_log(self, job_id, **k):
        """Render the terminal log page for a job.

        Any user who can see the job (via record rules) can view its logs.
        No extra role check needed — job visibility is the gate.
        """
        if not request.env.user._is_internal():
            return request.not_found()
        job = request.env["cloud.job"].browse(job_id)
        if not job.exists():
            return request.not_found()
        # Odoo rejects WebSocket handshakes whose ``version`` query
        # parameter doesn't match the server's current
        # ``WebsocketConnectionHandler._VERSION`` — it closes with
        # CLEAN / OUTDATED_VERSION, which our inline client reads as a
        # successful close and retries forever. Render the server's
        # current version into the template so the terminal page tracks
        # Odoo upgrades without a code change.
        payload = job.payload or {}
        trigger = payload.get("trigger") or ""
        context = {
            "job_id": job_id,
            "job_name": job.name or "",
            "job_host": job.host_id.name or "",
            "job_instance": job.instance_id.name or "",
            "job_project": job.instance_id.project_id.name or "",
            "job_trigger": trigger if trigger in ("webhook", "coalesced") else "",
            "job_push_sha": payload.get("push_sha") or "",
            "job_push_branch": payload.get("push_branch") or "",
            "job_push_message": payload.get("push_message") or "",
            "job_push_by": payload.get("push_by") or "",
            "job_coalesced_pushes": payload.get("coalesced_pushes") or [],
            "csrf_token": request.csrf_token(None),
            "session_info": request.env["ir.http"].session_info(),
            "ws_version": WebsocketConnectionHandler._VERSION,
        }
        response = request.render("incubacloud.log_terminal", context)
        response.headers["Cache-Control"] = "no-store"
        return response

    @http.route(["/cloud/log/<int:job_id>/download"], auth="user", type="http")
    def cloud_log_download(self, job_id, **k):
        """Download job logs as a plain-text .log file.

        Source-based and project-based scoping is enforced by record
        rules on ``cloud.job.log.chunk``. Stakeholders/consultants
        get only ``source='system'`` chunks of jobs in their projects;
        developers+ get the full stdout/stderr where credentials may
        appear.
        """
        if not request.env.user._is_internal():
            return request.not_found()
        job = request.env["cloud.job"].browse(job_id)
        if not job.exists():
            return request.not_found()
        chunks = request.env["cloud.job.log.chunk"].search(
            [
                ("job_id", "=", job_id),
            ],
            order="id",
        )
        source_tag = {"stdout": "out", "stderr": "err", "system": "sys"}
        lines = []
        for c in chunks:
            ts = c.create_date.strftime("%Y-%m-%d %H:%M:%SZ") if c.create_date else ""
            tag = source_tag.get(c.source, c.source)
            for line in (c.content or "").splitlines():
                lines.append(f"{ts} [{tag}] {line}")
        content = "\n".join(lines) + "\n"
        slug = (job.name or "job").replace(" ", "-").lower()
        filename = f"job-{job_id}-{slug}.log"
        return request.make_response(
            content,
            headers=[
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Disposition", f'attachment; filename="{filename}"'),
            ],
        )

    @http.route(["/cloud/instance/<int:instance_id>/logs"], auth="user", type="http")
    def cloud_instance_logs(self, instance_id, **k):
        """Render the standalone container log viewer for an instance."""
        if not request.env.user._is_internal():
            return request.not_found()
        request.env["cloud.security.mixin"]._check_can_view_logs()
        inst = request.env["cloud.instance"].browse(instance_id)
        if not inst.exists():
            return request.not_found()
        services = [
            s.strip()
            for s in (inst.compose_services or "odoo,db").split(",")
            if s.strip()
        ]
        context = {
            "instance_id": instance_id,
            "instance_name": inst.name or "",
            "instance_host": inst.host_id.name or "",
            "services": services,
            "csrf_token": request.csrf_token(None),
            "session_info": request.env["ir.http"].session_info(),
        }
        # One row per opened viewer — not per poll: the live tail
        # refreshes every 4 seconds, and a row for each would bury the
        # question this answers ("who read this instance's logs").
        request.env["cloud.audit.log"].sudo().create({
            "action": "Viewed instance logs",
            "instance_id": inst.id,
        })
        response = request.render("incubacloud.cloud_instance_logs", context)
        response.headers["Cache-Control"] = "no-store"
        return response

    @http.route(
        ["/cloud/instance/<int:instance_id>/log_archive/<string:name>"],
        auth="user",
        type="http",
    )
    def cloud_instance_log_archive_download(self, instance_id, name, **k):
        """Download one of an instance's archived Odoo logs, gzipped.

        The viewer shows a bounded tail; this is how an operator takes
        a whole day away to grep, diff or attach to a ticket.
        """
        if not request.env.user._is_internal():
            return request.not_found()
        request.env["cloud.security.mixin"]._check_can_view_logs()
        inst = request.env["cloud.instance"].browse(instance_id)
        if not inst.exists() or not inst.host_id:
            return request.not_found()
        if not is_safe_log_archive(name):
            return request.not_found()
        if first_tripped(Rule(
            f"log_download_user:{request.env.user.id}",
            cap_key="rate_limit_log_search_per_min",
            log_tag=f"log_download user={request.env.user.id}",
        )):
            return request.make_json_response(
                {"error": "Rate limit exceeded. Wait 60s and retry."},
                status=429,
            )
        settings = request.env["cloud.settings"].sudo()._get_system()
        command = log_archive_download_command(
            inst.get_remote_dir(), name,
            max_bytes=settings.log_download_max_mb * 1024 * 1024,
        )

        async def _run():
            async with asyncssh.connect(
                **inst.host_id.ssh_connect_kwargs(),
            ) as conn:
                result = await conn.run(command, check=False, encoding=None)
                return result.stdout or b""

        try:
            payload = run_async(_run())
        except Exception:
            _logger.exception(
                "Error downloading log archive %s of instance %s",
                name, instance_id,
            )
            return request.not_found()
        # Audited after the fetch, not before: a download takes a
        # customer's log out of the platform and that is the act worth
        # accounting for, while a failed attempt ends in ``not_found``
        # — which rolls the request back and would have taken the row
        # with it anyway.
        request.env["cloud.audit.log"].sudo().create({
            "action": "Downloaded instance log",
            "instance_id": inst.id,
            "details": name,
        })
        filename = f"{inst.name}-{name}"
        if not filename.endswith(".gz"):
            filename += ".gz"
        return request.make_response(
            payload,
            headers=[
                ("Content-Type", "application/gzip"),
                ("Content-Disposition", f'attachment; filename="{filename}"'),
                ("Cache-Control", "no-store"),
            ],
        )

    @http.route(
        ["/cloud/instance/<int:instance_id>/restore"],
        methods=["POST"],
        auth="user",
        type="http",
        max_content_length=2 * 1024 * 1024 * 1024,  # 2 GiB
    )
    def restore_instance_upload(self, instance_id, **k):
        """Receive a backup zip, save to a temp file, and enqueue the restore job."""
        request.env["cloud.security.mixin"]._check_can_manage_backups()
        if not request.env.user._is_internal():
            return request.make_json_response({"error": "Forbidden"}, status=403)
        # Per-user concurrency limit: each upload pins up to 2 GiB of
        # /tmp on the Odoo server until the executor consumes the file.
        # 2/min/user lets a typical operator retry a failed upload
        # without trouble, while preventing a developer from pinning
        # the server's tmpfs by spamming concurrent uploads.
        if first_tripped(Rule(
            f"restore_upload_user:{request.env.user.id}",
            max_per_window=2,
            log_tag=f"restore_upload user={request.env.user.id}",
        )):
            return request.make_json_response(
                {"error": "Rate limit exceeded. Wait 60s and retry."},
                status=429,
            )
        inst = request.env["cloud.instance"].browse(instance_id)
        if not inst.exists() or not inst.host_id:
            return request.make_json_response(
                {"error": "Instance not found"}, status=404
            )

        backup_file = request.httprequest.files.get("backup_file")
        if not backup_file:
            return request.make_json_response({"error": "No file provided"}, status=400)

        # Staged under the data dir, not /tmp: the executor that consumes
        # this file runs in the job-runner container, which shares the
        # data dir with this one and nothing else. See restore_staging.
        tmp_path = str(new_upload_path(instance_id))
        try:
            # ``save`` streams through a 16 KiB buffer, so a 2 GiB archive
            # never lands in the worker's memory — which matters, since
            # limit_memory_hard is that same 2 GiB.
            backup_file.save(tmp_path)
            os.chmod(tmp_path, 0o600)
            # Sanitise filename: it travels in the job payload as
            # metadata and may surface in logs / UI / future shell
            # interpolation. Restrict to a conservative whitelist
            # before it leaves this controller.
            raw_name = backup_file.filename or "restore.zip"
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name)[:255]
            payload = {
                "mode": "browser",
                "local_path": tmp_path,
                "filename": safe_name or "restore.zip",
                "backup_before_restore": (k.get("backup_before_restore") == "true"),
            }
            job_id = inst.restore_db(payload)
            return request.make_json_response({"job_id": job_id})
        except Exception:
            with suppress(Exception):
                Path(tmp_path).unlink()
            _logger.exception("restore_instance_upload failed")
            return request.make_json_response(
                {"error": "An internal error occurred. Check server logs."}, status=500
            )

    @http.route(["/cloud/ping"], type="jsonrpc", auth="user")
    def cloud_ping(self):
        return {"response": "pong"}

    @http.route(
        "/cloud/health",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def cloud_health(self, **kw):
        """Public liveness probe.

        Docker healthcheck, Kubernetes probes and load balancers can
        GET this without authentication to decide whether this worker
        is fit to serve. Returns HTTP 200 when the DB is reachable,
        503 otherwise. Body is intentionally minimal: any extra detail
        (module presence, version, etc.) would help an unauthenticated
        attacker fingerprint this stack.
        """
        # Per-IP rate limit. 60/min comfortably covers Docker (1/30s),
        # Kubernetes (every 10s) and Traefik (5s) healthchecks while
        # bounding the cost of a public-endpoint flood.
        ip = request.httprequest.remote_addr or "unknown"
        if first_tripped(Rule(
            f"health_ip:{ip}",
            max_per_window=60,
            log_tag=f"health ip={ip}",
        )):
            return request.make_response(
                json.dumps({"status": "rate_limited"}),
                headers=[
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-store"),
                ],
                status=429,
            )

        try:
            request.env.cr.execute("SELECT 1")
            return request.make_response(
                json.dumps({"status": "ok"}),
                headers=[
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-store"),
                ],
                status=200,
            )
        except Exception as exc:
            _logger.warning("health: db check failed: %s", exc)
            return request.make_response(
                json.dumps({"status": "down"}),
                headers=[
                    ("Content-Type", "application/json"),
                    ("Cache-Control", "no-store"),
                ],
                status=503,
            )
