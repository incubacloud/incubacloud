import contextlib
import logging
import re
from datetime import timedelta

from odoo import models, fields, api, _
from odoo.addons.queue_job.delay import chain as delay_chain
from odoo.addons.queue_job.exception import JobError, RetryableJobError
from odoo.exceptions import UserError

from ._repo_requirements import detect_pip_conflicts, create_pip_conflict_alert
from .registry import executor_registry
from .abstract_executor import (
    CONNECTION_RETRY_SECONDS,
    is_transient_connection_error,
)

_logger = logging.getLogger(__name__)


# Namespace used for PostgreSQL transactional advisory locks on
# ``cloud.job.enqueue``. The pair ``(namespace, instance_id)`` serialises
# concurrent enqueue() calls targeting the same instance so two racing
# requests cannot create parallel jobs. The lock is released on COMMIT
# or ROLLBACK automatically — no cleanup possible if the process dies.
_JOB_LOCK_NAMESPACE = 0x0C10D10B
# Separate namespace for host-scoped jobs (no instance_id). A host.id and an
# instance.id can collide numerically, so host locks must live in their own
# namespace to avoid falsely serialising unrelated host/instance work.
_HOST_JOB_LOCK_NAMESPACE = 0x0C10D10C


def _webhook_fields(payload):
    """Extract webhook trigger fields for job serialization.

    Recognises both ``webhook`` (direct push) and ``coalesced`` (a
    follow-up rebuild folding in pushes that arrived while a previous
    rebuild was running). For coalesced jobs ``coalesced_pushes`` lists
    every queued push so the UI can render the full set, not just the
    HEAD shown by the primary push_* fields.
    """
    p = payload or {}
    trigger = p.get('trigger')
    if trigger not in ('webhook', 'coalesced'):
        return {'trigger': ''}
    fields = {
        'trigger': trigger,
        'push_repo': p.get('push_repo', ''),
        'push_branch': p.get('push_branch', ''),
        'push_sha': p.get('push_sha', ''),
        'push_message': p.get('push_message', ''),
        'push_by': p.get('push_by', ''),
    }
    if trigger == 'coalesced':
        fields['coalesced_pushes'] = p.get('coalesced_pushes') or []
    return fields


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
    date_done = fields.Datetime(
        related='queue_job_id.date_done',
        store=True,
        # Stored so ``load_history`` does not need ``queue.job`` ACL —
        # non-Job-Queue-Manager users were tripping ``Access Error`` on
        # ``View all activity`` because reading ``queue_job_id.date_done``
        # required group ``queue_job.group_queue_job_manager``.
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

        # Advisory lock on every instance touched by the chain. Two
        # concurrent enqueue_chain()/enqueue() calls targeting the same
        # instance serialise here; once we hold the lock we verify there
        # is no active user job for that instance before creating new
        # ones. Locks are released on COMMIT/ROLLBACK automatically.
        seen_instance_ids = set()
        seen_host_ids = set()
        hidden = self._get_hidden_job_types()
        for step in steps:
            inst_id = step.get('instance_id')
            if inst_id and inst_id not in seen_instance_ids:
                self.env.cr.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (_JOB_LOCK_NAMESPACE, inst_id),
                )
                seen_instance_ids.add(inst_id)
                running = self.search([
                    ('instance_id', '=', inst_id),
                    ('state', 'in', self._active_states),
                    ('job_type_id.code', 'not in', hidden),
                ], limit=1)
                if running:
                    raise UserError(_(
                        "A job is already running for this instance: "
                        "%(name)s. Wait for it to complete or cancel "
                        "it first.",
                        name=running.name,
                    ))
            elif not inst_id:
                # Host-only step (provisioning, hardening, full setup…):
                # serialise per host so two chains can't run concurrently
                # on the same machine.
                host_id = step.get('host_id')
                if host_id and host_id not in seen_host_ids:
                    self.env.cr.execute(
                        "SELECT pg_advisory_xact_lock(%s, %s)",
                        (_HOST_JOB_LOCK_NAMESPACE, host_id),
                    )
                    seen_host_ids.add(host_id)
                    running = self.search([
                        ('host_id', '=', host_id),
                        ('instance_id', '=', False),
                        ('state', 'in', self._active_states),
                        ('job_type_id.code', 'not in', hidden),
                    ], limit=1)
                    if running:
                        raise UserError(_(
                            "A job is already running for this host: "
                            "%(name)s. Wait for it to complete or cancel "
                            "it first.",
                            name=running.name,
                        ))

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

    # ── Queue routing (tier → queue_job channel + priority) ──────────────
    #
    # Tiers:
    #   HIGH   → root.user / 5   (prod instance jobs)
    #   NORMAL → root.user / 10  (staging instance + host-only jobs)
    #   LOW    → root.bg   / 10  (background: health checks, metrics, prune)
    #
    # The tier is declared per cloud.job.type record (priority_tier field)
    # and can be auto-promoted NORMAL → HIGH when the target instance is
    # production. Requires the Odoo conf channels setting:
    #
    #   [queue_job]
    #   channels = root:3,root.user:2,root.bg:1
    #
    # See README for details.

    _TIER_TO_ROUTING = {
        'high':   ('root.user', 5),
        'normal': ('root.user', 10),
        'low':    ('root.bg',   10),
    }

    def _resolve_channel_priority(self, job_type, instance_id):
        """Return ``(channel, priority)`` for a job, promoting ``normal``
        → ``high`` when the target instance is in production."""
        tier = job_type.priority_tier or 'normal'
        if tier == 'normal' and instance_id:
            inst = self.env['cloud.instance'].browse(instance_id)
            if inst.exists() and inst.environment == 'production':
                tier = 'high'
        return self._TIER_TO_ROUTING.get(tier, self._TIER_TO_ROUTING['normal'])

    @api.model
    def enqueue(self, host_id, instance_id, job_type_code, payload=None,
                bypass_running_check=False):
        """
        Queue a job of given type for the given host/instance
        and return the job record ID.

        ``bypass_running_check`` skips the active-job guard. The guard
        exists to stop users triggering competing operations on the
        same instance (deploy + rebuild). It must be bypassed when an
        executor's ``on_success`` chains a follow-up internally — at
        that moment the parent job is still ``started`` and would
        otherwise block its own descendant. Internal use only;
        anything user-driven goes through the guard unchanged.
        """
        # Block if there is already an active *user* job for this target.
        # Hidden system jobs (health checks, metrics, probes…) don't block.
        # Instance-scoped jobs serialise per instance; host-scoped jobs
        # (instance_id falsy) serialise per host. The two scopes are
        # independent — an instance deploy does not block a host probe and
        # vice versa.
        if not bypass_running_check:
            if instance_id:
                # Advisory lock: serialise concurrent enqueue() calls for
                # the same instance. The second caller blocks until the
                # first commits; when it wakes up the running-job check
                # below sees the freshly-created job and raises. Released
                # automatically on COMMIT or ROLLBACK of this tx.
                self.env.cr.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (_JOB_LOCK_NAMESPACE, instance_id),
                )
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
            elif host_id:
                # Same serialisation, scoped to the host for host-only jobs
                # (probe, full setup, hardening, provisioning…). Separate
                # lock namespace so a host.id never collides with an
                # instance.id.
                self.env.cr.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (_HOST_JOB_LOCK_NAMESPACE, host_id),
                )
                running = self.search([
                    ('host_id', '=', host_id),
                    ('instance_id', '=', False),
                    ('state', 'in', self._active_states),
                    ('job_type_id.code', 'not in', self._get_hidden_job_types()),
                ], limit=1)
                if running:
                    raise UserError(_(
                        "A job is already running for this host: %(name)s. "
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
        # max_retries=1 by default: allow one automatic retry for transient
        # DB errors, then fail permanently. The execution guard prevents
        # double SSH runs. Executors that opt into connection retries
        # (``_retry_on_connection_loss``) get a higher cap so a momentarily
        # unreachable host is retried instead of failing on the first blip —
        # only transient connection errors consume those retries; any other
        # exception still fails permanently via JobError on the first try.
        executor_cls = executor_registry.get(job_type_id.code)
        max_retries = 1
        if executor_cls and getattr(
            executor_cls, '_retry_on_connection_loss', False,
        ):
            max_retries = executor_cls._connection_retry_attempts
        channel, priority = self._resolve_channel_priority(
            job_type_id, instance_id,
        )
        delayed = job_record.with_delay(
            max_retries=max_retries,
            channel=channel,
            priority=priority,
            description=job_type_id.name,
        ).execute()
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
        except RetryableJobError:
            # Executors can raise RetryableJobError to ask queue_job to
            # reschedule the job (e.g. the warm rebuild executor uses it
            # to honour a per-host advisory lock). Let it propagate so
            # queue_job applies its retry semantics; wrapping it as
            # JobError would mark the job permanently failed instead.
            raise
        except Exception as e:
            # Transient connection failures on opt-in executors (the host
            # monitoring probes) are retried instead of failing on the
            # first blip. queue_job stored the pre-increment retry count in
            # set_started, so inside execute() ``queue_job_id.retry`` is the
            # number of *prior* attempts → this attempt is ``retry + 1``.
            if (getattr(ssh_executor, '_retry_on_connection_loss', False)
                    and is_transient_connection_error(e)):
                qj = self.queue_job_id
                attempt = (qj.retry or 0) + 1
                max_retries = qj.max_retries or 0
                if not max_retries or attempt < max_retries:
                    # Not the last attempt — reschedule quietly. A single
                    # momentary blip must never notify anyone.
                    raise RetryableJobError(
                        f"Transient connection error on attempt {attempt}"
                        f"{f'/{max_retries}' if max_retries else ''}: {e}",
                        seconds=CONNECTION_RETRY_SECONDS,
                    ) from e
                # Last attempt still couldn't connect → the host is
                # genuinely unreachable. Notify, then fail permanently.
                ssh_executor.notify_host_unreachable(e, attempt)
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

    # Job types whose failure should raise a critical alert — these
    # are user-initiated, long-running operations whose failure the
    # operator almost always wants to investigate immediately. Anything
    # not listed here escalates to a ``warning`` alert instead. Extend
    # by overriding ``_get_severe_job_types()`` in child modules.
    # Generic, self-hosted-only job types that warrant a critical alert
    # on failure.
    _severe_job_types = frozenset({
        "deploy_instance",
        "rebuild_instance",
    })

    def _get_hidden_job_types(self):
        """Return job type codes that should not appear in the UI job drawer.

        Override in dependent modules and call super() to extend the list::

            def _get_hidden_job_types(self):
                return super()._get_hidden_job_types() + ['my_background_job']
        """
        return self._hidden_job_types.copy()

    @api.model
    def _get_severe_job_types(self):
        """Return job type codes whose failure should raise a critical
        alert. Override in child modules and call super() to extend.
        """
        return set(self._severe_job_types)

    @api.model
    def _create_job_failed_alert(self, cjob, exc_message=None):
        """Persist a ``cloud.alert`` when a job transitions to failed.

        Unlike the bus notification (which vanishes with the toast)
        the alert shows up in the Alerts panel so an operator who
        returns to the SPA later still sees the failure.

        Filters:
          * Hidden job types (``host_metrics``, ``instance_health``,
            ``docker_prune`` …) are skipped — they self-heal on the
            next tick and would flood the panel with noise.
          * Jobs without any host/instance/project target are skipped;
            the alert model requires at least one (constraint
            ``_check_target``).
        """
        if cjob.job_type_id.code in self._get_hidden_job_types():
            return None
        # ``cloud.job.host_id`` is required by the schema, so every
        # row has at least one target. Defensive check kept for child
        # modules that might add more exotic job rows.
        if not (cjob.host_id or cjob.instance_id):
            _logger.warning(
                "job_failed alert skipped: cjob id=%s has no host/"
                "instance target",
                cjob.id,
            )
            return None

        level = (
            'critical'
            if cjob.job_type_id.code in self._get_severe_job_types()
            else 'warning'
        )
        target_name = (
            cjob.instance_id.name
            or cjob.host_id.name
            or ''
        )
        # Include a snippet of the exception for at-a-glance triage.
        # Cap at 100 chars so the panel doesn't wrap into a wall of
        # text; the job log has the full traceback.
        msg = f"{cjob.name or 'Job'} on {target_name} failed"
        if exc_message:
            excerpt = exc_message.strip().split('\n', 1)[0][:100]
            if excerpt:
                msg = f"{msg}: {excerpt}"

        vals = {
            'code': 'job_failed',
            'level': level,
            'message': msg,
            'job_id': cjob.id,
        }
        if cjob.instance_id:
            vals['instance_id'] = cjob.instance_id.id
            if cjob.instance_id.project_id:
                vals['project_id'] = cjob.instance_id.project_id.id
        if cjob.host_id:
            vals['host_id'] = cjob.host_id.id

        return self.env['cloud.alert'].sudo().create(vals)

    @api.model
    def _dismiss_job_failed_alerts(self, cjob):
        """Mark active ``job_failed`` alerts for this cjob as dismissed.

        Called when the same cloud.job later transitions to ``done``
        (operator reintented and it worked). Keeps the Alerts panel
        tidy — nobody wants to see resolved failures stacked.
        """
        stale = self.env['cloud.alert'].sudo().search([
            ('code', '=', 'job_failed'),
            ('job_id', '=', cjob.id),
            ('state', '=', 'active'),
        ])
        if stale:
            stale.write({'state': 'dismissed'})
        return stale

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
                "host_id": job.host_id.id if job.host_id else None,
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
        """Return log chunks for the terminal view.

        Source-based and project-based scoping is enforced by record
        rules on ``cloud.job.log.chunk`` (``rule_job_log_chunk_member``
        filters stakeholders/consultants to ``source='system'`` and
        project membership). The search below honours those rules
        automatically.
        """
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
        instances = self.env['cloud.instance'].search(
            [], order='project_id, name asc',
        )
        user_ids = jobs.mapped('create_uid').sorted('name')
        return {
            'jobs': jobs._format_history(),
            'hosts': [{'id': h.id, 'name': h.name} for h in hosts],
            'instances': [
                {
                    'id': i.id,
                    'name': f"{i.project_id.name}/{i.name}" if i.project_id else i.name,
                }
                for i in instances
            ],
            'users': [{'id': u.id, 'name': u.name} for u in user_ids],
        }

    def _format_history(self):
        now = fields.Datetime.now()
        result = []
        for job in self:
            start = job.create_date
            # ``date_done`` is a related-stored mirror of
            # ``queue_job_id.date_done`` so this read does not require
            # ``queue.job`` ACL.  Fall back to ``write_date`` for
            # terminal jobs that were finalised without queue_job
            # writing date_done (cancel paths, legacy rows), and to
            # ``now`` for jobs still running.
            terminal = job.state in self._terminal_states
            end = (
                job.date_done or job.write_date if terminal else now
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
        channel, priority = self._resolve_channel_priority(
            new_job.job_type_id,
            new_job.instance_id.id if new_job.instance_id else False,
        )
        delayed = new_job.with_delay(
            max_retries=1,
            channel=channel,
            priority=priority,
            description=new_job.job_type_id.name,
        ).execute()
        new_job.write({'queue_job_uuid': delayed.uuid})
        return new_job.id

    @api.model
    def cron_cleanup_backup_attachments(self):
        """Delete expired backup artefacts (>2h old).

        Two passes:

        * **A — `cloud.instance.backup` rows with attachments**:
          rows older than the cutoff are unlinked together with
          their attachment.  Without this pass the row would survive
          (`attachment_id ondelete='set null'`) and the SPA would
          show a download button that no longer resolves.

        * **B — orphan job attachments**: attachments produced by
          one-shot download jobs (`BackupDownloadExecutor`,
          `BackupDownloadNeutralizedExecutor`) live on `cloud.job`
          and never got linked to a `cloud.instance.backup` row, so
          pass A never sees them.  Match them by name pattern.

        Both passes are bounded to `res_model='cloud.job'` for
        attachments — no risk of touching unrelated attachments.
        """
        cutoff = fields.Datetime.now() - timedelta(hours=2)

        # Pass A — rows + their attachments.
        Backup = self.env['cloud.instance.backup'].sudo()
        expired_rows = Backup.search([
            ('attachment_id', '!=', False),
            ('backup_time', '<', cutoff),
        ])
        if expired_rows:
            attachments = expired_rows.mapped('attachment_id')
            _logger.info(
                "Cleaning up %d expired backup row(s) + attachment(s)",
                len(expired_rows),
            )
            expired_rows.unlink()
            # Some attachments may already be gone (manual cleanup);
            # ``exists`` filters those out without raising.
            attachments.exists().unlink()

        # Pass B — orphan attachments from one-shot download jobs.
        orphan = self.env['ir.attachment'].search([
            ('res_model', '=', 'cloud.job'),
            ('create_date', '<', cutoff),
            '|',
            ('name', 'like', '%-backup-%'),
            ('name', 'like', '%-neutralized-%'),
        ])
        # ``cloud.instance.backup.attachment_id`` is the only legit
        # consumer of ``-backup-`` named attachments on cloud.job;
        # exclude any that are still referenced so pass B never
        # races pass A on an in-flight backup.
        if orphan:
            referenced = Backup.search([
                ('attachment_id', 'in', orphan.ids),
            ]).mapped('attachment_id.id')
            orphan = orphan.filtered(lambda a: a.id not in referenced)
        if orphan:
            _logger.info(
                "Cleaning up %d orphan backup/neutralized attachment(s)",
                len(orphan),
            )
            orphan.unlink()

    def unblock_and_enqueue(self):
        """Called by the resolve endpoint after a pip conflict is resolved."""
        self.ensure_one()
        self.blocked_alert_id = False
        channel, priority = self._resolve_channel_priority(
            self.job_type_id,
            self.instance_id.id if self.instance_id else False,
        )
        delayed = self.with_delay(
            max_retries=1,
            channel=channel,
            priority=priority,
            description=self.job_type_id.name,
        ).execute()
        self.write({'queue_job_uuid': delayed.uuid})

    # ── Multi-user notifications ───────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        """Broadcast newly-created jobs so the UI updates immediately.

        Runs on the caller's cursor (HTTP / cron / orm.call) — the
        ``pg_notify`` emitted by ``_bus_send`` fires at that cursor's
        commit. We deliberately do NOT hook ``write`` / ``_write``: the
        ``state`` field is a stored related on ``queue_job_id.state`` and
        its updates arrive via ``_write`` from whatever cursor queue_job
        is using (sometimes a fresh cursor in failure paths, sometimes
        the SSH executor's cursor), which is exactly the scenario the
        memory note warns about. State transitions are already
        broadcast explicitly from ``queue_job_ext.write`` (terminal) and
        ``abstract_executor._publish_bus`` (while running).
        """
        records = super().create(vals_list)
        for record in records:
            self._broadcast_job_update(record.id)
        return records

    @api.model
    def _broadcast_job_update(self, job_id):
        """Send bus notification for a job state change to all active users.

        Hidden job types (host_metrics, instance_health, docker_prune,
        …) are system/cron jobs the SPA never shows in the drawer.
        Broadcasting them is pure noise — tens of thousands of bus
        events per day × N internal users for zero UI benefit — so we
        skip them here. The frontend already filters these types out
        of the job list; skipping the bus hop saves a lot of work on
        the server, network and client.
        """
        job = self.browse(job_id)
        if not job.exists():
            return
        if job.job_type_id.code in self._get_hidden_job_types():
            return
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
