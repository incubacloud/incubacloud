import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import time
import urllib.request
import uuid
from datetime import timedelta

from markupsafe import escape
from psycopg2 import OperationalError

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.service.model import PG_CONCURRENCY_ERRORS_TO_RETRY
from odoo.tools import clean_context
from odoo.tools import config as odoo_config

from odoo.addons.queue_job.delay import chain as delay_chain
from odoo.addons.queue_job.exception import JobError, RetryableJobError

from ..github.http_utils import safe_urlopen
from ..mail_layout import wrap_in_mail_layout
from ..net.outbound import post_json
from ._concurrency import read_committed_cursor, run_after_commit
from ._repo_requirements import create_pip_conflict_alert, detect_pip_conflicts
from .encrypted_char import EncryptedChar
from .abstract_executor import (
    ALREADY_DONE,
    BLOCKED_BY_ALERT,
    CANCELLED_WHILE_RUNNING,
    CONNECTION_RETRY_MARKER,
    CONNECTION_RETRY_SECONDS,
    JobCancelled,
    JobRefused,
    JobWorkerLost,
    _is_refusal,
    is_transient_connection_error,
)
from .cloud_job_no_return import NO_RETURN_LOCK_NAMESPACE
from .cloud_security_mixin import defaults_as_sent
from .registry import executor_registry
from .res_users_ext import as_platform, telegram_markdown

_logger = logging.getLogger(__name__)


# Namespace used for PostgreSQL transactional advisory locks on an
# instance's job queue. The pair ``(namespace, instance_id)`` serialises
# the callers that enqueue, dispatch or cancel on the same instance, so
# two racing requests cannot both find it idle and start parallel jobs.
# The lock is released on COMMIT or ROLLBACK automatically — no cleanup
# possible if the process dies.
_JOB_LOCK_NAMESPACE = 0x0C10D10B
# Separate namespace for host-scoped jobs (no instance_id). A host.id and an
# instance.id can collide numerically, so host locks must live in their own
# namespace to avoid falsely serialising unrelated host/instance work.
_HOST_JOB_LOCK_NAMESPACE = 0x0C10D10C

#: How many entries may wait in one queue (an instance's, or a host's for
#: host-level jobs). A chain is one entry. Follow-ups an executor queues
#: from its own run, retries and pushes merged into a waiting rebuild
#: neither count nor are refused.
QUEUE_MAX_WAITING = 5

#: How long a job whose type has no executor in the process that picked it
#: up is put back in the queue, counted from its dispatch, and how often it
#: is tried again meanwhile (``cloud.job._executor_or_postpone``). A
#: rebuild of a panel restarts its server a few seconds after the update
#: that queued the job (6 s on 7-nov-2026); ten minutes covers a slow
#: restart and still ends, with an alert, a job no server of this database
#: will ever run.
NO_EXECUTOR_WAIT_SECONDS = 600
NO_EXECUTOR_RETRY_SECONDS = 30

#: What the dispatcher and the executor read to decide what a job runs,
#: where, when, with what and with which rights, and when it ended (the
#: health probe opens its error window after ``date_done``). Only code
#: with superuser rights writes them (``_check_queue_control_vals``): the
#: queue's own entrypoints, after their checks. The name, the result and
#: the log only describe the job.
_QUEUE_CONTROL_FIELDS = frozenset({
    "state", "queue_head", "queue_chain_key", "retry_of_id",
    "blocked_alert_id", "queue_job_uuid", "queue_job_id", "job_type_id",
    "payload", "secret_payload", "instance_id", "host_id",
    "asked_as_superuser", "date_done", "succeeded_before_cancel",
    "worker_lost_at",
})

_CHAIN_REF_RE = re.compile(r"^__chain_job_(\d+)__$")


def _scope_label(scope):
    """Name a queue for a log line: ``instance N`` or ``host N``."""
    return f"instance {scope[0]}" if scope[0] else f"host {scope[1]}"


def _dispatch_scope_fresh(registry, scope):
    """Serve one queue on a cursor of its own, READ COMMITTED; never raises.

    The dispatcher that every event registers for after its commit
    (``cloud.job._schedule_dispatch``) and that the sweep runs per queue.
    Odoo's cursors are REPEATABLE READ: the runner that ends a job reads
    the snapshot it took when the job started, and sees nothing queued
    during the run — a restart pressed while a rebuild ran. A fresh
    cursor sees what every transaction committed. Whatever goes wrong
    here is logged and stays here: the event that led to it (a job's
    end, a cancellation, an alert closed) has already been recorded, and
    the sweep comes by every two minutes.

    It runs as the platform (``as_platform``): what it writes itself — a
    waiting job cancelled because its instance is gone, with its note —
    is the platform's doing. The jobs it starts run as their creators
    (``cloud.job._dispatch_single``).

    :param registry: the Odoo registry to open the cursor on
    :param scope: ``(instance_id, host_id)`` as ``_scope_of_step`` gives it
    :return: whether an entry was dispatched
    :rtype: bool
    """
    try:
        with read_committed_cursor(registry) as cr:
            Job = as_platform(api.Environment(cr, SUPERUSER_ID, {})["cloud.job"])
            return bool(Job._dispatch_next(*scope))
    except Exception:  # the event this follows is recorded already
        _logger.exception(
            "cloud.job: could not serve the queue of %s", _scope_label(scope),
        )
        return False


def _webhook_fields(payload):
    """Extract webhook trigger fields for job serialization.

    Recognises both ``webhook`` (direct push) and ``coalesced`` (a
    follow-up rebuild folding in pushes that arrived while a previous
    rebuild was running). For coalesced jobs ``coalesced_pushes`` lists
    every queued push so the UI can render the full set, not just the
    HEAD shown by the primary push_* fields.
    """
    p = payload or {}
    trigger = p.get("trigger")
    if trigger not in ("webhook", "coalesced"):
        return {"trigger": ""}
    fields = {
        "trigger": trigger,
        "push_repo": p.get("push_repo", ""),
        "push_branch": p.get("push_branch", ""),
        "push_sha": p.get("push_sha", ""),
        "push_message": p.get("push_message", ""),
        "push_by": p.get("push_by", ""),
    }
    if trigger == "coalesced":
        fields["coalesced_pushes"] = p.get("coalesced_pushes") or []
    return fields


