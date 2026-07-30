"""``TerminalSession`` — SSH + PTY session scoped to a compose service.

Thin subclass of ``BaseTerminalSession`` (see ``terminal_session_base``)
that adds only the instance-specific ``_open_process`` hook: resolve the
Docker container for the requested compose service and ``docker exec``
into it. The whole session is therefore confined to that container —
this module never opens a command-less (host) shell.

Imported two ways, both without an ``odoo.*`` bootstrap:
  * by the per-session subprocess (``terminal_subprocess``) as a flat
    top-level module — hence ``from terminal_session_base import ...``
    resolves against the addon dir the subprocess put on ``sys.path``;
  * never in package context — the controller reads ``SESSION_TIMEOUT``
    from ``terminal_session_base`` directly.
"""

import shlex

from terminal_session_base import SESSION_TIMEOUT, BaseTerminalSession

__all__ = ['TerminalSession', 'SESSION_TIMEOUT']


class TerminalSession(BaseTerminalSession):
    """Interactive SSH terminal into a compose service's container."""

    _THREAD_PREFIX = 'terminal'

    def __init__(
        self,
        session_id,
        ssh_connect_kwargs,
        inst_dir,
        service,
        instance_name='',
        environment='',
        user_id=None,
        welcome_banner='',
    ):
        """Store instance-scoped params, then start the worker thread."""
        super().__init__(
            session_id,
            ssh_connect_kwargs,
            user_id=user_id,
            welcome_banner=welcome_banner,
        )
        self.inst_dir = inst_dir
        self.service = service
        self.instance_name = instance_name
        self.environment = environment
        self.start()

    async def _open_process(self, conn):
        """Resolve the service container and ``docker exec`` a shell in it.

        The session is confined to the container: we look up the running
        container id for ``self.service`` and exec a shell there, falling
        back to ``docker compose exec`` when no id is resolvable. A shell
        command is ALWAYS passed to ``create_process`` — this subclass
        never opens a host login shell.
        """
        # Quote inst_dir preserving the ~/ prefix (shlex.quote would
        # prevent tilde expansion by wrapping the whole string in quotes).
        svc = shlex.quote(self.service)
        _d = self.inst_dir
        safe_dir = (
            '~/' + shlex.quote(_d[2:]) if _d.startswith('~/')
            else shlex.quote(_d)
        )
        result = await conn.run(
            f"cd {safe_dir} && docker compose ps -q {svc} 2>/dev/null | head -1",
            check=False,
        )
        container_id = (result.stdout or '').strip()

        if container_id:
            cmd = (
                f"docker exec -it {container_id} bash 2>/dev/null"
                f" || docker exec -it {container_id} sh"
            )
        else:
            cmd = (
                f"cd {safe_dir}"
                f" && docker compose exec -T {svc} bash 2>/dev/null"
                f" || cd {safe_dir} && docker compose exec -T {svc} sh"
            )

        return await conn.create_process(
            cmd,
            request_pty=True,
            term_type='xterm-256color',
            term_size=(80, 24),
            encoding=None,  # raw bytes — xterm.js handles encoding
        )
