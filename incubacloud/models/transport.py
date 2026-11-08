"""Transport abstraction for remote command execution and file transfer.

``BaseTransport`` defines the interface that all backends must implement.
``SSHTransport`` is the asyncssh-backed implementation used today.

Adding a new backend (e.g. Kubernetes, GCP) requires only:
  1. A new ``XTransport(BaseTransport)`` class here.
  2. A ``backend_type`` field on ``cloud.host`` and a branch in
     ``cloud.host.get_transport()`` to return the right transport.
  3. Zero changes to existing executors.
"""
import asyncio
import contextlib
from abc import ABC, abstractmethod
from typing import NamedTuple

import asyncssh

# Default per-command SSH timeout. Long enough for big deploys and
# heavy backup restores; short enough that a runaway command (infinite
# loop, hung process) eventually surfaces as a clear timeout instead
# of pinning the worker forever. Executors may override per command.
DEFAULT_SSH_COMMAND_TIMEOUT = 3600  # 1 hour

# How long a cancelled command is given to end once it was sent TERM, in
# seconds: what a script's EXIT trap takes to put the stack back (the boot
# test's took 1.6 s, measured on 8-nov-2026). Kept far below the
# five-minute tick of the lost-job reconciliation and the ``limit_time_real``
# of the workers that run jobs (3600 s on the platform's job runner, which
# overrides the 1200 s its web workers keep), so the wait never turns the
# job into a lost worker. The executor's loop is not ticking meanwhile: nothing reaches the
# job's log (``_flush_logs``) until the wait is over, then all of it does.
CANCEL_TERM_WAIT_S = 60


class CommandResult(NamedTuple):
    stdout: str
    exit_status: int


class StagingPathTaken(RuntimeError):
    """A file that has to be created owner-only finds its path taken.

    Another account holds something there that this one may not remove,
    or put something there between the removal and the creation. The
    secret is not written; the message names the path, so whoever reads
    the job log knows what to remove on the host.
    """

    def __init__(self, path, why):
        """Name *path* and say *why* it could not be used.

        :param str path: the remote path that was taken
        :param str why: what was found there
        """
        super().__init__(
            f"Could not stage {path} on the host: {why}. A secret is"
            " only ever written into a file this job creates; remove"
            " what is there and run the job again."
        )
        self.path = path


class BaseTransport(ABC):
    """Protocol for executing commands and transferring files on a remote target."""

    @abstractmethod
    async def execute(
        self, command: str, on_stdout, on_stderr,
        timeout: float = DEFAULT_SSH_COMMAND_TIMEOUT,
    ) -> CommandResult:
        """Run *command*, streaming each output line to callbacks.

        ``on_stdout`` and ``on_stderr`` are async callables ``(line: str) -> None``.
        Used by the main executor loop so logs appear in real time.

        Aborts with ``asyncio.TimeoutError`` if the command is still
        running after *timeout* seconds.

        Cancelled (the executor stops a job a person cancelled), it sends
        the remote command TERM, waits up to ``CANCEL_TERM_WAIT_S`` for it
        to end, whatever it writes meanwhile still reaching the callbacks,
        and raises the ``CancelledError`` again. Without the signal the
        command went on after the connection closed until it next wrote
        to its output (8-nov-2026).
        """

    @abstractmethod
    async def run(
        self, command: str,
        timeout: float = DEFAULT_SSH_COMMAND_TIMEOUT,
    ) -> CommandResult:
        """Run *command* and capture its output without streaming.

        Intended for short setup / cleanup commands where real-time logging is
        not needed.  Callers check ``result.exit_status`` themselves.

        Aborts with ``asyncio.TimeoutError`` if the command is still
        running after *timeout* seconds. Cancelled, it stops the command
        as ``execute`` does.
        """

    @abstractmethod
    async def upload_text_files(self, files: dict, mode=None) -> None:
        """Write text files to the remote target.

        *files* is ``{remote_path: text_content}``. *mode*, when given,
        is the permission bits a file is created with (``0o600`` for a
        secret, so it is never readable by others, not even briefly):
        whatever is at the path is removed first and the file is created
        by this call, never written into an existing one, whose mode
        would stay as it was.
        """

    @abstractmethod
    async def upload_file(self, local_path: str, remote_path: str) -> None:
        """Upload a single local file to *remote_path*."""

    @abstractmethod
    async def upload_dir(self, local_path: str, remote_path: str) -> None:
        """Upload a local directory tree recursively to *remote_path*."""

    @abstractmethod
    async def download_file(self, remote_path: str, local_path: str) -> None:
        """Download *remote_path* to *local_path*."""


async def _stop_cancelled(process, drive=None):
    """Send a cancelled command TERM and give it a while to end.

    OpenSSH (7.9 and later) delivers the request to the session's whole
    process group: the script, its trap and its foreground children
    (measured on 8-nov-2026). Not in it, and going on: what a ``docker
    exec`` runs inside a container, a ``timeout`` inside a compound step
    (it takes a group of its own), and the container of an attached
    ``docker run`` whose PID 1 ignores TERM, unless the script's trap
    removes it. OpenSSH 8.9 ignores it for a session of
    root, which has no unprivileged child to send it from. Closing the
    connection instead signals nothing. Raises nothing: a channel already
    closed, or a command that outlives the wait, is left to the
    connection's close.

    :param process: the ``SSHClientProcess``, or None when the
        cancellation came before it existed
    :param drive: the task reading its output, given the same wait;
        cancelled if it outlives it
    """
    if process is None:
        return
    with contextlib.suppress(Exception):
        process.terminate()
    waited = drive if drive is not None else process.wait()
    # The stop goes on whatever the wait meets, its timeout included.
    with contextlib.suppress(Exception):
        await asyncio.wait_for(waited, timeout=CANCEL_TERM_WAIT_S)


