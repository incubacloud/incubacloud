import asyncio
import contextlib
import logging
import re
from abc import ABC, abstractmethod

from .registry import executor_registry
from ._repo_requirements import has_pip_conflicts

_TOKEN_RE = re.compile(r'https://[^@\s]*@(github\.com)', re.IGNORECASE)


def _redact_tokens(text):
    """Remove embedded credentials from GitHub HTTPS URLs in log lines."""
    return _TOKEN_RE.sub(r'https://\1', text)


class AbstractExecutor(ABC):

    _job_type = None  # required in subclasses

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
                    if opts.get('stop_on_failure') and result.exit_status != 0:
                        self._sys(
                            f"✗ '{label}' failed (exit {result.exit_status})"
                            f" — aborting remaining steps."
                        )
                        break

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
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            Log = env['cloud.job.log.chunk'].sudo()
            for line, source in entries:
                Log.create({
                    'job_id': self.job.id,
                    'content': _redact_tokens(line),
                    'source': source,
                })

    def _publish_bus(self):
        logger = logging.getLogger("AbstractExecutor")
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            user = env.user
            partner = user.partner_id
            logger.debug(
                "[_publish_bus] job_id=%s user=%s (id=%s) partner=%s (id=%s)",
                self.job.id, user.login, user.id, partner.name, partner.id,
            )
            user._bus_send('cloud_jobs', {'id': self.job.id})
            logger.debug(
                "[_publish_bus] _bus_send done for job_id=%s", self.job.id,
            )

    def _check_cancel(self):
        with self.job.env.registry.cursor() as cr:
            job = self.job.env(cr=cr)['cloud.job'].browse(self.job.id)
            if job.state == 'cancelled':
                raise Exception("Job cancelled")

    # ===============================
    # STREAM HANDLERS (OVERRIDABLE)
    # ===============================

    async def on_stdout(self, line):
        self._log_buffer.append((line, "stdout"))

    async def on_stderr(self, line):
        self._log_buffer.append((line, "stderr"))

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
