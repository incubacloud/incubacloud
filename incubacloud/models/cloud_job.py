import contextlib
import logging
import re
from datetime import timedelta

from odoo import models, fields, api, _
from odoo.addons.queue_job.delay import chain as delay_chain
from odoo.addons.queue_job.exception import JobError
from odoo.exceptions import UserError

from ._repo_requirements import detect_pip_conflicts, create_pip_conflict_alert
from .registry import executor_registry

_logger = logging.getLogger(__name__)


def _webhook_fields(payload):
    """Extract webhook trigger fields for job serialization."""
    p = payload or {}
    if p.get('trigger') != 'webhook':
        return {'trigger': ''}
    return {
        'trigger': 'webhook',
        'push_repo': p.get('push_repo', ''),
        'push_branch': p.get('push_branch', ''),
        'push_sha': p.get('push_sha', ''),
        'push_message': p.get('push_message', ''),
        'push_by': p.get('push_by', ''),
    }


class CloudJob(models.Model):
    _name = "cloud.job"
    _inherit = ["bus.listener.mixin"]
    _description = "Job para ejecución remota"

    name = fields.Char(
        string="Job Name"
    )
    host_id = fields.Many2one(
        "cloud.host",
        required=True,
        string="Cloud Host"
    )
    job_type_id = fields.Many2one(
        "cloud.job.type",
        required=True,
    )
    state = fields.Selection(
        related='queue_job_id.state',
        store=True,
    )
    message_ids = fields.One2many(
        "cloud.job.log.message", "job_id",
        string="Progress Messages"
    )
    log_chunk_ids = fields.One2many(
        "cloud.job.log.chunk", "job_id",
        string="Log Chunks"
    )
    result = fields.Serialized()
    payload = fields.Serialized(string="Job Payload")
    queue_job_uuid = fields.Char(
        string="Queue Job UUID",
        copy=False,
        index=True
    )
    queue_job_id = fields.Many2one(
        "queue.job",
        string="Queue Job",
        compute="_compute_queue_job_id",
        store=True,
    )
    instance_id = fields.Many2one(
        "cloud.instance",
        string="Instance",
        ondelete="set null",
    )
    blocked_alert_id = fields.Many2one(
        'cloud.alert',
        string='Blocking Alert',
        ondelete='set null',
        index=True,
        help='When set, this job is blocked waiting for the alert to be resolved.',
    )
    retry_of_id = fields.Many2one(
        'cloud.job',
        string='Retry of',
        ondelete='set null',
        index=True,
        help='When set, this job is a retry of the referenced failed job.',
    )

    @api.depends("queue_job_uuid")
    def _compute_queue_job_id(self):
        for job in self:
            if job.queue_job_uuid:
                queue_job = self.env["queue.job"].search([
                    ("uuid", "=", job.queue_job_uuid)
                ], limit=1)
                job.queue_job_id = queue_job or None
            else:
                job.queue_job_id = None

    @api.model
    def enqueue_chain(self, steps):
        """Enqueue a chain of jobs that run sequentially.

        Uses OCA queue_job's DelayableChain so each job waits for
        the previous one to finish before starting.

        steps: list of dicts {host_id, instance_id, job_type_code, payload}

        Payload values matching ``__chain_job_N__`` (0-indexed) are
        replaced with the cloud.job ID of step N, allowing later
        steps to reference earlier ones.

        Returns: list of cloud.job IDs (in execution order)
        """
        _REF_RE = re.compile(r'^__chain_job_(\d+)__$')

        # 1. Create all cloud.job records (without payloads that need resolving)
        records = []
        for step in steps:
            job_type = self.env['cloud.job.type'].search([
                ('code', '=', step['job_type_code']),
            ], limit=1)
            if not job_type:
                raise UserError(
                    _("Job type '{code}' not found.").format(
                        code=step['job_type_code'],
                    )
                )
            job_record = self.create({
                'host_id': step['host_id'],
                'job_type_id': job_type.id,
                'name': _(job_type.name),
                'instance_id': step.get('instance_id'),
                'payload': step.get('payload'),
            })
            records.append(job_record)

        # 2. Resolve __chain_job_N__ references in payloads
        job_ids = [r.id for r in records]
        for record in records:
            payload = record.payload
            if not payload:
                continue
            changed = False
            for key, val in payload.items():
                if isinstance(val, str):
                    m = _REF_RE.match(val)
                    if m:
                        idx = int(m.group(1))
                        payload[key] = job_ids[idx]
                        changed = True
            if changed:
                record.write({'payload': payload})

        # 2b. Create audit log entries for each step
        for record in records:
            self.env['cloud.audit.log'].sudo().create({
                'action': record.job_type_id.name,
                'instance_id': record.instance_id.id if record.instance_id else False,
                'host_id': record.host_id.id,
                'job_id': record.id,
            })

        # 3. Build delayable chain
        nodes = []
        for record in records:
            node = record.delayable(max_retries=1).execute()
            nodes.append(node)

        delay_chain(*nodes).delay()

        # 4. Store the generated queue_job UUIDs
        for node, record in zip(nodes, records):
            if node._generated_job:
                record.write({
                    'queue_job_uuid': node._generated_job.uuid,
                })

        return [r.id for r in records]

    @api.model
    def enqueue(self, host_id, instance_id, job_type_code, payload=None):
        """
        Queue a job of given type for the given host/instance
        and return the job record ID.
        """
        # Block if there is already an active *user* job for this instance.
        # Hidden system jobs (health checks, metrics, probes…) don't block.
        if instance_id:
            running = self.search([
                ('instance_id', '=', instance_id),
                ('state', 'in', self._active_states),
                ('job_type_id.code', 'not in', self._get_hidden_job_types()),
            ], limit=1)
            if running:
                raise UserError(_(
                    "A job is already running for this instance: %(name)s. "
                    "Wait for it to complete or cancel it first.",
                    name=running.name,
                ))
        job_type_id = self.env["cloud.job.type"].search([
            ("code", "=", job_type_code),
        ], limit=1)
        if not job_type_id:
            raise UserError(_("Job type with code {job_type_name} not found.").format(
                job_type_name=job_type_code
            ))
        vals = {
            "host_id": host_id,
            "job_type_id": job_type_id.id,
            "name": _(job_type_id.name),
        }
        if instance_id:
            vals["instance_id"] = instance_id
        if payload:
            vals["payload"] = payload
        job_record = self.create(vals)
        # Check pip_dependencies for conflict markers before queuing.
        _pip_blocked_types = ('deploy_instance', 'rebuild_instance')
        if instance_id and job_type_code in _pip_blocked_types:
            inst = self.env['cloud.instance'].browse(instance_id)
            conflicts = detect_pip_conflicts(inst.pip_dependencies)
            if conflicts:
                alert = create_pip_conflict_alert(
                    self.env, conflicts, instance_id=instance_id,
                )
                job_record.write({'blocked_alert_id': alert.id})
                return job_record.id
            # No conflicts — dismiss any stale active pip_conflict alerts
            self.env['cloud.alert'].search([
                ('instance_id', '=', instance_id),
                ('code', '=', 'pip_conflict'),
                ('state', '=', 'active'),
            ]).write({'state': 'dismissed'})
            # Block if there is an active addon_conflict alert
            addon_alert = self.env['cloud.alert'].search([
                ('instance_id', '=', instance_id),
                ('code', '=', 'addon_conflict'),
                ('state', '=', 'active'),
            ], limit=1)
            if addon_alert:
                job_record.write({'blocked_alert_id': addon_alert.id})
                return job_record.id
        self.env['cloud.audit.log'].sudo().create({
            'action': job_type_id.name,
            'instance_id': instance_id or False,
            'host_id': host_id,
            'job_id': job_record.id,
        })
        # max_retries=1: allow one automatic retry for transient DB errors,
        # then fail permanently. The execution guard prevents double SSH runs.
        delayed = job_record.with_delay(max_retries=1).execute()
        job_record.write({"queue_job_uuid": delayed.uuid})
        return job_record.id

    def execute(self):
        # queue_job will re-enqueue jobs that were in "started" state when Odoo
        # restarted; those will run from the beginning, which is the desired
        # behaviour (SSH operations are generally idempotent).
        #
        # JobError causes queue_job to mark the job as permanently failed,
        # regardless of max_retries.  We wrap everything so that any exception
        # from the executor (SSH failure, RuntimeError, etc.) results in a
        # clean permanent failure rather than a silent infinite-retry loop.
        #
        # IMPORTANT: after a long-running SSH job completes, the original
        # HTTP cursor (used by queue_job's _try_perform_job) may have gone
        # stale — causing flush_all/set_done to fail, which makes queue_job
        # treat the job as "dead" and re-enqueue it.  To prevent duplicate
        # runs, we verify the cursor is still usable before returning.
        executor = self._get_executor()
        ssh_executor = executor(
            job_record=self,
            host_record=self.host_id,
        )
        try:
            ssh_executor.pre_run_checks()
            ssh_executor._publish_bus()
            ssh_executor.run()
        except Exception as e:
            raise JobError(str(e)) from e
        finally:
            # After a long-running SSH job, the ORM environment used
            # by queue_job's _try_perform_job may have stale cache
            # entries (from on_success/on_failure writing via fresh
            # cursors).  Clear the env so flush_all() + set_done()
            # don't trip over phantom dirty records.
            with contextlib.suppress(Exception):
                self.env.cr.execute("SELECT 1")
                self.env.clear()

    def _get_executor(self):
        executor_cls = executor_registry.get(self.job_type_id.code)

        if not executor_cls:
            raise ValueError(f"No executor registered for '{self.job_type_id.code}'")

        return executor_cls

    @api.model
    def load_jobs(self, job_id=False):
        if job_id:
            job = self.browse(job_id)
            return job._format()
        return {
            "active": self._get_active_jobs(),
            "recent": self._get_recent_jobs(limit=15),
        }

    @api.model
    def get_instance_jobs(self, instance_id, limit=20, offset=0):
        """Return jobs for a specific instance (for the instance overview)."""
        hidden = self._get_hidden_job_types()
        domain = [
            ('instance_id', '=', instance_id),
            ('job_type_id.code', 'not in', hidden),
        ]
        total = self.search_count(domain)
        jobs = self.search(domain, order='id desc',
                           limit=limit, offset=offset)
        return {'jobs': jobs._format(), 'total': total}

    @api.model
    def get_host_jobs(self, host_id, limit=5, offset=0):
        """Return jobs for a specific host (for the host overview)."""
        hidden = self._get_hidden_job_types()
        domain = [
            ('host_id', '=', host_id),
            ('job_type_id.apply_to', '=', 'host'),
            ('job_type_id.code', 'not in', hidden),
        ]
        total = self.search_count(domain)
        jobs = self.search(domain, order='id desc',
                           limit=limit, offset=offset)
        return {'jobs': jobs._format(), 'total': total}

    _active_states = ["pending", "enqueued", "wait_dependencies", "started"]
    _terminal_states = ["done", "failed", "cancelled"]
    # Job types hidden from the drawer (automated background tasks).
    # These are still visible in the history page under the "Admin" category.
    # Override _get_hidden_job_types() in submodules to extend this list.
    _hidden_job_types = ["host_metrics", "docker_prune", "instance_health"]

    def _get_hidden_job_types(self):
        """Return job type codes that should not appear in the UI job drawer.

        Override in dependent modules and call super() to extend the list::

            def _get_hidden_job_types(self):
                return super()._get_hidden_job_types() + ['my_background_job']
        """
        return self._hidden_job_types.copy()

    def _get_active_jobs(self):
        domain = [
            ('job_type_id.code', 'not in', self._get_hidden_job_types()),
            '|',
            ('blocked_alert_id', '!=', False),
            ('state', 'in', self._active_states),
        ]
        return self.search(domain, order="id desc")._format()

    def _get_recent_jobs(self, limit=10):
        return self.search([
            ("state", "in", self._terminal_states),
            ("job_type_id.code", "not in", self._get_hidden_job_types()),
        ], order="id desc", limit=limit)._format()

    def _format(self):
        # Build a map of job_id → attachment_id for jobs that have a
        # downloadable file (e.g. export_instance).  Uses a single query
        # instead of per-job lookups.
        attachments = self.env['ir.attachment'].search([
            ('res_model', '=', 'cloud.job'),
            ('res_id', 'in', self.ids),
        ], order='id desc')
        att_map = {}
        for att in attachments:
            att_map.setdefault(att.res_id, att.id)

        # Build a set of job IDs that generated active alerts
        alert_job_ids = set(
            self.env['cloud.alert'].search([
                ('job_id', 'in', self.ids),
                ('state', '=', 'active'),
            ]).mapped('job_id.id')
        ) if self.ids else set()

        return [
            {
                "id": job.id,
                "name": job.name,
                "host": job.host_id.name,
                "job_type": job.job_type_id.name,
                "job_type_code": job.job_type_id.code,
                "instance_id": job.instance_id.id if job.instance_id else None,
                "state": 'blocked' if job.blocked_alert_id else job.state,
                "has_alert": job.id in alert_job_ids,
                "create_date": job.create_date,
                "last_system_message": job._get_last_system_message(),
                "download_url": (
                    f"/web/content/{att_map[job.id]}?download=true"
                    if job.id in att_map
                    else None
                ),
                "retry_of_id": job.retry_of_id.id if job.retry_of_id else None,
                "user_id": job.create_uid.id,
                "user_name": job.create_uid.name or "",
                **_webhook_fields(job.payload),
            }
            for job in self
        ]

    def _get_last_system_message(self):
        system_chunks = self.log_chunk_ids.filtered(lambda c: c.source == 'system')
        return system_chunks[-1].content if system_chunks else ''

    @api.model
    def load_chunks(self, job_id, after_id=0):
        """Return all log chunks (system, stdout, stderr) for the terminal view."""
        chunks = self.env['cloud.job.log.chunk'].search([
            ('job_id', '=', job_id),
            ('id', '>', after_id),
        ])
        job = self.browse(job_id)
        return {
            'chunks': chunks._format(),
            'state': job.state,
        }

    @api.model
    def load_history(self, filters=None):
        """Return jobs for the history page, optionally filtered.

        filters.job_category:
          - ``"operational"`` (default) — excludes admin background jobs
          - ``"admin"`` — only admin background jobs (host_metrics, docker_prune, …)
          - ``"all"`` — no category restriction
        """
        domain = []
        job_id = (filters or {}).get('job_id')
        if job_id:
            # Direct job lookup — bypass all other filters
            domain.append(('id', '=', int(job_id)))
        else:
            if filters:
                if filters.get('states'):
                    domain.append(('state', 'in', filters['states']))
                if filters.get('host_id'):
                    domain.append(('host_id', '=', int(filters['host_id'])))
                if filters.get('date_from'):
                    domain.append(('create_date', '>=', filters['date_from']))
                if filters.get('date_to'):
                    domain.append(('create_date', '<=', filters['date_to']))
                if filters.get('instance_id'):
                    domain.append(('instance_id', '=', int(filters['instance_id'])))
                if filters.get('user_id'):
                    domain.append(('create_uid', '=', int(filters['user_id'])))
                if filters.get('apply_to'):
                    domain.append(('job_type_id.apply_to', '=', filters['apply_to']))

            category = (filters or {}).get('job_category', 'operational')
            admin_types = self._get_hidden_job_types()
            if category == 'operational':
                domain.append(("job_type_id.code", "not in", admin_types))
            elif category == 'admin':
                domain.append(("job_type_id.code", "in", admin_types))
            # "all" → no category filter

        jobs = self.search(domain, order='id desc', limit=200)
        hosts = self.env['cloud.host'].search([], order='name asc')
        user_ids = jobs.mapped('create_uid').sorted('name')
        return {
            'jobs': jobs._format_history(),
            'hosts': [{'id': h.id, 'name': h.name} for h in hosts],
            'users': [{'id': u.id, 'name': u.name} for u in user_ids],
        }

    def _format_history(self):
        now = fields.Datetime.now()
        result = []
        for job in self:
            start = job.create_date
            # Use queue.job's date_done for accurate end time since
            # cloud.job.state is a related field that doesn't update
            # cloud.job.write_date. Note: queue.job has no write_date in Odoo 19.
            qj = job.queue_job_id
            end = (
                qj.date_done
                if qj and job.state in self._terminal_states
                else (job.write_date if job.state in self._terminal_states else now)
            )
            duration_s = int((end - start).total_seconds()) if start and end else 0
            result.append({
                'id': job.id,
                'name': job.name,
                'host': job.host_id.name,
                'host_id': job.host_id.id,
                'instance_name': (
                    f"{job.instance_id.project_id.name}/{job.instance_id.name}"
                    if job.instance_id else ''
                ),
                'instance_id': (
                    job.instance_id.id if job.instance_id else None
                ),
                'job_type': job.job_type_id.name,
                'state': job.state,
                'create_date': job.create_date,
                'write_date': job.write_date,
                'duration_s': max(0, duration_s),
                'log_lines': len(job.log_chunk_ids),
                'last_system_message': job._get_last_system_message(),
                'user_id': job.create_uid.id,
                'user_name': job.create_uid.name or "",
                **_webhook_fields(job.payload),
            })
        return result

    def cancel_job(self):
        if self.blocked_alert_id:
            raise UserError(_(
                "Blocked jobs cannot be cancelled. "
                "Resolve the pip dependency conflict first, then cancel if needed."
            ))
        if self.state not in self._active_states:
            raise UserError(_("Only active jobs can be cancelled."))
        self.write({'state': 'cancelled'})

    def retry_job(self):
        self.ensure_one()
        if self.blocked_alert_id:
            raise UserError(_(
                "Blocked jobs cannot be retried. "
                "Resolve the pip dependency conflict first."
            ))
        if self.state != "failed":
            raise UserError(_("Only failed jobs can be retried."))
        # Create a fresh job as a retry — keeps the original's logs intact.
        new_job = self.create({
            'name': self.name,
            'host_id': self.host_id.id,
            'job_type_id': self.job_type_id.id,
            'instance_id': self.instance_id.id if self.instance_id else False,
            'payload': self.payload,
            'retry_of_id': self.id,
        })
        delayed = new_job.with_delay(max_retries=1).execute()
        new_job.write({'queue_job_uuid': delayed.uuid})
        return new_job.id

    @api.model
    def cron_cleanup_backup_attachments(self):
        """Delete backup attachments older than 2 hours."""
        cutoff = fields.Datetime.now() - timedelta(hours=2)
        attachments = self.env['ir.attachment'].search([
            ('res_model', '=', 'cloud.job'),
            ('name', 'like', '%-backup-%'),
            ('create_date', '<', cutoff),
        ])
        if attachments:
            _logger.info(
                "Cleaning up %d expired backup attachment(s)",
                len(attachments),
            )
            attachments.unlink()

    def unblock_and_enqueue(self):
        """Called by the resolve endpoint after a pip conflict is resolved."""
        self.ensure_one()
        self.blocked_alert_id = False
        delayed = self.with_delay(max_retries=1).execute()
        self.write({'queue_job_uuid': delayed.uuid})

    # ── Multi-user notifications ───────────────────────────────────────────

    @api.model
    def _broadcast_job_update(self, job_id):
        """Send bus notification for a job state change to all active users."""
        users = self.env['res.users'].search([
            ('share', '=', False),
            ('active', '=', True),
        ])
        for user in users:
            user._bus_send('cloud_jobs', {'id': job_id})

    @api.model
    def _notify_by_email(self, job, state):
        """Email active internal users according to their notification prefs."""
        users = self.env['res.users'].search([
            ('share', '=', False),
            ('active', '=', True),
            ('cloud_notification_level', '!=', 'none'),
        ])
        for user in users:
            if (
                user.cloud_notification_level == 'failures'
                and state != 'failed'
            ):
                continue
            email = user.partner_id.email
            if not email:
                continue
            state_label = 'completed' if state == 'done' else state
            subject = (
                f"[IncubaCloud] Job '{job.name}' {state_label}"
            )
            body = self._build_email_body(job, state)
            self.env['mail.mail'].sudo().create({
                'subject': subject,
                'body_html': body,
                'email_to': email,
                'auto_delete': True,
            }).send()

    @api.model
    def _build_email_body(self, job, state):
        instance_line = (
            f"<tr><td><b>Instance</b></td>"
            f"<td>{job.instance_id.name}</td></tr>"
            if job.instance_id else ""
        )
        log_url = f"/cloud/log/{job.id}"
        state_label = 'completed' if state == 'done' else state
        return (
            f"<table>"
            f"<tr><td><b>Job</b></td><td>{job.name}</td></tr>"
            f"<tr><td><b>Host</b></td><td>{job.host_id.name}</td></tr>"
            f"{instance_line}"
            f"<tr><td><b>Status</b></td><td>{state_label}</td></tr>"
            f"</table>"
            f"<p><a href='{log_url}'>View logs</a></p>"
        )

    # ── Audit trail ───────────────────────────────────────────────────────

    @api.model
    def get_audit_log(
        self, instance_id=None, host_id=None, limit=100,
        q=None, action_filter=None, date_from=None, date_to=None,
    ):
        """Return audit log entries for a given instance or host."""
        if not self.env.user.has_group('incubacloud.group_cloud_manager'):
            return []
        domain = []
        if instance_id:
            domain.append(('instance_id', '=', int(instance_id)))
        elif host_id:
            domain.append(('host_id', '=', int(host_id)))
        else:
            return []
        if q:
            domain += ['|',
                ('user_id.name', 'ilike', q),
                ('action', 'ilike', q),
            ]
        if action_filter:
            domain.append(('action', '=', action_filter))
        if date_from:
            domain.append(('create_date', '>=', date_from))
        if date_to:
            domain.append(('create_date', '<=', date_to))
        return self.env['cloud.audit.log'].search(
            domain, order='id desc', limit=limit,
        )._format()
