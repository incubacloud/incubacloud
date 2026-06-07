import asyncio
import contextlib
import errno
import logging
import re
import socket
from abc import ABC, abstractmethod

import asyncssh

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


def _redact_tokens(text):
    """Remove embedded credentials from GitHub HTTPS URLs in log lines."""
    return _TOKEN_RE.sub(r'https://\1', text)


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
                    self._flush_logs()
                    self._publish_bus()
                    self._check_cancel()
                # Drain anything written between the last tick and
                # the final await inside _async_entry.
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

                results = {}
                for item in self.get_commands():
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

                errors = self.parse_results(results)
                if not errors:
                    await self.after_commands(transport, results)
                    logger.debug(
                        "[_async_entry] on_success START job_id=%s",
                        self.job.id,
                    )
                    with self.job.env.registry.cursor() as cr:
                        _orig_job, _orig_env = self.job, self.env
                        self.job = self.job.env(cr=cr)[
                            'cloud.job'
                        ].browse(self.job.id)
                        self.env = self.job.env
                        try:
                            await self.on_success(results)
                        finally:
                            self.job, self.env = _orig_job, _orig_env
                    logger.debug(
                        "[_async_entry] on_success END job_id=%s",
                        self.job.id,
                    )
                else:
                    logger.debug(
                        "[_async_entry] on_failure START job_id=%s errors=%s",
                        self.job.id, errors,
                    )
                    with self.job.env.registry.cursor() as cr:
                        _orig_job, _orig_env = self.job, self.env
                        self.job = self.job.env(cr=cr)[
                            'cloud.job'
                        ].browse(self.job.id)
                        self.env = self.job.env
                        try:
                            await self.on_failure(results, errors)
                        finally:
                            self.job, self.env = _orig_job, _orig_env
                    logger.debug(
                        "[_async_entry] on_failure END job_id=%s",
                        self.job.id,
                    )
                    raise RuntimeError("; ".join(errors))

            logger.debug("[_async_entry] Finishing _async_entry")
        except Exception as e:
            logger.exception("[_async_entry] Exception: %s", e)
            self._sys(f"✗ {type(e).__name__}: {e}")
            raise

    # ===============================
    # DB + BUS INFRASTRUCTURE
    # ===============================

    def _sys(self, msg):
        """Append a system-level progress message to the log buffer."""
        self._log_buffer.append((msg, "system"))

    def _flush_logs(self):
        if not self._log_buffer:
            return
        entries = list(self._log_buffer)
        self._log_buffer.clear()
        # If we already emitted the cap marker, drop everything past
        # this point silently. The job keeps running but stops bloating
        # the logs table.
        if self._cap_marker_emitted:
            return
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
                    break
                Log.create({
                    'job_id': self.job.id,
                    'content': _redact_tokens(line),
                    'source': source,
                })
                self._chunks_persisted += 1

    def _publish_bus(self):
        """Notify every watcher that new chunks are available.

        The executor's ``env.user`` is the queue_job worker identity
        (often OdooBot or SUPERUSER), *not* the user watching the
        terminal page. Sending to ``env.user`` only reaches that
        worker's channel, so real operators fall back to the 10 s
        polling loop and the live stream looks stuck.

        We delegate to ``cloud.job._broadcast_job_update`` — the same
        helper used by create() and queue_job_ext state transitions —
        so every active internal user receives the notification on
        their own presence channel. Hidden job types (host_metrics,
        docker_prune, instance_health) stay filtered out inside that
        helper.
        """
        logger = logging.getLogger("AbstractExecutor")
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            env['cloud.job']._broadcast_job_update(self.job.id)
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
        """Create or refresh an active alert for this job's host."""
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            Alert = env['cloud.alert']
            existing = Alert.search([
                ('host_id', '=', self.job.host_id.id),
                ('code', '=', code),
                ('state', '=', 'active'),
            ], limit=1)
            if existing:
                existing.write({
                    'message': message,
                    'level': level,
                    'job_id': self.job.id,
                })
            else:
                Alert.create({
                    'host_id': self.job.host_id.id,
                    'code': code,
                    'message': message,
                    'level': level,
                    'job_id': self.job.id,
                })

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
            env['cloud.alert'].search([
                ('host_id', '=', self.job.host_id.id),
                ('code', '=', code),
                ('state', '=', 'active'),
            ]).write({'state': 'dismissed'})

    # ===============================
    # PATH HELPERS
    # ===============================

    def _inst_dir(self, inst):
        """Return the remote directory for an instance."""
        return inst.get_remote_dir()

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

        results: dict of {label: {'stdout': str, 'exit_status': int}}
        """
        return []

    async def on_success(self, results):
        """Called when parse_results returns an empty list."""

    async def on_failure(self, results, errors):
        """Called when parse_results returns one or more error strings."""


# Backward-compat alias — kept so existing executor imports don't break.
AbstractSSHExecutor = AbstractExecutor
