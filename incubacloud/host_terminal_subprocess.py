"""Subprocess that owns one SSH/PTY host-shell session.

Same multi-worker rationale as ``terminal_subprocess`` (the SSH socket
does not migrate between Odoo workers, so it lives in a dedicated process
the workers proxy to via ``cloud.host.terminal.route``). All the shared
machinery — the loopback HTTP API, auth, watchdog, port binding and
SSH-kwargs rehydration — lives in ``terminal_subprocess_base``; this
script only builds the host-scoped ``HostTerminalSession``.

Run as a plain script (never ``-m``) for the same reason as
``terminal_subprocess``: loading the ``incubacloud`` package would execute
its ``__init__.py`` and pull ``odoo.http`` into a bare interpreter.
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Our own dir holds both ``host_terminal_session`` and the shared bases it
# imports. ``INCUBACLOUD_CORE_DIR`` is set by the parent controller to the
# same dir; honouring it keeps this script symmetrical with its sibling.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_CORE_DIR = os.environ.get("INCUBACLOUD_CORE_DIR")
if _CORE_DIR:
    sys.path.insert(0, _CORE_DIR)

from terminal_subprocess_base import (  # noqa: E402
    rehydrate_ssh_kwargs,
    run_server,
)
from host_terminal_session import HostTerminalSession  # noqa: E402


def main() -> int:
    """Read the config, build the ``HostTerminalSession``, serve it."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--auth-token")
    parser.add_argument("--config-file", required=True,
                        help="Path to JSON config. File is deleted "
                             "immediately after reading.")
    args = parser.parse_args()

    # Read the config and delete the file before anything else so secrets
    # live on disk for as short as possible.
    try:
        with open(args.config_file, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    finally:
        Path(args.config_file).unlink(missing_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format=f"[host-term-{args.session_id[:8]}] %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    session = HostTerminalSession(
        session_id=args.session_id,
        ssh_connect_kwargs=rehydrate_ssh_kwargs(config),
        host_label=config.get("host_label", ""),
        user_id=config.get("user_id"),
        welcome_banner=config.get("welcome_banner", ""),
    )
    auth_token = config.get("auth_token") or args.auth_token
    if not auth_token:
        parser.error("the config must include an auth token")
    return run_server(session, auth_token)


if __name__ == "__main__":
    sys.exit(main())
