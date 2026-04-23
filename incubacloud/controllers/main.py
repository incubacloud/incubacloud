# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from odoo import http, _
from odoo.addons.bus.websocket import WebsocketConnectionHandler
from odoo.fields import Domain
from odoo.http import request
from odoo.tools import format_amount, file_open
from odoo.http import Controller
from datetime import timedelta, datetime

_logger = logging.getLogger(__name__)


class CloudController(Controller):

    @http.route(["/cloud", "/cloud/<path:subpath>"], auth="user", type='http', methods=['GET'])
    def cloud_ui(self, subpath="", **k):
        """
        Render the main cloud management UI
        """
        is_internal_user = request.env.user._is_internal()
        if not is_internal_user:
            return request.not_found()

        company = request.env.user.company_id
        session_info = request.env['ir.http'].session_info()
        session_info['user_context']['allowed_company_ids'] = company.ids
        session_info['user_companies'] = {'current_company': company.id, 'allowed_companies': {company.id: session_info['user_companies']['allowed_companies'][company.id]}}
        context = {
            'from_backend': 1,
            'session_info': session_info,
        }
        response = request.render('incubacloud.index', context)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @http.route(['/cloud/log/<int:job_id>'], auth="user", type='http')
    def cloud_log(self, job_id, **k):
        """Render the terminal log page for a job.

        Any user who can see the job (via record rules) can view its logs.
        No extra role check needed — job visibility is the gate.
        """
        if not request.env.user._is_internal():
            return request.not_found()
        job = request.env['cloud.job'].browse(job_id)
        if not job.exists():
            return request.not_found()
        # Odoo rejects WebSocket handshakes whose ``version`` query
        # parameter doesn't match the server's current
        # ``WebsocketConnectionHandler._VERSION`` — it closes with
        # CLEAN / OUTDATED_VERSION, which our inline client reads as a
        # successful close and retries forever. Render the server's
        # current version into the template so the terminal page tracks
        # Odoo upgrades without a code change.
        context = {
            'job_id': job_id,
            'job_name': job.name or '',
            'job_host': job.host_id.name or '',
            'job_instance': job.instance_id.name or '',
            'job_project': job.instance_id.project_id.name or '',
            'csrf_token': request.csrf_token(None),
            'session_info': request.env['ir.http'].session_info(),
            'ws_version': WebsocketConnectionHandler._VERSION,
        }
        response = request.render('incubacloud.log_terminal', context)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @http.route(['/cloud/log/<int:job_id>/download'], auth='user', type='http')
    def cloud_log_download(self, job_id, **k):
        """Download job logs as a plain-text .log file."""
        if not request.env.user._is_internal():
            return request.not_found()
        job = request.env['cloud.job'].browse(job_id)
        if not job.exists():
            return request.not_found()
        chunks = request.env['cloud.job.log.chunk'].search([
            ('job_id', '=', job_id),
        ], order='id')
        source_tag = {'stdout': 'out', 'stderr': 'err', 'system': 'sys'}
        lines = []
        for c in chunks:
            ts = c.create_date.strftime('%Y-%m-%d %H:%M:%SZ') if c.create_date else ''
            tag = source_tag.get(c.source, c.source)
            for line in (c.content or '').splitlines():
                lines.append(f"{ts} [{tag}] {line}")
        content = '\n'.join(lines) + '\n'
        slug = (job.name or 'job').replace(' ', '-').lower()
        filename = f"job-{job_id}-{slug}.log"
        return request.make_response(
            content,
            headers=[
                ('Content-Type', 'text/plain; charset=utf-8'),
                ('Content-Disposition', f'attachment; filename="{filename}"'),
            ],
        )

    @http.route(['/cloud/instance/<int:instance_id>/logs'], auth='user', type='http')
    def cloud_instance_logs(self, instance_id, **k):
        """Render the standalone container log viewer for an instance."""
        if not request.env.user._is_internal():
            return request.not_found()
        request.env['cloud.security.mixin']._check_can_view_logs()
        inst = request.env['cloud.instance'].browse(instance_id)
        if not inst.exists():
            return request.not_found()
        services = [s.strip() for s in (inst.compose_services or 'odoo,db').split(',') if s.strip()]
        context = {
            'instance_id':   instance_id,
            'instance_name': inst.name or '',
            'instance_host': inst.host_id.name or '',
            'services':      services,
            'csrf_token':    request.csrf_token(None),
            'session_info':  request.env['ir.http'].session_info(),
        }
        response = request.render('incubacloud.cloud_instance_logs', context)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @http.route(
        ['/cloud/instance/<int:instance_id>/restore'],
        methods=['POST'], auth='user', type='http',
        max_content_length=2 * 1024 * 1024 * 1024,  # 2 GiB
    )
    def restore_instance_upload(self, instance_id, **k):
        """Receive a backup zip, save to a temp file, and enqueue the restore job."""
        request.env['cloud.security.mixin']._check_can_manage_backups()
        if not request.env.user._is_internal():
            return request.make_json_response(
                {'error': 'Forbidden'}, status=403
            )
        inst = request.env['cloud.instance'].browse(instance_id)
        if not inst.exists() or not inst.host_id:
            return request.make_json_response(
                {'error': 'Instance not found'}, status=404
            )

        backup_file = request.httprequest.files.get('backup_file')
        if not backup_file:
            return request.make_json_response(
                {'error': 'No file provided'}, status=400
            )

        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix='.zip',
            prefix=f'cloud_restore_{instance_id}_',
        )
        try:
            with os.fdopen(tmp_fd, 'wb') as tmp_fh:
                backup_file.save(tmp_fh)
            payload = {
                'mode': 'browser',
                'local_path': tmp_path,
                'filename': backup_file.filename or 'restore.zip',
            }
            job_id = inst.restore_db(payload)
            return request.make_json_response({'job_id': job_id})
        except Exception:
            with suppress(Exception):
                Path(tmp_path).unlink()
            _logger.exception("restore_instance_upload failed")
            return request.make_json_response(
                {'error': 'An internal error occurred. Check server logs.'}, status=500
            )

    @http.route(['/cloud/ping'], type='jsonrpc', auth='user')
    def cloud_ping(self):
        return {'response': 'pong'}

    @http.route(
        '/cloud/health', type='http',
        auth='public', methods=['GET'], csrf=False,
    )
    def cloud_health(self, **kw):
        """Public liveness/readiness probe.

        Docker healthcheck, Kubernetes probes and load balancers can
        GET this without authentication to decide whether this worker
        is fit to serve. Checks are deliberately cheap (SELECT 1 + one
        env.ref) so the probe itself doesn't become a load source.
        Returns HTTP 200 when every check passes, 503 otherwise, so
        orchestrators can treat it as a standard health endpoint.
        """
        checks = {}
        overall = 'ok'
        status = 200

        try:
            request.env.cr.execute("SELECT 1")
            checks['db'] = 'ok'
        except Exception as exc:
            _logger.warning("health: db check failed: %s", exc)
            checks['db'] = 'down'
            overall = 'down'
            status = 503

        try:
            ref = request.env.ref(
                'incubacloud.group_cloud_manager',
                raise_if_not_found=False,
            )
            checks['registry'] = 'ok' if ref else 'down'
            if not ref:
                overall = 'down'
                status = 503
        except Exception as exc:
            _logger.warning("health: registry check failed: %s", exc)
            checks['registry'] = 'down'
            overall = 'down'
            status = 503

        return request.make_response(
            json.dumps({'status': overall, 'checks': checks}),
            headers=[
                ('Content-Type', 'application/json'),
                ('Cache-Control', 'no-store'),
            ],
            status=status,
        )
