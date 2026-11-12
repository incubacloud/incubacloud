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

A step marked ``detached`` runs on its server apart from the request
(``scripts/detached_step.sh``), and the attempt that follows it gives up
its request before the limit: it runs for ``attempt_budget_seconds``, a
share of ``limit_time_real``, and the job goes on in a later attempt. The
limit then needs to fit the steps that still run inside the request, not
a whole build. Every long step of a job runs apart since phase 3
(1.0.226), so the warning asks for ``MIN_LIMIT_TIME_REAL``, Odoo.sh's
900 s. What still runs inside the request, and so has what is left of it,
is in ``docs/architecture.md`` («Steps run apart on the server»).
"""
import logging
import os

from .password_utils import IncubacloudCryptoError, _read_conf_section

_logger = logging.getLogger(__name__)

#: Odoo 19's per-worker memory defaults (``odoo/tools/config.py``).
ODOO_DEFAULT_MEMORY_SOFT = 2048 * 1024 * 1024
ODOO_DEFAULT_MEMORY_HARD = 2560 * 1024 * 1024

#: The alert the panel shows while the limits are too low.
JOB_LIMITS_ALERT_CODE = "job_server_limits_too_low"

#: The lowest ``limit_time_real`` the startup warning accepts, in seconds
#: (12-nov-2026, phase 3): Odoo.sh's 900. Every step that may outlast
#: ``ATTACHED_STEP_FACTOR`` of it (360 s) runs apart: the downloads of the
#: repositories, the image's pull and build, a new database's
#: initialization, the boot test, the module updates, the restores' loads,
#: the copies and their packaging, Full Setup's tools and PITR's base
#: copies and restore wait. Those left in the request that production
#: ran stayed below it (11-oct to 10-nov-2026). Below the limit a
#: step that is not run apart, which may start when the attempt's budget
#: is nearly spent, has too little time left to end. Only the warning
#: reads it: the budget and a step's share follow the server's limit.
#: What is not a step on the server, and so runs within the request
#: whatever the limit, is said in ``INSTALL.md``: the relays through the
#: panel by SFTP and the playbooks; a higher limit leaves them more room.
#: It was an hour (11-nov-2026) while only the rebuild's image build ran
#: apart.
MIN_LIMIT_TIME_REAL = 900

#: The share of ``limit_time_real`` an attempt of a job runs for before it
#: leaves its step to run on the server and the job goes on in a later
#: attempt (10-nov-2026): 540 s with 900, measured against a rebuild's
#: longest steps (p90 450 s, max 650 s), which then run apart.
ATTEMPT_BUDGET_FACTOR = 0.6

#: The share of ``limit_time_real`` a step that is not run apart must stay
#: under (10-nov-2026): it may start just before the budget is spent, and
#: budget plus step must fit in the limit. 360 s with 900; «Initialize
#: database» and the image build passed it, which is why they run apart
#: (phase 3, 12-nov-2026), as does every step that may.
ATTACHED_STEP_FACTOR = 0.4

#: The odoo.conf option, in the ``[incubacloud]`` section, that sets the
#: budget in seconds instead: to make a job wait for its step within a
#: minute while it is tested.
ATTEMPT_BUDGET_OPTION = "attempt_budget_seconds"


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
    if 0 < time_real < MIN_LIMIT_TIME_REAL:
        consequence = (
            "with workers = 0 the whole server reloads, and every job "
            "running in it dies"
            if threaded
            else "its worker is killed and the job dies"
        )
        problems.append(
            f"limit_time_real is {time_real} s, below the "
            f"{MIN_LIMIT_TIME_REAL} s the panel's jobs need: an attempt "
            f"runs for {attempt_budget_seconds(options)} s before it "
            f"leaves a long step to the server, and a step that runs in "
            f"the request must end within "
            f"{attached_step_limit_seconds(options)} s; past the limit "
            f"{consequence}. Set limit_time_real to {MIN_LIMIT_TIME_REAL} "
            f"or more on the server that runs the jobs."
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


#: The ``[incubacloud]`` section of each odoo.conf read, with the file's
#: mtime and size it was read at: every job starts its clock
#: (``AbstractExecutor._start_clock``), and the file changes only by hand.
_CONF_SECTIONS = {}


def conf_section(path):
    """Return the ``[incubacloud]`` section of the odoo.conf at *path*.

    Read again only once the file changed (its mtime or its size).

    :param path: the odoo.conf, or None
    :return: the section's options; empty without a file or a section
    :rtype: dict
    :raise IncubacloudCryptoError: when the file cannot be parsed
    """
    if not path:
        return {}
    try:
        stat = os.stat(path)
    except OSError:
        return {}
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _CONF_SECTIONS.get(path)
    if cached and cached[0] == key:
        return cached[1]
    section = _read_conf_section(path)
    _CONF_SECTIONS[path] = (key, section)
    return section


def _configured_budget(options):
    """Return the budget odoo.conf sets in ``[incubacloud]``, or None.

    :param options: Odoo's configuration; its ``config`` names the file
    :return: whole seconds, or None when the option is not set or is not
        a number of seconds (then logged)
    :rtype: int | None
    """
    try:
        section = conf_section(options.get("config"))
    except IncubacloudCryptoError:
        return None
    raw = (section.get(ATTEMPT_BUDGET_OPTION) or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        _logger.warning(
            "[incubacloud] %s = %r is not a number of seconds: ignored.",
            ATTEMPT_BUDGET_OPTION, raw,
        )
        return None
    return int(raw)


def attempt_budget_seconds(options):
    """Return how long an attempt of a job runs before it waits, in seconds.

    ``ATTEMPT_BUDGET_FACTOR`` of the server's ``limit_time_real``, or what
    ``[incubacloud] attempt_budget_seconds`` in odoo.conf says. 0 means an
    attempt never stops to wait: the server has no limit.

    The limit is the server process's (``odoo.tools.config`` of the server
    that runs the jobs, which on Odoo.sh comes from its command line), not
    the configuration file's.

    :param options: Odoo's configuration, or a mapping with
        ``limit_time_real`` and, for the file, ``config``
    :rtype: int
    """
    configured = _configured_budget(options)
    if configured is not None:
        return configured
    limit = options.get("limit_time_real") or 0
    if limit <= 0:
        return 0
    return int(limit * ATTEMPT_BUDGET_FACTOR)


def attached_step_limit_seconds(options):
    """Return how long a step that is not run apart may last, in seconds.

    ``ATTACHED_STEP_FACTOR`` of ``limit_time_real``; 0 when the server has
    no limit, and then no step has one either.

    :param options: Odoo's configuration, or a mapping with
        ``limit_time_real``
    :rtype: int
    """
    limit = options.get("limit_time_real") or 0
    if limit <= 0:
        return 0
    return int(limit * ATTACHED_STEP_FACTOR)
