import asyncio
import contextlib
import errno
import logging
import re
import shlex
import socket
from abc import ABC, abstractmethod
from pathlib import Path

import asyncssh

from odoo.tools import file_path

from .registry import executor_registry
from ._repo_requirements import has_pip_conflicts

# Delay before queue_job re-runs a job that failed to connect to its host.
# Short enough that a momentary blip recovers quickly, long enough that we
# don't hammer a host that is briefly rebooting.
CONNECTION_RETRY_SECONDS = 30

# Errnos that mark a transient, retryable connection problem (host
# momentarily unreachable / refused / reset / rebooting), as opposed to a
# permanent misconfiguration.
_TRANSIENT_ERRNOS = frozenset({
    errno.ETIMEDOUT, errno.ECONNREFUSED, errno.ECONNRESET,
    errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN,
})


def is_transient_connection_error(exc):
    """Return True if ``exc`` is a transient SSH/network connection failure.

    These are worth retrying because the host may simply be momentarily
    unreachable (network blip, sshd briefly busy, VM rebooting). Permanent
    failures — wrong credentials (``asyncssh.PermissionDenied``) or an
    unverifiable host key (``asyncssh.HostKeyNotVerifiable``) — are
    deliberately *not* matched here: they will never succeed on retry, so
    they must fail fast.

    Matches:
      * ``asyncssh.ConnectionLost`` — the connection dropped mid-handshake
        or was lost after establishment (the ``Connection lost`` case).
      * builtin ``TimeoutError`` / ``ConnectionError`` and
        ``socket.gaierror`` (DNS) — raised when asyncssh's underlying
        socket connect fails (the ``Connect call failed`` case).
      * any ``OSError`` whose errno is a known transient connection errno.
    """
    if isinstance(exc, asyncssh.ConnectionLost):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError, socket.gaierror)):
        return True
    if isinstance(exc, OSError) and exc.errno in _TRANSIENT_ERRNOS:
        return True
    return False

_TOKEN_RE = re.compile(r'https://[^@\s]*@(github\.com)', re.IGNORECASE)

# Whitelist of patterns that mark a log line as an error. Lines matching
# any of these get ``source='stderr'`` (rendered red as ``[err]``);
# everything else is treated as benign output and stored as
# ``source='stdout'`` regardless of which SSH channel it came from.
#
# This exists because many tools we drive over SSH (docker compose,
# duplicity, click-odoo-*, Odoo's own logging) write progress and
# WARNING/INFO/DEBUG to stderr, which used to flood the log viewer with
# red ``[err]`` lines that weren't actually errors. We pair this
# whitelist with a `[sys]` breadcrumb emitted on any non-zero exit code,
# so true failures stay visible even if their output doesn't match a
# pattern here.
_ERR_PATTERNS = (
    re.compile(r'\b(ERROR|FATAL|CRITICAL)\b'),
    re.compile(r'^Traceback \(most recent call last\)'),
    re.compile(r'^\s+File ".+", line \d+'),
    re.compile(r'^[a-z][a-z]+: cannot '),
    re.compile(
        r'^[a-z][a-z]+: .*: '
        r'(Operation not permitted|Permission denied'
        r'|No such file or directory)$'
    ),
    re.compile(r'^Error response from daemon:'),
    re.compile(r'^E: '),
)


def _looks_like_error(line):
    """Return True if the line matches any pattern in ``_ERR_PATTERNS``."""
    return any(p.search(line) for p in _ERR_PATTERNS)

# Per-line and per-job log caps. These bound the worst-case disk usage
# from a runaway job that writes unbounded output (e.g. an image bug
# that produces an infinite stream).
#   * MAX_LOG_LINE_LEN bounds a single line — anything past is truncated
#     with a marker so the operator sees it happened.
#   * MAX_CHUNKS_PER_JOB bounds the total chunks per cloud.job. When
#     reached, a single "cap reached" system marker is emitted and the
#     rest is silently dropped (the job continues running).
MAX_LOG_LINE_LEN = 8192
MAX_CHUNKS_PER_JOB = 20_000