class CloudJob(models.Model):
    _name = "cloud.job"
    _inherit = ["bus.listener.mixin"]
    _description = "Job executed remotely on a host"

    name = fields.Char(string="Job Name")
    # Both indexed: every look at a queue filters on one of them.
    host_id = fields.Many2one(
        "cloud.host", required=True, string="Cloud Host", index=True,
    )
    # Indexed: the health probe of every instance filters on it twice a
    # cycle (the jobs that block it, and the last one that cut its error
    # window), and among a host's jobs the type is what narrows them.
    job_type_id = fields.Many2one(
        "cloud.job.type",
        required=True,
        index=True,
    )
    # The job's own state, not a mirror of ``queue_job_id.state``: a job
    # waits in its target's queue (``queued``) with no queue.job at all,
    # and only once dispatched does it follow the queue.job's state,
    # which ``queue_job_ext`` writes here on every transition the runner
    # makes through the ORM (``enqueued`` is written by SQL and shows as
    # ``pending``). Empty while the job waits for an alert
    # (``blocked_alert_id``) before it was ever dispatched.
    state = fields.Selection(
        [
            ("queued", "Queued"),
            ("pending", "Pending"),
            ("enqueued", "Enqueued"),
            ("wait_dependencies", "Waiting on Dependencies"),
            ("started", "Started"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        string="State",
        index=True,
        copy=False,
    )
    queue_head = fields.Boolean(
        string="Head of the Queue",
        default=False,
        copy=False,
        help=(
            "Dispatched before everything else waiting on the target:"
            " a follow-up an executor queued from its own run, or the"
            " retry of a failed job."
        ),
    )
    queue_chain_key = fields.Char(
        string="Chain Key",
        index=True,
        copy=False,
        help=(
            "Shared by the steps of one chain. A chain waits in the"
            " queue as one entry and is dispatched whole, with its"
            " queue.job dependencies, when its turn comes."
        ),
    )
    succeeded_before_cancel = fields.Boolean(
        string="Succeeded Before the Cancel",
        copy=False,
        readonly=True,
        help=(
            "The run's success hook had returned when a person's cancel"
            " reached it: the job is stored done, not cancelled"
            " (AbstractExecutor._mark_success_before_cancel). Cleared"
            " when the job starts again."
        ),
    )
    worker_lost_at = fields.Datetime(
        string="Worker Lost At",
        copy=False,
        readonly=True,
        help=(
            "When the reconciliation took up this job, whose worker died"
            " for good (_cron_reconcile_lost_jobs): it has put back what"
            " the lost run left, or is doing so, and closes the job next."
            " Cleared when the job starts again."
        ),
    )
    date_done = fields.Datetime(
        related="queue_job_id.date_done",
        store=True,
        # Stored so ``load_history`` does not need ``queue.job`` ACL —
        # non-Job-Queue-Manager users were tripping ``Access Error`` on
        # ``View all activity`` because reading ``queue_job_id.date_done``
        # required group ``queue_job.group_queue_job_manager``.
    )
    log_chunk_ids = fields.One2many(
        "cloud.job.log.chunk", "job_id", string="Log Chunks"
    )
    result = fields.Serialized()
    payload = fields.Serialized(string="Job Payload")
    # A credential the job needs while it runs, kept out of ``payload``
    # on purpose: the payload is read back into the UI, echoed in
    # notifications and dumped when a job is inspected, and none of those
    # should ever carry a password. Encrypted at rest like every other
    # secret this module holds, and read by the server alone, with
    # ``sudo``: every member of a project reads its instances' jobs, and
    # without ``groups`` one read a restore's password with ``read``.
    secret_payload = EncryptedChar(
        string="Job Secret",
        copy=False,
        groups="base.group_system",
        help="Credential this job needs at run time. Never shown.",
    )
    queue_job_uuid = fields.Char(string="Queue Job UUID", copy=False, index=True)
    queue_job_id = fields.Many2one(
        "queue.job",
        string="Queue Job",
        compute="_compute_queue_job_id",
        store=True,
        # Every queue job deleted sets this column to NULL on the rows
        # that point at it. Unindexed, that is a full scan of cloud_job
        # per deleted job: 200 deletions took 1.57 s against 180k rows.
        index=True,
    )
    instance_id = fields.Many2one(
        "cloud.instance",
        string="Instance",
        ondelete="set null",
        index=True,
    )
    blocked_alert_id = fields.Many2one(
        "cloud.alert",
        string="Blocking Alert",
        ondelete="set null",
        index=True,
        help="When set, this job is blocked waiting for the alert to be resolved.",
    )
    retry_of_id = fields.Many2one(
        "cloud.job",
        string="Retry of",
        ondelete="set null",
        index=True,
        help="When set, this job is a retry of the referenced failed job.",
    )
    # Whether whoever asked for the job held superuser rights: the job
    # runs as its creator, with them only if so (``_dispatch_single``).
    # Set by the queue's entrypoints from the caller's environment
    # (``_create_queued``, ``unblock_and_enqueue``); a job created
    # directly, which only the superuser may do, was asked for with them.
    asked_as_superuser = fields.Boolean(
        string="Asked With Superuser Rights",
        default=lambda self: self.env.su,
        copy=False,
        help=(
            "The job runs as the user who created it, with superuser"
            " rights only when it was asked for with them."
        ),
    )

    @api.depends("queue_job_uuid")
    def _compute_queue_job_id(self):
        for job in self:
            if job.queue_job_uuid:
                queue_job = self.env["queue.job"].search(
                    [("uuid", "=", job.queue_job_uuid)], limit=1
                )
                job.queue_job_id = queue_job or None
            else:
                job.queue_job_id = None

    @api.model
    def enqueue_chain(self, steps, bypass_running_check=False):
        """Queue a chain of jobs that run one after the other.

        The chain is one entry of the queue of every target it touches
        (a staging built from production waits in production's queue
        and in the staging's): it is dispatched whole, as a queue_job
        ``DelayableChain`` whose steps wait on each other, once every one
        of those targets is free and the chain is first in each queue.
        Until then its steps are ``queued`` records sharing a
        ``queue_chain_key``. Cancelling it while it waits cancels every
        step.

        steps: list of dicts {host_id, instance_id, job_type_code, payload}

        Payload values matching ``__chain_job_N__`` (0-indexed) are
        replaced with the cloud.job ID of step N when the chain is
        dispatched, allowing later steps to reference earlier ones.

        ``bypass_running_check`` means what it does for ``enqueue``: the
        chain belongs to the job that queues it and goes to the head of
        its queues (a rebuild's follow-up chain of its addons). As there,
        it is refused to a caller without superuser rights, and so is a
        step whose target they cannot read (``_check_target_access``), or
        one of an instance on a host that is not its own nor its move's
        (``_check_step_host``).

        Returns: list of cloud.job IDs (in execution order)
        """
        hidden = self._get_hidden_job_types()
        for step in steps:
            # Same server-side role gate as ``enqueue`` — an arbitrary
            # chain of steps must not let a low-privilege caller smuggle a
            # manager-only job type through ``call_kw``.
            self._check_job_type_allowed(
                step["job_type_code"], step.get("instance_id"),
            )
        self._check_internal_queue_flags(
            bypass_running_check, [step.get("payload") for step in steps],
        )
        for step in steps:
            self._check_target_access(step.get("instance_id"), step["host_id"])
            self._check_step_host(step.get("instance_id"), step["host_id"])
        job_types = {}
        for step in steps:
            code = step["job_type_code"]
            if code in job_types:
                continue
            job_type = self.env["cloud.job.type"].search(
                [("code", "=", code)], limit=1,
            )
            if not job_type:
                raise UserError(
                    _("Job type '{code}' not found.").format(code=code)
                )
            job_types[code] = job_type

        scopes = {
            self._scope_of_step(step.get("instance_id"), step["host_id"])
            for step in steps
        }
        self._lock_scopes(scopes)
        head = bool(bypass_running_check)
        for scope in scopes:
            # Each step is held to its type's rule in its own queue; the
            # chain as a whole counts once towards the cap of each.
            for step in steps:
                if self._scope_of_step(
                    step.get("instance_id"), step["host_id"],
                ) != scope:
                    continue
                self._check_queue_exclusive(job_types[step["job_type_code"]], scope)
            if not head:
                self._check_queue_cap(scope)

        # Create every step as a waiting record of one chain. The
        # ``__chain_job_N__`` references stay in the payloads until the
        # chain is dispatched (``_dispatch_chain``).
        chain_key = uuid.uuid4().hex
        records = self.browse()
        for step in steps:
            job_type = job_types[step["job_type_code"]]
            records |= self._create_queued(
                {
                    "host_id": step["host_id"],
                    "job_type_id": job_type.id,
                    # Stored in English, as ``enqueue`` says why.
                    "name": job_type.with_context(lang="en_US").name,
                    "instance_id": step.get("instance_id"),
                    "payload": step.get("payload"),
                    "queue_chain_key": chain_key,
                    "queue_head": head,
                    "state": "queued",
                }
            )

        # Audit log entries for each step, skipping the cron-driven
        # background types for the same reason as ``enqueue``: they are
        # machine noise, not operator actions, and they dominate the
        # table.
        for record in records:
            if record.job_type_id.code in hidden:
                continue
            self.env["cloud.audit.log"].sudo().create(
                {
                    "action": record.job_type_id.with_context(lang="en_US").name,
                    "instance_id": record.instance_id.id
                    if record.instance_id
                    else False,
                    "host_id": record.host_id.id,
                    "job_id": record.id,
                }
            )

        # The fast path, as in ``enqueue``: every target quiet in this
        # snapshot and in a fresh one, or the chain waits its turn.
        if all(
            self._scope_quiet(scope, records.ids)
            and self._scope_quiet_fresh(scope, records.ids)
            for scope in scopes
        ):
            self._dispatch_next(*next(iter(scopes)))
        if records[:1].state == "queued":
            self._schedule_dispatch(scopes)
        return records.ids

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
        "high": ("root.user", 5),
        "normal": ("root.user", 10),
        "low": ("root.bg", 10),
    }

    def _resolve_channel_priority(self, job_type, instance_id):
        """Return ``(channel, priority)`` for a job, promoting ``normal``
        → ``high`` when the target instance is in production."""
        tier = job_type.priority_tier or "normal"
        if tier == "normal" and instance_id:
            inst = self.env["cloud.instance"].browse(instance_id)
            if inst.exists() and inst.environment == "production":
                tier = "high"
        return self._TIER_TO_ROUTING.get(tier, self._TIER_TO_ROUTING["normal"])

    @api.model
    def enqueue(
        self,
        host_id,
        instance_id,
        job_type_code,
        payload=None,
        bypass_running_check=False,
    ):
        """
        Queue a job of given type for the given host/instance
        and return the job record ID.

        Never refused because the target is busy: an instance runs one
        job at a time (a host, one host-level job at a time), and a job
        asked for while another runs waits in the target's queue as
        ``queued``, first come first served, until the dispatcher
        (``_dispatch_next``) starts it. Hidden background types (health
        probes, metrics) neither wait nor occupy the target.

        A free target starts the job at once — the one dispatch made in
        the caller's transaction. Odoo's cursors are REPEATABLE READ, so
        "free" has to hold twice: in this transaction's snapshot (which
        alone sees what it created itself) and in a fresh READ COMMITTED
        read (which alone sees what others committed since the snapshot
        was taken). If either sees a job running or waiting, the job
        stays ``queued`` and a dispatch is registered for after the
        commit (``_schedule_dispatch``). The collapse below reads this
        snapshot only (``_check_queue_admission`` says why): in the rare
        race of two identical requests committing at once, a duplicate
        is queued instead of merged.

        What the type says about its queue is applied before the record
        exists (``_check_queue_admission``): a collapsing type returns
        the identical job already waiting, an exclusive type refuses a
        second waiting one, and a queue holds at most
        ``QUEUE_MAX_WAITING`` entries. A rebuild asked for by a GitHub
        push merges into the rebuild of a push already waiting, whatever
        its repository (``RebuildInstanceExecutor._merge_queued_payload``).
        The type is the one the caller finally uses, so a layer that
        routes a code to another type (a rebuild of its own, say)
        is judged by the routed one.

        ``bypass_running_check`` means "I am part of the job running
        now": an executor's ``on_success`` chaining a follow-up, when the
        parent job is still ``started`` on the same target. The job goes
        to the head of the queue, before everything else waiting, and
        is dispatched as soon as the running job ends; it is not counted
        against the cap. Internal use: a caller without superuser rights
        is refused it, as it is ``move_rollback`` in the payload
        (``_check_internal_queue_flags``). Such a caller is refused, too,
        a target they cannot read, before its queue is looked at
        (``_check_target_access``).

        A ``delete_instance`` (and the types a layer adds through
        ``_get_queue_clearing_job_types``) first cancels everything
        waiting on the instance: what waited would have run on an
        instance about to be removed. The delete then waits only for the
        job running now. That clearing sees this snapshot only, as the
        collapse does: a job another request committed between the
        snapshot and the lock is not cancelled, and runs before the
        delete — it was asked for first. An instance with the frozen copy
        of a move on its old host has that copy's removal queued first.
        """
        # Server-side role gate: the public entrypoint is reachable via
        # call_kw with only create-ACL, so enforce the per-type manager
        # requirement here, not only in the HTTP controllers.
        self._check_job_type_allowed(job_type_code, instance_id)
        self._check_internal_queue_flags(bypass_running_check, [payload])
        self._check_target_access(instance_id, host_id)
        job_type_id = self.env["cloud.job.type"].search(
            [
                ("code", "=", job_type_code),
            ],
            limit=1,
        )
        if not job_type_id:
            raise UserError(
                _("Job type with code {job_type_name} not found.").format(
                    job_type_name=job_type_code
                )
            )
        hidden = job_type_code in self._get_hidden_job_types()
        head = bool(bypass_running_check)
        scope = self._scope_of_step(instance_id, host_id)
        if not hidden:
            # Instance-scoped jobs serialise per instance; host-scoped
            # jobs (instance_id falsy) serialise per host. The two scopes
            # are independent — an instance deploy does not block a host
            # probe and vice versa.
            self._lock_scopes({scope})
            if instance_id and job_type_code in self._get_queue_clearing_job_types():
                # Blocking locks on the chains' other queues are fine
                # here: PostgreSQL detects a deadlock and Odoo retries
                # the request. What waits is what this snapshot sees
                # (see the docstring). The caller must be able to stop
                # all of it, chains included, or nothing ends.
                waiting = self._queued_on(scope)
                self._check_may_end_waiting(
                    waiting, self.env["cloud.instance"].browse(int(instance_id)),
                )
                self._cancel_queued(
                    waiting,
                    _("Cancelled: the instance was asked to be deleted."),
                )
                # The copy a move froze on the old host goes first, while
                # the record still names it: queued before the delete,
                # it runs before it (``cloud.instance._enqueue_frozen_cleanup``).
                inst = self.env["cloud.instance"].browse(int(instance_id))
                if inst.move_frozen_source_host_id:
                    inst._enqueue_frozen_cleanup(
                        f"the {job_type_code} of the instance",
                    )
            existing = self._check_queue_admission(
                job_type_id, scope, payload, head=head,
            )
            if existing:
                return existing.id
        # The name and the audit action are stored in English whoever
        # asks: one row per language would split the audit's filter of
        # actions in two, and the mails and alerts that name the job are
        # written in English. A reader sees the type's name in their own
        # language (``_format``).
        vals = {
            "host_id": host_id,
            "job_type_id": job_type_id.id,
            "name": job_type_id.with_context(lang="en_US").name,
            "queue_head": head,
        }
        if not hidden:
            vals["state"] = "queued"
        if instance_id:
            vals["instance_id"] = instance_id
        if payload:
            vals["payload"] = payload
        job_record = self._create_queued(vals)
        # Check pip_dependencies for conflict markers before queuing. A
        # job left waiting for an alert is neither queued nor running:
        # no state, as a job blocked after its run ends ``done``. Written
        # with ``sudo``, as the job was created: the queue's fields.
        _pip_blocked_types = ("deploy_instance", "rebuild_instance")
        if instance_id and job_type_code in _pip_blocked_types:
            inst = self.env["cloud.instance"].browse(instance_id)
            conflicts = detect_pip_conflicts(inst.pip_dependencies)
            if conflicts:
                alert = create_pip_conflict_alert(
                    self.env,
                    conflicts,
                    instance_id=instance_id,
                )
                job_record.sudo().with_context(ic_skip_state_hook=True).write(
                    {"blocked_alert_id": alert.id, "state": False},
                )
                return job_record.id
            # No conflicts — dismiss any stale active pip_conflict alerts
            self.env["cloud.alert"].search(
                [
                    ("instance_id", "=", instance_id),
                    ("code", "=", "pip_conflict"),
                    ("state", "=", "active"),
                ]
            ).write({"state": "dismissed"})
            # Block if there is an active addon_conflict alert
            addon_alert = self.env["cloud.alert"].search(
                [
                    ("instance_id", "=", instance_id),
                    ("code", "=", "addon_conflict"),
                    ("state", "=", "active"),
                ],
                limit=1,
            )
            if addon_alert:
                job_record.sudo().with_context(ic_skip_state_hook=True).write(
                    {"blocked_alert_id": addon_alert.id, "state": False},
                )
                return job_record.id
        # Background job types (host_metrics, instance_health, docker_prune)
        # are enqueued by cron for every host and every instance on every
        # tick. Auditing them buries the handful of real operator actions
        # under machine noise and grows the table without bound. The same
        # predicate that keeps them out of the job drawer keeps them out of
        # the audit trail — they are cron-only (see cloud.host.cron_*), so
        # no user-initiated action is lost here.
        if not hidden:
            self.env["cloud.audit.log"].sudo().create(
                {
                    "action": job_type_id.with_context(lang="en_US").name,
                    "instance_id": instance_id or False,
                    "host_id": host_id,
                    "job_id": job_record.id,
                }
            )
        if hidden:
            # Outside the queue: it runs beside whatever the target does.
            self._dispatch_single(job_record)
            return job_record.id
        if self._scope_quiet(scope, job_record.ids) and self._scope_quiet_fresh(
            scope, job_record.ids,
        ):
            self._dispatch_next(*scope)
        if job_record.state == "queued":
            self._schedule_dispatch({scope})
        return job_record.id

    # Private: queue_job calls it by name, nobody else may. Through
    # ``call_kw`` anyone who reads a job would run it again, outside its
    # queue and past every check of ``enqueue``.
    @api.private
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
        executor = self._executor_or_postpone()
        ssh_executor = executor(
            job_record=self,
            host_record=self.host_id,
        )
        # An earlier attempt's ``on_success`` committed and its own
        # transaction did not: the store of ``done`` lost a serialization
        # race, or the worker died before the commit. The job is done;
        # this attempt only finishes what the lost transaction held.
        # First of all, before the stop flag: a stop that came after the
        # success does not undo it (``_mark_success_before_cancel``).
        outcome = self._committed_outcome()
        if outcome is not None:
            return self._replay_committed_outcome(ssh_executor, *outcome)
        # ``run`` hands a failure inside it to ``on_abort`` itself; one
        # before it is handed over here.
        running = False
        try:
            # A stop that came before this attempt began — before its worker
            # died, mostly: queue_job put it back in the queue and it starts
            # again, still ``cancelled`` (``queue_job_ext``). It does not run.
            if self._stopped_flags()[0]:
                raise JobCancelled(CANCELLED_WHILE_RUNNING)
            ssh_executor.pre_run_checks()
            if not self._has_dependents():
                blocking = ssh_executor.blocking_alert()
                if blocking:
                    # A failure while it is left blocked rolls back to
                    # here: what ``on_blocked`` wrote is undone and the
                    # instance's row freed, which ``on_abort`` writes on a
                    # cursor of its own — it waited 30 s for it, failed,
                    # and the instance stayed in 'deploying'.
                    with self.env.cr.savepoint():
                        return self._block_on(ssh_executor, blocking)
            else:
                # A step of a chain cannot wait: queue_job would store it
                # done and run the next step — on a move, stop the source
                # and restore onto a target never deployed. It fails, so
                # the chain is cancelled as before. Its alert is raised
                # on a cursor of its own: the failure rolls this
                # transaction back, and the alert is what says why.
                with ssh_executor._as_durable():
                    blocking = ssh_executor.blocking_alert()
                    refusal = blocking.message if blocking else None
                if refusal:
                    raise JobRefused(refusal)
            ssh_executor._publish_bus()
            running = True
            ssh_executor.run()
        except RetryableJobError:
            # Executors can raise RetryableJobError to ask queue_job to
            # reschedule the job (e.g. an executor of a module that
            # extends the panel, to honour a per-host advisory lock). Let
            # it propagate so queue_job applies its retry semantics;
            # wrapping it as JobError would mark the job permanently
            # failed instead.
            raise
        except JobCancelled as exc:
            # A person cancelled it and the executor stopped on the flag.
            # Returned, not raised: queue_job logs any exception with its
            # traceback before storing it, and the log scraper turned
            # each cancellation into an ``instance_error_logs`` alert on
            # the customer's panel (12-oct-2026). ``queue_job_ext``
            # stores this ending as the cancellation it is.
            if not running:
                self._close_stopped_before_run(ssh_executor, exc)
            return CANCELLED_WHILE_RUNNING
        except OperationalError as e:
            # Serialisation failures belong to queue_job, which already
            # postpones and retries them (``_runjob`` matches the same
            # PG_CONCURRENCY_ERRORS_TO_RETRY list). Wrapping one in
            # JobError turned a routine lost race — this probe and the
            # metrics cron stamping the same ``cloud_instance`` row —
            # into a permanently failed job plus a queue_job traceback,
            # which the log scraper then reported straight back to us
            # as an ``instance_error_logs`` alert. Let it through.
            if e.pgcode in PG_CONCURRENCY_ERRORS_TO_RETRY:
                raise
            if not running:
                self._abort_before_run(ssh_executor, e)
            raise JobError(str(e)) from e
        except Exception as e:
            # Transient connection failures on opt-in executors (the host
            # monitoring probes) are retried instead of failing on the
            # first blip.
            #
            # Whether this attempt was the *last* one is queue_job's
            # call, not ours: ``Job.perform`` increments the counter and
            # turns the final RetryableJobError into a FailedJobError
            # by itself. We used to decide it here, and got it wrong —
            # the runner can hand a job one more attempt than the
            # budget, so the alert fired on a job that then connected
            # and finished ``done``. Every ``host_unreachable`` on
            # 2026-08-29 was one of those. The alert now hangs off the
            # terminal state instead (see ``queue_job_ext``).
            if getattr(
                ssh_executor, "_retry_on_connection_loss", False
            ) and is_transient_connection_error(e):
                qj = self.queue_job_id
                attempt = (qj.retry or 0) + 1
                max_retries = qj.max_retries or 0
                # The marker first, then the exception *type*: ``str()``
                # of a bare TimeoutError is empty, and this text is what
                # the terminal-state alert will show as the last error.
                raise RetryableJobError(
                    f"{CONNECTION_RETRY_MARKER} {attempt}"
                    f"{f'/{max_retries}' if max_retries else ''}: "
                    f"{type(e).__name__}: {e}",
                    seconds=CONNECTION_RETRY_SECONDS,
                ) from e
            if not running:
                self._abort_before_run(ssh_executor, e)
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

    def _committed_outcome(self):
        """Return the mark an earlier attempt's ``on_success`` left, if any.

        ``AbstractExecutor._run_hook_durably`` writes it on the hook's
        cursor, which commits before this job's transaction. Read in this
        attempt's own transaction: queue_job began it after the earlier
        attempt ended, so it sees what that hook committed.

        :return: ``(own_job_vals, owed)`` from ``cloud.job.outcome``, or
            None when this job's queue job has no mark
        :rtype: tuple | None
        """
        self.ensure_one()
        if not self.queue_job_uuid:
            return None
        self.env.cr.execute(
            "SELECT own_job_vals, owed FROM cloud_job_outcome"
            " WHERE job_id = %s AND queue_job_uuid = %s"
            " ORDER BY id DESC LIMIT 1",
            (self.id, self.queue_job_uuid),
        )
        row = self.env.cr.fetchone()
        if not row:
            return None
        return row[0] or {}, row[1] or []

    def _replay_committed_outcome(self, executor, own_job_vals, owed):
        """End an attempt whose job an earlier attempt finished; run nothing.

        What the earlier attempt's transaction held is lost with it, so
        this one's carries it instead: the values the hook wrote on the
        job's own row (``AbstractExecutor._write_own_job`` — a move's
        fingerprint, read by the next step), and what the hook left for
        that commit (``_rearm_owed``). The job log says why nothing ran.
        queue_job stores the job done once this returns, and its end is
        acted on then as for any job done.

        :param executor: the job's executor, which does not run
        :param dict own_job_vals: ``cloud.job`` values, ``write`` format
        :param list owed: ``{'kind': ...}`` entries
        :return: ``ALREADY_DONE``
        """
        if own_job_vals:
            job = self.sudo()
            job.write(own_job_vals)
            job.flush_recordset(list(own_job_vals))
        self._rearm_owed(owed)
        _logger.info(
            "cloud.job %s: an earlier attempt's outcome was saved; this"
            " attempt runs nothing", self.id,
        )
        try:
            executor._sys(_(
                "An earlier attempt of this job finished and its outcome was"
                " saved: this attempt runs nothing again."
            ))
            executor._flush_logs()
            executor._publish_bus()
        except Exception:  # noqa: BLE001 — the job is done whatever the log does
            _logger.exception("job %s: could not log its earlier outcome", self.id)
        return ALREADY_DONE

    def _rearm_owed(self, owed):
        """Arm again, for this transaction's commit, what a lost one owed.

        ``instance_unlink``: the instance a delete marked
        ``removal_finalized``, or a purge left archived, is unlinked after
        the commit — decided again on a fresh read of the instance
        (``queue.job._unlink_owed_instances``). A kind this code does not
        know is logged and left.

        :param list owed: ``{'kind': ...}`` entries of ``cloud.job.outcome``
        """
        kinds = {
            entry.get("kind") for entry in owed or () if isinstance(entry, dict)
        }
        if "instance_unlink" in kinds:
            self.sudo().queue_job_id._unlink_owed_instances()
        for kind in sorted(kinds - {"instance_unlink"}, key=str):
            _logger.warning(
                "cloud.job %s: nothing knows how to arm %r again", self.id, kind,
            )

    def _has_dependents(self):
        """Whether queue_job runs another job once this one ends.

        True for every step of an ``enqueue_chain`` but the last: queue_job
        starts the next step when this one is stored done, whatever it
        returned.

        :rtype: bool
        """
        self.ensure_one()
        deps = self.sudo().queue_job_id.dependencies or {}
        return bool(deps.get("reverse_depends_on"))

    def _abort_before_run(self, executor, exc):
        """Report, and hand ``on_abort``, a failure before the run started.

        ``_async_entry`` dispatches ``on_abort`` for what fails inside the
        run. A refused check (``pre_run_checks``) or the conflict check of
        ``blocking_alert`` fail before it, and a deploy's instance stayed
        in 'deploying' for good. The log and the job log get the lines
        ``_async_entry`` would have written: a refusal is a warning, and
        anything else a defect of ours with its traceback — on a managed
        panel, the only record of it whoever manages it reads, since the probe
        leaves queue_job's tracebacks of a ``cloud.job`` out.

        Never raises: the caller raises *exc*'s ``JobError`` next.

        :param executor: the executor whose run never started
        :param Exception exc: what ended the job
        """
        logger = logging.getLogger("AbstractExecutor")
        if _is_refusal(exc):
            logger.warning("[execute] Job refused before it ran: %s", exc)
        else:
            logger.error(
                "[execute] Exception before the run: %s", exc, exc_info=exc,
            )
        if not executor._handles_abort():
            executor._sys(f"✗ {type(exc).__name__}: {exc}")
        asyncio.run(executor._dispatch_abort(exc))
        try:
            executor._flush_logs()
        except Exception:
            _logger.exception("job %s: could not log its refusal", self.id)

    def _stopped_flags(self):
        """Read this job's stop flag and success mark from its row.

        In the job's own transaction, which queue_job began once the
        attempt was stored ``started``: it sees a stop committed before,
        and the cache may hold neither.

        :return: ``(cancelled, succeeded_before_cancel)``
        :rtype: tuple
        """
        self.ensure_one()
        self.flush_recordset(["state", "succeeded_before_cancel"])
        self.env.cr.execute(
            "SELECT state = 'cancelled', succeeded_before_cancel"
            " FROM cloud_job WHERE id = %s",
            (self.id,),
        )
        row = self.env.cr.fetchone()
        return (bool(row[0]), bool(row[1])) if row else (False, False)

    def _close_stopped_before_run(self, executor, exc):
        """Close a job stopped before this attempt began, without running it.

        A person cancelled it while it ran, and its worker died before
        the executor stopped: queue_job put it back in the queue
        (``requeue_dead_jobs``) and it starts again with the flag set
        (or the stop came in the instant after the attempt was stored
        ``started``). It does not run: what a run left is put back by
        ``on_cancelled_running`` with no step results, as for a stop
        before the steps (``_dispatch_cancel``), the hook the
        reconciliation gives a stop whose worker died for good. A run
        whose ``on_success`` had returned before the stop gets no hook:
        it is stored done and its chain goes on
        (``queue_job_ext._store_success_before_cancel``). The job log
        says so, and the watchers hear of it. Never raises: the caller
        returns ``CANCELLED_WHILE_RUNNING`` next.

        :param executor: the executor whose run does not start
        :param JobCancelled exc: the stop
        """
        try:
            succeeded = self._stopped_flags()[1]
            executor._sys(_(
                "This job was cancelled before this attempt of it began:"
                " it is not run."
            ))
            if not succeeded:
                asyncio.run(executor._dispatch_cancel(exc))
            executor._flush_logs()
            executor._publish_bus()
        except Exception:
            _logger.exception("job %s: could not close its stop", self.id)

    def _block_on(self, executor, alert):
        """End this job blocked on *alert*, before anything ran.

        Returned, not raised, for the reason a cancellation is: queue_job
        logs any exception with its traceback and stores a failure, and a
        failure raised ``job_failed`` and mailed everyone about what the
        alert already says (16-oct-2026). Everything is written in the
        job's own transaction, which commits when queue_job stores the
        job done; ``execute`` clears the environment afterwards, so it is
        flushed here. The alert is linked with ``sudo``: it is one of the
        queue's fields, and a job asked for without superuser rights runs
        without them (``_dispatch_single``).

        :param executor: the executor that refused to start
        :param alert: the ``cloud.alert`` the job waits for
        :return: ``BLOCKED_BY_ALERT``, what queue_job stores as the result
        """
        self.sudo().write({"blocked_alert_id": alert.id})
        executor.on_blocked(alert)
        executor._sys(_("Waiting for the alert to be resolved: %s", alert.message))
        executor._flush_logs()
        self.env.flush_all()
        # The browser hears of the end through queue_job anyway; a bus
        # that fails here must not fail the block.
        with contextlib.suppress(Exception):
            executor._publish_bus()
        return BLOCKED_BY_ALERT

    def _get_executor(self):
        executor_cls = executor_registry.get(self.job_type_id.code)

        if not executor_cls:
            raise ValueError(f"No executor registered for '{self.job_type_id.code}'")

        return executor_cls

    def _executor_or_postpone(self):
        """Return this job's executor class, or put the job back for newer code.

        A database can be ahead of the server that runs its jobs: a
        rebuild updates it with ``click-odoo-update`` in a container of the
        new image while the old server keeps serving, and restarts that
        server only afterwards. A job of a type that server's code does not
        know, queued by the update (a migration's ``push_edge_guard``), was
        taken by its runner within the second and failed for good with
        ``No executor registered`` (a managed panel, 7-nov-2026): nothing
        queued it again, and the host never got its edge guard.

        Such a job is put back in the queue, without spending its
        retries, every ``NO_EXECUTOR_RETRY_SECONDS`` until a server whose
        code knows the type takes it, for ``NO_EXECUTOR_WAIT_SECONDS``
        from its dispatch (``queue.job.date_created``). Past that, or for a
        job that never went through queue_job, it fails saying what to do,
        and the failure raises the ``job_failed`` alert as any other. Nothing
        has run on the host either way: the executor is the first thing
        ``execute`` needs.

        :return: the executor class registered for the job's type
        :raises RetryableJobError: while the job may still be taken by a
            server with newer code
        :raises JobError: once the wait is over
        """
        try:
            return self._get_executor()
        except ValueError as exc:
            code = self.job_type_id.code
            dispatched = self.sudo().queue_job_id.date_created
            waited = (
                (fields.Datetime.now() - dispatched).total_seconds()
                if dispatched else NO_EXECUTOR_WAIT_SECONDS
            )
            if waited < NO_EXECUTOR_WAIT_SECONDS:
                _logger.warning(
                    "cloud.job %s: no executor for '%s' in this server; put"
                    " back for one with newer code (%ds waited)",
                    self.id, code, waited,
                )
                self._note_waiting_for_newer_code()
                raise RetryableJobError(
                    f"{exc}: put back for a server with newer code",
                    seconds=NO_EXECUTOR_RETRY_SECONDS,
                    ignore_retry=True,
                ) from exc
            # A job that never went through queue_job has nobody to wait
            # for: it fails at once, and the message must not claim a wait.
            after = (
                f" after {NO_EXECUTOR_WAIT_SECONDS // 60} minutes"
                if dispatched else ""
            )
            raise JobError(
                f"{exc} in the server that runs the jobs{after}: its code is"
                " older than its database. Restart it and run the job again."
            ) from exc

    def _note_waiting_for_newer_code(self):
        """Tell the job's log, once, that it waits for a server with newer code.

        Written on a cursor of its own: the job's transaction is rolled
        back by the retry, and the line is what a person opening the job
        sees while it waits. A job put back every
        ``NO_EXECUTOR_RETRY_SECONDS`` gets it on the first postponement
        only: the later ones find it in the log.
        """
        self.ensure_one()
        note = _("Waiting for a server with newer code to pick this job up.")
        with read_committed_cursor(self.env.registry) as cr:
            chunks = self.env(cr=cr)["cloud.job.log.chunk"].sudo()
            if not chunks.search_count([
                ("job_id", "=", self.id),
                ("source", "=", "system"),
                ("content", "=", note),
            ], limit=1):
                chunks.create({
                    "job_id": self.id, "content": note, "source": "system",
                })

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
    def _get_instance_timeline(
        self, instance_id, max_visible=5, include_pre_claim=False,
    ):
        """Return instance jobs formatted for the vertical activity timeline.

        Active jobs (running/waiting chains) are returned in execution order
        (id asc = running first). Recent terminal jobs fill the remaining
        slots. The caller merges both sets and sorts by id descending so the
        newest job renders at the top and the running job keeps its natural
        position without jumping when it finishes.

        When active jobs exceed *max_visible* (overflow), the caller forces
        the running job to the bottom and shows only the next-in-queue jobs
        above it, with an overflow slot at the top.

        Jobs older than the instance's ``_job_history_floor()`` are left
        out unless *include_pre_claim* asks for them; ``preClaim`` says
        how many were hidden so the caller can offer to show them.

        Returns ``{active, recent, total, preClaim}`` where *active* and
        *recent* are lists of ``_format()`` dicts.
        """
        hidden = self._get_hidden_job_types()
        base = [
            ("instance_id", "=", instance_id),
            ("job_type_id.code", "not in", hidden),
        ]
        pre_claim = self._pre_claim_cut(
            instance_id, base, include_pre_claim=include_pre_claim,
        )
        base = base + pre_claim["domain"]
        active = self.search(
            base + [("state", "in", self._active_states)],
            order="id asc",
        )
        recent_limit = max(0, max_visible - min(len(active), max_visible))
        recent = self.env["cloud.job"]
        if recent_limit > 0:
            recent = self.search(
                base + [("state", "in", self._terminal_states)],
                order="id desc",
                limit=recent_limit,
            )
        total = self.search_count(base)
        return {
            "active": active._format(),
            "recent": recent._format(),
            "total": total,
            "preClaim": pre_claim["info"],
        }

    @api.model
    def _pre_claim_cut(self, instance_id, base_domain, include_pre_claim=False):
        """Resolve the job-history cutoff for one instance.

        Central so the instance timeline and the history page draw the
        same line: everything logged against the record before
        :meth:`cloud.instance._job_history_floor` belongs to a previous
        life of the same machine.

        The count of what gets hidden is only computed when a cutoff
        actually applies, so instances without one (every instance, in
        core) pay nothing for this.

        :param instance_id: id of the target ``cloud.instance``.
        :param base_domain: the caller's domain, used to count the
            hidden jobs under the same filters the visible ones use.
        :param include_pre_claim: when true, resolve the cutoff for
            display but do not filter by it.
        :return: ``{"domain": [...], "info": {...} | False}`` — the leaf
            to append (empty when nothing is cut) and what the UI needs
            to offer the "show everything" toggle.
        """
        instance = self.env["cloud.instance"].browse(int(instance_id)).exists()
        floor = instance._job_history_floor() if instance else False
        if not floor:
            return {"domain": [], "info": False}
        hidden_count = self.search_count(
            base_domain + [("create_date", "<", floor)],
        )
        if not hidden_count:
            return {"domain": [], "info": False}
        info = {
            "since": fields.Datetime.to_string(floor),
            "hidden": hidden_count,
            "showing_all": bool(include_pre_claim),
        }
        if include_pre_claim:
            return {"domain": [], "info": info}
        return {"domain": [("create_date", ">=", floor)], "info": info}

    @api.model
    def get_instance_jobs(
        self, instance_id, limit=20, offset=0, include_pre_claim=False,
    ):
        """Return jobs for a specific instance (for the instance overview)."""
        data = self._get_instance_timeline(
            instance_id,
            max_visible=limit,
            include_pre_claim=include_pre_claim,
        )
        return {
            "activeJobs": data["active"],
            "recentJobs": data["recent"],
            "total": data["total"],
            "preClaim": data["preClaim"],
        }

    @api.model
    def get_host_jobs(self, host_id, limit=5, offset=0):
        """Return jobs for a specific host (for the host overview)."""
        hidden = self._get_hidden_job_types()
        domain = [
            ("host_id", "=", host_id),
            ("job_type_id.apply_to", "=", "host"),
            ("job_type_id.code", "not in", hidden),
        ]
        total = self.search_count(domain)
        jobs = self.search(domain, order="id desc", limit=limit, offset=offset)
        return {"jobs": jobs._format(), "total": total}

    # Dispatched to queue_job and not ended.
    _running_states = ["pending", "enqueued", "wait_dependencies", "started"]
    # What occupies a target: waiting in its queue, or running on it.
    _active_states = ["queued", "pending", "enqueued", "wait_dependencies", "started"]
    _terminal_states = ["done", "failed", "cancelled"]
    # Job types hidden from the drawer (automated background tasks).
    # These are still visible in the history page under the "Admin" category.
    # Override _get_hidden_job_types() in submodules to extend this list.
    _hidden_job_types = [
        "host_metrics", "docker_prune", "instance_health", "read_last_login",
        "capture_secret_key", "watch_restore_uploads",
        # Queued from the transaction that ends a restore, sometimes with
        # the queues' locks held: it must wait for nothing.
        "discard_handoff",
        # Steps of a move only the platform queues: the move shows
        # through its deploy, stops, restore and cutover.
        "move_quiet_check", "move_fingerprint", "move_freeze_source",
    ]

    # Job types whose failure should raise a critical alert — these
    # are user-initiated, long-running operations whose failure the
    # operator almost always wants to investigate immediately. Anything
    # not listed here escalates to a ``warning`` alert instead. Extend
    # by overriding ``_get_severe_job_types()`` in child modules.
    # Generic, self-hosted-only job types that warrant a critical alert
    # on failure.
    _severe_job_types = frozenset(
        {
            "deploy_instance",
            "rebuild_instance",
        }
    )

    # Job types whose end changes what their instance is: when one ends,
    # its instance is probed at once (``_refresh_instance_state``). Extend
    # by overriding ``_get_state_refresh_job_types()`` in child modules.
    _state_refresh_job_types = frozenset(
        {
            "start_instance",
            "stop_instance",
            "restart_instance",
            "deploy_instance",
            "rebuild_instance",
            "restore_instance",
            "backup_restore",
            "move_cutover",
            "move_rollback_cleanup",
        }
    )

    # Minimum cloud role required to enqueue each job type. ``enqueue``
    # and ``enqueue_chain`` are public @api.model methods reachable via
    # the stock ``call_kw`` JSON-RPC endpoint by anyone with create-ACL on
    # cloud.job (consultant+) — and the SPA calls them directly for some
    # actions — so the capability gates that live only in the HTTP
    # controllers do not protect them. This map is the server-side
    # authority for "who may ask for this action".
    #
    # Fail-closed: a code missing here requires the manager role, and a
    # structural test forces every declared job type to be listed
    # explicitly so nothing falls through silently. Extend via
    # ``_get_job_type_min_group()``.
    _job_type_min_group = {
        # Lifecycle / deploy — what a consultant may do on their projects.
        "deploy_instance": "group_cloud_consultant",
        "rebuild_instance": "group_cloud_consultant",
        "start_instance": "group_cloud_consultant",
        "stop_instance": "group_cloud_consultant",
        "restart_instance": "group_cloud_consultant",
        # Non-production only at this level: production deletion is
        # escalated to manager in ``_check_job_type_allowed``, so the
        # remote teardown can never start for a caller who would not be
        # allowed to delete the record afterwards.
        "delete_instance": "group_cloud_consultant",
        # Backups, restores and exports move customer data around.
        "backup_list": "group_cloud_developer",
        "backup_create": "group_cloud_developer",
        "backup_download": "group_cloud_developer",
        "backup_download_neutralized": "group_cloud_developer",
        "backup_restore": "group_cloud_developer",
        "restore_instance": "group_cloud_developer",
        # The upload key writes into one directory on the host and
        # nothing else, and only a restore consumes what it delivers —
        # so it sits with the restore it serves, not with the host-level
        # operations. Revocation is deliberately no higher: making it
        # harder to close a door than to open one would be backwards.
        "grant_restore_upload_key": "group_cloud_developer",
        "verify_restore_upload": "group_cloud_developer",
        "revoke_restore_upload_key": "group_cloud_developer",
        "export_instance": "group_cloud_developer",
        # Host-level, destructive or administrative. Every one of these is
        # enqueued by a manager-gated page, a cron or a sudo chain, so the
        # gate costs no legitimate flow anything.
        "host_probe": "group_cloud_manager",
        # Reads an instance's encryption key into the panel; only the
        # daily cron queues it.
        "capture_secret_key": "group_cloud_manager",
        "host_metrics": "group_cloud_manager",
        # One mail through a production's relay to its asker. A consultant
        # asks for it with ``cloud.instance.send_relay_test_mail``, which
        # holds the checks and the hourly count and queues it with
        # ``sudo``; queued directly it would skip them.
        "relay_test_mail": "group_cloud_manager",
        # Closes upload doors of every instance of the host; only the
        # cron queues it.
        "watch_restore_uploads": "group_cloud_manager",
        "host_hardening": "group_cloud_manager",
        "full_setup": "group_cloud_manager",
        # Rewrites what the host's proxy does with every request that
        # reaches it, so it sits with the other host-level operations.
        "push_trusted_proxies": "group_cloud_manager",
        # Rewrites who the host's direct port admits, in its firewall.
        "sync_direct_path": "group_cloud_manager",
        # Writes the router every request to the host's proxy meets first.
        "push_edge_guard": "group_cloud_manager",
        "setup_whitelist": "group_cloud_manager",
        "delete_host": "group_cloud_manager",
        "docker_prune": "group_cloud_manager",
        # Removes a handoff on a host; only a restore that will not run
        # queues it, with ``sudo``.
        "discard_handoff": "group_cloud_manager",
        "instance_health": "group_cloud_manager",
        # Opens the instance's own database with its own credentials.
        # It only reads one timestamp, but the capability is the same
        # one the health probe has, so it sits at the same level: only
        # the daily cron legitimately produces these.
        "read_last_login": "group_cloud_manager",
        "sync_metrics_accounts": "group_cloud_manager",
        # Both deploy containers on the host — cAdvisor runs privileged
        # with the root filesystem mounted, and the central brings up a
        # whole metrics stack.
        "install_observability": "group_cloud_manager",
        "deploy_metrics_central": "group_cloud_manager",
        # The move chain rewrites host_id and wipes the source directory
        # with no further guard of its own; only ``move_to_host`` (manager)
        # legitimately produces these.
        "move_cutover": "group_cloud_manager",
        "move_cleanup_source": "group_cloud_manager",
        "move_rollback_cleanup": "group_cloud_manager",
        "move_quiet_check": "group_cloud_manager",
        "move_fingerprint": "group_cloud_manager",
        "move_freeze_source": "group_cloud_manager",
        # Deletes a chain that nothing else can reach any more, and the
        # record with it. Manager-only for the same reason production
        # deletion is: there is no undo behind it.
        "purge_archived_backups": "group_cloud_manager",
        # Legacy: the executor is gone; the job type record is kept only
        # because historical jobs still reference it.
        "delete_project": "group_cloud_manager",
    }

    def _get_health_blocking_job_types(self):
        """Job types, besides the probe's own list, that skip a health pass.

        A job that restarts an instance's containers on purpose makes the
        probe see an outage that is not one. The probe keeps its list
        (``InstanceHealthExecutor._blocking_job_types``); an addon whose
        jobs do that extends this instead of overriding the probe, which a
        layer above may have replaced already.

        :return: job type codes; none in core
        :rtype: set
        """
        return set()

    def _get_health_blocking_host_job_types(self):
        """Host job types that skip the health pass of the host's instances.

        A job of the host itself, with no instance, can stop or restart
        the databases of every instance on it — a backup that copies them,
        say — and the probe of each one would read the connections it cut
        as errors. While one of these runs, the probes of the instances of
        its host skip their pass, and once it has ended their log window
        opens after its end, as for the instance's own jobs
        (``_get_health_blocking_job_types``).

        :return: job type codes; none in core
        :rtype: set
        """
        return set()

    def _get_hidden_job_types(self):
        """Return job type codes that should not appear in the UI job drawer.

        Override in dependent modules and call super() to extend the list::

            def _get_hidden_job_types(self):
                return super()._get_hidden_job_types() + ['my_background_job']
        """
        return self._hidden_job_types.copy()

    # ── Queue engine ──────────────────────────────────────────────────────
    #
    # One job at a time per instance, and per host for host-level jobs.
    # ``enqueue`` and ``enqueue_chain`` always create their records: a
    # target with a visible job running keeps the new ones ``queued``,
    # and the dispatcher starts the next one when the running job ends,
    # in order of creation — what the running job queued for itself
    # first. A queue holds while a failure of a holding type is open.
    # Everything that reads or changes a queue takes its advisory lock
    # first (``_lock_scopes``).
    #
    # Where the dispatcher runs: on a fresh READ COMMITTED cursor after
    # the transaction of the event commits (``_schedule_dispatch`` →
    # ``_dispatch_scope_fresh``), because a REPEATABLE READ transaction
    # — the runner's above all — does not see what was queued since its
    # snapshot. The one dispatch in the caller's own transaction is the
    # fast path of ``enqueue`` for the job it just created, and only when
    # its snapshot and a fresh read both find the target quiet. The
    # sweep cron (``_cron_dispatch_queued``) covers a dispatch that was
    # lost, with the same fresh dispatcher per queue.

    @api.model
    def _schedule_dispatch(self, scopes):
        """Serve *scopes* on fresh cursors once this transaction commits.

        Registered on ``cr.postcommit``, so a transaction that rolls back
        serves nothing — what it would have served it never recorded.
        Deduplicated per queue within the transaction: a chain of three
        steps ending on one instance registers one dispatch.

        :param scopes: iterable of ``(instance_id, host_id)``
        """
        cr = self.env.cr
        pending = cr.postcommit.data.setdefault("cloud_job_dispatch", set())
        registry = self.env.registry
        for scope in scopes:
            if scope in pending:
                continue
            pending.add(scope)
            cr.postcommit.add(
                lambda scope=scope: _dispatch_scope_fresh(registry, scope),
            )

    @contextlib.contextmanager
    def _fresh_env(self):
        """Yield an environment reading what every transaction committed.

        A READ COMMITTED cursor of its own (in registry test mode, one
        riding on the test's transaction), as the superuser: it only
        reads, and no record rule should hide a job from a check. This
        transaction's writes are flushed first, so a cursor sharing them
        (the test's) sees them too.
        """
        self.env.flush_all()
        with read_committed_cursor(self.env.registry) as cr:
            yield api.Environment(cr, SUPERUSER_ID, {})

    @api.model
    def _scope_quiet(self, scope, except_ids=()):
        """Whether nothing runs on a queue's target and nothing else waits.

        :param scope: the queue
        :param except_ids: ids of waiting jobs to leave out (the job the
            caller just created)
        :rtype: bool
        """
        if self._scope_busy(*scope):
            return False
        waiting = self._queued_on(scope).filtered(lambda j: j.id not in except_ids)
        return not waiting

    @api.model
    def _scope_quiet_fresh(self, scope, except_ids=()):
        """``_scope_quiet`` as a fresh READ COMMITTED cursor sees it.

        The second half of ``enqueue``'s fast path: what another
        transaction committed after this one took its snapshot. It still
        sees waiting what this transaction has just cancelled: a delete
        that cleared the queue starts after its commit, from the
        dispatch registered then, rather than at once.
        """
        with self._fresh_env() as env:
            return env["cloud.job"]._scope_quiet(scope, except_ids)

    @api.model
    def _fresh_rows(self, domain, fnames=()):
        """``search_read`` of *domain* as a fresh READ COMMITTED cursor sees it.

        :param list domain: a ``cloud.job`` domain
        :param fnames: fields to read besides ``id``
        :rtype: list of dict
        """
        with self._fresh_env() as env:
            return env["cloud.job"].search_read(domain, ["id", *fnames])

    @api.model
    def _fresh_only(self, domain, fnames=()):
        """The jobs matching *domain* that only a fresh read sees.

        For the refusals that must hold against what another transaction
        committed after this one's snapshot: the cap, the exclusive rule
        and the one retry per failure. A job this snapshot sees at all is
        this snapshot's to judge — its writes are the newest, and the
        fresh read still sees waiting what this transaction has just
        cancelled (a delete clearing the queue). So a fresh row counts
        only when its id does not exist for this transaction: one
        another transaction inserted after the snapshot.

        :param list domain: a ``cloud.job`` domain
        :param fnames: fields to read besides ``id``
        :rtype: list of dict, as ``_fresh_rows`` gives them
        """
        rows = self._fresh_rows(domain, fnames)
        seen = set(self.sudo().browse([row["id"] for row in rows]).exists().ids)
        return [row for row in rows if row["id"] not in seen]

    @api.model
    def _get_queue_clearing_job_types(self):
        """Job types whose enqueue cancels everything waiting on the instance.

        What waited would have run on an instance about to be removed.
        Override in child modules and call super() to extend (a layer
        adds a delete of its own).

        :return: job type codes
        :rtype: set
        """
        return {"delete_instance"}

    @api.model
    def _queue_holding_alert_codes(self):
        """Alert codes whose open alert on a failed job holds its queue.

        ``job_failed``, or the critical conflict alert that
        ``_create_job_failed_alert`` leaves in its place.

        :rtype: set
        """
        return {"job_failed"} | self._get_actionable_alert_codes()

    @staticmethod
    def _scope_of_step(instance_id, host_id):
        """Return the queue a job on *instance_id*/*host_id* waits in.

        :return: ``(instance_id, None)`` for an instance job,
            ``(None, host_id)`` for a host-level one
        """
        if instance_id:
            return (int(instance_id), None)
        return (None, int(host_id))

    def _scope_of(self):
        """Return the queue this job waits in (see ``_scope_of_step``)."""
        self.ensure_one()
        return self._scope_of_step(self.instance_id.id, self.host_id.id)

    @api.model
    def _scope_domain(self, instance_id, host_id):
        """Return the domain of the jobs that belong to one queue."""
        if instance_id:
            return [("instance_id", "=", instance_id)]
        return [("host_id", "=", host_id), ("instance_id", "=", False)]

    @api.model
    def _lock_scopes(self, scopes):
        """Take the advisory lock of every queue in *scopes*, in a fixed order.

        Whoever enqueues, dispatches or cancels on a queue holds its lock
        until the transaction ends, so two of them cannot both find the
        target idle and start parallel jobs. The fixed order keeps two
        chains that touch the same two instances from deadlocking.

        :param scopes: iterable of ``(instance_id, host_id)`` pairs
        """
        for instance_id, host_id in sorted(
            scopes, key=lambda s: (s[0] or 0, s[1] or 0),
        ):
            if instance_id:
                self.env.cr.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (_JOB_LOCK_NAMESPACE, instance_id),
                )
            else:
                self.env.cr.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (_HOST_JOB_LOCK_NAMESPACE, host_id),
                )

    @api.model
    def _try_lock_scope(self, scope):
        """Take a queue's lock if nobody holds it.

        Used for the other queues of a chain while this one's lock is
        held: waiting for them could deadlock with their own dispatch,
        which will look at the chain itself.

        :return: whether the lock was taken
        :rtype: bool
        """
        instance_id, host_id = scope
        namespace, key = (
            (_JOB_LOCK_NAMESPACE, instance_id) if instance_id
            else (_HOST_JOB_LOCK_NAMESPACE, host_id)
        )
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)", (namespace, key),
        )
        return bool(self.env.cr.fetchone()[0])

    @api.model
    def _scope_busy(self, instance_id, host_id):
        """Whether a visible job runs on the queue's target now.

        Dispatched and not ended: ``pending`` to ``started``. A job
        ``cancelled`` while it ran counts until its executor stops:
        ``cancel_job`` writes the flag while the queue.job is still
        ``started``, and the next job must not start beside a run that is
        still winding down (a move's rollback jobs beside the step they
        cancelled). A chain counts on every target it touches until its
        last step ends: the running step of a staging's clone may be the
        download on production while the staging waits for its restore.
        Hidden background types never count.

        :rtype: bool
        """
        Job = self.sudo()
        hidden = tuple(self._get_hidden_job_types()) or ("",)
        Job.flush_model([
            "state", "queue_chain_key", "instance_id", "host_id",
            "queue_job_uuid", "job_type_id",
        ])
        self.env["queue.job"].sudo().flush_model(["state", "uuid"])
        running = tuple(self._running_states)
        if instance_id:
            scope_sql, params = "{j}.instance_id = %s", [instance_id]
        else:
            scope_sql, params = (
                "{j}.host_id = %s AND {j}.instance_id IS NULL", [host_id],
            )
        # ``running`` names a job dispatched and not ended, or one a
        # person cancelled whose queue.job still runs.
        running_sql = (
            "({j}.state IN %s OR ({j}.state = 'cancelled' AND EXISTS ("
            " SELECT 1 FROM queue_job q"
            " WHERE q.uuid = {j}.queue_job_uuid AND q.state IN %s)))"
        )
        self.env.cr.execute(
            "SELECT 1 FROM cloud_job j"
            " JOIN cloud_job_type t ON t.id = j.job_type_id"
            f" WHERE {scope_sql.format(j='j')} AND t.code NOT IN %s"
            f" AND {running_sql.format(j='j')} LIMIT 1",
            (*params, hidden, running, running),
        )
        if self.env.cr.fetchone():
            return True
        self.env.cr.execute(
            "SELECT 1 FROM cloud_job r"
            " JOIN cloud_job s ON s.queue_chain_key = r.queue_chain_key"
            f" WHERE r.queue_chain_key IS NOT NULL AND {running_sql.format(j='r')}"
            f" AND {scope_sql.format(j='s')} LIMIT 1",
            (running, running, *params),
        )
        return bool(self.env.cr.fetchone())

    @api.model
    def _queue_held_by(self, instance_id, host_id):
        """Return the alert holding one queue, or an empty recordset.

        A queue is held while a job of a type with ``failure_holds_queue``
        failed on its target and the alert of that failure — the
        ``job_failed``, or the critical conflict alert raised instead —
        is still active: nothing behind it is dispatched until the
        failure is retried successfully or the alert is dismissed. The
        job stays ``failed`` meanwhile (a failed job cannot be cancelled),
        so nothing but the alert releases the queue.

        Four kinds of job pass a held queue (``_passes_hold``): the
        retry of a failure that holds it, which is what releases it; a
        delete of the instance (``_get_queue_clearing_job_types``); the
        rollback of a move, which is the recovery — a move step that
        fails after the source was stopped leaves the customer's
        production down until it runs; and a new attempt at what failed
        (``_supersedes_hold``: the rebuild of a push with a commit the
        failed rebuilds were not asked for). The rollback's jobs say so
        as a retry does, on the record: ``move_rollback`` in their
        payload (``cloud.instance._enqueue_rollback_jobs``; only the
        platform may ask for it, ``_check_internal_queue_flags``),
        honoured while their instance's rollback is in progress. None of
        them closes the alert: an operator dismisses it, or a retry or a
        new attempt that succeeds does.
        """
        return self._queue_holding_alerts(instance_id, host_id)[:1]

    @api.model
    def _queue_holding_alerts(self, instance_id, host_id):
        """Return every alert holding one queue, newest first.

        See ``_queue_held_by``; several failures of holding types can be
        open on one target, and the retry of any of them may pass.
        """
        domain = [
            ("state", "=", "active"),
            ("code", "in", list(self._queue_holding_alert_codes())),
            ("job_id", "!=", False),
            ("job_id.state", "=", "failed"),
            ("job_id.job_type_id.failure_holds_queue", "=", True),
        ]
        if instance_id:
            domain.append(("instance_id", "=", instance_id))
        else:
            domain += [("host_id", "=", host_id), ("instance_id", "=", False)]
        return self.env["cloud.alert"].sudo().search(domain, order="id desc")

    @api.model
    def _passes_hold(self, first, holding_jobs):
        """Whether the entry led by *first* is dispatched past a held queue.

        The four kinds of job ``_queue_held_by`` lists: the retry of a
        failure that holds the queue, a delete of the instance
        (``_get_queue_clearing_job_types``), the rollback of a move
        while it is in progress and a new attempt at what failed
        (``_supersedes_hold``).

        :param first: the first step of the entry, a ``cloud.job`` record
        :param holding_jobs: the failed jobs whose alerts hold the queue
            (``_queue_holding_alerts(...).mapped("job_id")``)
        :rtype: bool
        """
        return bool(
            (first.retry_of_id and first.retry_of_id in holding_jobs)
            or first.job_type_id.code in self._get_queue_clearing_job_types()
            or ((first.payload or {}).get("move_rollback")
                and first.instance_id.move_rollback_in_progress)
            or self._supersedes_hold(first, holding_jobs)
        )

    @api.model
    def _supersedes_hold(self, first, holding_jobs):
        """Whether *first* is a new attempt at the failures holding its queue.

        The type's executor decides (``_supersedes_failure``: a push's
        rebuild after rebuilds that failed), and only for a hold that is
        nothing but failed lone jobs with their ``job_failed``: a
        conflict alert open on one of them (``pip_conflict``,
        ``addon_conflict``, or a code a layer adds to
        ``_get_actionable_alert_codes``) asks for a person, and a failed
        step of a chain left an operation half done.

        :param first: the first step of the waiting entry
        :param holding_jobs: the failed jobs whose alerts hold the queue
        :rtype: bool
        """
        if not holding_jobs or first.queue_chain_key:
            return False
        if any(job.queue_chain_key for job in holding_jobs):
            return False
        executor_cls = executor_registry.get(first.job_type_id.code)
        supersedes = getattr(executor_cls, "_supersedes_failure", None)
        if not supersedes or not supersedes(first, holding_jobs):
            return False
        others = self._queue_holding_alert_codes() - {"job_failed"}
        return not self.env["cloud.alert"].sudo().search_count([
            ("job_id", "in", holding_jobs.ids),
            ("state", "=", "active"),
            ("code", "in", list(others)),
        ], limit=1)

    @api.model
    def _waiting_domain(self, scope):
        """Return the domain of the jobs waiting in one queue, blocked ones excluded.

        :param scope: the queue, as ``_scope_of_step`` returns it
        :rtype: list
        """
        return self._scope_domain(*scope) + [
            ("state", "=", "queued"),
            ("blocked_alert_id", "=", False),
        ]

    @api.model
    def _queued_on(self, scope):
        """Return the jobs waiting in one queue, blocked ones excluded."""
        return self.sudo().search(self._waiting_domain(scope))

    @api.model
    def _chain_steps(self, chain_key):
        """Return the steps of one chain, in execution order."""
        return self.sudo().search(
            [("queue_chain_key", "=", chain_key)], order="id asc",
        )

    @api.model
    def _queue_entries(self, scope):
        """Return what waits in one queue, in dispatch order.

        Each entry is a recordset: a lone job, or every step of a chain
        (the ones on other targets included). Heads first, then by
        creation. A job unblocked after its alert keeps the place its
        creation gives it: it was asked for before what was queued while
        it waited.

        :rtype: list of ``cloud.job`` recordsets
        """
        entries, seen = [], set()
        for job in self._queued_on(scope).sorted(
            key=lambda j: (not j.queue_head, j.create_date, j.id),
        ):
            key = job.queue_chain_key
            if not key:
                entries.append(job)
            elif key not in seen:
                seen.add(key)
                entries.append(self._chain_steps(key))
        return entries

    def _queue_places(self):
        """Find where each waiting job of *self* stands in its queue.

        Each queue is read once (``_queue_entries``), however many of
        its jobs *self* holds. A blocked job waits outside the queue and
        is left out, as is any job not ``queued``.

        :return: ``{job id: (scope, position, entry)}``: the queue, how
            many entries are dispatched before the job's, and that entry
        :rtype: dict
        """
        entries_by_scope, places = {}, {}
        for job in self.filtered(
            lambda j: j.state == "queued" and not j.blocked_alert_id,
        ):
            scope = job._scope_of()
            if scope not in entries_by_scope:
                entries_by_scope[scope] = self._queue_entries(scope)
            key = job.queue_chain_key
            for position, entry in enumerate(entries_by_scope[scope]):
                first = entry[:1]
                if (key and first.queue_chain_key == key) or (
                    not key and first.id == job.id
                ):
                    places[job.id] = (scope, position, entry)
                    break
        return places

    @api.model
    def _queue_scopes_of(self, jobs):
        """Return the queues *jobs* wait in, their chains' other steps included.

        :rtype: set of ``(instance_id, host_id)``
        """
        jobs = jobs.sudo()
        keys = {key for key in jobs.mapped("queue_chain_key") if key}
        if keys:
            jobs |= self.sudo().search([("queue_chain_key", "in", list(keys))])
        return {job._scope_of() for job in jobs}

    @api.model
    def _queue_identity(self, payload):
        """Return what tells one waiting job's load from another's.

        Two jobs of a collapsing type with the same identity are one.
        Core compares the payload alone; a layer that keeps part of the
        load apart (one that moves credentials to
        ``secret_payload`` when the job is created) overrides this to
        split *payload* the same way, so two pushes with the same payload
        and different secrets stay two jobs.

        :param dict payload: what the caller passed to ``enqueue``
        :return: ``(payload, secret_payload)`` as the stored job holds them
        """
        return json.loads(json.dumps(payload or {})), False

    @api.model
    def _check_queue_admission(self, job_type, scope, payload, head=False):
        """Apply the type's queue rules before a job of it is created.

        In this order: a collapsing type returns the identical job
        already waiting; a rebuild asked for by a push merges into the
        waiting rebuild of a push (the executor's
        ``_merge_queued_payload``); an exclusive type refuses a second
        waiting one; and the queue refuses its sixth entry — not to a
        head (*head*), which is part of the job running.

        Only jobs waiting on their own are collapsed into or merged
        with: a chain is dispatched whole and its steps are not
        anybody's twin.

        The exclusive rule and the cap count what this transaction's
        snapshot sees and the jobs only a fresh READ COMMITTED read sees
        (``_fresh_only``), for the reason ``enqueue`` gives. Collapse and
        merge read the snapshot
        alone, on purpose: they write on the waiting job they find, and
        under REPEATABLE READ an UPDATE of a row committed after the
        snapshot matches no row and says nothing — the request would
        return a job its write never reached. So in the rare race of two
        identical requests committing at once, the second queues a
        duplicate instead of becoming the first.

        :param job_type: the ``cloud.job.type`` record
        :param scope: the queue, as ``_scope_of_step`` returns it
        :param dict payload: what the caller passed
        :param bool head: whether the job goes to the head of the queue
        :return: the waiting job the request became, or ``None`` when a
            new one is to be created
        :raises UserError: refused by the exclusive rule or the cap
        """
        waiting = self._queued_on(scope).filtered(
            lambda j: j.job_type_id == job_type and not j.queue_chain_key,
        ).sorted("id")
        if job_type.queue_collapse:
            new_payload, new_secret = self._queue_identity(payload)
            for job in waiting:
                if (job.payload or {}) == new_payload and (
                    job.secret_payload or False
                ) == new_secret:
                    return job
        executor_cls = executor_registry.get(job_type.code)
        merge = getattr(executor_cls, "_merge_queued_payload", None)
        if merge:
            for job in waiting:
                if merge(job, payload or {}):
                    return job
        self._check_queue_exclusive(job_type, scope)
        if not head:
            self._check_queue_cap(scope)
        return None

    @api.model
    def _check_queue_exclusive(self, job_type, scope):
        """Refuse a second waiting job of an exclusive type on one queue.

        What waits is what this snapshot sees, and what only a fresh read
        sees (see ``_check_queue_admission``).

        :raises UserError: when one already waits, whatever its payload
        """
        if not job_type.queue_exclusive:
            return
        domain = self._waiting_domain(scope) + [
            ("job_type_id", "=", job_type.id),
        ]
        if self.sudo().search_count(domain) or self._fresh_only(domain):
            if scope[0]:
                message = _(
                    "A %(type)s is already queued for this instance; "
                    "cancel it first.",
                    type=job_type.name,
                )
            else:
                message = _(
                    "A %(type)s is already queued for this host; "
                    "cancel it first.",
                    type=job_type.name,
                )
            raise UserError(message)

    @api.model
    def _waiting_entry_keys(self, scope):
        """Return what tells apart the entries waiting in *scope*, heads left out.

        :return: a chain's key or a lone job's id per entry
        :rtype: set
        """
        return {
            job.queue_chain_key or job.id
            for job in self._queued_on(scope)
            if not job.queue_head
        }

    @api.model
    def _check_queue_cap(self, scope):
        """Refuse the entry that would take a queue past ``QUEUE_MAX_WAITING``.

        Heads (follow-ups and retries) are not counted. The entries are
        those of this snapshot and those only a fresh read sees
        (``_fresh_only``), for the reason ``enqueue`` gives.

        :raises UserError: when the queue is full
        """
        keys = self._waiting_entry_keys(scope)
        keys |= {
            row["queue_chain_key"] or row["id"]
            for row in self._fresh_only(
                self._waiting_domain(scope) + [("queue_head", "=", False)],
                ["queue_chain_key"],
            )
        }
        if len(keys) >= QUEUE_MAX_WAITING:
            if scope[0]:
                message = _(
                    "The instance has %(count)d jobs waiting in its queue.",
                    count=QUEUE_MAX_WAITING,
                )
            else:
                message = _(
                    "The host has %(count)d jobs waiting in its queue.",
                    count=QUEUE_MAX_WAITING,
                )
            raise UserError(message)

    @api.model
    def _dispatch_next(self, instance_id=None, host_id=None):
        """Start the next entry waiting in one queue, if its target is free.

        The one place a waiting job is started. Run on a fresh cursor
        after the transaction of each event commits (``_schedule_dispatch``:
        a visible job of the target ended, a waiting one was cancelled,
        unblocked or retried, the alert holding the queue was dismissed),
        by the sweep cron, and in ``enqueue``'s own transaction for the
        job it just created on a quiet target. Nothing starts while a
        visible job runs; while the queue is held (``_queue_held_by``)
        only the retry of a failure that holds it, a delete of the
        instance (``_get_queue_clearing_job_types``: it waits for the
        running job alone), the rollback of a move or a push's rebuild
        after failed rebuilds passes (see ``_queue_held_by``).

        An entry whose instance is gone, or archived when its type is not
        meant for archived instances (``_runs_on_archived``), is cancelled
        with a note and the next one is looked at; a lone job goes where its
        instance is now, so a move between queueing and dispatch is
        followed, unless its type runs on the host it was asked for
        (``_pinned_to_host``: a move's teardowns and cutover, an upload
        grant's jobs) or it retries a step of a move. The steps of a chain keep the hosts they were
        given: a move's steps name both hosts on purpose. A chain that waits
        in other queues too starts only when it is first in all of them
        and every target is free; until then this queue waits for it.

        :param int instance_id: the instance whose queue to serve, or
        :param int host_id: the host whose host-level queue to serve
        :return: the entry dispatched, or an empty recordset
        """
        Job = self.sudo()
        scope = (instance_id or None, host_id or None)
        Job._lock_scopes({scope})
        if Job._scope_busy(*scope):
            return self.browse()
        holding = Job._queue_holding_alerts(*scope).mapped("job_id")
        for entry in Job._queue_entries(scope):
            if holding and not Job._passes_hold(entry[:1], holding):
                continue
            verdict = Job._entry_verdict(entry, scope)
            if verdict == "cancelled":
                continue
            if verdict == "wait":
                return self.browse()
            Job._dispatch_entry(entry)
            return entry
        return self.browse()

    @api.model
    def _entry_verdict(self, entry, scope):
        """Say what to do with *entry*, first in *scope*'s queue.

        :return: ``"start"``, ``"wait"`` (another target of the chain is
            not ready, or its lock is held) or ``"cancelled"`` (its
            instance is gone, or archived for a type that needs it live;
            the entry was cancelled here)
        """
        # The other queues of a chain are only tried for, never waited
        # on: their own dispatcher may hold them and look at this chain
        # in turn. One not free leaves the entry for the next pass.
        others = self._queue_scopes_of(entry) - {scope}
        if any(not self._try_lock_scope(other) for other in others):
            return "wait"
        gone = any(
            (job.job_type_id.apply_to == "instance" and not job.instance_id)
            or (
                job.instance_id and not job.instance_id.active
                and not self._runs_on_archived(job)
            )
            for job in entry
        )
        if gone:
            self._cancel_queued(
                entry, _("Cancelled: the instance is no longer there."),
                skip_scope=scope, lock=False,
            )
            return "cancelled"
        if len(entry) == 1 and not entry.queue_chain_key:
            inst = entry.instance_id
            if (
                inst and inst.host_id and entry.host_id != inst.host_id
                and not self._pinned_to_host(entry)
                # A retry of a move's step keeps the host the step was
                # given, as the step did in its chain. Retry refuses them
                # (``_check_retry_of_a_move_step``); this holds whatever
                # queued one.
                and not self._is_a_move_step(entry._retry_root())
            ):
                entry.host_id = inst.host_id
            return "start"
        chain_key = entry[:1].queue_chain_key
        for other in others:
            if self._scope_busy(*other) or self._queue_held_by(*other):
                return "wait"
            ahead = self._queue_entries(other)
            if not ahead or ahead[0][:1].queue_chain_key != chain_key:
                return "wait"
        return "start"

    @api.model
    def _runs_on_archived(self, job):
        """Whether *job*'s type is meant for archived instances.

        Such a job is not cancelled because its instance is archived —
        only once the instance no longer exists. Declared by the
        executor (``AbstractExecutor._runs_on_archived_instance``).

        :param job: a ``cloud.job`` record
        :rtype: bool
        """
        executor_cls = executor_registry.get(job.job_type_id.code)
        return bool(getattr(executor_cls, "_runs_on_archived_instance", False))

    @api.model
    def _pinned_to_host(self, job):
        """Whether *job* runs on the host it was asked for.

        A lone job waiting in its queue goes, when it starts, where its
        instance lives then (``_entry_verdict``); such a job does not, nor
        does its retry, which copies its host (``retry_job``). Declared by
        the executor (``AbstractExecutor._pinned_to_host``).

        :param job: a ``cloud.job`` record
        :rtype: bool
        """
        executor_cls = executor_registry.get(job.job_type_id.code)
        return bool(getattr(executor_cls, "_pinned_to_host", False))

    @api.model
    def _dispatch_entry(self, entry):
        """Create the queue.job(s) of a waiting entry and mark it dispatched."""
        if len(entry) == 1 and not entry.queue_chain_key:
            self._dispatch_single(entry)
        else:
            self._dispatch_chain(entry)

    @api.model
    def _dispatch_single(self, job):
        """Hand one job to queue_job and record its uuid and state.

        max_retries=1 by default: allow one automatic retry for transient
        DB errors, then fail permanently. The execution guard prevents
        double SSH runs. Executors that opt into connection retries
        (``_retry_on_connection_loss``) get a higher cap so a momentarily
        unreachable host is retried instead of failing on the first blip —
        only transient connection errors consume those retries; any other
        exception still fails permanently via JobError on the first try.

        The job runs as its creator, with superuser rights only when it
        was asked for with them (``asked_as_superuser``), as until
        1.0.197, when ``enqueue`` handed queue_job the caller's own
        environment. queue_job keeps the env of the recordset it is
        handed (user and ``su``), and the dispatcher runs as the platform
        on a cursor of its own, possibly hours after the job was asked
        for: the creator and that flag are what is left of who asked. So
        a consultant's job runs under their record rules, and what it
        reads through its payload — the job a restore takes its data
        from, say — it reads as they could; what the platform's code
        asked for with ``sudo`` (an executor's follow-ups, the website's
        flows, a cron that elevates) runs as the superuser. Whether the
        creator may run the type was checked when they asked
        (``_check_job_type_allowed`` in ``enqueue`` and its siblings),
        the only way a job reaches the queue: ``create`` and ``write``
        refuse the queue's fields to anyone without superuser rights
        (``_check_queue_control_vals``). The creator stays the author of
        what the run writes. The dispatcher's own writes, the uuid and
        the state, are the platform's: made with ``sudo`` on the job as
        the dispatcher holds it.
        """
        job = job.sudo()
        runner = job._as_its_creator()
        job_type = job.job_type_id
        executor_cls = executor_registry.get(job_type.code)
        max_retries = 1
        if executor_cls and getattr(
            executor_cls,
            "_retry_on_connection_loss",
            False,
        ):
            max_retries = executor_cls._connection_retry_attempts
        channel, priority = self._resolve_channel_priority(
            job_type,
            job.instance_id.id if job.instance_id else False,
        )
        delayed = runner.with_delay(
            max_retries=max_retries,
            channel=channel,
            priority=priority,
            # The stored English name, as ``enqueue`` says why: the
            # dispatcher's language is nobody's in particular.
            description=job.name,
        ).execute()
        job.with_context(ic_skip_state_hook=True).write(
            {"queue_job_uuid": delayed.uuid, "state": "pending"},
        )
        self._broadcast_job_update(job.id)

    def _as_its_creator(self):
        """Return this job in the environment its run has: its creator's.

        With superuser rights only when it was asked for with them
        (``asked_as_superuser``); see ``_dispatch_single``. What the
        dispatcher hands queue_job, and what the reconciliation runs a
        lost job's hooks with (``_cron_reconcile_lost_jobs``).

        :return: the ``cloud.job``, as its creator holds it
        """
        self.ensure_one()
        job = self.sudo()
        runner = job.with_user(job.create_uid)
        if job.asked_as_superuser:
            # After ``with_user``, which drops it.
            runner = runner.sudo()
        return runner

    @api.model
    def _dispatch_chain(self, records):
        """Hand a chain to queue_job as a ``DelayableChain``.

        The ``__chain_job_N__`` references are resolved now, each step
        waits on the previous one, and the first step is the one pending.
        Every step runs as the chain's creator, with superuser rights only
        when the chain was asked for with them, for the reason
        ``_dispatch_single`` gives; the dispatcher's own writes are the
        platform's.
        """
        records = records.sudo()
        self._resolve_chain_refs(records)
        nodes = []
        for record in records:
            runner = record.with_user(record.create_uid)
            if record.asked_as_superuser:
                # After ``with_user``, which drops it.
                runner = runner.sudo()
            # The channel and priority of a single job of its type: a
            # step without one ran in ``root`` and escaped the cap of
            # ``root.user``.
            channel, priority = self._resolve_channel_priority(
                record.job_type_id,
                record.instance_id.id if record.instance_id else False,
            )
            nodes.append(runner.delayable(
                max_retries=1, channel=channel, priority=priority,
            ).execute())
        delay_chain(*nodes).delay()
        for position, (node, record) in enumerate(zip(nodes, records, strict=False)):
            vals = {"state": "pending" if position == 0 else "wait_dependencies"}
            if node._generated_job:
                vals["queue_job_uuid"] = node._generated_job.uuid
            record.with_context(ic_skip_state_hook=True).write(vals)
            self._broadcast_job_update(record.id)

    @api.model
    def _resolve_chain_refs(self, records):
        """Replace ``__chain_job_N__`` in *records*' payloads with step N's id."""
        job_ids = records.ids
        for record in records:
            payload = record.payload
            if not payload:
                continue
            changed = False
            for key, val in payload.items():
                if isinstance(val, str):
                    m = _CHAIN_REF_RE.match(val)
                    if m:
                        payload[key] = job_ids[int(m.group(1))]
                        changed = True
            if changed:
                record.write({"payload": payload})

    @api.model
    def _cancel_queued(self, jobs, note, skip_scope=None, lock=True):
        """Cancel *jobs* while they wait, their chains whole, and move on.

        Immediate: no executor runs and nothing reaches a host. Each job
        gets *note* in its log, its executor's ``on_cancelled_queued``
        hook, which puts back what was written when the job was asked
        for (a deploy's instance in 'deploying', a move's markers), and
        then ``_after_job_terminal("cancelled")`` for the layers. The
        queues they waited in are served after this transaction commits
        (``_schedule_dispatch``), except *skip_scope*, whose dispatcher
        is the caller.

        :param jobs: ``cloud.job`` records; those not ``queued`` any more
            are left alone
        :param str note: why, as the job's last system message
        :param skip_scope: a queue not to dispatch
        :param bool lock: take the queues' locks (blocking); False when
            the caller holds them already (the dispatcher)
        :return: the jobs cancelled
        """
        jobs = jobs.sudo()
        keys = {key for key in jobs.mapped("queue_chain_key") if key}
        if keys:
            jobs |= self.sudo().search([("queue_chain_key", "in", list(keys))])
        scopes = {job._scope_of() for job in jobs}
        if lock:
            self._lock_scopes(scopes)
        # Read again under the lock. On the dispatcher's READ COMMITTED
        # cursor that sees a job another dispatch started meanwhile; in a
        # REPEATABLE READ request it still sees the snapshot, and what
        # protects a job started since is the write below: PostgreSQL
        # refuses to update a row another transaction changed after the
        # snapshot (a serialization failure), and Odoo retries the
        # request, which then finds the job running.
        jobs.invalidate_recordset(["state"])
        jobs = jobs.filtered(lambda j: j.state == "queued")
        if not jobs:
            return jobs
        jobs.with_context(ic_skip_state_hook=True).write({"state": "cancelled"})
        for job in jobs:
            self.env["cloud.job.log.chunk"].sudo().create({
                "job_id": job.id,
                "content": note,
                "source": "system",
            })
            job._run_cancelled_queued_hook()
            job._run_after_terminal("cancelled")
            self._broadcast_job_update(job.id, state="cancelled")
        self._schedule_dispatch(scopes - {skip_scope})
        return jobs

    def _run_cancelled_queued_hook(self):
        """Give the executor its ``on_cancelled_queued``; never raises.

        The cancellation stands whatever the hook does: one that raises
        has its own writes rolled back and is reported in the job's log.
        """
        self.ensure_one()
        executor_cls = executor_registry.get(self.job_type_id.code)
        if not executor_cls:
            return
        try:
            with self.env.cr.savepoint():
                executor_cls(
                    job_record=self, host_record=self.host_id,
                ).on_cancelled_queued()
        except Exception as exc:  # the cancellation stands
            _logger.warning(
                "Job %s: on_cancelled_queued raised.", self.id, exc_info=True,
            )
            self.env["cloud.job.log.chunk"].sudo().create({
                "job_id": self.id,
                "content": _("⚠ Could not undo what the job had prepared: %s", exc),
                "source": "system",
            })

    def write(self, vals):
        """Act on a state transition written through the ORM.

        ``_on_state_changed`` runs for every terminal state written here
        — a job cancelled while it waited, one that ended blocked — but
        not for what the dispatcher and the queue_job mirror write with
        ``ic_skip_state_hook``: they call it themselves once the whole
        transition, alerts included, is recorded. A job linked to a
        queue.job without a state of its own takes the queue.job's
        (``_adopt_queue_job_state``). The queue's fields are refused to a
        caller without superuser rights (``_check_queue_control_vals``).

        The stop flag written on a started job is refused once the job is
        past its point of no return (``_refuse_stop_past_no_return``):
        here, so every way of writing it goes through it. Not what the
        mirror writes with ``ic_skip_state_hook``: that stores a stop
        already taken. Nor with ``ic_stop_past_no_return``: the key of a
        caller that is about to discard the instance the job works on,
        whose database no longer matters (a layer's provisioning started
        again archives its instances right after). Only code sets it:
        what a person asks for goes through ``cancel_job``, which refuses
        whatever context it is called with.
        """
        self._check_queue_control_vals([vals])
        context = self.env.context
        if (
            vals.get("state") == "cancelled"
            and not context.get("ic_skip_state_hook")
            and not context.get("ic_stop_past_no_return")
        ):
            self.filtered(
                lambda job: job.state == "started",
            )._refuse_stop_past_no_return()
        res = super().write(vals)
        if "queue_job_uuid" in vals and "state" not in vals:
            self._adopt_queue_job_state()
        if "state" in vals and not self.env.context.get("ic_skip_state_hook"):
            self._on_state_changed(vals["state"])
        return res

    def _refuse_stop_past_no_return(self):
        """Refuse to stop jobs of ``self`` that started a step past undoing.

        Takes, for each job, the advisory lock its executor takes before
        such a step (``AbstractExecutor._pass_no_return``), held until
        this transaction ends: from then on the executor sees the flag
        this transaction writes, or has already marked the job. The mark
        is read on a cursor of its own: this transaction's snapshot may
        predate its commit.

        The refusal says why that step cannot be cut, by job type
        (:meth:`_no_return_refusal`).

        :raises UserError: when one of them is marked
        """
        if not self:
            return
        for job in self.sorted("id"):
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                (NO_RETURN_LOCK_NAMESPACE, job.id),
            )
        with read_committed_cursor(self.env.registry) as fresh:
            fresh.execute(
                "SELECT job_id, step FROM cloud_job_no_return"
                " WHERE job_id IN %s ORDER BY job_id LIMIT 1",
                (tuple(self.ids),),
            )
            row = fresh.fetchone()
        if row:
            raise UserError(self.browse(row[0])._no_return_refusal(row[1]))

    def _no_return_refusal(self, step):
        """Say why this job cannot be stopped at *step*, by its type.

        A rebuild's update leaves the database half updated, a restore's
        load leaves it empty or half loaded. A layer that gives a job
        type of its own a point of no return adds its reason here.

        :param str step: the label of the step the job is at
        :return: the refusal, translated
        :rtype: str
        """
        self.ensure_one()
        if self.job_type_id.code == "restore_instance":
            return _(
                "Job #%(job)s cannot be stopped any more: it is at"
                " '%(step)s', which has already removed the instance's"
                " database to load the backup in its place. Stopped now,"
                " the database would be left empty or half loaded. Let it"
                " end; it takes as long as loading the backup does.",
                job=self.id,
                step=step,
            )
        return _(
            "Job #%(job)s cannot be stopped any more: it is at"
            " '%(step)s', which stores its changes in the instance's"
            " database as it goes. Stopped now, the database would be"
            " left half updated under the image that runs. Let it end;"
            " it takes a few minutes.",
            job=self.id,
            step=step,
        )

    def _adopt_queue_job_state(self):
        """Give a job linked to a queue.job that queue.job's state.

        The dispatcher writes the uuid and the state together; a link
        made on its own — a job created already linked, a link repaired
        by hand — leaves the mirror to catch up here, so ``state`` is
        never empty on a dispatched job.
        """
        for job in self.sudo():
            if job.queue_job_id and job.queue_job_id.state != job.state:
                job.with_context(ic_skip_state_hook=True).write(
                    {"state": job.queue_job_id.state},
                )

    def _on_state_changed(self, new_state, after_commit=False):
        """The one place a job's end is acted on.

        For a terminal state: ``_after_job_terminal`` (what a layer hooks
        into), then a dispatch of every queue the job waited in is
        registered for after this transaction commits. A ``cancelled``
        written on a job whose queue.job still runs is the cooperative
        stop flag of ``cancel_job``, not an end: the end arrives through
        ``queue_job_ext`` when the executor stops, and is acted on then.

        :param str new_state: the state just written
        :param bool after_commit: run ``_after_job_terminal`` once this
            transaction commits, on a cursor of its own
            (``_run_after_terminal_after_commit``): for the store of
            ``done`` by the runner, which writes nothing but the job's
            own rows
        """
        if new_state not in self._terminal_states:
            return
        hidden = self._get_hidden_job_types()
        scopes = set()
        for job in self.sudo():
            queue_job = job.queue_job_id
            if queue_job and queue_job.state in self._running_states:
                continue
            if after_commit:
                job._run_after_terminal_after_commit(new_state)
            else:
                job._run_after_terminal(new_state)
            if job.job_type_id.code in hidden:
                continue
            scopes |= self._queue_scopes_of(job)
        self._schedule_dispatch(scopes)

    def _run_after_terminal(self, new_state):
        """Give the layers their ``_after_job_terminal``; never raises.

        In a savepoint, logged: a hook of a layer never decides whether a
        state transition happens. Raised through the runner's ``store()``,
        it would turn a job that succeeded into a failure, with its alert
        and its mail.

        What the transaction wrote before the hooks is flushed outside
        their savepoint: it is the transition itself, and a concurrent
        update it meets is the runner's to retry (queue_job postpones the
        job). Flushed by the savepoint, the failure came before the
        savepoint existed, was taken for the hook's and left the
        transaction aborted: a rebuild that succeeded ended failed (QA,
        30-oct-2026).

        :param str new_state: ``done``, ``failed`` or ``cancelled``
        """
        self.env.flush_all()
        for job in self:
            try:
                with self.env.cr.savepoint():
                    job._after_job_terminal(new_state)
            except Exception:  # the transition stands
                _logger.exception(
                    "cloud.job %s: _after_job_terminal(%s) raised",
                    job.id, new_state,
                )

    def _run_after_terminal_after_commit(self, new_state):
        """``_run_after_terminal`` once this transaction commits.

        For the store of ``done`` by the runner. Its transaction must
        write nothing but the job's own rows: it began before the job's
        hooks committed on cursors of their own, and a row they or anyone
        else changed since — the instance's, which the probe's job keys
        on — makes PostgreSQL refuse its commit, and queue_job runs the
        whole job again (7-nov-2026). After the commit, on a fresh cursor,
        with ``ic_after_job_commit`` in the context. A job not committed
        is not acted on; a worker that dies between the commit and this
        loses it, and ``run_after_commit`` logs what raises.

        :param str new_state: the terminal state just stored
        """
        env = self.env
        ids = self.ids

        def _after_terminal():
            """Run the layers' hooks on a cursor of their own."""
            with env.registry.cursor() as cr:
                jobs = env(
                    cr=cr, context=env.context | {"ic_after_job_commit": True},
                )["cloud.job"].browse(ids).exists()
                jobs._run_after_terminal(new_state)

        run_after_commit(
            self.env.cr, f"the end of cloud.job {ids} ({new_state})",
            _after_terminal,
        )

    def _after_job_terminal(self, new_state):
        """Called once *self* reached a terminal state, before the queue moves.

        Core refreshes the state of the instance a job that changes it
        has just acted on (``_refresh_instance_state``). A layer overrides
        it to react to how a job ended (say, undoing what it set up for
        a deploy that failed or was cancelled before it started); call
        super(). Runs in the
        transaction that recorded the end — except a job the runner
        stored ``done``, whose hooks run once that commits, on a cursor of
        their own (``_run_after_terminal_after_commit``, with
        ``ic_after_job_commit`` in the context) — : the runner's for a
        failure of a job that ran, the request's for one cancelled while
        it waited (blocked
        twins dropped by ``unblock_and_enqueue`` included), the
        dispatcher's for one whose instance was gone when its turn came,
        and, for a step of a chain that never ran because the step before
        it failed or was cancelled (``cancelled``), the transaction that
        recorded that end (``queue_job_ext._end_chain_after``).
        Always in a savepoint (``_run_after_terminal``): a hook of a layer
        never decides whether the transition happens — one that raises
        is logged and the state stands.

        :param str new_state: ``done``, ``failed`` or ``cancelled``
        """
        self._refresh_instance_state(new_state)

    @api.model
    def _get_state_refresh_job_types(self):
        """Return the job types whose end changes what their instance is.

        Started, stopped, restarted, deployed, rebuilt, restored or moved:
        the panel reads the instance again as soon as one ends, instead of
        waiting for the next probe. Override in child modules and call
        super() to extend (PITR adds its restore).
        """
        return set(self._state_refresh_job_types)

    def _refresh_instance_state(self, new_state):
        """Probe the instance *self* acted on now, when its type changes it.

        Done or failed: either way the instance may not be what the panel
        last read. A job cancelled before it ran changed nothing, and one
        cancelled while it ran ends through its executor as failed or
        done. The probe reads the state alone and grades nothing
        (``cloud.instance._refresh_state_now``). It is a hidden job,
        outside the queues: asking for it here, with the queue's locks
        of the job that ended taken, waits for none.

        Asked for once the transaction that recorded the end commits, on
        a cursor of its own: its row keys on the instance, which the
        job's hooks changed on cursors of their own after that
        transaction began, and PostgreSQL refused the commit — queue_job
        ran the whole job again (7-nov-2026, ``FOR KEY SHARE`` on
        ``cloud_instance``). Already after the commit
        (``ic_after_job_commit``), it is asked for at once.

        :param str new_state: ``done``, ``failed`` or ``cancelled``
        """
        if new_state not in ("done", "failed"):
            return
        codes = self._get_state_refresh_job_types()
        instances = self.filtered(
            lambda job: job.instance_id and job.job_type_id.code in codes
        ).instance_id
        if not instances:
            return
        if self.env.context.get("ic_after_job_commit"):
            instances._refresh_state_now()
            return
        env = self.env
        ids = instances.ids

        def _probe():
            """Ask for the probes on a cursor of their own."""
            with env.registry.cursor() as cr:
                env(cr=cr)["cloud.instance"].browse(ids)._refresh_state_now()

        run_after_commit(
            self.env.cr, f"the state probe of cloud.instance {ids}", _probe,
        )

    @api.model
    def _on_holding_alert_closed(self, alerts):
        """Serve the queues that *alerts*' closing may have released.

        Called by ``cloud.alert.write`` for every alert dismissed. Only
        the codes that hold a queue, on a job, matter; the dispatcher
        itself decides whether the queue is free now.
        """
        codes = self._queue_holding_alert_codes()
        scopes = set()
        for alert in alerts.sudo():
            if alert.code in codes and alert.job_id:
                scopes.add(alert.job_id._scope_of())
        self._schedule_dispatch(scopes)

    @api.model
    def _cron_dispatch_queued(self):
        """Sweep: serve every queue that waits with nothing running.

        Covers a dispatch that was lost — a transaction that rolled back
        after the running job ended, an instance freed by a path nobody
        hooked. Each queue is served by the fresh dispatcher on a cursor
        of its own (``_dispatch_scope_fresh``), so one that fails does
        not stop the rest.

        :return: how many entries were dispatched
        :rtype: int
        """
        waiting = self.sudo().search([
            ("state", "=", "queued"), ("blocked_alert_id", "=", False),
        ])
        scopes = {job._scope_of() for job in waiting}
        registry = self.env.registry
        self.env.flush_all()
        return sum(
            _dispatch_scope_fresh(registry, scope)
            for scope in sorted(scopes, key=lambda s: (s[0] or 0, s[1] or 0))
        )

    #: Warn at 45 running minutes: the dedicated runner kills jobs at
    #: ``limit_time_real`` = 60, so the alert fires while there is still
    #: time to look before the hard kill (decision log P12.5).
    _LONG_RUNNING_MINUTES = 45
    _LONG_RUNNING_ALERT_CODE = "job_running_too_long"

    @api.model
    def _cron_watch_long_running(self):
        """Alert on jobs running past the duration ceiling; self-resolve.

        One warning per (host, instance) target while at least one of
        its jobs has been ``started`` for over ``_LONG_RUNNING_MINUTES``;
        the alert resolves on the pass where no long runner remains for
        that target. The point is to surface jobs about to hit the
        runner's ``limit_time_real`` **with real data**, so the limit is
        tuned from evidence instead of guessed.
        """
        cutoff = fields.Datetime.now() - timedelta(
            minutes=self._LONG_RUNNING_MINUTES,
        )
        alerts = self.env["cloud.alert"].sudo()

        def _started_at(job):
            return job.queue_job_id.date_started or job.create_date

        long_jobs = (
            self.sudo()
            .search([("state", "=", "started")])
            .filtered(lambda j: _started_at(j) and _started_at(j) < cutoff)
        )
        long_targets = set()
        for job in long_jobs:
            long_targets.add((job.host_id.id, job.instance_id.id or False))
            minutes = int(
                (fields.Datetime.now() - _started_at(job)).total_seconds()
                // 60
            )
            alerts.raise_alert(
                self._LONG_RUNNING_ALERT_CODE,
                _(
                    "Job %(name)s (#%(id)d) has been running for "
                    "%(minutes)d minutes; the job runner kills it at 60.",
                    name=job.display_name or job.job_type_id.code,
                    id=job.id,
                    minutes=minutes,
                ),
                level="warning",
                host=job.host_id or None,
                instance=job.instance_id or None,
                job=job,
            )
        for alert in alerts.search(
            [
                ("code", "=", self._LONG_RUNNING_ALERT_CODE),
                ("state", "=", "active"),
            ]
        ):
            key = (alert.host_id.id or False, alert.instance_id.id or False)
            if key not in long_targets:
                alerts.resolve_alert(
                    self._LONG_RUNNING_ALERT_CODE,
                    host=alert.host_id or None,
                    instance=alert.instance_id or None,
                )

    #: How long a job's two rows must have stood still before the
    #: reconciliation reads them as final. Only for what may still be in
    #: flight: an ORM transition writes both rows in one transaction
    #: (``queue_job_ext``), and the jobrunner's conversion of a dead job is
    #: one statement. It is no deadline for a slow job: one whose
    #: queue.job is not ended is never taken up, however long it runs —
    #: whether a worker died is queue_job's lock to say (6-nov-2026).
    _RECONCILE_GRACE_MINUTES = 2
    #: Jobs read per pass of the reconciliation.
    _RECONCILE_BATCH = 20
    #: Seconds after which a pass starts the clean-up of no other job. A
    #: hook may reach a host or a provider and wait on it, and the pass
    #: holds the cron thread the other crons of the runner wait for: the
    #: first job's clean-up always runs, the rest are the next pass's,
    #: five minutes later (6-nov-2026).
    _RECONCILE_HOOK_BUDGET_S = 60

    @staticmethod
    def _worker_lost_message(env):
        """Return what a job closed as failed by the reconciliation ended on.

        The ``exc_message`` of its queue.job, which its ``job_failed``
        alert quotes, and the ``JobWorkerLost`` its ``on_abort`` gets.

        :param env: the environment whose language it is written in
        :rtype: str
        """
        return env._(
            "The process running this job died and queue_job gave up on it;"
            " closed by the reconciliation of lost jobs."
        )

    @api.model
    def _cron_reconcile_lost_jobs(self):
        """Close the jobs whose worker died, and put back what they left.

        queue_job finds a job whose worker died (its lock released) and
        puts it back in the queue with SQL, or fails it ``JobFoundDead``
        once its retries are spent (``requeue_dead_jobs``). Neither goes
        through the ORM, so ``queue_job_ext`` never hears of it: the
        cloud job stayed ``started`` for good, its queue busy
        (``_scope_busy``), its move never rolled back
        (``cloud.instance.cron_recover_stuck_moves``), the steps after it
        waiting forever (6-nov-2026). The same holds for a queue.job
        ended or deleted by hand.

        Each job found is taken in three steps, each on a cursor of its
        own, so that whatever happens it is cleaned up at most once:

        1. claimed: both rows locked without waiting (``SKIP LOCKED``),
           the case read again on them, ``worker_lost_at`` set and
           committed — a row someone holds is left for the next pass;
        2. cleaned up with no lock held: a new executor, as the job's
           creator, gives ``on_abort`` (a failure) or
           ``on_cancelled_running`` (a stop) what the lost run left, and
           the job log says what happened;
        3. closed: locked again, and the end the ORM never saw written
           through it, so ``queue_job_ext`` acts on it as on any end —
           the mirror, ``job_failed``, the end of its chain, the queue,
           the bus.

        A job claimed whose close did not happen (the cron died between
        the two) is only closed on the next pass, and its log says the
        clean-up may have stopped half way. A pass starts no clean-up
        once ``_RECONCILE_HOOK_BUDGET_S`` have gone by.

        Assumes the database lets idle transactions be
        (``idle_in_transaction_session_timeout`` off): cut off, a live
        job's lock is released and queue_job takes it for dead
        (``docs/architecture.md``).

        :return: how many jobs were closed
        :rtype: int
        """
        began = time.monotonic()
        cleaned = False
        closed = 0
        job_ids = self._lost_job_ids()
        for position, job_id in enumerate(job_ids):
            if cleaned and time.monotonic() - began >= self._RECONCILE_HOOK_BUDGET_S:
                _logger.info(
                    "cloud.job: the reconciliation leaves %d lost job(s)"
                    " for its next pass", len(job_ids) - position,
                )
                break
            try:
                claim = self._claim_lost_job(job_id)
                if claim is None:
                    continue
                case, resumed = claim
                cleaned |= self._clean_up_lost_job(job_id, case, resumed)
                closed += self._close_lost_job(job_id)
            except Exception:
                # One job that cannot be dealt with must not hold the
                # others: its claim stands and the next pass closes it.
                _logger.exception(
                    "cloud.job %s: the reconciliation of its lost worker failed",
                    job_id,
                )
        return closed

    @api.model
    def _lost_job_ids(self):
        """Return the ids of the jobs the reconciliation may have to close.

        One read, without locks, of what ``_lost_job_case`` names: an
        active job whose queue.job ended or is gone, and a stopped one
        whose queue.job was found dead. A cancelled job whose queue.job
        is gone is history (the autovacuum deletes them after 30 days),
        and one whose queue.job still waits or runs is queue_job's.

        :rtype: list[int]
        """
        self.env.flush_all()
        cutoff = fields.Datetime.now() - timedelta(
            minutes=self._RECONCILE_GRACE_MINUTES,
        )
        self.env.cr.execute(
            "SELECT c.id FROM cloud_job c"
            " LEFT JOIN queue_job q ON q.uuid = c.queue_job_uuid"
            " WHERE c.queue_job_uuid IS NOT NULL AND c.state IN %(active)s"
            "   AND (q.id IS NULL OR q.state IN ('failed', 'cancelled', 'done'))"
            "   AND c.write_date < %(cutoff)s AND (q.id IS NULL OR COALESCE("
            "     q.date_done, q.date_cancelled, q.date_enqueued, q.date_created"
            "   ) < %(cutoff)s)"
            " UNION"
            " SELECT c.id FROM queue_job q"
            " JOIN cloud_job c ON c.queue_job_uuid = q.uuid"
            " WHERE q.state = 'failed' AND q.exc_name = 'JobFoundDead'"
            "   AND c.state = 'cancelled'"
            "   AND c.write_date < %(cutoff)s AND COALESCE(q.date_done,"
            "     q.date_cancelled, q.date_enqueued, q.date_created) < %(cutoff)s"
            " ORDER BY 1 LIMIT %(limit)s",
            {
                "active": tuple(self._running_states),
                "cutoff": cutoff,
                "limit": self._RECONCILE_BATCH,
            },
        )
        return [row[0] for row in self.env.cr.fetchall()]

    @api.model
    def _lost_job_case(self, job_state, queue_state, exc_name):
        """Name how a job whose worker may have died is to be closed.

        - ``failed``: active, its queue.job failed or gone — queue_job
          gave up on a dead worker. ``on_abort``, then closed failed.
        - ``stopped``: cancelled, its queue.job found dead — a person
          stopped it and its worker died before it stopped.
          ``on_cancelled_running``, then closed cancelled.
        - ``cancelled_running``: started, its queue.job cancelled.
          ``on_cancelled_running``, then closed cancelled.
        - ``cancelled_waiting``: dispatched and not started, its
          queue.job cancelled. Closed cancelled, which gives it its
          ``on_never_run``.
        - ``done``: active, its queue.job done. Closed done.
        - ``succeeded``: any of the above but ``done``, with the outcome
          mark of its queue.job (``cloud.job.outcome``) — its
          ``on_success`` committed, then the worker died or a stop came.
          No hook; closed done, with what the mark holds
          (``_lost_job_succeeded``).

        :param str job_state: the ``cloud_job`` row's state
        :param queue_state: its ``queue_job`` row's state, None if gone
        :param exc_name: what the queue.job failed on
        :return: the case, or None when there is nothing to close
        """
        if job_state in self._running_states:
            if queue_state in (None, "failed"):
                return "failed"
            if queue_state == "cancelled":
                if job_state == "started":
                    return "cancelled_running"
                return "cancelled_waiting"
            if queue_state == "done":
                return "done"
            return None
        if (
            job_state == "cancelled"
            and queue_state == "failed"
            and exc_name == "JobFoundDead"
        ):
            return "stopped"
        return None

    @api.model
    def _lost_job_succeeded(self, cr, job_row, case):
        """Return *case*, or ``succeeded`` when the job's success was saved.

        An outcome mark for the job's queue.job means its ``on_success``
        committed: what came after — a dead worker, a stop — does not
        undo it. Without this, the reconciliation ran ``on_abort`` or
        ``on_cancelled_running`` on a job that had succeeded and closed it
        failed or cancelled, cutting its chain.

        :param cr: the reconciliation's cursor
        :param dict job_row: the ``cloud_job`` row, as ``_lock_lost_job``
            returns it
        :param case: as ``_lost_job_case`` names it
        :return: the case to close the job as
        """
        if case in (None, "done"):
            return case
        cr.execute(
            "SELECT 1 FROM cloud_job_outcome"
            " WHERE job_id = %s AND queue_job_uuid = %s LIMIT 1",
            (job_row["id"], job_row["queue_job_uuid"]),
        )
        return "succeeded" if cr.fetchone() else case

    @api.model
    def _lock_lost_job(self, cr, job_id):
        """Lock a job's two rows without waiting and return what they hold.

        ``cloud_job`` first, then ``queue_job``, both ``SKIP LOCKED``:
        nothing ever waits, so no order of locks can deadlock.

        :param cr: the cursor to lock them on
        :param int job_id: the ``cloud.job``
        :return: ``(job_row, queue_row)`` as dicts, ``queue_row`` None
            when the queue.job is gone; None when a row is held or the
            job is gone
        """
        cr.execute(
            "SELECT id, state, queue_job_uuid, worker_lost_at, write_date"
            " FROM cloud_job WHERE id = %s FOR NO KEY UPDATE SKIP LOCKED",
            (job_id,),
        )
        job_row = cr.dictfetchone()
        if not job_row:
            return None
        cr.execute(
            "SELECT id, state, exc_name, COALESCE(date_done, date_cancelled,"
            " date_enqueued, date_created) AS moved"
            " FROM queue_job WHERE uuid = %s FOR NO KEY UPDATE SKIP LOCKED",
            (job_row["queue_job_uuid"],),
        )
        queue_row = cr.dictfetchone()
        if queue_row is None:
            cr.execute(
                "SELECT 1 FROM queue_job WHERE uuid = %s",
                (job_row["queue_job_uuid"],),
            )
            if cr.fetchone():
                # There, and held by someone.
                return None
        return job_row, queue_row

    @api.model
    def _claim_lost_job(self, job_id):
        """Step 1 of the reconciliation: take a job up, on a cursor of its own.

        The case is read again on the locked rows, with the same margin
        as the read that found the job, and ``worker_lost_at`` is set and
        committed before anything is cleaned up. A job already claimed
        is taken up again for its close alone.

        :param int job_id: the ``cloud.job``
        :return: ``(case, resumed)`` — *resumed* when an earlier pass
            had claimed it — or None to leave it alone
        """
        cutoff = fields.Datetime.now() - timedelta(
            minutes=self._RECONCILE_GRACE_MINUTES,
        )
        with self.env.registry.cursor() as cr:
            rows = self._lock_lost_job(cr, job_id)
            if rows is None:
                return None
            job_row, queue_row = rows
            case = self._lost_job_succeeded(cr, job_row, self._lost_job_case(
                job_row["state"],
                queue_row and queue_row["state"],
                queue_row and queue_row["exc_name"],
            ))
            if case is None:
                return None
            if job_row["worker_lost_at"]:
                return case, True
            if (job_row["write_date"] or cutoff) >= cutoff or (
                queue_row and (queue_row["moved"] or cutoff) >= cutoff
            ):
                return None
            cr.execute(
                "UPDATE cloud_job SET worker_lost_at ="
                " (now() at time zone 'UTC') WHERE id = %s",
                (job_id,),
            )
        return case, False

    @api.model
    def _lost_job_note(self, env, case, gone):
        """Return the job log's line for a job the reconciliation closes.

        :param env: the environment whose language it is written in
        :param str case: as ``_lost_job_case`` names it
        :param bool gone: whether the queue.job is gone
        :rtype: str
        """
        if case == "failed" and gone:
            return env._(
                "The process running this job was lost and its queue_job"
                " record is gone: it is closed as failed."
            )
        if case == "failed":
            return env._(
                "The process running this job died and queue_job gave up"
                " on it (JobFoundDead): it is closed as failed."
            )
        if case == "stopped":
            return env._(
                "This job was cancelled while it ran, and the process"
                " running it died before it stopped (queue_job:"
                " JobFoundDead): it is closed as cancelled."
            )
        if case == "done":
            return env._(
                "queue_job recorded this job as done, but its end was never"
                " acted on: it is closed as done."
            )
        if case == "succeeded":
            return env._(
                "This job had finished and its outcome was saved when the"
                " process running it was lost: it is closed as done, and"
                " nothing is undone."
            )
        return env._(
            "queue_job recorded this job as cancelled, but its end was never"
            " acted on: it is closed as cancelled."
        )

    @api.model
    def _clean_up_lost_job(self, job_id, case, resumed):
        """Step 2 of the reconciliation: put back what the lost run left.

        No lock is held. A new executor, in the job's creator's
        environment (``_as_its_creator``), is handed the hook of the
        case: ``on_abort`` with ``JobWorkerLost`` for a failure,
        ``on_cancelled_running`` with no step results for a stop — not
        for a run whose ``on_success`` had returned before the stop,
        which is closed done, nor for one whose outcome mark is there
        (``succeeded``). Both run on a durable cursor and never
        raise (``_dispatch_abort``, ``_dispatch_cancel``). The executor
        knows nothing of the lost run but what the database holds.
        Then the job log says what happened, and its watchers hear of
        it. A job claimed by an earlier pass is not cleaned up again:
        its log says the clean-up may have stopped half way. Never
        raises: the job is closed next whatever happens here.

        :param int job_id: the ``cloud.job``
        :param str case: as ``_lost_job_case`` names it
        :param bool resumed: whether an earlier pass claimed it
        :return: whether a hook was run, which may have waited on a host
        :rtype: bool
        """
        hooked = False
        try:
            with self.env.registry.cursor() as cr:
                job = self.env(cr=cr)["cloud.job"].browse(job_id)
                runner = job._as_its_creator()
                note = self._lost_job_note(
                    runner.env, case, not runner.sudo().queue_job_id,
                )
                executor_cls = executor_registry.get(job.sudo().job_type_id.code)
                if not executor_cls:
                    job.env["cloud.job.log.chunk"].sudo().create({
                        "job_id": job_id, "content": note, "source": "system",
                    })
                    return hooked
                executor = executor_cls(
                    job_record=runner, host_record=runner.sudo().host_id,
                )
                executor._sys(note)
                if resumed:
                    executor._sys(runner.env._(
                        "An earlier pass took this job up and stopped before"
                        " closing it: what the lost run left may not all"
                        " have been put back."
                    ))
                elif case == "failed":
                    hooked = True
                    asyncio.run(executor._dispatch_abort(
                        JobWorkerLost(self._worker_lost_message(runner.env)),
                    ))
                elif case in ("stopped", "cancelled_running") and not (
                    runner.sudo().succeeded_before_cancel
                ):
                    hooked = True
                    asyncio.run(executor._dispatch_cancel(
                        JobCancelled(CANCELLED_WHILE_RUNNING),
                    ))
                executor._flush_logs()
                executor._publish_bus()
        except Exception:
            _logger.exception(
                "cloud.job %s: the clean-up of its lost run failed", job_id,
            )
        return hooked

    @api.model
    def _close_lost_job(self, job_id):
        """Step 3 of the reconciliation: write the end the ORM never saw.

        Locked again without waiting, and closed only if the job is still
        to be closed and was claimed. The end goes through the ORM —
        through ``queue.job.write``, or the job's own for a queue.job
        that is gone — so it is acted on as every end is.

        :param int job_id: the ``cloud.job``
        :return: whether the job was closed
        :rtype: bool
        """
        with self.env.registry.cursor() as cr:
            rows = self._lock_lost_job(cr, job_id)
            if rows is None:
                return False
            job_row, queue_row = rows
            case = self._lost_job_succeeded(cr, job_row, self._lost_job_case(
                job_row["state"],
                queue_row and queue_row["state"],
                queue_row and queue_row["exc_name"],
            ))
            if case is None or not job_row["worker_lost_at"]:
                return False
            env = self.env(cr=cr)
            if queue_row is None:
                self._close_lost_job_without_queue(
                    env, job_id, succeeded=case == "succeeded",
                )
                return True
            qjob = env["queue.job"].sudo().browse(queue_row["id"])
            if case == "succeeded":
                # Done as a success stored after a stop is: its own row
                # and the unlink it owed back, the steps after it going.
                qjob._store_success_before_cancel({"state": "done"})
            elif case == "failed":
                qjob.write({
                    "state": "failed",
                    "exc_message": self._worker_lost_message(env),
                })
            elif case == "stopped":
                # Stored failed, which ``queue_job_ext`` turns into the
                # stop it was, or into done after a success.
                qjob.write({"state": "failed"})
            elif case == "cancelled_running":
                qjob.with_context(ic_cancelled_while_running=True).write(
                    {"state": "cancelled"},
                )
            elif case == "cancelled_waiting":
                qjob.write({"state": "cancelled"})
            else:
                qjob.write({"state": "done"})
            _logger.info(
                "cloud.job %s: closed by the reconciliation (%s)", job_id, case,
            )
        return True

    @api.model
    def _close_lost_job_without_queue(self, env, job_id, succeeded=False):
        """Close a job whose queue.job is gone: failed, or done after a success.

        What ``queue_job_ext`` does for a failure, with nothing to write
        it through: the state, the ``job_failed`` alert, the bus, then
        the end acted on (``_on_state_changed``).

        With *succeeded* — its outcome mark is there — it is closed done
        instead, with the values the mark holds for its own row. What the
        mark says it owed is not armed again: that is done through the
        queue.job (``queue.job._unlink_owed_instances``), which is gone.

        :param env: the closing cursor's environment
        :param int job_id: the ``cloud.job``
        :param bool succeeded: whether its ``on_success`` committed
        """
        Job = env["cloud.job"].sudo()
        job = Job.browse(job_id)
        if succeeded:
            outcome = job._committed_outcome()
            job.with_context(ic_skip_state_hook=True).write(
                {**(outcome[0] if outcome else {}), "state": "done"},
            )
            Job._broadcast_job_update(job.id, state="done")
            job._on_state_changed("done", after_commit=True)
            _logger.info(
                "cloud.job %s: closed done by the reconciliation (queue job"
                " gone, outcome saved)", job_id,
            )
            return
        job.with_context(ic_skip_state_hook=True).write({"state": "failed"})
        Job._create_job_failed_alert(
            job, exc_message=self._worker_lost_message(env),
        )
        Job._broadcast_job_update(job.id, state="failed")
        job._on_state_changed("failed")
        _logger.info(
            "cloud.job %s: closed by the reconciliation (queue job gone)",
            job_id,
        )

    #: Terminal-job retention. Jobs feed the instance timeline, so the
    #: window is deliberately long (settings default: 180 days). The
    #: batched loop mirrors the chunk purge: a purge that cannot drain
    #: its backlog is indistinguishable from no retention at all.
    _PURGE_BATCH = 5000
    _PURGE_MAX_BATCHES = 40

    @api.model
    def _cron_purge_old(self):
        """Delete terminal jobs older than the configured retention.

        Only ``done``/``failed``/``cancelled`` jobs are purged — active
        jobs (pending/enqueued/started/waiting) are never touched
        regardless of age. Unlinking a job cascades its remaining log
        chunks and nulls the FKs on alerts and retries
        (``ondelete='set null'``), so nothing dangles. Deletes in
        batches until the backlog drains or ``_PURGE_MAX_BATCHES`` is
        reached; the leftover is picked up by the next daily run.

        Returns the number of rows deleted in this run.
        """
        days = self.env["cloud.settings"].sudo()._get().job_retention_days
        if not days or days <= 0:
            return 0
        cutoff = fields.Datetime.now() - timedelta(days=days)
        domain = [
            ("create_date", "<", cutoff),
            ("state", "in", ("done", "failed", "cancelled")),
        ]
        total = 0
        for _pass in range(self._PURGE_MAX_BATCHES):
            jobs = self.sudo().search(
                domain, order="id", limit=self._PURGE_BATCH,
            )
            if not jobs:
                return total
            total += len(jobs)
            jobs.unlink()
        # Ceiling reached with matching rows still on the table. Say so:
        # silence here would read exactly like "retention is keeping up"
        # while the backlog quietly outlives every run.
        _logger.warning(
            "cloud.job: purge stopped at the %d-batch ceiling after "
            "deleting %d row(s); terminal jobs older than %d day(s) "
            "remain and will be picked up by the next run.",
            self._PURGE_MAX_BATCHES, total, days,
        )
        return total

    @api.model
    def _get_severe_job_types(self):
        """Return job type codes whose failure should raise a critical
        alert. Override in child modules and call super() to extend.
        """
        return set(self._severe_job_types)

    @api.model
    def _get_job_type_min_group(self):
        """Return the job type -> minimum cloud group map for the enqueue gate.

        Override in child modules and call super() to extend, e.g. a
        layer adds its provisioning types::

            def _get_job_type_min_group(self):
                return super()._get_job_type_min_group() | {
                    'provision_vps': 'group_cloud_manager',
                }
        """
        return self._job_type_min_group.copy()

    @api.model
    def _check_job_type_allowed(self, job_type_code, instance_id=None):
        """Raise ``AccessError`` if the caller may not enqueue this type.

        Superuser/sudo internal callers (crons, executors chaining a
        follow-up) bypass the check; everyone else must hold at least the
        role mapped in ``_get_job_type_min_group()`` — manager when the
        code is unknown, so a new type is gated until someone maps it.
        ``delete_instance`` escalates to manager for production instances,
        matching ``_check_can_delete_instance``: the remote teardown runs
        before the record is unlinked, so gating only the unlink would let
        a consultant destroy a production deployment.

        This is the server-side backstop for the public ``enqueue`` and
        ``enqueue_chain`` entrypoints, which the SPA and any RPC client
        can call directly.

        :param job_type_code: code of the ``cloud.job.type`` to enqueue.
        :param instance_id: target instance id, when the job has one; used
            for the per-record escalation above.
        """
        if self.env.su:
            return
        sec = self.env["cloud.security.mixin"]
        min_group = self._get_job_type_min_group().get(
            job_type_code, "group_cloud_manager",
        )
        sec._check_cloud_group(min_group)
        if job_type_code == "delete_instance" and instance_id:
            instance = self.env["cloud.instance"].browse(int(instance_id))
            # Runs in the caller's env on purpose: an instance hidden by
            # ``rule_instance_member`` reads as non-existing here and the
            # escalation is skipped — which is safe ONLY because
            # ``rule_job_member`` scopes cloud.job *creation* by the very
            # same membership domain, so the job row cannot be created for
            # a target the caller cannot see. Those two rule domains must
            # stay aligned (pinned by
            # ``test_foreign_production_instance_cannot_be_deleted``).
            if instance.exists():
                sec._check_can_delete_instance(instance)

    @api.model
    def _check_internal_queue_flags(self, bypass_running_check, payloads):
        """Refuse what only the platform may ask of a queue, to anyone else.

        ``bypass_running_check`` puts a job at the head of its queue, and
        ``move_rollback`` in a payload lets it pass a held queue
        (``_passes_hold``). ``enqueue`` and ``enqueue_chain`` are reachable
        through ``call_kw``, and every legitimate caller is server code
        that asks with ``.sudo()``: ``cloud.instance._enqueue_rollback_jobs``,
        or an executor chaining a follow-up — its job runs with superuser
        rights only when it was asked for with them (``_dispatch_single``),
        as a cron runs as its bot without. Refused rather than
        ignored, so a caller that forgot ``sudo`` fails where it is tested
        instead of queuing a follow-up at the tail, or a recovery behind
        the failure it recovers from.

        :param bool bypass_running_check: what the caller asked for
        :param payloads: the payload of the job, or of each step of a chain
        :raises AccessError: when a caller without superuser rights asks
            for either
        """
        if self.env.su:
            return
        if bypass_running_check or any(
            isinstance(payload, dict) and payload.get("move_rollback")
            for payload in payloads
        ):
            raise AccessError(_(
                "Only the platform may put a job at the head of its queue "
                "or past a queue held by a failure.",
            ))

    @api.model
    def _check_target_access(self, instance_id, host_id):
        """Refuse a target the caller cannot read, before its queue is read.

        ``enqueue`` and ``enqueue_chain`` look at the target's queue with
        ``sudo`` — a waiting job to collapse the request into, a waiting
        rebuild to merge a push into, the exclusive rule, the cap — and
        ``_create_queued`` checks the record rules only on the new rows,
        after all of it. Asked by someone outside the instance's project,
        that would answer with another project's job, write their payload
        into its rebuild and tell what waits there. So a caller without
        superuser rights must read the target first, in their own
        environment: the instance of an instance job, the host of a
        host-level one.

        :param instance_id: the instance, or nothing for a host-level job
        :param host_id: the host
        :raises AccessError: when the caller may not read the target, as
            its record rule says
        """
        if self.env.su:
            return
        if instance_id:
            target = self.env["cloud.instance"].browse(int(instance_id))
        else:
            target = self.env["cloud.host"].browse(int(host_id))
        target.check_access("read")

    @api.model
    def _check_step_host(self, instance_id, host_id):
        """Refuse a step of a chain on a host that is not its instance's.

        A lone job goes where its instance is when its turn comes
        (``_entry_verdict``), but the steps of a chain keep the hosts
        they name: a clone, a refresh and a move address more than one.
        ``cloud.host`` has no record rule, so ``_check_target_access``
        reads only the instance, and a caller without superuser rights
        could have their instance deployed on any host. A step of an
        instance runs on its host or, while it moves, on the move's
        origin or destination, set before the move's chain is queued
        (``move_to_host``).

        :param instance_id: the step's instance, or nothing for a
            host-level step
        :param host_id: the step's host
        :raises AccessError: when the host is none of those
        """
        if self.env.su or not instance_id:
            return
        inst = self.env["cloud.instance"].browse(int(instance_id))
        hosts = inst.host_id | inst.move_origin_host_id | inst.move_target_host_id
        if int(host_id or 0) not in hosts.ids:
            raise AccessError(_(
                "A step of a chain runs on its instance's host, or on a "
                "host its instance is moving between.",
            ))

    @api.model
    def _check_queue_control_vals(self, vals_list):
        """Refuse the queue's fields to a direct create or write without su.

        Consultants may create and write ``cloud.job`` (the ACL), and the
        sweep dispatches every ``queued`` row as its creator, with
        superuser rights when it was asked for with them
        (``asked_as_superuser``), trusting that it was asked for through
        ``enqueue`` and its checks. A ``create`` or ``write`` through
        ``call_kw`` would skip them all — the role gate, the internal
        flags, the queue's rules — and could queue again a job that
        ended, change what a waiting one runs, or give it superuser
        rights. So the fields that decide it (``_QUEUE_CONTROL_FIELDS``)
        are written only with superuser rights: the queue's entrypoints
        (``enqueue``, ``enqueue_chain``, ``retry_job``,
        ``unblock_and_enqueue``, ``cancel_job``) do so after their own
        checks.

        :param vals_list: the values of each job created, or of the write
        :raises AccessError: when a caller without superuser rights sets
            any of them
        """
        if self.env.su:
            return
        names = sorted(_QUEUE_CONTROL_FIELDS.intersection(set().union(*vals_list)))
        if names:
            raise AccessError(_(
                "A job is queued, retried or cancelled through the panel's "
                "actions; these fields cannot be set directly: %(fields)s.",
                fields=", ".join(names),
            ))

    @api.model
    def _create_queued(self, vals_list):
        """Create jobs with the queue's fields, as the caller may create jobs.

        For the queue's entrypoints, once their own checks passed: the
        records are created with ``sudo``, which ``create`` requires for
        the queue's fields (``_check_queue_control_vals``) and which keeps
        the caller's uid — they stay the job's creator, as whom
        ``_dispatch_single`` runs it, with superuser rights only if the
        caller held them: ``asked_as_superuser`` is this environment's,
        set here whatever *vals_list* says, as the ``sudo`` below would
        say yes for everyone. The caller's right to create them is
        checked as the ORM checks it: the access rights before, the record
        rules on the new rows after — a consultant still creates jobs only
        on the instances of their projects. The defaults the caller's
        context names (``default_*``) are not applied to them.

        :param vals_list: a dict, or a list of them, as ``create`` takes
        :return: the new jobs, in the caller's environment
        :raises AccessError: when the caller may not create them
        """
        if isinstance(vals_list, dict):
            vals_list = [vals_list]
        vals_list = [
            vals | {"asked_as_superuser": self.env.su} for vals in vals_list
        ]
        self.browse().check_access("create")
        Jobs = self.sudo()
        if not self.env.su:
            Jobs = Jobs.with_context(clean_context(self.env.context))
        records = Jobs.create(vals_list).with_env(self.env)
        records.check_access("create")
        return records

    # Alert codes that block (re-)enqueueing a job until the operator
    # resolves them (see the pre-check in ``enqueue`` and
    # ``blocked_alert_id``). Only these warrant an "Action required"
    # badge in the UI; any other alert linked to a job is informational
    # — a plain failure only needs a Retry.
    _actionable_alert_codes = frozenset({"pip_conflict", "addon_conflict"})

    @api.model
    def _get_actionable_alert_codes(self):
        """Return alert codes that require operator action before the
        job can run again. Override in child modules and call super()
        to extend.
        """
        return set(self._actionable_alert_codes)

    @api.model
    def _failure_excerpt(self, exc_message):
        """Shorten a job's exception to one line for at-a-glance triage.

        The failed-job alert and the failed-job toast both show it. Only
        the first line, capped at 100 characters, so neither wraps into a
        wall of text; the job log keeps the full traceback.

        :param str exc_message: the queue job's exception message, or None
        :return: the excerpt, or ``""`` when there is nothing to show
        :rtype: str
        """
        if not exc_message:
            return ""
        return exc_message.strip().split("\n", 1)[0][:100]

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
            a targetless alert would be invisible to the member-scoped
            record rule (targetless rows are reserved for global
            platform events).
          * Jobs whose failure is an active conflict alert of their own
            (``pip_conflict`` / ``addon_conflict`` with this ``job_id``)
            that is, or was, critical: it reached the people a
            ``job_failed`` would. A ``pip_conflict`` is a warning, which
            the default notification level does not send, so its job
            still raises ``job_failed``.
          * Jobs whose executor names its own failure alert
            (``_failure_alert_code``, e.g. ``hardening_failed``) and has
            it open for this job, unless the type's failure holds its
            queue.
        """
        if cjob.job_type_id.code in self._get_hidden_job_types():
            return None
        # A job that failed on a critical conflict it raised itself has
        # said so: the conflict alert names it and carries the Resolve
        # flow. A second, critical alert for the same failure mailed
        # everyone the same news twice (aitrax, 16-oct-2026).
        if self.env["cloud.alert"].sudo().search_count([
            ("job_id", "=", cjob.id),
            ("code", "in", list(self._get_actionable_alert_codes())),
            ("state", "=", "active"),
            "|", ("level", "=", "critical"), ("was_critical", "=", True),
        ], limit=1):
            return None
        # Same for a job type whose executor files an alert of its own
        # for a failure, with what failed in it (``_failure_alert_code``):
        # while that one is open, a ``job_failed`` beside it told the
        # same failure twice (a host's hardening, 27-oct-2026). Not
        # for a type whose failure holds its queue: the hold is read off
        # ``job_failed`` (``_queue_holding_alert_codes``).
        own_code = getattr(
            executor_registry.get(cjob.job_type_id.code),
            "_failure_alert_code", None,
        )
        if (
            own_code
            and not cjob.job_type_id.failure_holds_queue
            and self.env["cloud.alert"].sudo().search_count([
                ("job_id", "=", cjob.id),
                ("code", "=", own_code),
                ("state", "=", "active"),
            ], limit=1)
        ):
            return None
        # ``cloud.job.host_id`` is required by the schema, so every
        # row has at least one target. Defensive check kept for child
        # modules that might add more exotic job rows.
        if not (cjob.host_id or cjob.instance_id):
            _logger.warning(
                "job_failed alert skipped: cjob id=%s has no host/instance target",
                cjob.id,
            )
            return None

        level = (
            "critical"
            if cjob.job_type_id.code in self._get_severe_job_types()
            else "warning"
        )
        target_name = cjob.instance_id.name or cjob.host_id.name or ""
        msg = f"{cjob.name or 'Job'} on {target_name} failed"
        excerpt = self._failure_excerpt(exc_message)
        if excerpt:
            msg = f"{msg}: {excerpt}"

        # One incident, not one row per attempt. A failing cron job
        # re-enqueues on its own schedule, and every attempt used to
        # stack another alert *and* another mail/Telegram/webhook: on
        # 2026-08-24 a broken metrics ACL sync produced fourteen of
        # them in six hours, all dismissed by the same later success.
        # Refresh the open one instead, so the panel shows the latest
        # failure of a problem that is still open and the count says
        # how long it has been going.
        Alert = self.env["cloud.alert"].sudo()
        existing = Alert.search(self._job_failed_domain(cjob), limit=1)
        count = existing.occurrences + 1 if existing else 1
        if existing:
            since = fields.Datetime.to_string(existing.create_date)
            msg = f"{msg} (×{count} since {since} UTC)"
        return Alert.raise_alert(
            "job_failed",
            msg,
            level=level,
            host=cjob.host_id or None,
            instance=cjob.instance_id or None,
            job=cjob,
            key=self._job_failed_key(cjob),
            extra={"occurrences": count},
        )

    @api.model
    def _create_host_unreachable_alert(self, cjob, qjob):
        """Raise ``host_unreachable`` when a probe used up its retries.

        Hangs off the *terminal state*, deliberately. ``execute`` used
        to decide "this was the last attempt" from the retry counter
        and alert on the spot, but the runner can hand a job one more
        attempt than the budget — so every alert raised that way was
        followed, seconds later, by the same job connecting and
        finishing ``done``. Fired that way on 2026-08-29 while the host
        was rebooting for security updates: three critical alerts, no
        incident. Here the job really has given up.

        Only opt-in executors qualify, and only when the failure is the
        one we ourselves marked as a connection retry — a probe that
        failed for any other reason is not evidence about the host.
        """
        executor_cls = executor_registry.get(cjob.job_type_id.code)
        if not getattr(executor_cls, "_retry_on_connection_loss", False):
            return None
        message = qjob.exc_message or ""
        if CONNECTION_RETRY_MARKER not in message:
            return None
        if not cjob.host_id:
            return None
        attempts = qjob.retry or qjob.max_retries or 0
        # queue_job wraps our text as "Max. retries (N) reached: <marker>
        # <attempt>/<budget>: <type>: <detail>". The operator wants the
        # part after the attempt counter — the exception itself.
        tail = message.partition(CONNECTION_RETRY_MARKER)[2]
        last_error = tail.partition(": ")[2].strip() or tail.strip()
        return (
            self.env["cloud.alert"]
            .sudo()
            .raise_alert(
                "host_unreachable",
                f"Host unreachable: {attempts} consecutive connection "
                f"attempts failed. Last error: {last_error}",
                level="critical",
                host=cjob.host_id,
                job=cjob,
            )
        )

    @api.model
    def _job_failed_key(self, cjob):
        """Return what tells *cjob*'s ``job_failed`` apart on its target.

        :rtype: str
        """
        return f"job_type:{cjob.job_type_id.code}"

    def _job_failed_domain(self, cjob):
        """Return the domain identifying *cjob*'s open ``job_failed`` alert.

        Identity is (job type, target) rather than the job id, because
        ``retry_job`` creates a fresh ``cloud.job`` for every retry:
        keyed by id, a retry would file a second alert for the same
        incident and the original would never be dismissed. It is the
        engine's identity (``cloud.alert._dedup_domain``) with the job
        type as key, so a host-scoped job never touches the
        instance-scoped alerts of the same host.

        Shared with ``_dismiss_job_failed_alerts`` on purpose — raising
        and clearing must agree on what "the same incident" means, and
        two copies of that rule is precisely how they stop agreeing.
        """
        return self.env["cloud.alert"]._dedup_domain(
            "job_failed",
            host=cjob.host_id or None,
            instance=cjob.instance_id or None,
            key=self._job_failed_key(cjob),
        )

    @api.model
    def _dismiss_job_failed_alerts(self, cjob):
        """Mark stale active ``job_failed`` alerts as dismissed when a
        job reaches ``done``.

        ``retry_job`` always creates a *fresh* cloud.job (linked via
        ``retry_of_id``), so matching only ``job_id == cjob.id`` would
        leave the original failure's alert active forever. Instead we
        dismiss every active ``job_failed`` alert whose originating job
        has the same type and target: if a rebuild now succeeds on this
        instance, earlier rebuild failures are resolved history. Keeps
        the Alerts panel tidy — nobody wants to see fixed failures
        stacked.

        Each one records this job as what resolved it, and its
        resolution is announced where the failure was: the failure
        reached email and Telegram, and its quiet dismissal left the
        panel showing nothing of it (two rebuilds, 2026-10-08).
        """
        stale = self.env["cloud.alert"].sudo().search(
            self._job_failed_domain(cjob),
        )
        if stale:
            stale.write({
                "state": "dismissed",
                "resolution": "auto",
                "resolved_by_job_id": cjob.id,
            })
            for alert in stale:
                alert._notify_alert_resolved()
        return stale

    def _get_active_jobs(self):
        domain = [
            ("job_type_id.code", "not in", self._get_hidden_job_types()),
            "|",
            ("blocked_alert_id", "!=", False),
            ("state", "in", self._active_states),
        ]
        return self.search(domain, order="id desc")._format()

    def _get_recent_jobs(self, limit=10):
        return self.search(
            [
                ("state", "in", self._terminal_states),
                ("job_type_id.code", "not in", self._get_hidden_job_types()),
            ],
            order="id desc",
            limit=limit,
        )._format()

    def _format(self):
        # Build a map of job_id → attachment_id for jobs that have a
        # downloadable file (e.g. export_instance).  Uses a single query
        # instead of per-job lookups.
        attachments = self.env["ir.attachment"].search(
            [
                ("res_model", "=", "cloud.job"),
                ("res_id", "in", self.ids),
            ],
            order="id desc",
        )
        att_map = {}
        for att in attachments:
            att_map.setdefault(att.res_id, att.id)

        # Jobs whose active alert actually demands operator action
        # (conflicts that block re-running). Informational alerts
        # (``job_failed``, …) don't count — a failed job only needs a
        # Retry, so flagging it "Action required" would be misleading.
        actionable_codes = list(self._get_actionable_alert_codes())
        action_alerts = {}
        if self.ids:
            for alert in self.env["cloud.alert"].search(
                [
                    ("job_id", "in", self.ids),
                    ("state", "=", "active"),
                    ("code", "in", actionable_codes),
                ],
                order="id desc",
            ):
                action_alerts.setdefault(alert.job_id.id, alert)
        action_job_ids = set(action_alerts)

        # What a waiting job's queue looks like: how many entries go
        # before it, and the alert holding the queue, if the job does
        # not pass the hold (it then needs the operator as much as a
        # blocked one does; what passes — a retry, a delete, a move's
        # rollback, a push's rebuild after failed rebuilds — does not).
        places = self._queue_places()
        holding_by_scope, held = {}, {}
        for job_id, (scope, _position, entry) in places.items():
            if scope not in holding_by_scope:
                holding_by_scope[scope] = self._queue_holding_alerts(*scope)
            alerts = holding_by_scope[scope]
            if alerts and not self._passes_hold(entry[:1], alerts.mapped("job_id")):
                held[job_id] = alerts[:1]

        def action_fields(job):
            """Why *job* reads "Action required", for the chip to say so.

            The alert it needs a person for — what blocks it, what holds
            its queue, or a conflict of its own — its code, and the type
            of the job it is on (the failure holding the queue). Read
            with ``sudo``: the job is the reader's, and so is the alert
            that keeps it.
            """
            alert = (
                job.blocked_alert_id or held.get(job.id)
                or action_alerts.get(job.id) or self.env["cloud.alert"]
            ).sudo()
            return {
                "action_alert_id": alert.id or None,
                "action_alert_code": alert.code or None,
                "action_job_type_code": alert.job_id.job_type_id.code or None,
            }

        return [
            {
                "id": job.id,
                # In the reader's language; the stored name is English.
                "name": job.job_type_id.name or job.name,
                "host": job.host_id.name,
                "host_id": job.host_id.id if job.host_id else None,
                "job_type": job.job_type_id.name,
                "job_type_code": job.job_type_id.code,
                "instance_id": job.instance_id.id if job.instance_id else None,
                "state": "blocked" if job.blocked_alert_id else job.state,
                "requires_action": (
                    bool(job.blocked_alert_id)
                    or job.id in action_job_ids
                    or bool(held.get(job.id))
                ),
                "queue_position": places[job.id][1] if job.id in places else None,
                # A step of a chain: cancelling it takes the rest with it.
                "in_chain": bool(job.queue_chain_key),
                "queue_held": bool(held.get(job.id)),
                "queue_held_alert_id": (
                    held[job.id].id if held.get(job.id) else None
                ),
                **action_fields(job),
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

    def _restore_url_secret(self):
        """Return what a from-URL restore keeps encrypted, as a dict.

        The job runs as whoever asked for it, without superuser rights
        unless they had them; the field is the server's.

        :return: the decoded ``secret_payload``, ``{}`` without one
        :rtype: dict
        """
        self.ensure_one()
        raw = self.sudo().secret_payload
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _restore_url_link(self):
        """Return the link a from-URL restore downloads.

        Kept encrypted with the credential, never in the payload: a
        presigned link's signature, or a share link's token, downloads
        the archive by itself, and every member of a project reads its
        instances' jobs. A job queued before 1.0.212 that its upgrade did
        not reach still has it in its payload.

        :return: the link, or ``''``
        :rtype: str
        """
        self.ensure_one()
        link = self._restore_url_secret().get("url")
        if not link:
            link = (self.payload or {}).get("url")
        return str(link or "").strip()

    def _restore_url_credential(self):
        """Return the credential a from-URL restore must authenticate with.

        Stored encrypted and apart from the payload, so the password
        never reaches the job list, a notification or the log. Returns
        the three values the host needs to log the download in.

        :return: ``(machine, login, password)``, or ``None``.
        """
        self.ensure_one()
        data = self._restore_url_secret()
        machine = data.get("machine")
        login = data.get("login")
        password = data.get("password")
        if not (machine and login):
            return None
        return machine, login, password or ""

    def _get_last_system_message(self):
        system_chunks = self.log_chunk_ids.filtered(lambda c: c.source == "system")
        return system_chunks[-1].content if system_chunks else ""

    @api.model
    def load_chunks(self, job_id, after_id=0):
        """Return log chunks for the terminal view.

        Source-based and project-based scoping is enforced by record
        rules on ``cloud.job.log.chunk`` (``rule_job_log_chunk_member``
        filters stakeholders/consultants to ``source='system'`` and
        project membership). The search below honours those rules
        automatically.
        """
        chunks = self.env["cloud.job.log.chunk"].search(
            [
                ("job_id", "=", job_id),
                ("id", ">", after_id),
            ]
        )
        job = self.browse(job_id)
        return {
            "chunks": chunks._format(),
            "state": job.state,
        }

    @api.model
    def load_history(self, filters=None):
        """Return jobs for the history page, optionally filtered.

        filters.job_category:
          - ``"operational"`` (default) — excludes admin background jobs
          - ``"admin"`` — only admin background jobs (host_metrics, docker_prune, …)
          - ``"all"`` — no category restriction

        When scoped to a single instance, jobs older than that
        instance's ``_job_history_floor()`` are hidden and summarised in
        ``preClaim``; ``filters.include_pre_claim`` brings them back.
        """
        domain = []
        pre_claim = False
        job_id = (filters or {}).get("job_id")
        if job_id:
            # Direct job lookup — bypass all other filters
            domain.append(("id", "=", int(job_id)))
        else:
            if filters:
                if filters.get("states"):
                    domain.append(("state", "in", filters["states"]))
                if filters.get("host_id"):
                    domain.append(("host_id", "=", int(filters["host_id"])))
                if filters.get("date_from"):
                    domain.append(("create_date", ">=", filters["date_from"]))
                if filters.get("date_to"):
                    domain.append(("create_date", "<=", filters["date_to"]))
                if filters.get("instance_id"):
                    domain.append(("instance_id", "=", int(filters["instance_id"])))
                if filters.get("user_id"):
                    domain.append(("create_uid", "=", int(filters["user_id"])))
                if filters.get("apply_to"):
                    domain.append(("job_type_id.apply_to", "=", filters["apply_to"]))

            category = (filters or {}).get("job_category", "operational")
            admin_types = self._get_hidden_job_types()
            if category == "operational":
                domain.append(("job_type_id.code", "not in", admin_types))
            elif category == "admin":
                domain.append(("job_type_id.code", "in", admin_types))
            # "all" → no category filter

            # Scoped to one instance: hide what predates the handover,
            # under the same category/state filters the visible jobs
            # use, so the count in the notice matches what the toggle
            # would actually reveal.
            if filters and filters.get("instance_id"):
                cut = self._pre_claim_cut(
                    filters["instance_id"],
                    domain,
                    include_pre_claim=bool(filters.get("include_pre_claim")),
                )
                domain = domain + cut["domain"]
                pre_claim = cut["info"]

        jobs = self.search(domain, order="id desc", limit=200)
        hosts = self.env["cloud.host"].search([], order="name asc")
        instances = self.env["cloud.instance"].search(
            [],
            order="project_id, name asc",
        )
        user_ids = jobs.mapped("create_uid").sorted("name")
        return {
            "jobs": jobs._format_history(),
            "hosts": [{"id": h.id, "name": h.name} for h in hosts],
            "instances": [
                {
                    "id": i.id,
                    "name": f"{i.project_id.name}/{i.name}" if i.project_id else i.name,
                }
                for i in instances
            ],
            "users": [{"id": u.id, "name": u.name} for u in user_ids],
            "preClaim": pre_claim,
        }

    def _format_history(self):
        now = fields.Datetime.now()
        places = self._queue_places()
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
            end = job.date_done or job.write_date if terminal else now
            duration_s = int((end - start).total_seconds()) if start and end else 0
            result.append(
                {
                    "id": job.id,
                    # In the reader's language, as in ``_format``.
                    "name": job.job_type_id.name or job.name,
                    "host": job.host_id.name,
                    "host_id": job.host_id.id,
                    "instance_name": (
                        f"{job.instance_id.project_id.name}/{job.instance_id.name}"
                        if job.instance_id
                        else ""
                    ),
                    "instance_id": (job.instance_id.id if job.instance_id else None),
                    "job_type": job.job_type_id.name,
                    "state": job.state,
                    # How many entries go before a waiting job, as in
                    # ``_format``: the history says it as the lists do.
                    "queue_position": (
                        places[job.id][1] if job.id in places else None
                    ),
                    "create_date": job.create_date,
                    "write_date": job.write_date,
                    "duration_s": max(0, duration_s),
                    "log_lines": len(job.log_chunk_ids),
                    "last_system_message": job._get_last_system_message(),
                    "user_id": job.create_uid.id,
                    "user_name": job.create_uid.name or "",
                    **_webhook_fields(job.payload),
                }
            )
        return result

    def cancel_job(self):
        """Cancel an active job.

        The caller must be someone who may ask for a job of its type
        (``_check_job_type_allowed``), in whatever state it is — a restore
        of production stopped half-way, after its database was dropped, is
        no smaller a thing than one started — and for every other job the
        cancellation ends with it (``_ended_with``): the rest of a chain
        waiting in the queue, or the steps waiting on a dispatched or
        running one. One of them they may not ask for, or cannot read,
        refuses the whole cancellation before anything ends, naming it:
        stopping the stop that heads a manager's move would end the move.

        A job waiting in its queue (``queued``) is cancelled on the spot,
        its chain with it: no executor runs, nothing reaches a host. A job
        that waits for an alert is not running and not queued: cancelling
        it only stops resolving the alert from dispatching it.

        A dispatched job that has not started yet is cancelled through
        the queue.job so the runner skips it entirely
        (``button_cancelled``) and the terminal state is mirrored back
        as ``cancelled``: writing ``state`` here alone would be clobbered
        when the runner picked it up. A started job cannot be
        force-cancelled by queue_job (its runner is mid-flight), so we
        set ``state`` cancelled as a cooperative stop flag the executor
        polls on a separate cursor (``_check_cancel``).

        A job past its point of no return is refused whatever its state
        (``_refuse_stop_past_no_return``): started, or back in the queue
        between two attempts (``pending``), whose database the attempt
        before already moved on. Here, whatever the call's context: the
        browser writes it.

        Every way needs the caller's write access to the job. What is
        written on the job itself is written with ``sudo``: ``state`` is
        one of the queue's fields (``_check_queue_control_vals``). The
        queue.job is cancelled with ``sudo`` too: no role of the panel
        reads ``queue.job``.
        """
        self.ensure_one()
        job, reason = self._first_unstoppable(self | self._ended_with())
        if job and job.id == self.id:
            raise AccessError(reason)
        if job:
            raise AccessError(_(
                "Cancelling this job would also end job #%(job)s, %(type)s. "
                "%(reason)s",
                job=job.id,
                type=job.job_type_id.name,
                reason=reason,
            ))
        if self.blocked_alert_id:
            self.check_access("write")
            self.sudo().write({"state": "cancelled", "blocked_alert_id": False})
            return
        if self.state == "queued":
            self.check_access("write")
            self._cancel_queued(
                self,
                _("Cancelled in the queue by %(user)s.", user=self.env.user.name),
            )
            return
        if self.state not in self._active_states:
            raise UserError(_("Only active jobs can be cancelled."))
        self.check_access("write")
        self._refuse_stop_past_no_return()
        if self.state == "started" or not self.queue_job_id:
            self.sudo().write({"state": "cancelled"})
        else:
            self.queue_job_id.sudo().button_cancelled()

    def _ended_with(self):
        """Return the other jobs that cancelling this one ends.

        Read before anything ends, as the cancellation goes on to end
        them: a job waiting in its queue takes the rest of its chain with
        it (``_cancel_queued``), and a dispatched or running one the steps
        waiting on it in its queue_job graph, which
        ``queue.job._end_chain_after`` ends once the cancellation of its
        queue.job is stored or, for a job that runs, once it stops. A job
        that waits for an alert, or that is in no chain, ends nothing
        else.

        :return: the ``cloud.job`` records, with ``sudo``
        """
        self.ensure_one()
        job = self.sudo()
        if job.blocked_alert_id:
            return job.browse()
        if job.state == "queued":
            if not job.queue_chain_key:
                return job.browse()
            return job.search([
                ("queue_chain_key", "=", job.queue_chain_key),
                ("state", "=", "queued"),
                ("id", "!=", job.id),
            ])
        graph = job.queue_job_id.graph_uuid
        if not graph:
            return job.browse()
        return job.search([
            ("queue_job_id.graph_uuid", "=", graph),
            ("state", "=", "wait_dependencies"),
            ("id", ">", job.id),
        ])

    @api.model
    def _first_unstoppable(self, jobs):
        """Return the first of *jobs* the caller may not end, and why.

        A caller may end a job they may read and whose type they may ask
        for (``_check_job_type_allowed``): what ``cancel_job`` holds them
        to, and what deleting, archiving or removing an instance holds
        them to for what waits on it (``_check_may_end_waiting``).

        :param jobs: ``cloud.job`` records in any environment, in the
            order to judge them
        :return: ``(job, reason)``, the job with ``sudo`` and the text of
            the refusal, or ``(None, None)`` when they may end them all
        """
        if self.env.su:
            return None, None
        for job in jobs.sudo():
            try:
                job.with_env(self.env).check_access("read")
                self._check_job_type_allowed(
                    job.job_type_id.code,
                    job.instance_id.id if job.instance_id else None,
                )
            except AccessError as exc:
                return job, exc.args[0]
        return None, None

    @api.model
    def _check_may_end_waiting(self, waiting, instance):
        """Refuse ending what waits on *instance* to whom may not stop it.

        Deleting an instance empties its queue (``enqueue``), archiving it
        has the dispatcher cancel what waits there (``_entry_verdict``),
        and removing its record cancels it at once
        (``cloud.instance.unlink``): each job with the rest of its chain
        (``_ended_with``), wherever that waits. A consultant who may
        delete a staging must not end that way a restore a developer
        asked for, nor a manager's move: the caller is held, for each of
        them, to what ``cancel_job`` would hold them to, and one they may
        not stop refuses the whole action before anything ends.

        :param waiting: the ``cloud.job`` records waiting on *instance*
            that the action would end
        :param instance: the ``cloud.instance``, named in the refusal
        :raises AccessError: naming the first job they may not stop
        """
        if self.env.su:
            return
        ended = waiting.sudo()
        for job in waiting.sudo():
            ended |= job._ended_with()
        job, reason = self._first_unstoppable(ended)
        if job:
            raise AccessError(_(
                "%(instance)s has work waiting that your role cannot stop: "
                "job #%(job)s, %(type)s. %(reason)s",
                instance=instance.sudo().name,
                job=job.id,
                type=job.job_type_id.name,
                reason=reason,
            ))

    def _retry_root(self):
        """Return the job this one retries first, following its retries back.

        A retry is a lone job: the chain it came from is known only
        through what it retries.

        :return: the ``cloud.job`` at the start of the line of retries,
            with ``sudo`` (this job itself when it retries nothing)
        """
        self.ensure_one()
        job = self.sudo()
        seen = set()
        while job.retry_of_id and job.id not in seen:
            seen.add(job.id)
            job = job.retry_of_id
        return job

    @api.model
    def _is_a_move_step(self, job):
        """Whether *job* is a step of a move's chain: one with a cutover.

        A chain is known by ``queue_chain_key``. The steps queued before
        23-oct-2026 have none: they were a queue_job graph, known by the
        ``graph_uuid`` of their queue.job, and are looked up through that.

        :param job: a ``cloud.job`` record, the first of its retries
            (``_retry_root``)
        :rtype: bool
        """
        job = job.sudo()
        if job.queue_chain_key:
            return bool(job.search_count([
                ("queue_chain_key", "=", job.queue_chain_key),
                ("job_type_id.code", "=", "move_cutover"),
            ]))
        graph = job.queue_job_id.graph_uuid
        if not graph:
            return False
        return bool(job.search_count([
            ("queue_job_id.graph_uuid", "=", graph),
            ("job_type_id.code", "=", "move_cutover"),
        ]))

    def _check_retry_of_a_move_step(self):
        """Refuse the retry of a step of a move, unless nothing can go wrong.

        A retry is a lone job and follows its instance (``_entry_verdict``).
        A generic step of a move's chain (a restore, a stop) retried once
        the move ended or was rolled back would act on the host the
        instance lives on then: restore it from its own dump, stop its
        odoo. Retried while the move goes on or is being rolled back, a
        step aimed at the target went to the source, by then the only
        live copy, and the steps after it had ended «Not run»: it does not
        resume the move either. The chain of the move is known by its
        cutover; the move under way, by the markers on the instance, whose
        chain lists the step. A retry is looked up through what it
        retries. Only a step aimed at its host on purpose
        (``_pinned_to_host``: the teardown of the old copy) is retried,
        once no move of the instance is under way, and on that host.

        :raises UserError: the job is a step of a move that is no longer
            in progress, or a move of the instance is in progress or
            being rolled back
        """
        self.ensure_one()
        job = self._retry_root()
        if not self._is_a_move_step(job):
            return
        inst = job.instance_id
        current = bool(inst.move_origin_host_id) and str(job.id) in (
            inst.move_chain_job_ids or ""
        ).split(",")
        if not current and not self._pinned_to_host(self):
            raise UserError(_(
                "This job belongs to a move that is no longer in progress, so "
                "it cannot be retried: it would act on the wrong host. If you "
                "still need the move, start it again."
            ))
        if inst.move_origin_host_id or inst.move_rollback_in_progress:
            raise UserError(_(
                "A move of this instance is in progress or being rolled back, "
                "so a step of a move cannot be retried now: run alone, it "
                "would act on the wrong host, and the steps after it would "
                "not run. Let the rollback finish, or use “Roll back move” if "
                "the move failed; then start the move again if you still "
                "need it."
            ))

    def retry_job(self):
        self.ensure_one()
        if self.blocked_alert_id:
            raise UserError(
                _(
                    "This job waits for an alert and starts by itself once "
                    "it is resolved. Resolve it in Alerts."
                )
            )
        if self.state != "failed":
            raise UserError(_("Only failed jobs can be retried."))
        # Same role gate as ``enqueue``: the retry runs under the
        # *retrying* user's env, so without this a consultant who can read
        # a failed higher-role job of their project (e.g. a move step)
        # could relaunch it as themselves.
        self._check_job_type_allowed(
            self.job_type_id.code,
            self.instance_id.id if self.instance_id else None,
        )
        self._check_retry_of_a_move_step()
        scope = self._scope_of()
        self._lock_scopes({scope})
        # In this snapshot, or committed since by another request: two
        # clicks at once (``_fresh_only``).
        retries = [
            ("retry_of_id", "=", self.id), ("state", "in", self._active_states),
        ]
        if self.sudo().search_count(retries) or self._fresh_only(retries):
            raise UserError(
                _("A retry of this job is already waiting in the queue."),
            )
        # Create a fresh job as a retry — keeps the original's logs
        # intact. It goes to the head of the queue and, when this
        # failure holds the queue, past the hold: the retry is what
        # releases it. One retry per failed job at a time: a second one
        # while the first waits or runs is refused. It runs with the
        # credential the failed job held (a restore from a URL's), kept
        # encrypted apart from the payload as there.
        new_job = self._create_queued(
            {
                "name": self.name,
                "host_id": self.host_id.id,
                "job_type_id": self.job_type_id.id,
                "instance_id": self.instance_id.id if self.instance_id else False,
                "payload": self.payload,
                "secret_payload": self.sudo().secret_payload,
                "retry_of_id": self.id,
                "queue_head": True,
                "state": "queued",
            }
        )
        self._schedule_dispatch({scope})
        return new_job.id

    # Private: the cron's housekeeping, which deletes backup rows as the
    # superuser; nothing calls it through ``call_kw``.
    @api.private
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
        Backup = self.env["cloud.instance.backup"].sudo()
        expired_rows = Backup.search(
            [
                ("attachment_id", "!=", False),
                ("backup_time", "<", cutoff),
            ]
        )
        if expired_rows:
            attachments = expired_rows.mapped("attachment_id")
            _logger.info(
                "Cleaning up %d expired backup row(s) + attachment(s)",
                len(expired_rows),
            )
            expired_rows.unlink()
            # Some attachments may already be gone (manual cleanup);
            # ``exists`` filters those out without raising.
            attachments.exists().unlink()

        # Pass B — orphan attachments from one-shot download jobs.
        orphan = self.env["ir.attachment"].search(
            [
                ("res_model", "=", "cloud.job"),
                ("create_date", "<", cutoff),
                "|",
                ("name", "like", "%-backup-%"),
                ("name", "like", "%-neutralized-%"),
            ]
        )
        # ``cloud.instance.backup.attachment_id`` is the only legit
        # consumer of ``-backup-`` named attachments on cloud.job;
        # exclude any that are still referenced so pass B never
        # races pass A on an in-flight backup.
        if orphan:
            referenced = Backup.search(
                [
                    ("attachment_id", "in", orphan.ids),
                ]
            ).mapped("attachment_id.id")
            orphan = orphan.filtered(lambda a: a.id not in referenced)
        if orphan:
            _logger.info(
                "Cleaning up %d orphan backup/neutralized attachment(s)",
                len(orphan),
            )
            orphan.unlink()

    # Private: the resolve endpoints and the panel's own code call it once
    # the conflict is gone; through ``call_kw`` it would queue a job past
    # the alert it waits for, still open.
    @api.private
    def unblock_and_enqueue(self):
        """Dispatch a job that waited for an alert, now that it is resolved.

        Called by the resolve endpoints (pip and addon conflicts), by a
        repository save that leaves no conflict, and by a new deploy of
        an instance whose deploy was waiting.

        Queued like ``enqueue``: the job has been sitting blocked,
        possibly for a long time, and the operator may well have started
        something else on the same target meanwhile — once this
        transaction commits it starts if the target is free and waits
        its turn otherwise, in the place its creation gives it
        (``_queue_entries``). A blocked job is never active — it was not
        dispatched, or its run ended blocked — so it cannot wait for
        itself.

        The job goes where its instance is now, and the other jobs of the
        same type waiting on the same instance are cancelled: this one
        does what they would have.

        What it writes are the queue's fields, written with ``sudo``
        (``_check_queue_control_vals``) once the caller passed the role
        gate and holds write access to the job. A caller without
        superuser rights may only unblock a job that waits for an alert:
        any other would be queued again after it ended. The job still
        runs as its creator, now with superuser rights only if the caller
        who sets it going holds them (``asked_as_superuser``): resolved by
        a consultant, the rebuild a GitHub push asked for runs without.
        """
        self.ensure_one()
        inst = self.instance_id
        # Same role gate as ``enqueue``: the unblocking user is the one
        # who sets the job going.
        self._check_job_type_allowed(
            self.job_type_id.code,
            inst.id if inst else None,
        )
        self.check_access("write")
        if not self.env.su and not self.blocked_alert_id:
            raise UserError(_("Only a job that waits for an alert can be unblocked."))
        job = self.sudo()
        # An instance that is not deployed can change host while its
        # deploy waits; the job would deploy on the old one and leave an
        # orphan stack there while the record names the new one.
        if inst and not inst.deployed and inst.host_id != self.host_id:
            job.host_id = inst.host_id
        scope = self._scope_of()
        self._lock_scopes({scope})
        if inst:
            twins = self.search([
                ("id", "!=", self.id),
                ("instance_id", "=", inst.id),
                ("job_type_id", "=", self.job_type_id.id),
                ("blocked_alert_id", "!=", False),
            ])
            # Resolving an alert dispatches one of them; the rest would
            # wait for good. Without the state hook: the queue is served
            # once this job is in it, below; the layers hear of the end.
            # Jobs of the same instance as this one, which the caller may
            # write: with ``sudo``, for the same reason.
            twins.sudo().with_context(ic_skip_state_hook=True).write(
                {"state": "cancelled", "blocked_alert_id": False},
            )
            twins._run_after_terminal("cancelled")
        job.blocked_alert_id = False
        executor_cls = executor_registry.get(self.job_type_id.code)
        if executor_cls:
            executor_cls(job_record=self, host_record=self.host_id).on_unblocked()
        job.with_context(ic_skip_state_hook=True).write({
            "state": "queued",
            "queue_head": False,
            "asked_as_superuser": self.env.su,
        })
        self._schedule_dispatch({scope})

    # ── Multi-user notifications ───────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        """Broadcast newly-created jobs so the UI updates immediately.

        Runs on the caller's cursor (HTTP / cron / orm.call) — the
        ``pg_notify`` emitted by ``_bus_send`` fires at that cursor's
        commit. State transitions are broadcast where they are recorded:
        ``queue_job_ext.write`` (terminal), the dispatcher (a waiting job
        started or cancelled) and ``abstract_executor._publish_bus``
        (while running).

        The queue's fields are refused to a caller without superuser
        rights (``_check_queue_control_vals``): a job is created through
        ``enqueue`` and its siblings (``_create_queued``). The defaults the
        caller chose are checked as what they send.
        """
        self = defaults_as_sent(self, vals_list)
        self._check_queue_control_vals(vals_list)
        records = super().create(vals_list)
        linked = self.browse([
            record.id
            for record, vals in zip(records, vals_list, strict=True)
            if vals.get("queue_job_uuid") and not vals.get("state")
        ])
        if linked:
            linked._adopt_queue_job_state()
        for record in records:
            self._broadcast_job_update(record.id)
        return records

    @api.model
    def _bus_audience(self, job):
        """Return the internal users allowed to read *job*.

        The bus does not apply record rules, so the audience is the
        gate: we resolve who may read the job through ``_job_visible_to``
        (the same helper the email and external notifiers use, so the
        ``cloud.job`` ir.rule stays the single source of truth) and send
        only to them. Two things follow from that. A stakeholder stops
        receiving events for the six projects they are not a member of,
        and the payload can safely name the job's target — every
        recipient has already passed the ACL for it.

        Resolving this costs one ``check_access`` per candidate user, so
        callers on a repeating path (the executor tick) should compute
        it once and pass the ids back in. See
        ``AbstractExecutor._publish_bus``.

        :param job: a single ``cloud.job`` record.
        :returns: ``res.users`` recordset.
        """
        return (
            self.env["res.users"]
            .search([("share", "=", False), ("active", "=", True)])
            .filtered(lambda u: self._job_visible_to(job, u))
        )

    @api.model
    def _broadcast_job_update(
        self, job_id, state=None, audience_ids=None, kind=None, system=False,
    ):
        """Send bus notification for a job state change to its audience.

        Hidden job types (host_metrics, instance_health, docker_prune,
        …) are system/cron jobs the SPA never shows in the drawer.
        Broadcasting them is pure noise — tens of thousands of bus
        events per day × N internal users for zero UI benefit — so we
        skip them here. The frontend already filters these types out
        of the job list; skipping the bus hop saves a lot of work on
        the server, network and client.

        :param kind: what happened: ``created``, ``progress`` (the running
            job wrote log lines) or ``state`` (it ended). Inferred from
            ``state`` when not given: with one, ``state``; without,
            ``created``. ``_publish_bus`` passes ``progress``.
        :param bool system: for ``progress``, whether the lines include a
            message of the executor's own (a step done, a warning): what
            a job list shows as the job's last message. Output lines
            change nothing a list renders.
        :param audience_ids: precomputed ``res.users`` ids from
            ``_bus_audience``. Pass it from repeating callers to avoid
            re-running the per-user ACL check on every event; omit it
            for one-shot broadcasts (create, terminal transition).
        """
        job = self.browse(job_id)
        if not job.exists():
            return
        if job.job_type_id.code in self._get_hidden_job_types():
            return
        users = (
            self.env["res.users"].browse(audience_ids)
            if audience_ids is not None
            else self._bus_audience(job)
        )
        if not users:
            return
        # ``state`` lets the client toast terminal transitions without
        # polling: it is only present when the caller knows it
        # (queue_job_ext passes the terminal state it just wrote);
        # create/chunk-flush broadcasts omit it and the client ignores
        # them. Passing it explicitly instead of reading ``job.state``
        # here keeps an ORM read off the hot chunk-flush path.
        #
        # ``host_id`` / ``instance_id`` / ``project_id`` name the job's
        # target so a watcher can drop an event that does not concern it
        # without a round-trip — instance_detail used to spend one
        # ``load_jobs`` call per event purely to learn this. Shipping
        # them is safe precisely because the audience above is
        # ACL-filtered; do not widen the payload without keeping that
        # gate.
        #
        # ``project_id`` is a traversal, not a field: host-level jobs
        # have no instance and therefore no project, and they report
        # ``None``. Watchers must read that as "unknown, refresh
        # anyway" rather than "not mine" — a host job still changes the
        # host rows a project view renders.
        #
        # ``kind`` is what lets a watcher ignore the storm. A running job
        # publishes every half second it wrote output, and every page
        # used to answer each one with a full refetch: the host page
        # reloaded its data and its jobs twice a second through a deploy
        # of an instance it does not even list (QA, 20-oct-2026). A
        # ``progress`` event changes a list only when it carries a
        # system message (``system``); the live log page wants them all.
        payload = {
            "id": job_id,
            "host_id": job.host_id.id or None,
            "instance_id": job.instance_id.id or None,
            "project_id": job.instance_id.project_id.id or None,
            "kind": kind or ("state" if state else "created"),
        }
        if kind == "progress":
            payload["system"] = bool(system)
        if state:
            payload["state"] = state
        for user in users:
            user._bus_send("cloud_jobs", payload)

    @api.model
    def _notify_by_email(self, job, state):
        """Email active internal users according to their notification prefs.

        Skips:
          * ``cancelled`` — a user-initiated non-event; the preference
            labels only promise success + failure.
          * Hidden job types (``host_metrics``, …) — they run on cron
            every few minutes, so a persistent failure (e.g. host down)
            would email every subscriber per tick. The bus and alert
            channels already filter them for the same reason.
        """
        if state == "cancelled":
            return
        if job.job_type_id.code in self._get_hidden_job_types():
            return
        # Start from every subscribed internal user, then narrow to
        # what each may actually read (the cloud.job record rules —
        # single source of truth, not a duplicated membership domain)
        # and hasn't muted. A user who cannot see the job in the UI
        # must not learn about it by email.
        users = (
            self.env["res.users"]
            .search(
                [
                    ("share", "=", False),
                    ("active", "=", True),
                    ("cloud_notification_level", "!=", "none"),
                    ("cloud_email_enabled", "=", True),
                ]
            )
            .filtered(
                lambda u: self._job_visible_to(job, u)
                and not self._job_muted_for(job, u)
            )
        )
        # Digest users only get instant mail for severe failures
        # (production deploys/rebuilds); everything else reaches them
        # in the daily digest instead.
        severe = (
            state == "failed" and job.job_type_id.code in self._get_severe_job_types()
        )
        if not severe:
            users = users.filtered(
                lambda u: u.cloud_notification_mode != "daily_digest",
            )
        for user in users:
            # Only ``all`` hears about jobs that did not fail; the
            # failure levels differ in alerts, not in jobs.
            if user.cloud_notification_level != "all" and state != "failed":
                continue
            email = user.partner_id.email
            if not email:
                continue
            state_label = "completed" if state == "done" else state
            # Text, on one line: whoever may write the job writes its
            # name, and a line break would end the mail's header there.
            name = " ".join((job.name or "").split())
            subject = f"[IncubaCloud] Job '{name}' {state_label}"
            body = wrap_in_mail_layout(
                self.env, self._build_email_body(job, state),
            )
            self.env["mail.mail"].sudo().create(
                {
                    "subject": subject,
                    "body_html": body,
                    "email_to": email,
                    "auto_delete": True,
                }
            ).send()

    @api.model
    def _job_visible_to(self, job, user):
        """Return True when *user* passes ACL + record rules to read
        *job* — used to scope job emails to what the UI would show.
        """
        try:
            job.with_user(user).check_access("read")
        except AccessError:
            return False
        return True

    @api.model
    def _job_muted_for(self, job, user):
        """Return True when the job's project is muted by *user* —
        shared by email, digest and the live-toast brief endpoint so
        every personal channel honours the same mute list.
        """
        project = job.instance_id.project_id if job.instance_id else None
        return user._cloud_project_muted(project)

    @api.model
    def _build_email_body(self, job, state):
        # Everything interpolated is text, never markup, as in the alerts'
        # mail (``cloud.alert._build_alert_email_body``): whoever may write
        # the job writes its name, and a link forged into it must not be
        # live in the mail of everyone who follows the project.
        instance_line = (
            f"<tr><td><b>Instance</b></td><td>{escape(job.instance_id.name)}</td></tr>"
            if job.instance_id
            else ""
        )
        log_url = f"/cloud/log/{job.id}"
        state_label = "completed" if state == "done" else state
        return (
            f"<table>"
            f"<tr><td><b>Job</b></td><td>{escape(job.name or '')}</td></tr>"
            f"<tr><td><b>Host</b></td><td>{escape(job.host_id.name)}</td></tr>"
            f"{instance_line}"
            f"<tr><td><b>Status</b></td><td>{state_label}</td></tr>"
            f"</table>"
            f"<p><a href='{log_url}'>View logs</a></p>"
        )

    @api.model
    def _notify_external(self, job, state):
        """Push job notifications to Telegram and/or external webhooks.

        Runs the same user filter as ``_notify_by_email``: only active
        internal users who are subscribed, can see the job via record
        rules, and haven't muted its project. Digest-mode users only
        receive severe failures immediately — everything else waits for
        the daily digest, matching the email delivera-bility promise.

        Telegram and webhook sends each run inside a try/except so one
        broken endpoint can never starve another user's notification.

        In test mode the pipeline is disabled by default — real
        HTTP calls would spill credentials to external APIs.  Tests
        that intentionally exercise the pipeline pass
        ``with_context(test_external_notify=True)`` on the recordset.
        """
        if odoo_config.get("test_enable") and not self.env.context.get(
            "test_external_notify"
        ):
            return
        if state == "cancelled":
            return
        if job.job_type_id.code in self._get_hidden_job_types():
            return
        users = (
            self.env["res.users"]
            .search(
                [
                    ("share", "=", False),
                    ("active", "=", True),
                    ("cloud_notification_level", "!=", "none"),
                ]
            )
            .filtered(
                lambda u: self._job_visible_to(job, u)
                and not self._job_muted_for(job, u)
            )
        )
        severe = (
            state == "failed" and job.job_type_id.code in self._get_severe_job_types()
        )
        if not severe:
            users = users.filtered(
                lambda u: u.cloud_notification_mode != "daily_digest",
            )
        for user in users:
            # Only ``all`` hears about jobs that did not fail; the
            # failure levels differ in alerts, not in jobs.
            if user.cloud_notification_level != "all" and state != "failed":
                continue
            if (user._get_cloud_telegram_bot_token()
                    and user.cloud_telegram_chat_id):
                try:
                    self._send_telegram(user, job, state, severe)
                except Exception:
                    _logger.warning(
                        "telegram send failed for user %s",
                        user.id,
                    )
            if user.cloud_webhook_url:
                try:
                    self._send_webhook(user, job, state, severe)
                except Exception:
                    _logger.warning(
                        "webhook send failed for user %s",
                        user.id,
                    )

    @api.model
    def _send_telegram(self, user, job, state, severe):
        """POST a job state notification to the user's Telegram chat.

        Uses the Bot API ``sendMessage`` endpoint. Failure is logged
        but never propagated — a bad token or connectivity blip must
        not abort the notification pipeline for other users. What is
        interpolated is text, never Markdown (``telegram_markdown``):
        whoever may write the job writes its name.
        """
        base_url = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(
                "web.base.url",
                "",
            )
        )
        emoji = "\U0001f534" if state == "failed" else "\U0001f7e2"
        severity_tag = " \u26a0\ufe0f SEVERE" if severe else ""
        lines = [
            f"{emoji} {telegram_markdown(job.name, bold=True)} {state}{severity_tag}",
            f"Host: {telegram_markdown(job.host_id.name)}",
        ]
        if job.instance_id:
            lines.append(f"Instance: {telegram_markdown(job.instance_id.name)}")
        lines.append(f"Logs: {telegram_markdown(base_url)}/cloud/log/{job.id}")
        text = "\n".join(lines)
        token = user._get_cloud_telegram_bot_token()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": user.cloud_telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
        ).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        safe_urlopen(req, timeout=10)  # nosec B310 — Telegram API, no redirects

    @api.model
    def _send_webhook(self, user, job, state, severe):
        """POST a signed JSON notification to the user's webhook URL.

        The ``X-IncubaCloud-Signature`` header carries the HMAC-SHA256
        hex digest so the receiver can verify authenticity and integrity.
        When *user* has no signing secret the header is omitted — basic
        HTTPS alone serves as the authenticity channel.
        """
        base_url = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param(
                "web.base.url",
                "",
            )
        )
        payload = json.dumps(
            {
                "event": "job_state_change",
                "job_id": job.id,
                "job_name": job.name,
                "state": state,
                "severe": severe,
                "host": job.host_id.name,
                "instance": job.instance_id.name if job.instance_id else None,
                "log_url": f"{base_url}/cloud/log/{job.id}",
                "timestamp": fields.Datetime.to_string(fields.Datetime.now()),
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        secret = user._get_cloud_webhook_secret() or ""
        if secret:
            sig = hmac.new(
                secret.encode(),
                payload,
                hashlib.sha256,
            ).hexdigest()
            headers["X-IncubaCloud-Signature"] = "sha256=" + sig
        # The destination is the only one in this codebase a user picks,
        # and this process can reach the metadata service, its own
        # loopback and the internal database. ``post_json`` re-resolves
        # and pins the address; see :mod:`..net.outbound` for why the
        # https check at save time was never enough on its own.
        post_json(user.cloud_webhook_url, payload, headers=headers)

    # ── Audit trail ───────────────────────────────────────────────────────

    # Private: ``/cloud/get_audit_log`` serves it; nothing calls it
    # through ``call_kw``.
    @api.private
    @api.model
    def get_audit_log(
        self,
        instance_id=None,
        host_id=None,
        limit=100,
        offset=0,
        q=None,
        action_filter=None,
        date_from=None,
        date_to=None,
    ):
        """Return a page of audit log entries for an instance or host.

        Returns ``{'entries', 'total', 'actions'}``: ``total`` drives the
        paginator and ``actions`` is the full set of distinct action names
        for the target (ignoring the current filters) so the filter
        dropdown stays complete across pages.
        """
        empty = {"entries": [], "total": 0, "actions": []}
        if not self.env.user.has_group("incubacloud.group_cloud_manager"):
            return empty
        if instance_id:
            base = [("instance_id", "=", int(instance_id))]
        elif host_id:
            base = [("host_id", "=", int(host_id))]
        else:
            return empty
        domain = list(base)
        if q:
            domain += [
                "|",
                ("user_id.name", "ilike", q),
                ("action", "ilike", q),
            ]
        if action_filter:
            domain.append(("action", "=", action_filter))
        if date_from:
            domain.append(("create_date", ">=", date_from))
        if date_to:
            domain.append(("create_date", "<=", date_to))
        Log = self.env["cloud.audit.log"]
        total = Log.search_count(domain)
        entries = Log.search(
            domain,
            order="id desc",
            limit=limit,
            offset=offset,
        )._format()
        actions = [a for (a,) in Log._read_group(base, groupby=["action"]) if a]
        return {"entries": entries, "total": total, "actions": actions}
