"""Odoo limits that cut the panel's jobs short.

A job runs inside an Odoo request for as long as its SSH commands do, and
one command may last ``DEFAULT_SSH_COMMAND_TIMEOUT`` (an hour). Odoo's
default ``limit_time_real`` is 120 seconds: a longer job is killed, retried
from the start and, at its third death, failed for good. With
``workers = 0`` it is worse: past the limit Odoo reloads the whole server,
and every job running in it dies at once — measured on Odoo 19 (8-nov-2026,
``Initiating server reload`` within a minute of a request passing the
limit). The memory limits change meaning there too: Odoo's defaults are
figures for one worker, and in a threaded server they apply to the whole
process (``limit_memory_soft`` reloads it, ``limit_memory_hard`` makes its
allocations fail).

Only a server that runs jobs is judged: one with ``queue_job`` among its
``server_wide_modules``, which starts the job runner. A web-only server
next to a dedicated runner, loaded with ``--load=web``, has nothing to
warn about.
"""

from .transport import DEFAULT_SSH_COMMAND_TIMEOUT

#: Odoo 19's per-worker memory defaults (``odoo/tools/config.py``).
ODOO_DEFAULT_MEMORY_SOFT = 2048 * 1024 * 1024
ODOO_DEFAULT_MEMORY_HARD = 2560 * 1024 * 1024

#: The alert the panel shows while the limits are too low.
JOB_LIMITS_ALERT_CODE = "job_server_limits_too_low"


def _mib(value):
    """Return *value* bytes as whole MiB, for messages."""
    return int(value) // (1024 * 1024)


def job_server_limit_problems(options):
    """Return what in *options* cuts the panel's jobs short.

    :param options: Odoo's configuration (``odoo.tools.config``) or a
        mapping with the same keys: ``server_wide_modules``,
        ``stop_after_init``, ``workers``, ``limit_time_real``,
        ``limit_memory_soft`` and ``limit_memory_hard``
    :return: None when this server runs no jobs (no ``queue_job`` among
        its server-wide modules, or a run that stops after init);
        otherwise one message per problem, an empty list when there is
        none
    :rtype: list or None
    """
    modules = options.get("server_wide_modules") or []
    if isinstance(modules, str):
        modules = [m.strip() for m in modules.split(",")]
    if "queue_job" not in modules or options.get("stop_after_init"):
        return None
    threaded = not options.get("workers")
    problems = []
    time_real = options.get("limit_time_real") or 0
    if 0 < time_real < DEFAULT_SSH_COMMAND_TIMEOUT:
        consequence = (
            "with workers = 0 the whole server reloads, and every job "
            "running in it dies"
            if threaded
            else "its worker is killed and the job dies"
        )
        problems.append(
            f"limit_time_real is {time_real} s, and a panel job runs for as "
            f"long as its commands, up to {DEFAULT_SSH_COMMAND_TIMEOUT} s "
            f"each: past the limit {consequence}. Set limit_time_real to "
            f"{DEFAULT_SSH_COMMAND_TIMEOUT} or more on the server that "
            f"runs the jobs."
        )
    if threaded:
        soft = options.get("limit_memory_soft") or 0
        if 0 < soft <= ODOO_DEFAULT_MEMORY_SOFT:
            problems.append(
                f"limit_memory_soft is {_mib(soft)} MiB, a figure for one "
                f"worker: with workers = 0 it measures the whole server, "
                f"which reloads with every running job when it passes it. "
                f"Set it to 0 or to what the whole server may use."
            )
        hard = options.get("limit_memory_hard") or 0
        if 0 < hard <= ODOO_DEFAULT_MEMORY_HARD:
            problems.append(
                f"limit_memory_hard is {_mib(hard)} MiB, a figure for one "
                f"worker: with workers = 0 it caps the whole server, whose "
                f"allocations fail past it. Set it to 0 or to what the "
                f"whole server may use."
            )
    return problems
