"""Tests for the host-shell session — the one place a login shell opens.

Where ``test_terminal_session`` pins that nothing else in the addon opens
a command-less PTY, these assert the host session does, correctly: it
subclasses the shared base and hands asyncssh no command, so the SSH
user's login shell on the whole box runs.

Like the other session tests, this drives the flat import graph the
per-session subprocess uses: the session module is imported top-level,
with the addon dir on ``sys.path``.
"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import asyncssh

from odoo.modules.module import get_module_path
from odoo.tests.common import BaseCase

_CORE_DIR = get_module_path('incubacloud')
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

import host_terminal_session as host_session       # noqa: E402
import terminal_session_base as session_base       # noqa: E402


class TestHostTerminalSessionCapability(BaseCase):
    """The host session opens a login shell on top of the shared base."""

    def test_subclasses_the_shared_base(self):
        """``HostTerminalSession`` extends ``BaseTerminalSession``."""
        self.assertTrue(issubclass(
            host_session.HostTerminalSession,
            session_base.BaseTerminalSession,
        ))

    def test_open_process_is_commandless(self):
        """``_open_process`` opens a PTY with NO command — a login shell."""
        s = host_session.HostTerminalSession.__new__(
            host_session.HostTerminalSession,
        )
        conn = MagicMock(spec=asyncssh.SSHClientConnection)
        conn.create_process = AsyncMock(return_value=object())
        asyncio.run(s._open_process(conn))
        args, kwargs = conn.create_process.call_args
        # No positional command → asyncssh runs the SSH user's login shell.
        self.assertEqual(args, ())
        self.assertTrue(kwargs.get('request_pty'))