# Remote directory (suffixed with the job id) where an executor uploads
# the versioned scripts it runs. Recreated from scratch for every job
# and removed once the command loop ends, so nothing lingers on the host
# between runs and two concurrent jobs never share a payload.
SCRIPT_REMOTE_ROOT = "/tmp/.incubacloud-scripts"


def _redact_tokens(text):
    """Remove embedded credentials from GitHub HTTPS URLs in log lines."""
    return _TOKEN_RE.sub(r'https://\1', text)


def handoff_archive_path(job_id):
    """Return the on-host path where a ``backup_download`` job with
    ``handoff='host'`` leaves its ZIP for the next job in the chain.

    Keyed by the *producing* job's id so the consumer (``restore_instance``
    mode ``from_host``) can recompute it from ``source_job_id`` alone, and
    so two chains touching the same instance name can never collide.
    Defined at module level because both executors must agree on it.

    :param int job_id: id of the ``cloud.job`` that produced the archive
    :return: absolute path on the producing job's host
    """
    return f"/tmp/.incubacloud-handoff-{int(job_id)}.zip"


def sql_escape_literal(s):
    """Escape a Python string for embedding in a PostgreSQL single-quoted
    SQL literal (``'...'``).

    Use when composing SQL via ``psql -c "..."`` over SSH where we cannot
    parameterise through psycopg2. Doubles every single quote per the
    PostgreSQL standard. Callers MUST still validate inputs upstream
    (e.g. with regex) — this is a defense-in-depth layer, not a
    sanitiser for arbitrary user input.
    """
    return (s or "").replace("'", "''")


# duplicity --time accepts:
#   * presets:                latest, live, now
#   * relative offsets:       12h_ago, 1Y_ago, 30m_ago…
#   * absolute (ISO 8601):    2026-03-19, 2026-03-19T02:00:00, …Z
# Anything else is rejected. The regex is the sole defense against shell
# injection via ``payload['time']`` because the value flows into
# ``--time "<value>"`` (and into ``sh -c '... "<value>" ...'`` in the
# restore executor). With this constraint the value can never contain
# whitespace, quotes, dollars, semicolons or backticks.
DUP_TIME_RE = re.compile(
    r'^('
    r'latest|live|now'
    r'|\d+[smhDWMY]_ago'
    r'|\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}Z?)?'
    r')$'
)


def validate_dup_time(raw):
    """Raise ValueError if ``raw`` is not a duplicity-safe ``--time`` value.

    Returns the value unchanged on success so callers can chain.
    """
    if not isinstance(raw, str) or not DUP_TIME_RE.match(raw):
        raise ValueError(
            f"Invalid 'time' value: {raw!r}. Expected 'latest', 'live', "
            f"'now', a relative offset like '12h_ago', or an ISO 8601 "
            f"timestamp like '2026-03-19T02:00:00'."
        )
    return raw