class SSHTransport(BaseTransport):
    """asyncssh-backed transport.

    Wraps an open ``asyncssh.SSHClientConnection`` and exposes the
    ``BaseTransport`` interface.  The connection lifecycle (open / close)
    is managed by ``cloud.host.get_transport()``, not here.
    """

    def __init__(self, conn):
        self._conn = conn

    async def execute(
        self, command: str, on_stdout, on_stderr,
        timeout: float = DEFAULT_SSH_COMMAND_TIMEOUT,
    ) -> CommandResult:
        stdout_lines = []
        # Nothing to signal while the session is being opened: a
        # cancellation there raises before ``process`` exists.
        process = await self._conn.create_process(command)

        async def _read_stdout():
            async for line in process.stdout:
                line = line.rstrip()
                if line:
                    await on_stdout(line)
                    stdout_lines.append(line)

        async def _read_stderr():
            async for line in process.stderr:
                line = line.rstrip()
                if line:
                    await on_stderr(line)

        async def _drive():
            await asyncio.gather(_read_stdout(), _read_stderr())
            await process.wait()

        # Shielded, so a cancellation leaves the readers running: what
        # the command's trap writes once it is sent TERM reaches the log.
        drive = asyncio.ensure_future(_drive())
        try:
            await asyncio.wait_for(asyncio.shield(drive), timeout=timeout)
        except asyncio.TimeoutError:
            drive.cancel()
            # Kill the remote process so SSH closes cleanly and we
            # don't leak server-side state. Re-raise so the executor
            # treats it as a failure.
            with contextlib.suppress(Exception):
                process.terminate()
            await on_stderr(
                f'⚠ Command exceeded {int(timeout)}s timeout — killed.'
            )
            raise
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await on_stderr(
                    "⚠ Stopped: the command running on the server was"
                    " sent TERM."
                )
            await _stop_cancelled(process, drive)
            raise
        return CommandResult(
            stdout='\n'.join(stdout_lines),
            exit_status=process.exit_status,
        )

    async def run(
        self, command: str,
        timeout: float = DEFAULT_SSH_COMMAND_TIMEOUT,
    ) -> CommandResult:
        # ``conn.run`` is ``create_process`` and ``wait``: done here, so a
        # cancelled command has a process to be sent TERM.
        process = None
        try:
            process = await self._conn.create_process(command)
            result = await asyncio.wait_for(
                process.wait(check=False), timeout=timeout,
            )
        except asyncio.CancelledError:
            await _stop_cancelled(process)
            raise
        return CommandResult(
            stdout=(result.stdout or '').strip(),
            exit_status=result.exit_status,
        )

    async def upload_text_files(self, files: dict, mode=None) -> None:
        attrs = (
            asyncssh.SFTPAttrs(permissions=mode) if mode is not None
            else asyncssh.SFTPAttrs()
        )
        # The server applies the mode only to a file it creates. With a
        # mode, a copy an earlier run left (0644, before secrets were
        # staged this way) is removed, and the create is exclusive: a
        # file of another account in /tmp, which this one cannot remove,
        # fails the upload instead of receiving the secret.
        flags = 'w' if mode is None else 'x'
        async with self._conn.start_sftp_client() as sftp:
            for path, content in files.items():
                if mode is not None:
                    await self._clear_for_private(sftp, path)
                created = False
                try:
                    async with sftp.open(path, flags, attrs) as fh:
                        created = True
                        await fh.write(content)
                except asyncssh.SFTPError as exc:
                    # Only the exclusive create is about the path; a
                    # write that fails afterwards is the server's story.
                    if mode is None or created:
                        raise
                    raise StagingPathTaken(
                        path, "another account created it as it was cleared",
                    ) from exc

    @staticmethod
    async def _clear_for_private(sftp, path):
        """Remove what is at *path* before it is created owner-only.

        :param sftp: the open SFTP client
        :param str path: the remote path about to be created
        :raise StagingPathTaken: when something is there this account may
            not remove (another account's file in a sticky ``/tmp``) or
            cannot (a directory)
        """
        try:
            await sftp.remove(path)
        except asyncssh.SFTPNoSuchFile:
            return
        except asyncssh.SFTPPermissionDenied as exc:
            raise StagingPathTaken(
                path, "another account holds it",
            ) from exc
        except asyncssh.SFTPError as exc:
            raise StagingPathTaken(
                path, f"something there cannot be removed ({exc})",
            ) from exc

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        async with self._conn.start_sftp_client() as sftp:
            await sftp.put(local_path, remote_path)

    async def upload_dir(self, local_path: str, remote_path: str) -> None:
        async with self._conn.start_sftp_client() as sftp:
            await sftp.put(local_path, remote_path, recurse=True)

    async def download_file(self, remote_path: str, local_path: str) -> None:
        async with self._conn.start_sftp_client() as sftp:
            await sftp.get(remote_path, local_path)
