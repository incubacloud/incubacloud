"""Subprocess that owns one SSH/PTY session for an instance web terminal.

Problem solved:
    A ``TerminalSession``'s SSH socket + PTY is a live OS resource that
    cannot move between processes. Under multi-worker Odoo, keeping the
    session in a worker's memory means N-1 workers return 404 for
    subsequent polls. This subprocess owns the socket; Odoo workers become
    thin proxies that look up its port in ``cloud.terminal.route``
    (Postgres, shared across workers).

Design and the shared machinery both terminals use — the loopback HTTP
API, Bearer-token auth, idle watchdog, port binding and SSH-kwargs
rehydration — live in ``terminal_subprocess_base``. This script only
reads its config and builds the instance-scoped ``TerminalSession``.

Invocation (as a plain script, never ``-m``): running it as a module would
execute the addon ``__init__`` and pull in ``odoo.http`` with no Odoo boot,
dying on a circular import. The parent puts the addon dir on ``sys.path``
so ``terminal_session`` / ``terminal_subprocess_base`` import as flat
top-level modules and the package init is skipped entirely.

    python3 -u terminal_subprocess.py \\
        --session-id <sid> --auth-token <hex> --config-file <json>

The config file is read and deleted immediately so no secrets (the SSH
private key) persist on disk beyond the first millisecond. CLI args are
visible in ``ps aux`` which is why the SSH key does NOT go there.
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Run as a plain script, NOT ``-m incubacloud.terminal_subprocess`` —
# loading the ``incubacloud`` package would execute its ``__init__.py``
# and pull ``odoo.http`` into a bare interpreter → circular ImportError.
# Putting our own dir (and the core addon dir, which for this core script
# is the same dir) on ``sys.path`` lets us import the flat modules and
# skip the package init.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_CORE_DIR = os.environ.get("INCUBACLOUD_CORE_DIR")
if _CORE_DIR:
    sys.path.insert(0, _CORE_DIR)

from terminal_subprocess_base import (  # noqa: E402
    rehydrate_ssh_kwargs,
    run_server,
)
from terminal_session import TerminalSession  # noqa: E402


def main() -> int:
    """Read the config, build the instance ``TerminalSession``, serve it."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--auth-token", required=True)
    parser.add_argument("--config-file", required=True,
                        help="Path to JSON config. File is deleted "
                             "immediately after reading.")
    args = parser.parse_args()

    # Read the config and delete the file before doing anything else so
    # secrets live on disk for as short as possible.
    with open(args.config_file, "r", encoding="utf-8") as fh:
        config = json.load(fh)
    Path(args.config_file).unlink(missing_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format=f"[terminal-{args.session_id[:8]}] %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    session = TerminalSession(
        session_id=args.session_id,
        ssh_connect_kwargs=rehydrate_ssh_kwargs(config),
        inst_dir=config["inst_dir"],
        service=config.get("service", "odoo"),
        instance_name=config.get("instance_name", ""),
        environment=config.get("environment", ""),
        user_id=config.get("user_id"),
        welcome_banner=config.get("welcome_banner", ""),
    )
    return run_server(session, args.auth_token)


if __name__ == "__main__":
    sys.exit(main())