class AbstractExecutor(ABC):

    _job_type = None  # required in subclasses

    # Connection-retry opt-in. When True, a transient connection failure
    # (see ``is_transient_connection_error``) does not fail the job on the
    # first try: ``cloud.job.execute`` raises ``RetryableJobError`` so
    # queue_job reschedules it, up to ``_connection_retry_attempts`` total
    # attempts. Only when the *last* attempt still cannot connect is a
    # ``host_unreachable`` alert raised. This keeps momentary blips from
    # spamming notifications while still surfacing a genuinely down host.
    # Any non-connection error still fails permanently on the first try.
    _retry_on_connection_loss = False
    _connection_retry_attempts = 3

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls._job_type:
            executor_registry.register(cls._job_type, cls)

    def __init__(self, job_record, host_record, sleep_interval=0.5):
        self.job = job_record
        self.env = job_record.env
        self._log_buffer = []
        self._loop = None
        self._host_record = host_record
        self.host = host_record.ip_address
        self.port = host_record.port
        self.username = host_record.user
        self.sleep_interval = sleep_interval
        # Per-job log cap counters. ``_chunks_persisted`` is bumped
        # every time a chunk is written to ``cloud.job.log.chunk``;
        # when it reaches MAX_CHUNKS_PER_JOB we emit a single marker
        # and drop the rest. Counters live for the lifetime of this
        # executor instance — a retry creates a fresh executor so its
        # counter restarts; that is acceptable and keeps the logic
        # cheap (no per-flush COUNT(*) round-trip).
        self._chunks_persisted = 0
        self._cap_marker_emitted = False
        # Bus recipients for this run, resolved lazily on the first
        # publish and reused afterwards. See ``_publish_bus``.
        self._bus_audience_ids = None
        # Versioned-script plumbing. ``run_script()`` flips
        # ``_scripts_requested`` while ``get_commands()`` builds its
        # tuples; the overlay is then uploaded once, before the first
        # command runs. See the VERSIONED SCRIPT HELPERS section.
        self._scripts_requested = False
        self._scripts_uploaded = False
        self._script_overlay_cache = None

    # ===============================
    # PUBLIC ENTRYPOINT
    # ===============================

    def run(self):
        """Drive the SSH async workflow from a synchronous caller.

        Creates a dedicated event loop per call, runs ``_async_entry``
        as an owned task and polls the log buffer / bus / cancel flag
        every ``sleep_interval`` seconds while the task is in flight.
        Exits as soon as the task completes (no extra sleep tick).

        The event loop is always torn down cleanly: any lingering
        background tasks (asyncssh keepalives, SFTP readers) are
        cancelled and awaited before ``loop.close()`` so the queue_job
        worker log doesn't accumulate "Task was destroyed but it is
        pending" warnings across runs.
        """
        logger = logging.getLogger("AbstractExecutor")
        self._loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(self._loop)
            # Owning the task lets us inspect its status / pull its
            # exception / cancel it if the main loop bails out.
            main_task = self._loop.create_task(self._async_entry())
            try:
                while not main_task.done():
                    # Drive the loop in sleep_interval bursts. wait()
                    # returns early if the task finishes before the
                    # timeout fires, so we don't wait a full tick for
                    # nothing at the end of a fast job.
                    self._loop.run_until_complete(
                        asyncio.wait(
                            {main_task},
                            timeout=self.sleep_interval,
                        )
                    )
                    # Only notify when the tick actually produced log
                    # rows. The loop ticks every ``sleep_interval``
                    # regardless of output, so an unconditional publish
                    # turned a long silent step (a multi-minute
                    # ``docker compose build``) into a steady 2 events/s
                    # heartbeat to every watcher — each one costing the
                    # SPA a full refetch. Watchers that need a floor on
                    # latency (the standalone log page) keep their own
                    # fallback poll.
                    if self._flush_logs():
                        self._publish_bus()
                    self._check_cancel()
                # Drain anything written between the last tick and
                # the final await inside _async_entry. Published
                # unconditionally: this is the last word on the job, so
                # watchers must settle on it even if the tail was empty.
                self._flush_logs()
                self._publish_bus()
                exc = main_task.exception()
                if exc is not None:
                    raise exc
            except BaseException:
                # Cancel the running task if we abandon the loop
                # (e.g. _check_cancel raised because the job was
                # cancelled out-of-band). Wait for it to honour the
                # CancelledError before closing the loop.
                if not main_task.done():
                    main_task.cancel()
                    with contextlib.suppress(BaseException):
                        self._loop.run_until_complete(main_task)
                raise
        finally:
            try:
                pending = asyncio.all_tasks(self._loop)
                for t in pending:
                    t.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True),
                    )
            except Exception:
                logger.debug("executor cleanup error", exc_info=True)
            asyncio.set_event_loop(None)
            self._loop.close()
            self._loop = None

    # ===============================
    # ASYNC ENTRYPOINT
    # ===============================

    async def _async_entry(self):
        logger = logging.getLogger("AbstractExecutor")
        logger.debug("[_async_entry] Starting _async_entry")
        try:
            self._sys(f"Connecting to {self.host}:{self.port}...")
            async with self._host_record.get_transport() as transport:
                logger.debug("[_async_entry] Connection established")
                self._sys("Connection established.")
                await self.before_execute(transport)

                # Materialise the commands before running any of them:
                # ``run_script()`` queues its uploads while
                # ``get_commands()`` builds the tuples, so the scripts
                # have to reach the host before the first command fires.
                commands = list(self.get_commands())
                await self._upload_scripts(transport)

                results = {}
                try:
                    for item in commands:
                        label, command = item[0], item[1]
                        opts = item[2] if len(item) == 3 else {}
                        logger.debug("[_async_entry] Executing: %s", command)
                        self._sys(f"Running: {label}")
                        result = await transport.execute(
                            command, self.on_stdout, self.on_stderr,
                        )
                        results[label] = {
                            'stdout': result.stdout,
                            'exit_status': result.exit_status,
                        }
                        if result.exit_status != 0:
                            if opts.get('stop_on_failure'):
                                self._sys(
                                    f"✗ '{label}' failed"
                                    f" (exit {result.exit_status})"
                                    f" — aborting remaining steps."
                                )
                                break
                            self._sys(
                                f"✗ '{label}' exited with status"
                                f" {result.exit_status}."
                            )
                    # Inside the try so ``after_commands`` can still run
                    # a script, and in a finally so a failed job cleans
                    # up after itself too.
                    await self._dispatch_outcome(results, transport)
                finally:
                    await self._cleanup_scripts(transport)

            logger.debug("[_async_entry] Finishing _async_entry")
        except Exception as e:
            logger.exception("[_async_entry] Exception: %s", e)
            self._sys(f"✗ {type(e).__name__}: {e}")
            raise

    async def _dispatch_outcome(self, results, transport=None):
        """Run ``parse_results`` and fire the matching terminal hook.

        Both callbacks run on their own cursor so what they write is
        committed independently of the job's transaction, and the
        executor's ``job``/``env`` are restored afterwards.

        Raises ``RuntimeError`` after ``on_failure`` so the caller (and
        queue_job) sees the job as failed.

        :param dict results: ``{label: {'stdout': str, 'exit_status': int}}``
        :param transport: open transport, when the executor has one.
            ``after_commands`` needs it, so it is skipped when there is
            none (an Ansible-backed job opens no transport of its own).
        """
        logger = logging.getLogger("AbstractExecutor")
        errors = self.parse_results(results)
        hook, args = (
            (self.on_success, (results,)) if not errors
            else (self.on_failure, (results, errors))
        )
        if not errors and transport is not None:
            await self.after_commands(transport, results)
        logger.debug(
            "[_dispatch_outcome] %s START job_id=%s errors=%s",
            hook.__name__, self.job.id, errors,
        )
        with self.job.env.registry.cursor() as cr:
            _orig_job, _orig_env = self.job, self.env
            self.job = self.job.env(cr=cr)['cloud.job'].browse(self.job.id)
            self.env = self.job.env
            try:
                await hook(*args)
            finally:
                self.job, self.env = _orig_job, _orig_env
        logger.debug(
            "[_dispatch_outcome] %s END job_id=%s", hook.__name__, self.job.id,
        )
        if errors:
            raise RuntimeError("; ".join(errors))

    # ===============================
    # DB + BUS INFRASTRUCTURE
    # ===============================

    def _sys(self, msg):
        """Append a system-level progress message to the log buffer."""
        self._log_buffer.append((msg, "system"))

    def _flush_logs(self):
        """Persist the buffered lines; return how many chunks were written.

        The count is what drives the bus: ``run()`` only notifies
        watchers on a tick that actually produced rows. Counting real
        ``create`` calls rather than ``bool(entries)`` matters for a
        capped job — the branch below drains the buffer and writes
        nothing, so a buffer-based signal would keep the notification
        firing forever with no new content behind it.

        Deliberately counted here and not flagged in ``_sys`` /
        ``on_stdout`` / ``on_stderr``: several tests rebind those to
        ``_log_buffer.append``, which would bypass a flag set there.

        :returns: number of ``cloud.job.log.chunk`` rows created.
        """
        if not self._log_buffer:
            return 0
        entries = list(self._log_buffer)
        self._log_buffer.clear()
        # If we already emitted the cap marker, drop everything past
        # this point silently. The job keeps running but stops bloating
        # the logs table.
        if self._cap_marker_emitted:
            return 0
        written = 0
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            Log = env['cloud.job.log.chunk'].sudo()
            for line, source in entries:
                if self._chunks_persisted >= MAX_CHUNKS_PER_JOB:
                    Log.create({
                        'job_id': self.job.id,
                        'content': (
                            f"⚠ Log cap reached "
                            f"({MAX_CHUNKS_PER_JOB} chunks). "
                            f"Further output is discarded; "
                            f"the job continues running."
                        ),
                        'source': 'system',
                    })
                    self._cap_marker_emitted = True
                    written += 1
                    break
                Log.create({
                    'job_id': self.job.id,
                    'content': _redact_tokens(line),
                    'source': source,
                })
                self._chunks_persisted += 1
                written += 1
        return written

    def _publish_bus(self):
        """Notify every watcher that new chunks are available.

        The executor's ``env.user`` is the queue_job worker identity
        (often OdooBot or SUPERUSER), *not* the user watching the
        terminal page. Sending to ``env.user`` only reaches that
        worker's channel, so real operators fall back to the 10 s
        polling loop and the live stream looks stuck.

        We delegate to ``cloud.job._broadcast_job_update`` — the same
        helper used by create() and queue_job_ext state transitions —
        so every user allowed to read the job receives the notification
        on their own presence channel. Hidden job types (host_metrics,
        docker_prune, instance_health) stay filtered out inside that
        helper.

        The audience is resolved once and reused for the rest of the
        run: it costs an ACL check per candidate user, which is fine
        for the one-shot broadcasts but not on a path that repeats for
        every chunk flush of a long build. Project membership does not
        meaningfully change mid-job.
        """
        logger = logging.getLogger("AbstractExecutor")
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            Job = env['cloud.job']
            if self._bus_audience_ids is None:
                self._bus_audience_ids = Job._bus_audience(
                    Job.browse(self.job.id),
                ).ids
            Job._broadcast_job_update(
                self.job.id, audience_ids=self._bus_audience_ids,
            )
            logger.debug(
                "[_publish_bus] broadcast done for job_id=%s", self.job.id,
            )

    def _check_cancel(self):
        with self.job.env.registry.cursor() as cr:
            job = self.job.env(cr=cr)['cloud.job'].browse(self.job.id)
            if job.state == 'cancelled':
                raise Exception("Job cancelled")

    # ===============================
    # STREAM HANDLERS (OVERRIDABLE)
    # ===============================

    @staticmethod
    def _truncate_line(line):
        """Cap a single log line to MAX_LOG_LINE_LEN bytes.

        Anything past the cap is replaced with a visible marker so the
        operator knows the line was truncated.
        """
        if len(line) <= MAX_LOG_LINE_LEN:
            return line
        return line[:MAX_LOG_LINE_LEN] + ' …(truncated)'

    async def on_stdout(self, line):
        truncated = self._truncate_line(line)
        source = "stderr" if _looks_like_error(truncated) else "stdout"
        self._log_buffer.append((truncated, source))

    async def on_stderr(self, line):
        truncated = self._truncate_line(line)
        source = "stderr" if _looks_like_error(truncated) else "stdout"
        self._log_buffer.append((truncated, source))

    # ===============================
    # PRE-RUN CHECKS
    # ===============================

    def _write_system_direct(self, msg):
        """Write a system log chunk directly to DB (without async buffer)."""
        with self.job.env.registry.cursor() as cr:
            self.job.env(cr=cr)['cloud.job.log.chunk'].sudo().create({
                'job_id': self.job.id,
                'content': msg,
                'source': 'system',
            })

    def pre_run_checks(self):
        """Called before connecting. Raises RuntimeError to abort the job.

        Checks pip_dependencies for conflict markers.
        Subclasses may extend this with additional checks.
        """
        inst = self.job.instance_id
        if not inst:
            return
        if has_pip_conflicts(inst.pip_dependencies):
            msg = (
                "⚠ Deployment blocked: unresolved pip dependency conflicts"
                " in pip_dependencies. Open Alerts → Resolve conflicts"
                " before retrying."
            )
            self._write_system_direct(msg)
            raise RuntimeError(msg)

    # ===============================
    # ALERT HELPERS
    # ===============================

    def _alert(self, code, message, level='warning'):
        """Create or refresh an active alert for this job's host.

        The dedup rule itself lives in ``cloud.alert.raise_alert`` (one
        implementation, shared with the job-less producers such as the
        metric-rule cron). What stays here is the private cursor: an
        alert about a failure must survive the rollback of the very
        transaction that failed.
        """
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            env['cloud.alert'].raise_alert(
                code, message, level=level,
                host=env['cloud.host'].browse(self.job.host_id.id),
                job=env['cloud.job'].browse(self.job.id),
            )

    def notify_host_unreachable(self, exc, attempts):
        """Raise a host-scoped alert after connection retries are exhausted.

        Called by ``cloud.job.execute`` on the final failed attempt of a
        connection-retrying job. Creates (or refreshes) a single
        ``host_unreachable`` alert for this job's host — deduped by
        ``_alert`` — which broadcasts to the operator overview. A later
        successful probe clears it via ``_resolve_alert``.

        :param exc: the connection exception from the last attempt
        :param attempts: how many attempts were made before giving up
        """
        self._alert(
            'host_unreachable',
            f"Host unreachable: {attempts} consecutive connection attempts "
            f"failed. Last error: {type(exc).__name__}: {exc}",
            level='critical',
        )

    def _resolve_alert(self, code):
        """Dismiss any active alert with the given code for this job's host."""
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            env['cloud.alert'].resolve_alert(
                code, host=env['cloud.host'].browse(self.job.host_id.id),
            )

    # ===============================
    # PATH HELPERS
    # ===============================

    def _inst_dir(self, inst):
        """Return the remote directory for an instance."""
        return inst.get_remote_dir()

    def _base_url(self):
        """Return the full HTTPS URL for web.base.url (scheme always https).

        Empty string when the job's instance has no domain — callers must
        treat that as "leave ``web.base.url`` alone", never as "write an
        empty parameter".

        Lives here rather than on the deploy executor because three
        different jobs need the same rule: deploy and rebuild set the
        parameter on a fresh stack, and restore has to overwrite whatever
        base URL travelled inside the restored dump.

        :return: ``https://<domain>`` or ``''``
        """
        domain = (self.job.instance_id.domain or "").strip()
        if not domain:
            return ""
        if domain.startswith(("http://", "https://")):
            return domain.rstrip("/")
        return f"https://{domain}"

    # ===============================
    # VERSIONED SCRIPT HELPERS
    # ===============================

    def _script_root(self):
        """Return the remote directory this job uploads its scripts to."""
        return f"{SCRIPT_REMOTE_ROOT}-{self.job.id}"

    @classmethod
    def _addon_chain(cls):
        """Return the addon of every class in the MRO, most derived first.

        Both the ``scripts/`` overlay and the ``ansible/`` tree resolve
        through this chain: an executor sees the assets of its own addon
        *and* those of the addons its base classes come from, which is
        how a saas subclass reuses (or shadows) a core script.
        """
        addons = []
        for klass in cls.__mro__:
            parts = (klass.__module__ or "").split(".")
            if parts[:2] == ["odoo", "addons"] and len(parts) > 2:
                if parts[2] not in addons:
                    addons.append(parts[2])
        return addons

    def _addon_overlay(self, subdir, pattern="*"):
        """Return ``{relative path: absolute local path}`` for ``<addon>/<subdir>``.

        Merges that directory across every addon in ``_addon_chain()``,
        most derived first, so a subclass shadows a base-class file of
        the same relative name. Addons that don't ship the directory are
        skipped.
        """
        overlay = {}
        for addon in self._addon_chain():
            try:
                base = file_path(f"{addon}/{subdir}")
            except FileNotFoundError:
                continue
            for local in sorted(Path(base).rglob(pattern)):
                if local.is_file():
                    overlay.setdefault(
                        local.relative_to(base).as_posix(), str(local),
                    )
        return overlay

    def _script_overlay(self):
        """Return the merged ``scripts/`` overlay, cached per instance."""
        if self._script_overlay_cache is None:
            self._script_overlay_cache = self._addon_overlay("scripts", "*.sh")
        return self._script_overlay_cache

    def run_script(self, name, args=()):
        """Return the shell command that runs the versioned script *name*.

        Queues this executor's whole ``scripts/`` overlay for upload (so
        a script can ``source lib/common.sh``) and returns
        ``bash <remote>/<name> <args...>`` with every argument
        shell-quoted. Use it as the command of a ``get_commands()``
        tuple::

            ("Deploy", self.run_script("deploy.sh", [d, inst.name]))

        Passing values as *arguments* — never as text interpolated into
        the script — is what keeps remote execution injection-safe and
        the script itself reviewable and lintable in git.

        :param str name: path of the script relative to ``scripts/``
        :param args: positional arguments passed to the script
        :return: the command string to hand to ``transport.execute()``
        :raises FileNotFoundError: if no addon in the MRO ships *name*
        """
        if name not in self._script_overlay():
            raise FileNotFoundError(
                f"No versioned script named {name!r} under scripts/ of: "
                f"{', '.join(self._addon_chain())}."
            )
        self._scripts_requested = True
        command = f"bash {self._script_root()}/{name}"
        quoted = shlex.join(str(a) for a in args)
        return f"{command} {quoted}" if quoted else command

    async def _upload_scripts(self, transport):
        """Upload the queued ``scripts/`` overlay to this job's remote dir.

        No-op unless ``run_script()`` was called, and no-op again once
        the upload has happened. The directory is wiped first so a
        leftover from an interrupted run can never shadow the scripts of
        this one, and created 0700 so the payload is unreadable by other
        accounts on the host.

        ``_async_entry`` calls this between ``get_commands()`` and the
        command loop. An executor that runs a script from
        ``before_execute`` — where the upload has not happened yet —
        calls ``run_script()`` and then awaits this itself; the second
        call from ``_async_entry`` is then a no-op.
        """
        if not self._scripts_requested or self._scripts_uploaded:
            return
        root = self._script_root()
        files = {}
        dirs = {root}
        for rel, local in self._script_overlay().items():
            remote = f"{root}/{rel}"
            files[remote] = Path(local).read_text(encoding="utf-8")
            dirs.add(remote.rsplit("/", 1)[0])
        mkdir = " ".join(sorted(dirs))
        result = await transport.run(f"rm -rf {root} && mkdir -p -m 700 {mkdir}")
        if result.exit_status != 0:
            raise RuntimeError(
                f"Could not create the remote script directory {root} "
                f"(exit {result.exit_status})."
            )
        await transport.upload_text_files(files)
        self._scripts_uploaded = True
        self._sys(f"Uploaded {len(files)} script file(s) to {root}.")

    async def _cleanup_scripts(self, transport):
        """Remove this job's remote script directory (best effort).

        Runs in the ``finally`` of the command loop, so a failed job
        cleans up too. Suppresses its own errors: the connection may
        already be gone, and a cleanup failure must never mask the real
        error that is on its way up.
        """
        if not self._scripts_requested:
            return
        with contextlib.suppress(Exception):
            await transport.run(f"rm -rf {self._script_root()}")

    # ===============================
    # ABSTRACT METHODS
    # ===============================

    @abstractmethod
    def get_commands(self):
        """Return a list of (label, shell_command[, opts]) tuples to execute.

        Example::

            return [
                ("Check OS", "uname -s"),
                ("Check disk", "df -B1 --output=avail / | tail -n 1"),
                ("Critical step", "cmd", {"stop_on_failure": True}),
            ]
        """
        raise NotImplementedError

    # ===============================
    # OPTIONAL HOOKS
    # ===============================

    async def before_execute(self, transport):
        """Called after connecting, before running any command."""

    async def after_commands(self, transport, results):
        """Called after all commands succeed, before on_success.

        Override instead of ``_async_entry()`` when you need transport access
        (e.g. SFTP download) after commands finish but before the connection
        closes.
        """

    def parse_results(self, results):
        """Validate outputs. Return error strings (empty list = success).

        Default policy is fail-closed: any command that exited non-zero is
        an error, so an executor that does not override this still fails
        the job (and reaches ``on_failure``) instead of silently reporting
        success. Executors whose steps intentionally tolerate a non-zero
        exit must override this and skip those labels.

        results: dict of {label: {'stdout': str, 'exit_status': int}}
        """
        return [
            f"'{label}' exited with status {data.get('exit_status')}"
            for label, data in results.items()
            if data.get('exit_status', 1) != 0
        ]

    async def on_success(self, results):
        """Called when parse_results returns an empty list."""

    async def on_failure(self, results, errors):
        """Called when parse_results returns one or more error strings."""


# Backward-compat alias — kept so existing executor imports don't break.
AbstractSSHExecutor = AbstractExecutor
