"""``HostTerminalSession`` — SSH + PTY login shell on a whole ``cloud.host``.

Thin subclass of ``BaseTerminalSession`` (see ``terminal_session_base``)
that adds only the host-specific ``_open_process`` hook. Unlike the
instance terminal, this opens a *command-less* PTY: asyncssh runs the SSH
user's login shell, exactly what ``ssh user@host`` gives you.

Security note — this is the ONLY place in the addon that opens a
command-less PTY, and the hygiene test in ``tests/test_terminal_session``
pins that. It grants nothing the caller does not already hold: the
endpoint in front of it is reserved to whoever manages hosts, who already
runs root on them through every host job, with the same key. Since no
command string is built from the user, command injection is impossible
by construction.

Imported as a flat top-level module by the per-session subprocess, which
puts this addon's dir on ``sys.path`` first.
"""

from terminal_session_base import SESSION_TIMEOUT, BaseTerminalSession

__all__ = ['HostTerminalSession', 'SESSION_TIMEOUT']


class HostTerminalSession(BaseTerminalSession):
    """Interactive SSH login shell against a ``cloud.host``."""

    _THREAD_PREFIX = 'host-terminal'

    def __init__(
        self,
        session_id,
        ssh_connect_kwargs,
        host_label='',
        user_id=None,
        welcome_banner='',
    ):
        """Store the host label, then start the worker thread."""
        super().__init__(
            session_id,
            ssh_connect_kwargs,
            user_id=user_id,
            welcome_banner=welcome_banner,
        )
        self.host_label = host_label
        self.start()

    async def _open_process(self, conn):
        """Open a login shell on the host — a command-less PTY.

        Passing no ``command`` makes asyncssh run the SSH user's default
        login shell (same as ``ssh user@host``). No user-supplied string
        is interpolated, so command injection is impossible here.
        """
        return await conn.create_process(
            request_pty=True,
            term_type='xterm-256color',
            term_size=(80, 24),
            encoding=None,  # raw bytes — xterm.js handles encoding
        )
