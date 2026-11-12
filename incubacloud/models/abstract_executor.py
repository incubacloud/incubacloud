import asyncio
import base64
import contextlib
import errno
import hashlib
import hmac
import json
import logging
import re
import secrets
import shlex
import socket
import time
from abc import ABC, abstractmethod
from datetime import timedelta
from pathlib import Path

import asyncssh
from psycopg2 import OperationalError
from psycopg2.errors import LockNotAvailable

from odoo import fields
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.service.model import PG_CONCURRENCY_ERRORS_TO_RETRY
from odoo.sql_db import Cursor
from odoo.tools import config, file_path

from odoo.addons.queue_job.exception import RetryableJobError

from ._concurrency import read_committed_cursor
from ._job_server_limits import ATTEMPT_BUDGET_FACTOR, attempt_budget_seconds
from ._repo_requirements import has_pip_conflicts
from .cloud_job_no_return import NO_RETURN_LOCK_NAMESPACE
from .registry import executor_registry
from .transport import (
    CANCEL_TERM_WAIT_S,
    DEFAULT_SSH_COMMAND_TIMEOUT,
    CommandResult,
)

# Delay before queue_job re-runs a job that failed to connect to its host.
# Short enough that a momentary blip recovers quickly, long enough that we
# don't hammer a host that is briefly rebooting.
CONNECTION_RETRY_SECONDS = 30

#: Marker embedded in the ``RetryableJobError`` message raised for a
#: transient connection failure. queue_job keeps the text when it gives
#: up (``FailedJobError("Max. retries (N) reached: <original>")``), so
#: this is how the terminal-state hook recognises "the host stayed
#: unreachable for the whole retry budget" without having to guess from
#: the exception class alone. Ours, so safe to match on.
CONNECTION_RETRY_MARKER = "Transient connection error on attempt"

#: ``docker compose run`` for an instance's stack, out of Traefik and
#: Sablier. A one-off container inherits its service's labels: Traefik
#: would route visitors to it and Sablier would count it in a sleeping
#: instance's group, holding the panel behind its waiting page and stopping
#: the one-off when the session expires. The same labels as
#: ``IC_ONEOFF_LABELS`` in ``scripts/lib/common.sh``, which the scripts
#: use; a test keeps the two equal and every command on one of them.
COMPOSE_RUN = (
    "docker compose run"
    " --label traefik.enable=false --label sablier.enable=false"
)

#: The opening of a ``docker inspect -f`` template that keeps what follows,
#: up to its ``{{end}}``, for a container the stack itself created and
#: drops it for a one-off. ``docker compose ps`` lists one-offs under their
#: service's name — ``-a -q <service>`` on every compose version, the
#: other listings from 2.29 on (the hosts run 5.x) — and the
#: ``com.docker.compose.oneoff`` label is the only thing that tells them
#: apart on every version.
NOT_ONEOFF = (
    '{{if ne (index .Config.Labels "com.docker.compose.oneoff") "True"}}'
)


def compose_own_container(service):
    """Return a shell command printing *service*'s own container id.

    ``docker compose ps -a -q <service>`` also prints the ids of the
    service's one-off containers, running or left behind, and a caller
    that takes its output for one id breaks on the second. This keeps the
    container the stack created.

    :param str service: a compose service name of the instance's stack,
        run from the instance directory.
    :returns: a command that prints one id, or nothing when the service
        has no container, and fails when docker cannot list the stack.
    :rtype: str
    """
    own_id = shlex.quote(NOT_ONEOFF + "{{.Id}}{{end}}")
    return (
        f"ids=$(docker compose ps -a -q {service}) && "
        f'{{ [ -z "$ids" ] || docker inspect -f {own_id} $ids; }}'
        f" | awk NF | head -n 1"
    )

#: ``scripts/compose_op.sh`` ran the operation it was asked for, but could
#: not read what the steps that follow it need — the compose, what runs,
#: odoo's routes — to check them: the egress sidecar restarted after
#: odoo, odoo restarted into the gatekeeper's network. The step did its
#: work, so an executor running the script to start containers takes this
#: for success, with a warning (the step's ``continue_on`` and
#: :meth:`AbstractExecutor._accept_unchecked_compose_op`). The health
#: probe catches what may be left wrong: an unfiltered route through
#: ``odoo_net_setup``'s healthcheck (``instance_egress_filter_lost``), an
#: odoo outside its gatekeeper's network, or with its gatekeeper stopped,
#: through the routes odoo has (``instance_unresponsive``).
COMPOSE_OP_EXIT_UNCHECKED = 24

#: Label of the step that brings a restored database's modules up to the
#: instance's code (:meth:`AbstractExecutor._update_restored_modules_step`),
#: the rebuild's own label for the same update.
UPDATE_RESTORED_MODULES_LABEL = "Update changed modules"

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

#: The credential of an ``http(s)`` URL, whatever the server: the
#: ``user:token@`` or ``token@`` between ``://`` and the host, which git-aggregator
#: logs with the remote (``Adding remote origin https://oauth2:<token>@host/…``)
#: and git repeats in its ``fatal: unable to access '…'``. It ends at the last
#: ``@`` before a ``/``, ``?``, ``#``, a quote, an angle bracket or a space,
#: so a token with an ``@`` of its own goes whole and a ``@`` further on, in a
#: path or another word, is left alone.
_TOKEN_RE = re.compile(r'(https?://)[^/?#\s\'"<>]+@', re.IGNORECASE)
#: What ``run_script`` accepts as the name of an environment variable.
_ENV_NAME_RE = re.compile(r'^[A-Z_][A-Z0-9_]*$')

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

# Prefix of the private working directory a job stages its files in on
# its host: the scripts it runs, the files its steps move into place,
# the modules it installs, the archive it restores. The rest of the name
# is random (``AbstractExecutor._job_dir``), the directory is created
# 0700 and only if nothing is at that path, and the job removes it when
# it ends. Another account on the host can neither guess the name in
# advance, nor create the directory first, nor read or replace what is
# inside — on a host its owner shares with other accounts, a predictable
# path in /tmp was all three.
JOB_DIR_PREFIX = "/tmp/.incubacloud-job-"

# How long a durable write waits for a row lock before giving up. The one
# holder that keeps a lock that long is the job's own transaction, and
# then waiting is a deadlock: the job cannot commit until the executor
# returns. The cap turns that hang into an error with a traceback.
DURABLE_LOCK_TIMEOUT = "30s"


def _redact_tokens(text):
    """Remove the embedded credential from every http(s) URL in *text*.

    Any server (GitHub, GitLab, a self-hosted one), any shape
    (``user:token@``, ``token@``) and any port; a URL without credential is
    returned as it was.
    """
    return _TOKEN_RE.sub(r'\1', text)


#: Prefix of the directory a ``backup_download`` with ``handoff='host'``
#: hands its archive over in (:func:`handoff_dir`).
HANDOFF_PREFIX = "/tmp/.incubacloud-handoff-"

#: Remove this account's handoffs older than a day: the archive of a
#: chain whose restore never came for it (a production database). Run
#: when a handoff starts and when a restore from one starts, on the
#: job's host, and by the day's prune, best effort, sparing what a
#: restore still waits for (:func:`handoff_sweep`). Only directories,
#: only this account's.
HANDOFF_SWEEP = (
    f"find /tmp -maxdepth 1 -type d -name '{HANDOFF_PREFIX[5:]}*'"
    ' -user "$(id -u)" -mmin +1440 -exec rm -rf {} +'
)


def handoff_dir(env, job_id):
    """Return the directory a handoff of job *job_id* is handed over in.

    Named with the panel's ``database.secret``: the consumer
    (``restore_instance`` mode ``from_host``) recomputes it from
    ``source_job_id`` alone, as before, while no account on the host can
    tell it in advance and create it first. The producer creates it 0700
    when it hands the archive over (``backup_download.sh handover``);
    the restore removes it, or a cancelled chain, or the sweep a day
    later. Module level because both executors must agree on it.

    :param env: an environment to read the secret with
    :param int job_id: id of the ``cloud.job`` that produced the archive
    :return: absolute path on the producing job's host
    :raise RuntimeError: when the database has no secret to name it with
    """
    secret = env["ir.config_parameter"].sudo().get_param("database.secret")
    if not secret:
        raise RuntimeError("database.secret is not set: cannot name a handoff.")
    digest = hmac.new(
        secret.encode(), f"handoff:{int(job_id)}".encode(), hashlib.sha256,
    ).hexdigest()
    return f"{HANDOFF_PREFIX}{digest[:16]}"


def handoff_archive_path(env, job_id):
    """Return the archive a handoff of job *job_id* leaves for the restore.

    :param env: an environment to read the secret with
    :param int job_id: id of the ``cloud.job`` that produced the archive
    :return: absolute path on the producing job's host
    """
    return f"{handoff_dir(env, job_id)}/archive.zip"


def same_chain(job, source):
    """Whether *source* is a step of the chain *job* belongs to.

    A retry is in no chain: the job it retries, followed back to the
    first, is the one that was. Read as the superuser: what decides is
    the chain, not what the caller may read.

    :param job: a ``cloud.job``
    :param source: a ``cloud.job``
    :rtype: bool
    """
    job = job.sudo()
    seen = set()
    while job.retry_of_id and job.id not in seen:
        seen.add(job.id)
        job = job.retry_of_id
    key = job.queue_chain_key
    return bool(key) and source.sudo().queue_chain_key == key


def handoff_sweep(env, host, root="/tmp"):
    """Return :data:`HANDOFF_SWEEP` for *host*, sparing awaited handoffs.

    A handoff a day old is a chain whose restore never came — unless the
    restore is still waiting its turn behind a step, a busy queue or a
    retry: that one stays (28-oct-2026: the sweep took it). Only a
    restore of the download's own chain counts (:func:`same_chain`), so
    a restore queued by hand cannot keep another chain's archive on the
    host for good.

    :param env: an environment to read the jobs with
    :param host: the ``cloud.host`` the sweep runs on
    :param str root: the directory swept, ``/tmp`` but in tests
    :rtype: str
    """
    Job = env["cloud.job"].sudo()
    spared = set()
    for restore in Job.search([
        ("job_type_id.code", "=", "restore_instance"),
        ("state", "in", Job._active_states),
    ]):
        payload = restore.payload or {}
        source_id = str(payload.get("source_job_id") or "")
        if payload.get("mode") != "from_host" or not source_id.isdigit():
            continue
        source = Job.browse(int(source_id)).exists()
        if source and source.host_id.id == host.id \
                and same_chain(restore, source):
            spared.add(handoff_dir(env, source.id).rsplit("/", 1)[1])
    names = "".join(f" ! -name {shlex.quote(name)}" for name in sorted(spared))
    return (
        f"find {shlex.quote(root)} -maxdepth 1 -type d"
        f" -name '{HANDOFF_PREFIX[5:]}*'{names}"
        ' -user "$(id -u)" -mmin +1440 -exec rm -rf {} +'
    )


#: Prefix of the directory a step marked ``detached`` runs in on its
#: server (:func:`detached_dir`, ``scripts/detached_step.sh``).
DETACHED_PREFIX = "/tmp/.incubacloud-detached-"

#: How often a running step apart is read, in seconds (10-nov-2026): its
#: lines reach the job log about as soon as an attached step's (0.5 s), at
#: the cost of one ``exec`` a second on the connection already open.
DETACHED_POLL_S = 1

#: The most one read of a step apart returns, in bytes (10-nov-2026): a
#: build does not write that much in a second; a read that hits it is
#: followed by another at once.
DETACHED_READ_MAX_BYTES = 1024 * 1024

#: What a step apart keeps of its stdout for its result, in bytes
#: (10-nov-2026): a ``parse_results`` that reads a long step's stdout sees
#: its last 256 KiB.
DETACHED_STDOUT_TAIL = 256 * 1024

#: How long past its cap a step apart may run before the panel stops it
#: and fails the job, in seconds (10-nov-2026): ``timeout`` on the server
#: sends TERM at the cap and KILL 60 s later; 120 s is that and room.
DETACHED_OVERDUE_GRACE_S = 120

#: How long a step apart waits for its trap once it is sent TERM when the
#: job is stopped, in seconds: what the transport gives a stopped command
#: (``CANCEL_TERM_WAIT_S``).
DETACHED_STOP_WAIT_S = CANCEL_TERM_WAIT_S

#: ``detached_step.sh start``: a step of another job still runs for the
#: same instance or host. The job waits and tries again
#: (``DETACHED_BUSY_RETRY_S``); the other step ends on its own, or the
#: reconciliation stops it.
DETACHED_EXIT_BUSY = 73
#: ``detached_step.sh start``: the step's path is held by something that is
#: not this account's directory.
DETACHED_EXIT_TAKEN = 74
#: ``detached_step.sh clean``: the step still runs, and its directory stays.
DETACHED_EXIT_RUNNING = 75
#: ``detached_step.sh start``: the server kills this account's processes
#: when its SSH session ends (logind's ``KillUserProcesses``); the step runs
#: attached to the request instead.
DETACHED_EXIT_KILLED_AT_LOGOUT = 78
#: ``detached_step.sh start``: the server lacks a tool the script needs to
#: follow or stop the step (``flock``, ``setsid``, ``timeout``, ``pgrep``,
#: ``pkill``); the step runs attached to the request, as with 78.
DETACHED_EXIT_MISSING_TOOLS = 79

#: The answers of ``detached_step.sh start`` on which the step runs attached.
DETACHED_EXIT_ATTACHED = (DETACHED_EXIT_KILLED_AT_LOGOUT, DETACHED_EXIT_MISSING_TOOLS)

#: How long a job whose step found another job's step running for the same
#: target stays parked before it tries again, in seconds (10-nov-2026): as
#: long as a job that waits for its host's build lock.
DETACHED_BUSY_RETRY_S = 30

#: How long the reconciliation waits for a step's trap once it sent it TERM,
#: in seconds (10-nov-2026): it fits in the pass's own budget
#: (``cloud.job._RECONCILE_HOOK_BUDGET_S``, 60 s) with the connection.
DETACHED_RECONCILE_WAIT_S = 20

#: How long after a step's cap the reconciliation stops trying to reach
#: its server, in seconds (10-nov-2026): the cap's own ``timeout`` killed
#: the step long before, and the checkpoint is then given up.
DETACHED_GIVE_UP_AFTER_CAP_S = 3600

#: How long the checkpoint a step past its job's point of no return left
#: running keeps the job's queue busy, in seconds, counted from when it
#: began to (12-nov-2026): a step's default cap
#: (``DEFAULT_SSH_COMMAND_TIMEOUT``), the reconciliation's give-up after
#: it (``DETACHED_GIVE_UP_AFTER_CAP_S``) and ten minutes of its passes.
#: Past it the queue goes on even with the row still there: a stopped
#: cron, or an executor no longer installed, must not hold it for good.
DETACHED_HOLDS_QUEUE_MAX_S = (
    DEFAULT_SSH_COMMAND_TIMEOUT + DETACHED_GIVE_UP_AFTER_CAP_S + 600
)

#: How long a parked job waits for its next attempt, in seconds (9-nov-2026).
#: queue_job's runner wakes at the job's ``eta`` exactly, so the gap is not
#: rounded up to any cycle; the attempt's budget, not the gap, sets how
#: often the job reconnects; and 5 s let the attempt that parked close its
#: request and its connection before the next one starts. Less would risk
#: overlapping a slow store of the postpone.
PARK_GAP_SECONDS = 5

#: The priority a parked job's queue.job comes back with (9-nov-2026): the
#: most urgent, so that once its ``eta`` passes the runner gives it the next
#: slot of its channel before any job pending there (``queue_job_ext``).
PARKED_RETURN_PRIORITY = 0

#: How long a parked job may be late to come back before it says so, in
#: seconds (10-nov-2026): two passes of Odoo.sh's crons. Its place in the
#: channel is kept however late it is, decided that day: no place given
#: up frees the channel, which only jobs outside the panel can hold. Past
#: it the job log says so, with the channel it waits in, and the slow job
#: warning counts the wait (``cloud.job._cron_watch_long_running``).
PARKED_OVERDUE_S = 600

#: How long a job that cannot reach the server of its running step stays
#: parked before it connects again, in seconds (10-nov-2026): as long as a
#: job that cannot reach its host waits (``CONNECTION_RETRY_SECONDS``).
DETACHED_RECONNECT_GAP_S = CONNECTION_RETRY_SECONDS

#: How long a job may go without reaching the server of its running step
#: before it fails, in seconds (10-nov-2026): the tolerance of
#: ``_connection_retry_attempts`` (10 × 30 s), which covers a host that
#: reboots for its updates. The step is then left, with the job's
#: checkpoint, to the reconciliation.
DETACHED_CONTACT_GRACE_S = 600

#: An attempt whose closing hooks fetch through the panel
#: (``_outcome_needs_room``) leaves them to a fresh attempt once it has run
#: past this share of the request's limit, in other words once less than
#: ``OUTCOME_ROOM_FACTOR`` of it is left (12-nov-2026, Fable's review of
#: phase 3): the time to make an archive and the time to fetch it grow
#: together, and parking costs a few seconds where a fetch cut short
#: reloads the whole server. With 900 s it parks from 135 s on; with 3600,
#: from 540 s.
OUTCOME_ROOM_FACTOR = 0.85

#: An attempt that has run less than this, in seconds, never parks for
#: room (12-nov-2026): its hooks start with nearly all of the request, and
#: a park would only add its gap.
OUTCOME_ROOM_MIN_ELAPSED_S = 60

#: The header ``detached_step.sh read`` answers with: state, exit status,
#: whether the cap cut the step, and the offset the bytes after it end at.
_DETACHED_HEADER_RE = re.compile(
    r"^ic-detached v1 state=(running|done|lost|none)"
    r" exit=(-|\d+) timedout=([01]) off=(\d+)$"
)

#: The ``source`` of a ``_log_buffer`` entry that is not a line but the
#: offset in a step's log the lines before it end at (``_flush_logs``).
_LOG_OFFSET = "detached_offset"


def detached_dir(env, job_id, queue_job_uuid, index):
    """Return the directory step *index* of job *job_id* runs apart in.

    Named with the panel's ``database.secret``, as a handoff
    (:func:`handoff_dir`): any later attempt of the job, or the
    reconciliation, recomputes it from the job and the step alone, and no
    account on the server can tell it in advance and create it first. One
    per step: a job may run several long steps one after the other. The
    queue job that ran it names it too: a cloud job run again by another
    queue job never reaches the directories of the first.

    :param env: an environment to read the secret with
    :param int job_id: id of the ``cloud.job``
    :param str queue_job_uuid: the queue job whose attempts run the step
    :param int index: position of the step in the job's plan
    :return: absolute path on the job's host
    :raise RuntimeError: when the database has no secret to name it with
    """
    secret = env["ir.config_parameter"].sudo().get_param("database.secret")
    if not secret:
        raise RuntimeError(
            "database.secret is not set: cannot name a step's directory."
        )
    digest = hmac.new(
        secret.encode(),
        f"detached:{int(job_id)}:{queue_job_uuid or ''}:{int(index)}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{DETACHED_PREFIX}{digest[:16]}"


def _detached_ctl(directory, order, *args):
    """Return the command that runs *order* of the step in *directory*.

    Through the step's own copy of the script (``ctl.sh``), and only when
    *directory* is this account's directory and not a link: what another
    account put at that path is never run. Without it, the answer of a
    step that is not there: ``none`` for ``read``, nothing for the rest.

    :param str directory: :func:`detached_dir`
    :param str order: ``read``, ``result``, ``stop`` or ``clean``
    :param args: the order's arguments after the directory
    :rtype: str
    """
    quoted = shlex.quote(directory)
    absent = (
        f"printf 'ic-detached v1 state=none exit=- timedout=0 off=%s\\n'"
        f" {shlex.quote(str(args[0]))}" if order == "read" else "true"
    )
    return (
        f"if [ -d {quoted} ] && [ ! -L {quoted} ] && [ -O {quoted} ]"
        f" && [ -f {quoted}/ctl.sh ]; then"
        f" bash {quoted}/ctl.sh {shlex.join([order, directory, *map(str, args)])};"
        f" else {absent}; fi"
    )


def _stdout_lines(data):
    """Return *data*'s lines as a step's ``stdout`` is kept: trimmed, none empty.

    The same as ``SSHTransport.execute`` keeps a command's: each line
    without its trailing blanks, empty lines dropped.

    :param str data: text
    :rtype: list[str]
    """
    return [line.rstrip() for line in data.split("\n") if line.rstrip()]


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
    if not isinstance(raw, str) or not DUP_TIME_RE.fullmatch(raw):
        raise ValueError(
            f"Invalid 'time' value: {raw!r}. Expected 'latest', 'live', "
            f"'now', a relative offset like '12h_ago', or an ISO 8601 "
            f"timestamp like '2026-03-19T02:00:00'."
        )
    return raw


class JobCancelled(Exception):
    """A person cancelled the job while it ran, and the executor stopped.

    Raised by ``_check_cancel`` once ``cloud.job.cancel_job`` has set the
    cooperative stop flag. queue_job sees any exception as a failure;
    ``queue.job.write`` (``queue_job_ext``) records this one as the
    cancellation it is.
    """


class JobWorkerLost(Exception):
    """The process that ran the job died, and queue_job gave up on it.

    Never raised by a run: ``cloud.job._cron_reconcile_lost_jobs`` hands
    it to ``on_abort`` once queue_job found the job dead for the last
    time (``JobFoundDead``) or its queue.job is gone, so the executor
    puts back what a run that stopped half way left. The executor that
    receives it is a new one: what the lost run kept in memory (its step
    results, a plan, an id it was given) is not there.
    """


# What ``cloud.job.execute`` returns when the executor stopped on the
# cancel flag, and what ``queue_job_ext`` recognises in the job it stores.
CANCELLED_WHILE_RUNNING = "Cancelled while it ran."

# What ``cloud.job.execute`` returns when ``blocking_alert`` stopped the
# job before it started: it waits, ``blocked_alert_id`` says on what.
BLOCKED_BY_ALERT = "Blocked until an alert is resolved."

# What ``cloud.job.execute`` returns when an earlier attempt's
# ``on_success`` committed (``cloud.job.outcome``): this one runs nothing.
ALREADY_DONE = "Done by an earlier attempt."


class JobParked(RetryableJobError):
    """The attempt left the job to a later one; the job goes on.

    Raised once the attempt's budget is spent, while another job's step
    holds the job's target on its server, or when that server cannot be
    reached while a step of the job runs there. The job's checkpoint says
    where it is, marked ``parked``, and queue_job runs the job again
    *seconds* later without counting the attempt (``ignore_retry``). No
    hook runs, the job's staging stays on its server, and the cloud job
    stays ``started`` (``queue_job_ext``).
    """

    def __init__(self, msg, seconds=PARK_GAP_SECONDS):
        """Park for *seconds*, off the job's retry budget.

        :param str msg: why, for queue_job's ``result``
        :param int seconds: how long until the next attempt
        """
        super().__init__(msg, seconds=seconds, ignore_retry=True)


class JobRefused(RuntimeError):
    """A check of the executor's own refused to go on, before any step ran.

    The message says why and is all there is to say: it is logged as a
    warning, without the traceback an unexpected exception gets, so the
    log scrapers that page on ERROR lines are not handed a decision the
    job already reported as its own failure.
    """


def _is_refusal(exc):
    """Whether *exc* is a refusal the job reports, not a defect of ours.

    ``JobRefused`` and a ``UserError``: its message is the whole story.
    An ``AccessError`` or a ``MissingError`` is a ``UserError`` too, but
    in a job it means our code read what it may not or what is gone.

    :rtype: bool
    """
    return isinstance(exc, (JobRefused, UserError)) and not isinstance(
        exc, (AccessError, MissingError),
    )


def deferred_instance_unlink(job_env, inst, job_id, host_id, sudo=False):
    """Return the callable that unlinks *inst* once it is called.

    Meant for a cursor's ``postcommit``: it opens a cursor of its own,
    does nothing if the record is gone already, and never raises — an
    unlink that fails is logged and raised as an alert instead. Shared
    by ``AbstractExecutor._unlink_after_job_commit`` and by
    ``queue_job_ext``, which arms it again for a job stored done after
    its own transaction was rolled back.

    :param job_env: the job's environment; its user and context are kept
    :param inst: the ``cloud.instance`` to remove
    :param int job_id: the job the alert names, if the unlink fails
    :param int host_id: the host the alert names, if the unlink fails
    :param bool sudo: unlink as superuser
    :return: a function of no arguments
    """
    registry = job_env.registry
    inst_id, inst_name = inst.id, inst.name

    def _unlink():
        with registry.cursor() as cr:
            env = job_env(cr=cr)
            rec = env["cloud.instance"].with_context(
                active_test=False,
            ).browse(inst_id)
            if sudo:
                rec = rec.sudo()
            if not rec.exists():
                return
            try:
                rec.unlink()
            except Exception:
                logging.getLogger(__name__).exception(
                    "deferred unlink of instance %s failed", inst_name,
                )
                cr.rollback()
                env["cloud.alert"].sudo().raise_alert(
                    "instance_unlink_failed",
                    f"'{inst_name}' was removed from its host but its "
                    "record could not be deleted; see the server log. "
                    "Delete it again once the cause is fixed.",
                    level="warning",
                    host=env["cloud.host"].browse(host_id),
                    instance=rec,
                    job=env["cloud.job"].browse(job_id),
                    # "See the server log": only the platform can.
                    audience="provider",
                )

    return _unlink


class AbstractExecutor(ABC):

    _job_type = None  # required in subclasses

    # Set on the instance once ``on_success`` / ``on_failure`` returned
    # (``_dispatch_outcome``): an exception after that point already had
    # its hook, one before it gets ``on_abort``.
    _outcome_dispatched = False
    # Set when that hook raised instead: a defect, whatever the exception.
    _hook_raised = False
    # Set as that hook starts (``_dispatch_outcome``), or ``on_abort``
    # (``_dispatch_abort``): the run is closing, and ``run`` no longer
    # reads the cancel flag.
    _outcome_started = False
    # What the steps answered so far, set when the first one starts
    # (``_run_steps``): None while the run has not reached them. A run a
    # person cancels is closed by ``on_failure`` with it, or by
    # ``on_abort`` while it is None (``on_cancelled_running``).
    _step_results = None
    # The label of the step the run gave the host last (``_run_steps``):
    # one missing from ``_step_results`` never answered, as when the
    # connection dropped or its time ran out.
    _running_step = None

    # Connection-retry opt-in. When True, a transient connection failure
    # (see ``is_transient_connection_error``) does not fail the job on the
    # first try: ``cloud.job.execute`` raises ``RetryableJobError`` so
    # queue_job reschedules it, up to ``_connection_retry_attempts`` total
    # attempts. The ``host_unreachable`` alert is raised only once the
    # job has actually given up — see
    # ``cloud.job._create_host_unreachable_alert``. This keeps momentary
    # blips from spamming notifications while still surfacing a genuinely
    # down host. Any non-connection error still fails permanently on the
    # first try.
    #
    # Ten attempts, ~30 s apart, is a deliberate ~10 minute tolerance.
    # Three (~90 s) was shorter than the routine maintenance of the very
    # hosts being probed: unattended-upgrades reboots them inside its
    # 04:00 window, which took a production host out for two minutes, and a
    # network blip on the same day took it out for four. Both paged.
    # A host that is genuinely gone is still critical ten minutes later,
    # and ``metrics_host_absent`` covers the same ground from the other
    # side in the meantime.
    _retry_on_connection_loss = False
    _connection_retry_attempts = 10

    # Whether the job is meant for an archived instance. The queue cancels
    # a waiting job whose instance was archived meanwhile
    # (``cloud.job._entry_verdict``): a deploy or a restart has nothing
    # left to act on. A job that exists for archived instances — emptying
    # the backup chain they keep — sets this, and is cancelled only once
    # its instance no longer exists.
    _runs_on_archived_instance = False

    # Whether the job runs on the host it was asked for, wherever its
    # instance lives when it starts. The queue sends a lone waiting job
    # to its instance's host (``cloud.job._entry_verdict``), so a move
    # between queueing and dispatch is followed. A job aimed at another
    # host on purpose — the half-built copy of a move being rolled back,
    # the old copy after a cutover, an upload grant's host — sets this,
    # and keeps that host, in its retries too: sent where the instance
    # lived, the rollback's teardown removed the live copy (28-oct-2026).
    _pinned_to_host = False

    # Whether the host's specs and disk are read again from metrics as
    # soon as the job ends, whatever its outcome (``_refresh_host_disk``),
    # instead of on the next pass of the five-minute cron: a build and a
    # teardown change the disk, and the host's Check is asked to show it.
    _refreshes_host_disk = False

    # Whether the job runs in ticks: it asks queue_job to postpone it
    # (``RetryableJobError``) and starts again later, holding between two
    # ticks what it had begun (an order it placed). A stop committed in
    # the instant between the tick's own ``_check_cancel`` and the store
    # of the postpone ends the job ``cancelled`` with no hook run
    # (``queue_job_ext.write``); with this set, its ``on_never_run`` lets
    # go then, as for a job cancelled between two ticks. That hook must
    # then do nothing a second time (6-nov-2026).
    _postpones_between_ticks = False

    # The label of the first step a stop must not cut, or None: from it on
    # the job cannot be put back as it was (a rebuild's database update,
    # which commits module by module; a restore's load, which drops the
    # database first). Before the step starts, the stop and the step are
    # decided under one lock (``_pass_no_return``): a stop already asked
    # for ends the job there; otherwise the job is marked, and a stop
    # asked for later is refused (``cloud.job.no.return``).
    _no_return_from = None

    # Set once the plan has a step marked ``detached`` and the job's
    # ``cloud.job.checkpoint`` row was written (``_open_checkpoint``); unset
    # once the row is gone. A plan without such a step writes no row.
    _checkpoint = False
    # The row's ``data`` as this run knows it (plan, results, staging).
    _checkpoint_data = None
    # The directory of the step apart that runs now, until it is removed.
    _detached_running = None
    # Bytes of that step's log already read.
    _detached_offset = 0
    # Something of a step apart was not stopped or removed: the server
    # could not be reached, or a process outlived its KILL. The row stays,
    # for whoever reaches the server next.
    _detached_left_alive = False
    # A step marked ``detached`` ran attached to the request: the server
    # kills processes at logout or lacks a tool (``DETACHED_EXIT_ATTACHED``).
    _detached_attached = False
    # The step of the plan whose process may run on the server now, from
    # just before its start until it ended or was stopped: what a lost
    # connection leaves running (``_contact_lost``).
    _on_server_index = None
    # ``(directory, index)`` of a step apart that ended, whose directory
    # goes once its result is recorded in the checkpoint.
    _ended_apart = None
    # Whether what the close of an ended job says on every try is said:
    # the reconciliation says it the first time and when it gives up.
    _say_again = True
    # The close left a step past the point of no return running on a
    # server that answered (``_close_on_server``): the reconciliation looks
    # at it again at its next pass, not ten minutes later.
    _left_running = False

    #: Attributes of the executor its closing hooks read and that are set
    #: before its steps: kept in the checkpoint, and set again on an attempt
    #: that resumes the job (``_resume``). Names of instance attributes,
    #: whose values JSON keeps, each with a default on its class. Every
    #: class of the executor adds its own (``_carried_names``): a mixin
    #: declares what its hooks read without repeating its bases' names.
    _carried = ()
    #: Carried for every executor: the job log's cap is the job's, not the
    #: attempt's.
    _CARRIED_ALWAYS = ("_chunks_persisted", "_cap_marker_emitted")

    # When this attempt's budget ends (``time.monotonic()``), or None: an
    # attempt whose clock was not started (``_start_clock``) never parks.
    _budget_ends = None
    #: The closing hooks fetch through the panel what the steps made (a
    #: download to an attachment, over SFTP): they cannot run apart on the
    #: server, and need the request's time. An attempt that has run long
    #: leaves them to a fresh one (``OUTCOME_ROOM_FACTOR``), only for a
    #: plan with a step marked ``detached``: no other keeps a checkpoint
    #: to park on (``_may_park``). The fresh attempt calls neither
    #: ``before_execute`` nor ``get_commands``: what the hooks read of
    #: theirs goes in ``_carried``.
    _outcome_needs_room = False
    # ``time.monotonic()`` when this attempt started (``_start_clock``).
    _attempt_started = None
    # The request's limit this attempt runs under, in seconds, or None
    # when nothing cuts it: ``limit_time_real``, or what the configured
    # budget stands for.
    _attempt_limit = None
    # The attempt goes on from an earlier one's checkpoint (``_resume``).
    _resumed = False
    # The first step a resumed attempt runs.
    _resume_index = 0
    # A resumed attempt whose first step had started on the server: when it
    # started, and how much of its log the job log has.
    _resumed_step_started_at = None
    _resumed_log_offset = 0
    # Since when the server of the running step could not be reached, as
    # the checkpoint says; None while it can.
    _lost_contact_since = None
    # The host whose build lock this attempt took (``HostBuildLockMixin``),
    # which a park keeps in the checkpoint; None when it took none.
    _held_build_lock_host_id = None

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
        # Trusted reader: the action was already authorized at enqueue
        # (``cloud.job._check_job_type_allowed``), and the job runs under
        # the enqueuing user's env — who may be a consultant, below the
        # developer gate on the connection fields. Elevate just this read;
        # everything else keeps the user's env so the ORM guards and the
        # audit attribution still apply.
        conn = host_record.sudo()
        self.host = conn.ip_address
        self.port = conn.port
        self.username = conn.user
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
        # Whether the chunks written since the last publish include a
        # system message: what ``_publish_bus`` tells the watchers.
        self._flushed_system = False
        # Bus recipients for this run, resolved lazily on the first
        # publish and reused afterwards. See ``_publish_bus``.
        self._bus_audience_ids = None
        # Versioned-script plumbing. ``run_script()`` flips
        # ``_scripts_requested`` while ``get_commands()`` builds its
        # tuples; the overlay is then uploaded once, before the first
        # command runs. See the VERSIONED SCRIPT HELPERS section.
        self._scripts_requested = False
        self._scripts_uploaded = False
        self._script_overlay_cache = None

    def _host(self):
        """Return the ``cloud.host`` record this job runs against.

        Defined here rather than left to subclasses because it was a
        convention several of them implemented with this exact line
        while others simply called it. Two callers on the instance side
        did the latter — ``DeleteInstanceExecutor.on_success`` on its
        re-run path and ``MoveCleanupSourceExecutor.on_success`` on
        every run — and raised ``AttributeError`` from inside a success
        handler, which reports finished work as failed. A convention
        that only some of the classes honour is not a convention.

        ``job.host_id`` is required on ``cloud.job``, so this is always
        a real record; it stays valid after the instance is unlinked,
        which is the case that exposed the gap.
        """
        return self.job.host_id

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
                    # Only notify when the tick actually produced log
                    # rows. The loop ticks every ``sleep_interval``
                    # regardless of output, so an unconditional publish
                    # turned a long silent step (a multi-minute
                    # ``docker compose build``) into a steady 2 events/s
                    # heartbeat to every watcher — each one costing the
                    # SPA a full refetch. Watchers that need a floor on
                    # latency (the standalone log page) keep their own
                    # fallback poll.
                    if self._flush_logs():
                        self._publish_bus()
                    # Not once the run has ended: one that ended well as
                    # the flag was set ended cancelled, and its chain with
                    # it — a cutover that had cut over, then the freeze of
                    # its source. Nor once its outcome hook, or
                    # ``on_abort``, has started: cut off there, what the
                    # hook wrote was undone, and ``on_failure`` closed
                    # steps that had all passed.
                    if not (main_task.done() or self._outcome_started):
                        self._check_cancel()
                # Drain anything written between the last tick and
                # the final await inside _async_entry. Published
                # unconditionally: this is the last word on the job, so
                # watchers must settle on it even if the tail was empty.
                self._flush_logs()
                self._publish_bus()
                exc = main_task.exception()
                if exc is not None:
                    raise exc
                if self._outcome_dispatched:
                    # Again, at the very end of a run that succeeded: a
                    # stop committed since ``on_success`` returned —
                    # while the staging was discarded, the transport
                    # closed — still found the job unmarked.
                    self._mark_success_before_cancel()
            except BaseException as exc:
                # Cancel the running task if we abandon the loop
                # (e.g. _check_cancel raised because the job was
                # cancelled out-of-band). Wait for it to honour the
                # CancelledError before closing the loop.
                stopped = not main_task.done()
                if stopped:
                    main_task.cancel()
                    with contextlib.suppress(BaseException):
                        self._loop.run_until_complete(main_task)
                # The stop, when it is what ended the run: the task was
                # stopped here, or raised the stop itself.
                if isinstance(exc, JobCancelled) and (
                    stopped
                    or (not main_task.cancelled() and main_task.exception() is exc)
                ):
                    # The task is gone, and with it whatever it had not
                    # cleaned up: a deploy left in 'deploying' could be
                    # neither deployed again nor deleted (issue #2).
                    self._loop.run_until_complete(self._dispatch_cancel(exc))
                    # What it wrote to the job log; a failure here must
                    # not turn the cancellation into another error.
                    try:
                        self._flush_logs()
                        self._publish_bus()
                    except Exception:  # noqa: BLE001 — logged, never fatal
                        logger.warning(
                            "[job %s] could not write the cancellation's log",
                            self.job.id, exc_info=True,
                        )
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
        connected = False
        try:
            self._sys(f"Connecting to {self.host}:{self.port}...")
            async with self._host_record.get_transport() as transport:
                connected = True
                logger.debug("[_async_entry] Connection established")
                self._sys("Connection established.")
                if self._lost_contact_since:
                    self._write_checkpoint({"lost_contact_since": False})
                    self._lost_contact_since = None
                # What the job left on the server for a later attempt stays.
                keep_staging = False
                try:
                    if self._resumed:
                        # The plan as the first attempt materialised it,
                        # with the scripts and the staging it uploaded.
                        commands = [
                            tuple(step) for step in self._checkpoint_data["plan"]
                        ]
                    else:
                        # What an earlier run left that this one does not
                        # take up goes first: its row is not written over.
                        await self._close_stale_checkpoint(transport)
                        await self.before_execute(transport)

                        # Materialise the commands before running any of
                        # them: ``run_script()`` queues its uploads while
                        # ``get_commands()`` builds the tuples, so the
                        # scripts have to reach the host before the first
                        # command fires.
                        commands = list(self.get_commands())
                        await self._upload_scripts(transport)

                    results, skipped = await self._run_steps(
                        transport, commands,
                    )
                    if self._may_park() and self._budget_spent():
                        raise self._park(len(commands), (
                            "This attempt's time is up: the job closes in"
                            " a few seconds."
                        ))
                    # A step that failed and stopped the run leaves the
                    # hooks nothing to fetch.
                    if not skipped and self._outcome_wants_fresh_attempt(
                        len(commands),
                    ):
                        raise self._park(len(commands), (
                            "The result is fetched in a fresh attempt, with"
                            " all of its time: the job goes on in a few"
                            " seconds."
                        ))
                    # Before the cleanup below, so ``after_commands`` can
                    # still run a script.
                    await self._dispatch_outcome(
                        results, transport, skipped=skipped,
                    )
                except Exception as exc:
                    keep_staging = (
                        isinstance(exc, JobParked) or self._contact_lost(exc)
                    )
                    raise
                finally:
                    # Outermost, so what ``before_execute`` staged — a
                    # secret, the scripts — is gone even when that hook
                    # is what failed.
                    if not keep_staging:
                        await self._discard_staging(transport)

            logger.debug("[_async_entry] Finishing _async_entry")
        except Exception as e:
            if not isinstance(e, JobParked) and (
                self._contact_lost(e)
                # An attempt that resumes the job and cannot reach its
                # server: what the job left there waits for it, between
                # two steps as well as during one.
                or (
                    self._resumed and not connected
                    and is_transient_connection_error(e)
                )
            ):
                parked = self._park_on_lost_contact(e)
                if parked is not None:
                    logger.info("[_async_entry] Job parked: %s", parked)
                    raise parked from e
            if isinstance(e, JobParked):
                # Nothing ends: the job goes on in a later attempt.
                logger.info("[_async_entry] Job parked: %s", e)
                raise
            if self._resumed and not connected:
                # What the first attempt staged is still on the server, and
                # this one could not remove it: the checkpoint stays, for
                # whoever reaches the server next.
                self._detached_left_alive = True
            retrying = self._will_be_retried(e)
            cancelled = isinstance(e, JobCancelled)
            # Neither ``on_success`` nor ``on_failure`` ran, and nothing
            # will run this job again: ``on_abort`` closes it.
            aborting = not (retrying or cancelled or self._outcome_dispatched)
            if retrying:
                # The job runs again, and a host that stays away raises
                # ``host_unreachable`` when the retries run out. An ERROR
                # per attempt would reach whoever scrapes this database's
                # log as an incident — every scheduled 04:00 reboot of a
                # host for its security updates.
                logger.warning(
                    "[_async_entry] Attempt failed, the job will retry: "
                    "%s: %s", type(e).__name__, e,
                )
            elif cancelled:
                # A person asked for it; ``queue_job_ext`` records it, and
                # ``run`` closes the job (``_dispatch_cancel``).
                logger.info("[_async_entry] Job cancelled: %s", e)
            elif self._outcome_dispatched or (
                _is_refusal(e) and not self._hook_raised
            ):
                # A step that failed (``on_failure`` already ran and the
                # job log holds its output) or a refusal of our own: the
                # message is the whole story. Its traceback, at ERROR,
                # reached the platform's log scraper as an incident of
                # the panel's every time a customer's job failed for a
                # reason the customer had already been told (16-oct-2026).
                logger.warning("[_async_entry] Job failed: %s", e)
            else:
                logger.exception("[_async_entry] Exception: %s", e)
            if not (cancelled or (aborting and self._handles_abort())):
                self._sys(f"✗ {type(e).__name__}: {e}")
            if aborting:
                await self._dispatch_abort(e)
            elif retrying:
                # Run again from the start: the next attempt does not take
                # up this one's place (a busy cron must run its step again).
                self._forget_checkpoint()
            raise

    def _will_be_retried(self, exc):
        """Whether *exc* ends this attempt only, not the job.

        queue_job runs the job again after a ``RetryableJobError`` and
        after a lost serialisation race; ``cloud.job.execute`` turns a
        transient connection failure into the former for executors that
        opt in. None of them is the job's end, so none gets ``on_abort``.

        :param Exception exc: what stopped this attempt
        :rtype: bool
        """
        if isinstance(exc, RetryableJobError):
            return True
        if isinstance(exc, OperationalError):
            return exc.pgcode in PG_CONCURRENCY_ERRORS_TO_RETRY
        return bool(
            self._retry_on_connection_loss
            and is_transient_connection_error(exc)
        )

    async def _run_steps(self, transport, commands):
        """Run the job's steps in order and report what ran and what did not.

        A step is ``(label, command)`` or ``(label, command, opts)``. The
        option ``timeout`` gives the step its own seconds instead of the
        transport's default. Two options shape what a non-zero exit does:

        - ``stop_on_failure`` — the remaining steps are not run.
        - ``continue_on`` — exit codes the step uses to say something
          other than "failed" (e.g. "nothing to do"). They never stop the
          run, whatever ``stop_on_failure`` says; whether they count as an
          error is still ``parse_results``'s call.

        :param transport: open transport the commands run through.
        :param list commands: the materialised ``get_commands()`` output.
        The option ``detached`` runs the step on the server apart from the
        connection (``_run_detached``), within the same ``timeout``: it
        outlives the request, and answers as ``transport.execute`` would.
        A plan with such a step keeps its place in ``cloud.job.checkpoint``
        from before its first step to its end. Its attempt parks
        (``JobParked``) once its budget is spent: before a step, or while
        it follows a step apart. An attempt that resumes the job
        (``_resume``) starts at the checkpoint's step, with the results of
        the steps before it.

        :return: ``(results, skipped)``. ``results`` maps each label that
            ran to ``{'stdout': str, 'exit_status': int}``; ``skipped``
            lists, in order, the labels a ``stop_on_failure`` step cut
            off (empty when everything ran).
        """
        logger = logging.getLogger("AbstractExecutor")
        # Shared with ``_step_results``: a cancellation halfway hands
        # ``on_failure`` what had run.
        results = self._step_results = {}
        first = 0
        if self._resumed:
            results.update(self._checkpoint_data.get("results") or {})
            first = self._resume_index
            # The directory of the step apart before this one, recorded and
            # maybe not removed, and those ``clean`` had to leave.
            await self._clean_ended_steps(transport, first)
            cut = self._checkpoint_data.get("skipped")
            if cut is not None:
                # A step stopped the run before the attempt parked.
                return results, list(cut)
        elif any(len(item) == 3 and item[2].get("detached") for item in commands):
            await self._open_checkpoint(transport, commands)
        for index, item in enumerate(commands):
            if index < first:
                continue
            label, command = item[0], item[1]
            opts = item[2] if len(item) == 3 else {}
            if self._may_park() and self._budget_spent():
                raise self._park(index, (
                    "This attempt's time is up: the job goes on in a few"
                    " seconds."
                ))
            # The step an earlier attempt left running on the server: it
            # passed its point of no return and said it runs back then.
            resumed = index == first and self._resumed_step_started_at is not None
            logger.debug("[_run_steps] Executing: %s", command)
            if label == self._no_return_from and not resumed:
                self._pass_no_return(label)
            self._running_step = label
            if not resumed:
                self._sys(f"Running: {label}")
            # ``timeout``: a step that needs longer than the transport's
            # default per command (``fetch-repos``).
            extra = {"timeout": opts["timeout"]} if opts.get("timeout") else {}
            if opts.get("detached"):
                result = await self._run_detached(
                    transport, index, label, command, opts, resume=resumed,
                )
            else:
                result = await transport.execute(
                    command, self.on_stdout, self.on_stderr, **extra,
                )
            status = result.exit_status
            results[label] = {'stdout': result.stdout, 'exit_status': status}
            stops = (
                status != 0
                and status not in opts.get('continue_on', ())
                and opts.get('stop_on_failure')
            )
            skipped = [step[0] for step in commands[index + 1:]] if stops else None
            if self._checkpoint:
                self._save_checkpoint_step(
                    len(commands) if stops else index + 1, skipped,
                )
            if self._ended_apart is not None:
                ended, self._ended_apart = self._ended_apart, None
                await self._clean_detached(transport, *ended)
            if status == 0:
                continue
            if status in opts.get('continue_on', ()):
                self._sys(
                    f"'{label}' exited with status {status}, which this"
                    f" step accepts — continuing."
                )
                continue
            if stops:
                self._sys(
                    f"✗ '{label}' failed (exit {status})"
                    f" — aborting remaining steps."
                )
                return results, skipped
            self._sys(f"✗ '{label}' exited with status {status}.")
        return results, []

    def _outcome_errors(self, results, skipped=()):
        """Return the job's errors: ``parse_results`` plus skipped steps.

        A run that skipped steps can never succeed, whatever
        ``parse_results`` thinks of the step that stopped it. An executor
        may legitimately forgive an exit code there — but then it must
        not stop the run on it (``continue_on``), because the steps it
        cut off are part of what the job promises. Until 2026-10-01 the
        delete forgave its purge's "already empty" after the purge had
        stopped the run, so it reported instances removed whose
        containers, volumes and directory were all still on the host.

        ``parse_results`` runs first and its exceptions propagate: a
        ``RetryableJobError`` (the rebuild's exit 75) is a retry, not a
        failure, and must stay one.

        :param dict results: what ran, as returned by ``_run_steps``.
        :param skipped: labels of the steps that never ran.
        :return: list of error strings (empty means success).
        """
        errors = self.parse_results(results)
        if skipped and not errors:
            stopper = next(reversed(results), "a step")
            errors = [
                f"'{stopper}' stopped the run, so these steps never ran: "
                + ", ".join(f"'{label}'" for label in skipped)
            ]
        return errors

    async def _dispatch_outcome(self, results, transport=None, skipped=()):
        """Run ``parse_results`` and fire the matching terminal hook.

        Both callbacks run on their own cursor so what they write is
        committed independently of the job's transaction, and the
        executor's ``job``/``env`` are restored afterwards.

        Raises ``RuntimeError`` after ``on_failure`` so the caller (and
        queue_job) sees the job as failed.

        :param dict results: ``{label: {'stdout': str, 'exit_status': int}}``
        :param transport: open transport, when the executor has one.
            ``after_commands`` needs it, so it is skipped when there is
            none (an Ansible-backed job opens no transport of its own).
        :param skipped: labels a ``stop_on_failure`` step cut off; any
            of them makes the run a failure (see ``_outcome_errors``).
        """
        logger = logging.getLogger("AbstractExecutor")
        errors = self._outcome_errors(results, skipped)
        hook, args = (
            (self.on_success, (results,)) if not errors
            else (self.on_failure, (results, errors))
        )
        if not errors and transport is not None:
            await self.after_commands(transport, results)
        logger.debug(
            "[_dispatch_outcome] %s START job_id=%s errors=%s",
            hook.__name__, self.job.id, errors,
        )
        # From here the run is closing: a stop is no longer read
        # (``run``). ``after_commands`` above can still be stopped.
        self._outcome_started = True
        try:
            await self._run_hook_durably(hook, *args, record_outcome=not errors)
        except Exception:
            # Whatever it raised, a ``UserError`` included, is a defect
            # of ours: logged with its traceback, not as a refusal.
            self._hook_raised = True
            raise
        # Only once the hook returned: one that raised has not dealt with
        # the outcome, and is handed to ``on_abort``.
        self._outcome_dispatched = True
        logger.debug(
            "[_dispatch_outcome] %s END job_id=%s", hook.__name__, self.job.id,
        )
        if not errors:
            self._mark_success_before_cancel()
        self._refresh_host_disk()
        if errors:
            raise RuntimeError("; ".join(errors))

    def _mark_success_before_cancel(self):
        """Record that ``on_success`` returned on a job a person cancelled.

        The run is not stopped once its outcome hook started, but the stop
        flag ``cancel_job`` committed on the job's row while it ran still
        made the store of ``done`` fail to serialize: queue_job put the job
        back in the queue, ``queue_job_ext`` read the flag there and stored
        it cancelled, and ``_end_chain_after`` cancelled what followed — a
        cutover that had cut over, then the freeze of its source
        (6-nov-2026). With ``succeeded_before_cancel`` set, ``queue_job_ext``
        stores ``done`` instead.

        Written only on a job already cancelled: on any other, a write of
        its row from this cursor would make that same store fail. Called
        once ``on_success`` returns and again at the end of ``run``, so a
        stop committed while the staging was discarded or the transport
        closed is marked too; only one committed after ``run`` returned,
        before the job is stored, still ends it cancelled. Idempotent. The
        mark is cleared when the job starts again (``queue_job_ext``).
        Never raises: the outcome is decided.
        """
        try:
            with self._durable_env() as env:
                env.cr.execute(
                    "UPDATE cloud_job SET succeeded_before_cancel = TRUE"
                    " WHERE id = %s AND state = 'cancelled'",
                    (self.job.id,),
                )
        except Exception as exc:  # noqa: BLE001 — logged, never fatal
            logging.getLogger("AbstractExecutor").warning(
                "[job %s] could not record its success before the stop: %s",
                self.job.id, exc,
            )

    def _refresh_host_disk(self):
        """Read the job's host's disk from metrics again, if this job asks to.

        For executors that set ``_refreshes_host_disk``, once their
        outcome hook ran: on a durable cursor of its own, so it is
        committed whatever the job's transaction does. Never raises: the
        job's outcome is already decided, and a disk that could not be
        read is the cron's to read in five minutes.
        """
        if not self._refreshes_host_disk:
            return
        try:
            with self._durable_env() as env:
                env["cloud.host"].browse(
                    self._host().id,
                ).exists()._refresh_specs_from_metrics_now()
        except Exception as exc:  # noqa: BLE001 — logged, never fatal
            logging.getLogger("AbstractExecutor").warning(
                "[job %s] could not refresh the host's disk: %s",
                self.job.id, exc,
            )

    async def _run_hook_durably(self, hook, *args, record_outcome=False):
        """Await *hook* with ``self.job``/``self.env`` on a durable cursor.

        A durable environment, READ COMMITTED: this is where
        ``on_success`` stamps ``cloud_instance``, the same rows the
        metrics cron stamps on its own schedule. See ``_concurrency`` for
        why losing that race is worse than waiting for it, and
        ``_durable_env`` for why the wait is capped. The executor's
        ``job``/``env`` are restored afterwards.

        Whichever hook it is, it closes the job: the job's checkpoint goes
        in the same transaction, unless something of a step apart was left
        on the server (``_detached_left_alive``); then it is marked
        ``closed`` there instead, so no attempt resumes the job from it.

        :param hook: coroutine function to await
        :param args: positional arguments for *hook*
        :param bool record_outcome: leave the job's outcome mark, as the
            hook's last statement and on its cursor
            (``_outcome_mark_vals``): for ``on_success`` only
        """
        forget = self._checkpoint and not self._detached_left_alive
        with self._as_durable() as env:
            await hook(*args)
            if record_outcome and self.job.queue_job_uuid:
                env.flush_all()
                env["cloud.job.outcome"].sudo().create(
                    self._outcome_mark_vals(),
                ).flush_recordset()
            if forget:
                env.flush_all()
                env.cr.execute(
                    "DELETE FROM cloud_job_checkpoint WHERE job_id = %s",
                    (self.job.id,),
                )
            elif self._checkpoint:
                env.flush_all()
                env.cr.execute(
                    "UPDATE cloud_job_checkpoint SET closed = TRUE, parked = FALSE"
                    " WHERE job_id = %s",
                    (self.job.id,),
                )
        if forget:
            self._checkpoint = False

    def _outcome_mark_vals(self):
        """Return the outcome mark of a run whose ``on_success`` returned.

        Written by ``_run_hook_durably`` as the last statement of the
        hook's transaction, so the mark commits with what the hook wrote
        or not at all: a hook that raises, or a worker that dies before
        that commit, leaves none, and the next attempt runs the job again
        as before. One that finds the mark — the store of ``done`` lost a
        serialization race after this commit, or the worker died after
        it — runs nothing again (``cloud.job.execute``), writes back what
        this run wrote on the job's own row (``_write_own_job``), which
        went with the lost transaction, and arms again what it left for
        that commit (``_unlink_after_job_commit``).

        Only for a job queue_job runs: its uuid names the attempts the
        mark answers for.

        :return: ``cloud.job.outcome`` values
        :rtype: dict
        """
        return {
            "job_id": self.job.id,
            "queue_job_uuid": self.job.queue_job_uuid,
            "own_job_vals": self._own_job_vals or {},
            "owed": list(self._owed or ()),
        }

    #: What ``_write_own_job`` wrote in this run, for the outcome mark.
    _own_job_vals = None
    #: What this run left for after the job's commit, for the outcome
    #: mark: ``{'kind': ...}`` entries ``cloud.job._rearm_owed`` knows.
    _owed = None

    @contextlib.contextmanager
    def _as_durable(self):
        """Run the block with ``self.job``/``self.env`` on a durable cursor.

        What the block writes through them commits when it ends, whatever
        becomes of the job's transaction; both are restored afterwards.
        See :meth:`_run_hook_durably`.
        """
        # The job's own cursor, pinned before the swap below: a hook that
        # needs something to happen *after* the job commits registers it
        # there (``_unlink_after_job_commit``), never on the hook cursor,
        # whose commit is the very thing that must be waited for.
        self._job_cr = self.job.env.cr
        with self._durable_env() as env:
            _orig_job, _orig_env = self.job, self.env
            self.job = env['cloud.job'].browse(self.job.id)
            self.env = self.job.env
            try:
                yield env
            finally:
                self.job, self.env = _orig_job, _orig_env

    def _handles_abort(self):
        """Whether this executor overrides ``on_abort``.

        An override writes the failure to the job log itself (a deploy's
        ``on_failure`` does), so the run does not write it a second time.

        :rtype: bool
        """
        return type(self).on_abort is not AbstractExecutor.on_abort

    async def _dispatch_abort(self, exc):
        """Give ``on_abort`` the exception that ended the run before its steps.

        Never raises: the caller is re-raising *exc*, and a failure here
        must not take its place. Logged instead.

        The host's disk is read again afterwards, as after an outcome
        (``_refresh_host_disk``): a build or a teardown that stopped half
        way changed it too.

        :param Exception exc: what stopped the run
        """
        if self._handles_abort():
            # The run is closing, as once an outcome hook starts: a stop
            # read now would cut this hook off, undo what it wrote and
            # run it a second time (``_dispatch_cancel``).
            self._outcome_started = True
            try:
                await self._run_hook_durably(self.on_abort, exc)
            except Exception:
                logging.getLogger("AbstractExecutor").exception(
                    "[_dispatch_abort] on_abort failed for job %s",
                    getattr(getattr(self, "job", None), "id", None),
                )
        # The job ends here, with or without a hook to take the row along.
        self._forget_checkpoint()
        self._refresh_host_disk()

    async def _dispatch_cancel(self, exc):
        """Close a run a person cancelled as a failure would close it.

        Nothing else would: the job ends ``cancelled`` (``queue_job_ext``)
        with neither ``on_failure`` nor ``on_abort`` run, and a deploy
        stayed in 'deploying' for good, refused by every deploy after it
        and by its delete (issue #2, 6-nov-2026). ``on_cancelled_running``
        runs on a durable cursor like the other terminal hooks, with
        ``ic_job_cancelled`` in its context: ``cloud.alert.raise_alert``
        raises nothing there, as a person asked for the stop. Not after
        an outcome hook returned: that one closed the job already.

        Never raises: the caller is re-raising *exc*. The host's disk is
        read again afterwards, as after an outcome.

        :param JobCancelled exc: the stop
        """
        if self._outcome_dispatched:
            return
        orig_job = self.job
        self.job = orig_job.with_context(ic_job_cancelled=True)
        self.env = self.job.env
        try:
            await self._run_hook_durably(
                self.on_cancelled_running, self._step_results, exc,
            )
        except Exception:
            logging.getLogger("AbstractExecutor").exception(
                "[_dispatch_cancel] on_cancelled_running failed for job %s",
                orig_job.id,
            )
        finally:
            self.job, self.env = orig_job, orig_job.env
        # The job ends here, even when its hook raised.
        self._forget_checkpoint()
        self._refresh_host_disk()

    def _unlink_after_job_commit(self, inst, sudo=False):
        """Unlink *inst* once this job's own transaction has committed.

        A success hook runs on its own cursor (``_dispatch_outcome``) so
        what it writes survives the job's transaction. Unlinking an
        instance there is the one write that cannot: ``cloud_job``
        references it ``ON DELETE SET NULL``, so the unlink's commit
        updates this very job's row, and the job's transaction — whose
        snapshot ``job.lock()`` fixed before any of this ran — then
        fails to mark itself done (``could not serialize access due to
        concurrent update``) and queue_job runs the whole job again.
        Measured in production on 2026-09-19: every successful
        ``delete_instance`` ran twice; no other job type ever did.

        Nothing inside the transaction can fix that. A commit is
        forbidden by queue_job (it would release the job lock); moving
        the terminal ``UPDATE`` to a fresh cursor still leaves the
        stored related ``cloud.job.state`` to the ORM on the old
        snapshot; and touching the job row *before* the hook would
        make the hook's cascade block on it, which is a hang, not an
        error. So the unlink waits for the commit: ``postcommit`` fires
        right after it, on a cursor of its own, when nobody holds the
        row.

        If the job does not commit, the rollback clears ``postcommit``
        and the unlink is simply dropped: the record stays exactly as
        the hook left it (marked, for the teardown; archived, for the
        purge), and the re-run finds that state and arms this again. A
        job stored done after a stop runs no more: the cursor that stores
        it arms the unlink again (``queue_job_ext``).

        Never raises. By the time it runs the job is already done, so
        a failure here is logged and raised as an alert rather than
        turning a finished job into a failed one after the fact.

        :param inst: the ``cloud.instance`` to remove.
        :param sudo: unlink as superuser: the platform completing a
            removal authorised when it was asked for (a delete's, an
            archived instance's purge).
        """
        job_env = self.job.env
        # ``_job_cr`` is set by ``_dispatch_outcome``; a hook invoked
        # directly (tests) has only its own cursor, which is the job's.
        main_cr = getattr(self, "_job_cr", None) or job_env.cr
        main_cr.postcommit.add(deferred_instance_unlink(
            job_env, inst, self.job.id, self._host().id, sudo=sudo,
        ))
        # For the outcome mark: a repetition that finds it arms the
        # unlink again (``cloud.job._rearm_owed``).
        self._owed = [*(self._owed or ()), {
            "kind": "instance_unlink", "instance_id": inst.id,
        }]

    # ===============================
    # DURABLE WRITES
    # ===============================

    @contextlib.contextmanager
    def _durable_env(self):
        """Yield an environment whose writes outlive this job's transaction.

        The rule: **the job's transaction is not written.** While an
        executor runs, whatever it writes through ``self.env`` is lost
        one way or another:

        * ``cloud.job.execute`` ends with ``env.clear()``, which drops
          every write not flushed yet. Core 1.0.135 started
          VictoriaMetrics with a deletion key the database never
          received that way.
        * queue_job rolls the whole transaction back when the job fails
          or is retried, and forbids committing it midway, because the
          commit would release the job's lock.
        * A write that did get flushed holds its row until the job ends,
          so a hook writing the same row on its own cursor waits for a
          transaction that is waiting for the hook.

        Writes that must last go through here or through :meth:`_persist`.
        ``on_success``/``on_failure`` already run inside one (see
        :meth:`_dispatch_outcome`) and write with ``self.env``; the one
        write that waits for the job's commit instead is
        :meth:`_unlink_after_job_commit`.

        What is written here stays invisible to the job's own
        transaction, which reads the snapshot it took when the job
        started: invalidating its cache and reading again returns the old
        value. Use :meth:`_persist` when the rest of the job must see it.

        The cursor is fresh and READ COMMITTED (see ``_concurrency``).
        On a real one the lock wait is capped at
        ``DURABLE_LOCK_TIMEOUT``, so the deadlock above surfaces as an
        error instead of a stuck worker. Under tests ``registry.cursor()``
        may hand back a pseudo-cursor riding on the test's transaction,
        which is left untouched for the reason ``read_committed_cursor``
        leaves its isolation alone.

        Commits on a clean exit and rolls back if the block raises. The
        environment keeps the job's user, superuser flag and context.

        :return: a context manager yielding an ``api.Environment``
        """
        with read_committed_cursor(self.job.env.registry) as cr:
            if isinstance(cr, Cursor):
                cr.execute(
                    "SET LOCAL lock_timeout = %s", (DURABLE_LOCK_TIMEOUT,),
                )
            yield self.job.env(cr=cr)

    def _call_addon_hook(self, inst, name, *args):
        """Call ``inst.<name>(*args)`` from an outcome hook, in a savepoint.

        The job did what it was for, whatever an addon does next: a hook
        that raises has its own writes rolled back and is reported in the
        job's log, and the job's outcome stands.

        :param inst: the ``cloud.instance`` the hook is called on
        :param str name: ``_after_stack_rendered`` or ``_after_restore_hook``
        :param args: the hook's arguments
        """
        try:
            with inst.env.cr.savepoint():
                getattr(inst, name)(*args)
        except Exception as exc:  # noqa: BLE001 — never undo the job's outcome
            logging.getLogger(__name__).warning(
                "Job %s: %s of instance %s raised.", self.job.id, name, inst.id,
                exc_info=True,
            )
            self._sys(f"⚠ A follow-up step for '{inst.name}' failed: {exc}")

    def _write_own_job(self, vals):
        """Write *vals* on this job's own row, in the job's transaction.

        The one write of a hook that does not go on its durable cursor.
        That cursor commits when the hook ends, before the job's
        transaction, whose snapshot predates it, stores the job ``done``
        on the same row: PostgreSQL answers ``could not serialize access
        due to concurrent update``, and queue_job runs the job again in
        5 s without counting the attempt — for ever, on a move, with the
        panel stopped. Written where ``done`` is written, the values land
        with it or not at all, and the next step of a chain, which starts
        once that transaction committed, reads them. Flushed now:
        ``cloud.job.execute`` ends with ``env.clear()``, which drops
        writes not flushed yet.

        :param dict vals: values of ``cloud.job`` fields, in ``write``
            format
        """
        # ``_job_cr`` is set by ``_dispatch_outcome``; a hook invoked
        # directly (tests) has only its own cursor, which is the job's.
        cr = getattr(self, "_job_cr", None) or self.job.env.cr
        job = self.job.with_env(self.job.env(cr=cr)).sudo()
        job.write(vals)
        job.flush_recordset(list(vals))
        # For the outcome mark, which outlives this transaction: an
        # attempt that finds it writes them again (``cloud.job.execute``).
        self._own_job_vals = {**(self._own_job_vals or {}), **vals}

    def _persist(self, record, vals):
        """Write *vals* on *record* durably and let this job see the result.

        :meth:`_durable_env` makes a write last; this also makes it
        visible. Invalidating the job's cache is not enough — its
        transaction would read the old value back from its snapshot, and
        that is how a deployment merged a new requirement into the
        database and then uploaded the list from before the merge. The
        committed values are put in the job's cache instead, as clean
        entries, so the job's own flush never writes them a second time.

        What the write recomputed comes along: stored computed fields of
        the same record that depend on *vals* (``rebuild_fingerprint``
        after ``pip_dependencies``) are copied, and non-stored ones are
        dropped from the cache to recompute from the new values.
        Dependents on other records are not followed.

        The write keeps *record*'s own user, superuser flag and context,
        so ``record.sudo().with_context(...)`` behaves as it would with a
        plain ``write``. Never call it inside :meth:`_durable_env`: the
        two cursors would contend for the same row.

        :param record: a single record bound to the job's environment
        :param dict vals: values in ``write`` format
        :raise ValueError: if *vals* names a one2many or many2many field,
            whose commands do not translate into cache values
        """
        record.ensure_one()
        relational = [
            name for name in vals
            if record._fields[name].type in ("one2many", "many2many")
        ]
        if relational:
            raise ValueError(
                "_persist() cannot copy x2many fields into the job's "
                f"cache: {', '.join(relational)}"
            )
        with self._durable_env() as env:
            durable = record.with_env(record.env(cr=env.cr))
            durable.write(vals)
            values, volatile = self._durable_values(durable, vals)
        self._mirror_to_job_cache(record, values, volatile)

    @staticmethod
    def _durable_values(durable, fnames):
        """Return what *durable* now holds for *fnames* and their dependents.

        Read on the durable cursor after the write, so the values are the
        ones about to be committed, including whatever the ORM recomputed
        from them.

        :param durable: a single record bound to a durable environment
        :param fnames: names of the fields that were written
        :return: ``(values, volatile)`` — ``values`` maps the written and
            the dependent stored fields to their values in ``write``
            format (relational ones as ids, never as records bound to a
            cursor about to close); ``volatile`` is the set of non-stored
            dependents to drop from the job's cache
        """
        model = durable._name
        stored, volatile = set(fnames), set()
        for name in fnames:
            for dep in durable.pool.get_dependent_fields(durable._fields[name]):
                if dep.model_name != model:
                    continue
                if not dep.store:
                    volatile.add(dep.name)
                elif dep.column_type:
                    stored.add(dep.name)
        values = {
            name: durable._fields[name].convert_to_write(durable[name], durable)
            for name in stored
        }
        return values, volatile - stored

    @staticmethod
    def _mirror_to_job_cache(record, values, volatile=()):
        """Make the job's transaction see values committed on another cursor.

        One limit: a many2one naming a row *created* on the durable
        cursor points at a row the job's snapshot cannot see, so reading
        through it from the job fails. Leave such a link out of the copy.

        :param record: the record as the job's environment holds it
        :param dict values: stored values, as :meth:`_durable_values`
            returns them
        :param volatile: non-stored field names to recompute on next read
        """
        record._update_cache(values, validate=False)
        if volatile:
            record.invalidate_recordset(list(volatile), flush=False)

    # ===============================
    # DB + BUS INFRASTRUCTURE
    # ===============================

    def _sys(self, msg):
        """Append a system-level progress message to the log buffer."""
        self._log_buffer.append((msg, "system"))

    def _flush_logs(self):
        """Persist the buffered lines; return how many chunks were written.

        The count is what drives the bus: ``run()`` only notifies
        watchers on a tick that actually produced rows. Counting real
        ``create`` calls rather than ``bool(entries)`` matters for a
        capped job — the branch below drains the buffer and writes
        nothing, so a buffer-based signal would keep the notification
        firing forever with no new content behind it.

        Deliberately counted here and not flagged in ``_sys`` /
        ``on_stdout`` / ``on_stderr``: several tests rebind those to
        ``_log_buffer.append``, which would bypass a flag set there.

        The offset a step apart's lines end at (an entry ``_LOG_OFFSET``,
        put after them by ``_read_detached``) is written on the same
        cursor as the lines: a run that dies loses none of them, and the
        next read of the step does not repeat them.

        :returns: number of ``cloud.job.log.chunk`` rows created.
        """
        if not self._log_buffer:
            return 0
        entries = []
        offset = None
        for line, source in self._log_buffer:
            if source == _LOG_OFFSET:
                offset = line
            else:
                entries.append((line, source))
        self._log_buffer.clear()
        # If we already emitted the cap marker, drop everything past
        # this point silently. The job keeps running but stops bloating
        # the logs table.
        if self._cap_marker_emitted and offset is None:
            return 0
        written = 0
        with self._durable_env() as env:
            if offset is not None:
                env.cr.execute(
                    "UPDATE cloud_job_checkpoint SET log_offset = %s"
                    " WHERE job_id = %s",
                    (offset, self.job.id),
                )
            if self._cap_marker_emitted:
                entries = []
            Log = env['cloud.job.log.chunk'].sudo()
            for line, source in entries:
                if source == 'system':
                    self._flushed_system = True
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
                    self._flushed_system = True
                    written += 1
                    break
                Log.create({
                    'job_id': self.job.id,
                    'content': _redact_tokens(line),
                    'source': source,
                })
                self._chunks_persisted += 1
                written += 1
        return written

    def _publish_bus(self):
        """Notify every watcher that new chunks are available.

        A ``progress`` event, flagged ``system`` when the chunks since
        the last one include a message of this executor's own: a job
        list refreshes on those and ignores plain output
        (``cloud.job._broadcast_job_update``).

        The executor's ``env.user`` is the queue_job worker identity
        (often OdooBot or SUPERUSER), *not* the user watching the
        terminal page. Sending to ``env.user`` only reaches that
        worker's channel, so real operators fall back to the 10 s
        polling loop and the live stream looks stuck.

        We delegate to ``cloud.job._broadcast_job_update`` — the same
        helper used by create() and queue_job_ext state transitions —
        so every user allowed to read the job receives the notification
        on their own presence channel. Hidden job types (host_metrics,
        docker_prune, instance_health) stay filtered out inside that
        helper.

        The audience is resolved once and reused for the rest of the
        run: it costs an ACL check per candidate user, which is fine
        for the one-shot broadcasts but not on a path that repeats for
        every chunk flush of a long build. Project membership does not
        meaningfully change mid-job.
        """
        logger = logging.getLogger("AbstractExecutor")
        with self._durable_env() as env:
            Job = env['cloud.job']
            if self._bus_audience_ids is None:
                self._bus_audience_ids = Job._bus_audience(
                    Job.browse(self.job.id),
                ).ids
            Job._broadcast_job_update(
                self.job.id, audience_ids=self._bus_audience_ids,
                kind='progress', system=self._flushed_system,
            )
            self._flushed_system = False
            logger.debug(
                "[_publish_bus] broadcast done for job_id=%s", self.job.id,
            )

    def _pass_no_return(self, label):
        """Decide, against a stop, whether the step *label* starts.

        On a durable cursor of its own, committed before the step runs:
        the job's advisory lock (``NO_RETURN_LOCK_NAMESPACE``), which
        whoever writes the stop flag on a started job takes too
        (``cloud.job._refuse_stop_past_no_return``); then the flag, read
        at READ COMMITTED. A stop already committed ends the job here,
        the step never started. Otherwise the job's mark is inserted:
        from its commit on, a stop is refused. Nothing is written on
        ``cloud_job``, whose row the job's transaction stores at the end.

        A lock held past ``DURABLE_LOCK_TIMEOUT`` (a request holding it
        that long) does not stop the job: the step goes on unmarked, as
        before this lock existed, and the log says so.

        :param str label: the step about to run
        :raises JobCancelled: when the job was stopped before the step
        """
        try:
            with self._durable_env() as env:
                cr = env.cr
                cr.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (NO_RETURN_LOCK_NAMESPACE, self.job.id),
                )
                cr.execute(
                    "SELECT state FROM cloud_job WHERE id = %s", (self.job.id,),
                )
                row = cr.fetchone()
                if row is None:
                    # Not committed: a job of a test's own transaction.
                    # Nobody can have stopped it, nor see a mark.
                    return
                if row[0] == "cancelled":
                    raise JobCancelled(CANCELLED_WHILE_RUNNING)
                cr.execute(
                    "INSERT INTO cloud_job_no_return (job_id, step)"
                    " VALUES (%s, %s)"
                    " ON CONFLICT (job_id) DO UPDATE SET step = EXCLUDED.step",
                    (self.job.id, label),
                )
        except LockNotAvailable:
            self._sys(
                f"⚠ Could not take the job's stop lock before '{label}': it"
                f" runs unmarked, and a stop asked for now would still cut it."
            )

    def _check_cancel(self):
        with self._durable_env() as env:
            job = env['cloud.job'].browse(self.job.id)
            if job.state == 'cancelled':
                raise JobCancelled(CANCELLED_WHILE_RUNNING)

    # ===============================
    # STEPS APART ON THE SERVER
    # ===============================

    def _write_checkpoint(self, vals):
        """Write *vals* on this job's ``cloud.job.checkpoint``, creating it.

        On a durable cursor of its own, committed at once: the job's
        transaction is rolled back by a retry, and holds the job's row.

        :param dict vals: ``cloud.job.checkpoint`` values
        """
        with self._durable_env() as env:
            Checkpoint = env["cloud.job.checkpoint"].sudo()
            row = Checkpoint.search([("job_id", "=", self.job.id)], limit=1)
            if row:
                row.write(vals)
            else:
                Checkpoint.create({"job_id": self.job.id, **vals})

    def _forget_checkpoint(self):
        """Delete this job's checkpoint, unless a step apart was left alive.

        For a job that ends without a hook to take the row along, and for
        an attempt that ends in a retry from the start. A row kept because
        something was left alive is marked ``closed``: no attempt resumes
        the job from it. Never raises: the row left behind is the
        reconciliation's, and the job's end stands.
        """
        if not self._checkpoint:
            return
        try:
            with self._durable_env() as env:
                if self._detached_left_alive:
                    env.cr.execute(
                        "UPDATE cloud_job_checkpoint SET closed = TRUE,"
                        " parked = FALSE WHERE job_id = %s",
                        (self.job.id,),
                    )
                    return
                env.cr.execute(
                    "DELETE FROM cloud_job_checkpoint WHERE job_id = %s",
                    (self.job.id,),
                )
            self._checkpoint = False
        except Exception as exc:  # noqa: BLE001 — logged, never fatal
            logging.getLogger("AbstractExecutor").warning(
                "[job %s] could not delete its checkpoint: %s", self.job.id, exc,
            )

    async def _open_checkpoint(self, transport, commands):
        """Write the job's checkpoint before the first step of its plan.

        For a plan with a step marked ``detached``: the plan as it was
        materialised, no result yet, the job's working directory and the
        secrets it staged, and the step of the plan a stop must not cut
        (``_no_return_from``) as it was then: whoever closes the job later
        reads it there (``_past_no_return``), not from the instance as it
        is by then. The script that runs the steps apart goes with the
        job's other scripts, uploaded now if none was asked for.

        :param transport: the job's open transport
        :param list commands: the materialised ``get_commands()`` output
        """
        # Asked for, so the overlay it is part of is uploaded.
        self.run_script("detached_step.sh")
        await self._upload_scripts(transport)
        self._detached_uuid = self.job.queue_job_uuid or ""
        self._checkpoint_data = {
            "plan": [
                [item[0], item[1], json.loads(json.dumps(
                    item[2] if len(item) == 3 else {}, default=sorted,
                ))]
                for item in commands
            ],
            "results": {},
            "job_dir": self._job_dir() if self._job_dir_created else None,
            "private_uploads": list(self._private_uploads),
            "carried": self._carried_state(),
            "no_return_from": self._no_return_from,
        }
        self._write_checkpoint({
            "queue_job_uuid": self.job.queue_job_uuid or False,
            "host_id": self._host().id,
            "next_index": 0,
            "detached_started": False,
            "step_started_at": False,
            "log_offset": 0,
            "parked": False,
            "parked_until": False,
            "first_started_at": fields.Datetime.now(),
            "attempts": 1,
            # From the row's first commit, not from a park: a worker that
            # dies before any park lets the lock go with its transaction,
            # and the attempt that resumes the job builds on.
            "build_lock_host_id": self._held_build_lock_host_id or False,
            "data": json.dumps(self._checkpoint_data),
        })
        self._checkpoint = True

    def _save_checkpoint_step(self, next_index, skipped=None):
        """Record in the checkpoint that the steps before *next_index* ended.

        The lines buffered so far are written first, with the offset of
        the step apart they come from: the step's end is recorded after
        all of its log.

        :param int next_index: the first step of the plan not run yet
        :param skipped: the labels a step that stopped the run cut off, or
            None: an attempt that resumes after it runs no step
        """
        if self._flush_logs():
            self._publish_bus()
        self._checkpoint_data["results"] = self._kept_results()
        self._checkpoint_data["carried"] = self._carried_state()
        if skipped is not None:
            self._checkpoint_data["skipped"] = list(skipped)
        self._write_checkpoint({
            "next_index": next_index,
            "detached_started": False,
            "step_started_at": False,
            "log_offset": 0,
            "data": json.dumps(self._checkpoint_data),
        })

    def _start_clock(self):
        """Start this attempt's budget; ``cloud.job.execute`` calls it first.

        ``attempt_budget_seconds`` of the server's configuration, counted
        from now: the connection, the uploads and the steps before a long
        one take their share. No budget when it is 0 (no limit).
        """
        budget = attempt_budget_seconds(config)
        self._attempt_started = time.monotonic()
        self._budget_ends = self._attempt_started + budget if budget > 0 else None
        # The limit the budget is a share of: ``limit_time_real``, or the
        # one a configured budget stands for.
        self._attempt_limit = budget / ATTEMPT_BUDGET_FACTOR if budget > 0 else None

    def _outcome_wants_fresh_attempt(self, total):
        """Whether the closing hooks should start in a fresh attempt.

        For an executor whose hooks fetch through the panel
        (``_outcome_needs_room``) and may park: once this attempt has run
        ``OUTCOME_ROOM_MIN_ELAPSED_S`` and less than ``OUTCOME_ROOM_FACTOR``
        of the request's limit is left. Never for an attempt that resumed
        the job at its hooks: it is the fresh one.

        :param int total: how many steps the plan has
        :rtype: bool
        """
        if not (self._outcome_needs_room and self._may_park()):
            return False
        if self._attempt_limit is None or self._attempt_started is None:
            return False
        if self._resumed and self._resume_index >= total:
            return False
        elapsed = time.monotonic() - self._attempt_started
        return (
            elapsed >= OUTCOME_ROOM_MIN_ELAPSED_S
            and self._attempt_limit - elapsed
            < OUTCOME_ROOM_FACTOR * self._attempt_limit
        )

    def _budget_spent(self):
        """Whether this attempt's budget is spent.

        :rtype: bool
        """
        return self._budget_ends is not None and time.monotonic() >= self._budget_ends

    def _may_park(self):
        """Whether this attempt may leave the job to a later one.

        Only a job that keeps a checkpoint, and none of whose steps marked
        ``detached`` ran attached to the request: such a step dies with it.

        :rtype: bool
        """
        return self._checkpoint and not self._detached_attached

    def _carried_names(self):
        """Return the names of the attributes the checkpoint carries.

        ``_CARRIED_ALWAYS``, then the ``_carried`` of every class of the
        executor, its own first, each name once.

        :rtype: tuple
        """
        names = dict.fromkeys(self._CARRIED_ALWAYS)
        for cls in type(self).__mro__:
            names.update(dict.fromkeys(vars(cls).get("_carried", ())))
        return tuple(names)

    def _carried_state(self):
        """Return the executor's carried attributes (``_carried_names``), by name.

        :rtype: dict
        """
        return {name: getattr(self, name) for name in self._carried_names()}

    def _kept_results(self):
        """Return the steps' results as the checkpoint keeps them.

        Each step's stdout is cut to its last ``DETACHED_STDOUT_TAIL``
        bytes, without the line they cut, as a step apart's is.

        :rtype: dict
        """
        kept = {}
        for label, data in (self._step_results or {}).items():
            stdout = data.get("stdout") or ""
            raw = stdout.encode("utf-8")
            if len(raw) > DETACHED_STDOUT_TAIL:
                stdout = raw[-DETACHED_STDOUT_TAIL:].partition(b"\n")[2].decode(
                    "utf-8", "replace",
                )
            kept[label] = {**data, "stdout": stdout}
        return kept

    def _park(self, next_index, reason, seconds=PARK_GAP_SECONDS, vals=None):
        """Leave the job to a later attempt; return the ``JobParked`` to raise.

        The lines read so far go first, with the offset of the step apart
        they come from (``_flush_logs``). Then, in one durable transaction,
        where the job is: the step it goes on from, the results so far,
        the carried state, ``parked`` and when it connects again. Then the
        job log says why, and its watchers hear of it. Nothing on the
        server is touched: a step running there goes on, and the job's
        staging stays for it.

        :param int next_index: the step the next attempt starts at
        :param str reason: the job log's line
        :param int seconds: how long until the next attempt
        :param dict vals: more checkpoint values to write with the rest
        :rtype: JobParked
        """
        self._flush_logs()
        if self._step_results is not None:
            # An attempt that parks before its steps keeps the row's.
            self._checkpoint_data["results"] = self._kept_results()
        self._checkpoint_data["carried"] = self._carried_state()
        until = fields.Datetime.now() + timedelta(seconds=seconds)
        if self._held_build_lock_host_id:
            # Written before the job's transaction lets the lock go: no
            # other build of the host starts in between (``HostBuildLockMixin``).
            vals = {
                **(vals or {}),
                "build_lock_host_id": self._held_build_lock_host_id,
            }
        self._write_checkpoint({
            **(vals or {}),
            "next_index": next_index,
            "parked": True,
            "parked_until": until,
            "data": json.dumps(self._checkpoint_data),
        })
        self._sys(reason)
        self._flush_logs()
        self._publish_bus()
        return JobParked(
            f"Parked until {fields.Datetime.to_string(until)} UTC: {reason}",
            seconds=seconds,
        )

    def _retake_host_build_lock(self):
        """Take again the host's build lock a parked attempt of the job held.

        Nothing to take for an executor that takes none: see
        ``HostBuildLockMixin``.

        :return: whether the job may go on
        :rtype: bool
        """
        return True

    def _wait_for_build_lock(self):
        """Park the job again for a moment: another job holds its host's lock.

        It is about to defer itself, as it finds this job's checkpoint
        (``HostBuildLockMixin.pre_run_checks``). The checkpoint stays as
        the last attempt left it, marked ``parked`` again — an attempt that
        took it up and died left it unmarked — and when the job comes back
        changes.

        :return: the ``JobParked`` to raise
        """
        until = fields.Datetime.now() + timedelta(seconds=PARK_GAP_SECONDS)
        self._write_checkpoint({"parked": True, "parked_until": until})
        self._sys(
            "Another job holds this host's build lock for a moment: the job"
            f" tries again in {PARK_GAP_SECONDS} s."
        )
        self._flush_logs()
        return JobParked(
            "Waits for the host's build lock, which it holds while it is"
            " parked.",
        )

    def _resume(self, row):
        """Take the job up where an earlier attempt left it.

        Called by ``cloud.job.execute`` with the checkpoint an attempt of
        the same queue job wrote. The attempt adopts what the first one
        set up (``_adopt_checkpoint``) and starts at the checkpoint's
        step: ``before_execute`` and ``get_commands()`` are not called
        again. A step that was started on the server is followed from the
        offset its log had reached. The row is marked as taken up,
        ``parked`` and ``holds_queue_since`` cleared.

        :param row: the job's ``cloud.job.checkpoint``
        """
        self._adopt_checkpoint(row)
        self._write_checkpoint({
            "parked": False,
            "parked_until": False,
            "overdue_noted": False,
            # A mark a closed run left, which a later close would take for
            # its own (``_hold_queue`` keeps the first one).
            "holds_queue_since": False,
            "attempts": row.attempts + 1,
        })

    def _adopt_checkpoint(self, row):
        """Take on what the job's earlier attempts left, as the checkpoint says.

        Its plan, its working directory and the scripts in it, the
        secrets it staged, the steps' results, the carried state, and the
        step that may run on the server. For an attempt that resumes the
        job, and for whoever closes it without running it: a stop when
        the job comes back, the reconciliation.

        :param row: the job's ``cloud.job.checkpoint``
        """
        data = row._data()
        self._checkpoint = True
        self._resumed = True
        self._checkpoint_data = data
        self._resume_index = row.next_index
        self._job_dir_path = data.get("job_dir")
        self._job_dir_created = bool(data.get("job_dir"))
        self._private_uploads = tuple(data.get("private_uploads") or ())
        self._scripts_requested = self._scripts_uploaded = True
        names = set(self._carried_names())
        for name, value in (data.get("carried") or {}).items():
            if name in names:
                setattr(self, name, value)
        if row.detached_started:
            self._on_server_index = row.next_index
            self._resumed_step_started_at = row.step_started_at
            self._resumed_log_offset = row.log_offset
        plan = data.get("plan") or []
        if row.next_index < len(plan) and (row.detached_started or not row.parked):
            # The step an attempt ran when it ended: started apart, or an
            # attempt that died with it running (between its start and the
            # record of it, or attached). Unless its end is learnt, it did
            # not end (``_fail_cut_off_run``).
            self._running_step = plan[row.next_index][0]
        self._lost_contact_since = row.lost_contact_since or None
        self._checkpoint_row = row
        self._detached_uuid = row.queue_job_uuid or ""

    #: The checkpoint adopted (``_adopt_checkpoint``), or None.
    _checkpoint_row = None
    #: The queue job whose attempts run the steps of the checkpoint's plan.
    _detached_uuid = None

    def _detached_dir(self, index):
        """Return the directory step *index* of the checkpoint's plan runs in.

        Named after the queue job that wrote the checkpoint
        (:func:`detached_dir`): this one's for a plan it opens, the row's
        for one it adopts.

        :param int index: position of the step in the plan
        :rtype: str
        """
        uuid = self._detached_uuid
        if uuid is None:
            uuid = self.job.queue_job_uuid or ""
        return detached_dir(self.env, self.job.id, uuid, index)

    async def _close_on_server(self, wait_s, transport=None, say_again=True):
        """Stop and remove what the job left on its server; never raises.

        For a job closed without running: a stop that finds it back from
        a park, the reconciliation of a lost or ended one, an attempt that
        runs the job again over a row it does not take up. Its step apart
        is read first: one that ended is said in the job log with its
        exit status, which the closing hook gets as the step's result
        (``_record_ended_apart``). One still running is sent TERM, given
        *wait_s* for its trap and killed, its last lines reach the job
        log, and its directory goes; then those of the steps apart that
        ended and were not removed; then the job's working directory and
        the secrets it staged. A step past the job's point of no return
        still running is not stopped: it is left to end, the checkpoint
        stays for a later pass, and it keeps the job's queue busy
        (``_hold_queue``). So does everything when the server cannot be
        reached (``_detached_left_alive``), and such a step keeps the
        queue busy then too.

        :param int wait_s: how long the step's trap gets after TERM
        :param transport: a transport to the job's server already open, or
            None to open one
        :param bool say_again: write to the job log what is said on every
            try that fails; the reconciliation says it the first time only
        """
        plan = self._checkpoint_data.get("plan") or []
        index = self._checkpoint_row.next_index if self._checkpoint_row else len(plan)
        apart = index < len(plan) and (plan[index][2] or {}).get("detached")
        past = apart and self._past_no_return(index)
        # Whether the step is known not to run any more: it ended, or was
        # stopped.
        settled = not apart
        self._say_again = say_again
        try:
            async with self._transport_to_close(transport) as transport:
                if apart:
                    directory = self._detached_dir(index)
                    # Its lines from where the job log has them.
                    self._detached_offset = self._resumed_log_offset
                    state, exit_status, timedout = await self._read_to_end(
                        transport, directory,
                    )
                    if state == "running" and past:
                        self._detached_left_alive = True
                        self._left_running = True
                        self._hold_queue()
                        if say_again:
                            self._sys(
                                f"'{plan[index][0]}' is past the point of"
                                " no return and still runs on the server:"
                                " it is left to end, and removed later."
                            )
                        return
                    settled = state != "running"
                    if state == "done":
                        await self._record_ended_apart(
                            transport, directory, index, exit_status, timedout,
                        )
                    await self._stop_detached(
                        transport, directory, wait_s=wait_s, index=index,
                    )
                    settled = True
                await self._clean_ended_steps(transport, index)
                if self._detached_left_alive:
                    return
                paths = list(self._private_uploads)
                if self._job_dir_created:
                    paths.append(self._job_dir())
                if paths:
                    result = await transport.run(f"rm -rf {shlex.join(paths)}")
                    if result.exit_status != 0:
                        self._detached_left_alive = True
        except Exception as exc:  # noqa: BLE001 — the row stays for later
            self._detached_left_alive = True
            if past and not settled and self._step_may_run():
                self._hold_queue()
            logging.getLogger("AbstractExecutor").warning(
                "[job %s] could not reach its server to close what it left"
                " there: %s: %s", self.job.id, type(exc).__name__, exc,
            )
            if say_again:
                self._sys(
                    "The server could not be reached to stop what this job"
                    " left there: it is tried again later."
                )
        finally:
            self._say_again = True

    async def _read_to_end(self, transport, directory):
        """Read the step apart in *directory* up to the end of its log.

        Its lines go to the job log; a log longer than one read is read
        on, at most eight times: the state says ``done`` or ``lost`` only
        once the bytes reach the end.

        :param transport: an open transport to the step's server
        :param str directory: :func:`detached_dir`
        :return: ``(state, exit_status, timedout)`` as the last read said
        """
        for _read in range(8):
            state, exit_status, timedout, more = await self._read_detached(
                transport, directory,
            )
            if state != "running" or not more:
                break
        return state, exit_status, timedout

    async def _record_ended_apart(
        self, transport, directory, index, exit_status, timedout,
    ):
        """Say how the step apart at *index* ended, and give it to the hook.

        Found ended by whoever closes the job: the job log says its exit
        status, and the step's result — that status and its stdout — goes
        among the steps' results the closing hook gets, so it does not
        read "did not end" into a step that did. Not said again for a step
        whose directory a process kept (``unclean``): an earlier close
        said it.

        :param transport: an open transport to the step's server
        :param str directory: :func:`detached_dir`
        :param int index: position of the step in the plan
        :param int exit_status: the step's exit status
        :param bool timedout: whether its cap cut it
        """
        label = self._checkpoint_data["plan"][index][0]
        if index not in (self._checkpoint_data.get("unclean") or ()):
            cut = " (its cap cut it)" if timedout else ""
            self._sys(
                f"'{label}' ended on the server with exit status"
                f" {exit_status}{cut}."
            )
        if self._step_results is not None and label not in self._step_results:
            self._step_results[label] = {
                "stdout": await self._detached_stdout(transport, directory),
                "exit_status": exit_status,
            }

    def _step_may_run(self):
        """Whether the adopted checkpoint's step at ``next_index`` may run.

        Started apart, or the row of an attempt that ended while it ran:
        neither parked nor closed. A close writes ``parked = FALSE``, and
        the attempt that closed the row was alive: had it started the step
        apart, ``detached_started`` says so. Not a step whose end was read
        and whose directory a process kept (``unclean``).

        :rtype: bool
        """
        row = self._checkpoint_row
        if not row:
            return False
        if row.next_index in (self._checkpoint_data.get("unclean") or ()):
            return False
        return bool(row.detached_started or not (row.parked or row.closed))

    def _hold_queue(self):
        """Mark the job's checkpoint as keeping the job's queue busy.

        For a step past the point of no return left running, or that
        could not be asked: ``cloud.job._scope_busy`` counts the row until
        it goes or ``DETACHED_HOLDS_QUEUE_MAX_S`` pass from the first
        mark. A mark already there is kept. Never raises.
        """
        try:
            with self._durable_env() as env:
                env.cr.execute(
                    "UPDATE cloud_job_checkpoint SET holds_queue_since ="
                    " COALESCE(holds_queue_since, %s) WHERE job_id = %s",
                    (fields.Datetime.now(), self.job.id),
                )
        except Exception as exc:  # noqa: BLE001 — the 73 of start remains
            logging.getLogger("AbstractExecutor").warning(
                "[job %s] could not mark its checkpoint as holding its queue:"
                " %s", self.job.id, exc,
            )

    @contextlib.asynccontextmanager
    async def _transport_to_close(self, transport):
        """Yield *transport*, or one opened to the job's server for the block.

        :param transport: a transport already open, or None
        """
        if transport is not None:
            yield transport
            return
        async with self._host_record.get_transport() as opened:
            yield opened

    async def _close_stale_checkpoint(self, transport):
        """Stop and remove what an earlier run left, before this one runs.

        A row this attempt does not take up (``cloud.job.execute``): one a
        hook closed and kept, the job run again by hand; or one another
        queue job of the job wrote. It is neither resumed nor written over:
        what it says runs on the server is stopped and removed through
        *transport*, and the row goes. When that cannot be done — a step
        past its point of no return still runs, or something outlived its
        KILL — the job waits 30 s, as for its host's build lock, and the
        row stays as it was.

        :param transport: the job's open transport
        :raise RetryableJobError: when what the row says is still alive
        """
        row = self._stale_checkpoint
        if not row:
            return
        host = row.host_id or self._host_record
        cleaner = type(self)(self.job, host, sleep_interval=self.sleep_interval)
        cleaner._log_buffer = self._log_buffer
        cleaner._adopt_checkpoint(row)
        self._sys(
            "An earlier run of this job left something on its server: it is"
            " stopped and removed before the job runs again."
        )
        await cleaner._close_on_server(
            DETACHED_STOP_WAIT_S,
            transport=transport if host == self._host_record else None,
        )
        if cleaner._detached_left_alive:
            raise RetryableJobError(
                "What an earlier run of this job left on its server still"
                f" runs: the job tries again in {DETACHED_BUSY_RETRY_S} s.",
                seconds=DETACHED_BUSY_RETRY_S,
                ignore_retry=True,
            )
        with self._durable_env() as env:
            env.cr.execute(
                "DELETE FROM cloud_job_checkpoint WHERE id = %s", (row.id,),
            )
        self._stale_checkpoint = None

    #: A checkpoint of the job this attempt does not take up
    #: (``_close_stale_checkpoint``), or None.
    _stale_checkpoint = None

    def _past_no_return(self, index):
        """Whether step *index* of the adopted plan is past the point of no return.

        The point as the plan was materialised (``_open_checkpoint``): a
        move's restore has none, and the rollback that removes the move's
        marks does not give the load that ran one. A row written by a
        version that did not keep it there falls back to the executor's.

        :param int index: a position in the plan
        :rtype: bool
        """
        data = self._checkpoint_data or {}
        label = (
            data["no_return_from"] if "no_return_from" in data
            else self._no_return_from
        )
        if not label:
            return False
        labels = [step[0] for step in self._checkpoint_data.get("plan") or []]
        return label in labels and index >= labels.index(label)

    def _contact_lost(self, exc):
        """Whether *exc* cut the job off the server of a step that runs there.

        :param Exception exc: what ended the attempt
        :rtype: bool
        """
        return bool(
            self._checkpoint
            and self._on_server_index is not None
            and is_transient_connection_error(exc)
        )

    def _park_on_lost_contact(self, exc):
        """Park a job cut off the server of its running step, or let it fail.

        The step goes on there, and the job connects again in
        ``DETACHED_RECONNECT_GAP_S``. Once ``DETACHED_CONTACT_GRACE_S``
        have gone by since contact was lost, the job fails instead and
        leaves its checkpoint, and the step, to the reconciliation
        (``_detached_left_alive``).

        A resumed attempt that could not connect parks at the step it was
        to go on from, whether or not a step runs on the server.

        :param Exception exc: the transient connection error
        :return: the ``JobParked`` to raise, or None to fail
        """
        now = fields.Datetime.now()
        since = self._lost_contact_since or now
        plan = self._checkpoint_data.get("plan") or []
        index = (
            self._on_server_index if self._on_server_index is not None
            else self._resume_index
        )
        label = plan[index][0] if index < len(plan) else "the step"
        running = self._on_server_index is not None
        where = f"while '{label}' runs there" if running else f"before '{label}'"
        if (now - since).total_seconds() >= DETACHED_CONTACT_GRACE_S:
            self._detached_left_alive = True
            if running and self._past_no_return(index):
                # It may still run, past the point of no return: the next
                # job of the queue must not start beside it.
                self._hold_queue()
            left = "the step is" if running else "what the job left there is"
            self._sys(
                f"✗ The server could not be reached for"
                f" {DETACHED_CONTACT_GRACE_S} s {where}: the job fails, and"
                f" {left} left to the reconciliation."
            )
            return None
        return self._park(index, (
            f"Lost contact with the server {where}"
            f" ({type(exc).__name__}): the panel connects again in"
            f" {DETACHED_RECONNECT_GAP_S} s."
        ), seconds=DETACHED_RECONNECT_GAP_S, vals={"lost_contact_since": since})

    def _detached_scope(self):
        """Return what a step apart of this job holds on its server.

        One step at a time per instance, or per host for a job of the
        host: ``detached_step.sh start`` refuses a second one. A hidden
        job (``cloud.job._get_hidden_job_types``) holds a scope of its
        own, per type: it runs beside the visible jobs of its target
        (a base copy of PITR beside a rebuild), and must not make them
        wait for it on the server, nor wait for them.

        :rtype: str
        """
        inst = self.job.instance_id
        scope = f"instance:{inst.id}" if inst else "host"
        code = self.job.sudo().job_type_id.code
        if code and code in self.env["cloud.job"]._get_hidden_job_types():
            scope = f"{scope}:{code}"
        return scope

    @staticmethod
    def _script_message(output):
        """Return the last line a script printed, without its prefix.

        :param str output: what the script printed
        :rtype: str
        """
        lines = [line for line in (output or "").splitlines() if line.strip()]
        last = lines[-1] if lines else ""
        return re.sub(r"^\[incubacloud\] (ERROR: )?", "", last.strip())

    async def _run_detached(
        self, transport, index, label, command, opts, resume=False,
    ):
        """Run step *index* on the server apart from the connection.

        ``detached_step.sh start`` runs it in a session of its own, capped
        by the step's ``timeout`` (``DEFAULT_SSH_COMMAND_TIMEOUT`` without
        one); then it is read every ``DETACHED_POLL_S`` until it ends, its
        lines going to the job log as an attached step's do. Its result is
        what ``transport.execute`` would return: the exit status, and its
        stdout's lines (the last ``DETACHED_STDOUT_TAIL`` bytes of it).

        A server that kills processes at logout runs the step attached
        instead. A step of another job still running for the same target
        parks the job for ``DETACHED_BUSY_RETRY_S``. A step the cap cut
        raises ``asyncio.TimeoutError``, as an attached one does; one whose
        process died, or whose server lost it, fails the job. Ended by
        anything but its own end — a cancellation, an error — the step is
        stopped and its directory removed before the error goes on
        (``_stop_detached``). Not when the attempt parks, nor when the
        server cannot be reached: the step goes on, for a later attempt.

        :param transport: the job's open transport
        :param int index: position of the step in the plan
        :param str label: the step's label
        :param str command: the step's command
        :param dict opts: the step's options
        :param bool resume: an earlier attempt started the step: it is
            followed from where its log was read up to, and not started
            again (the start of a step whose directory is gone would run
            it a second time)
        :return: the step's ``CommandResult``
        """
        directory = self._detached_dir(index)
        cap = int(opts.get("timeout") or DEFAULT_SSH_COMMAND_TIMEOUT)
        # From before the start: a stop that comes while it runs finds a
        # step it may have launched already.
        self._detached_running = directory
        self._on_server_index = index
        try:
            if resume:
                self._detached_offset = self._resumed_log_offset
                started_at = self._resumed_step_started_at
                self._resumed_step_started_at = None
            else:
                started_at = await self._start_detached(
                    transport, index, directory, cap, label, command,
                )
                if started_at is None:
                    extra = {"timeout": opts["timeout"]} if opts.get("timeout") else {}
                    return await transport.execute(
                        command, self.on_stdout, self.on_stderr, **extra,
                    )
            # The cap counts from the step's start, whichever attempt saw it.
            overdue = started_at + timedelta(seconds=cap + DETACHED_OVERDUE_GRACE_S)
            deadline = time.monotonic() + (
                overdue - fields.Datetime.now()
            ).total_seconds()
            return await self._follow_detached(
                transport, directory, label, cap, deadline, index,
            )
        except JobParked:
            # Left to run on its server, for the next attempt.
            self._detached_running = None
            raise
        except Exception as exc:
            if is_transient_connection_error(exc) and not isinstance(
                exc, asyncio.TimeoutError,
            ):
                # The server cannot be reached: nothing can be stopped, and
                # the job parks to connect again (``_park_on_lost_contact``).
                self._detached_running = None
            raise
        finally:
            if self._detached_running:
                await self._stop_detached(transport, directory, index=index)

    async def _start_detached(self, transport, index, directory, cap, label, command):
        """Start step *index* apart on the server; return when it started.

        ``detached_step.sh start`` does nothing for a step already started
        (its ``pid`` is there): an attempt that died before it recorded
        the start starts it again harmlessly.

        :param transport: the job's open transport
        :param int index: position of the step in the plan
        :param str directory: :func:`detached_dir`
        :param int cap: the step's cap, in seconds
        :param str label: the step's label
        :param str command: the step's command
        :return: the start's time, or None when the step is to run attached
            (the server kills processes at logout, or lacks a tool)
        :raise JobParked: when another job's step holds the target
        """
        self._detached_offset = 0
        start = await transport.run(self.run_script("detached_step.sh", [
            "start", directory, cap, self._detached_scope(), self.job.id,
            label, command,
        ]) + " 2>&1")
        status = start.exit_status
        if status in (DETACHED_EXIT_BUSY, DETACHED_EXIT_TAKEN, *DETACHED_EXIT_ATTACHED):
            # Nothing of this step was started, and a path taken by someone
            # else is not this job's to stop or remove. Any other answer
            # may come from a step that started (a start that waited past
            # its 10 s): it is stopped and removed with the error.
            self._detached_running = None
            self._on_server_index = None
        if status in DETACHED_EXIT_ATTACHED:
            self._detached_attached = True
            self._sys(
                f"⚠ {self._script_message(start.stdout)}. '{label}' runs"
                " attached to this connection."
            )
            return None
        if status == DETACHED_EXIT_BUSY:
            raise self._park(index, (
                f"'{label}' waits: {self._script_message(start.stdout)}."
                f" The job tries again in {DETACHED_BUSY_RETRY_S} s."
            ), seconds=DETACHED_BUSY_RETRY_S)
        if status != 0:
            raise RuntimeError(
                f"Could not start '{label}' on the server (exit {status}):"
                f" {self._script_message(start.stdout)}"
            )
        started_at = fields.Datetime.now()
        self._write_checkpoint({
            "detached_started": True,
            "step_started_at": started_at,
            "log_offset": 0,
        })
        return started_at

    async def _follow_detached(
        self, transport, directory, label, cap, deadline, index,
    ):
        """Read the step apart in *directory* until it ends; return its result.

        No read starts once the attempt's budget is spent: the job parks,
        and the step goes on.

        :param transport: the job's open transport
        :param str directory: :func:`detached_dir`
        :param str label: the step's label
        :param int cap: the step's cap, in seconds
        :param float deadline: ``time.monotonic()`` past which a step still
            running is stopped and fails the job
        :param int index: position of the step in the plan
        :return: the step's ``CommandResult``
        :raise asyncio.TimeoutError: when its cap cut it, or it ran past
            the cap and the grace
        :raise RuntimeError: when its process died, or the server lost it
        :raise JobParked: when the budget is spent
        """
        while True:
            if self._may_park() and self._budget_spent():
                raise self._park(index, (
                    "The step keeps running on the server; the panel"
                    " reconnects in a few seconds."
                ))
            state, exit_status, timedout, more = await self._read_detached(
                transport, directory,
            )
            if state == "running":
                if time.monotonic() > deadline:
                    await self.on_stderr(
                        f"⚠ '{label}' still ran on the server past its"
                        f" {cap}s cap — stopped."
                    )
                    raise asyncio.TimeoutError()
                if not more:
                    await asyncio.sleep(DETACHED_POLL_S)
                continue
            if state == "done":
                if timedout:
                    # What ``SSHTransport.execute`` writes for its timeout.
                    await self.on_stderr(
                        f"⚠ Command exceeded {cap}s timeout — killed."
                    )
                    raise asyncio.TimeoutError()
                stdout = await self._detached_stdout(transport, directory)
                # It ended: nothing of it to stop. Its directory goes once
                # its result is recorded (``_run_steps``): an attempt that
                # dies in between finds its end there, not a step gone.
                self._detached_running = None
                self._on_server_index = None
                self._ended_apart = (directory, index)
                return CommandResult(stdout=stdout, exit_status=exit_status)
            raise RuntimeError(
                f"'{label}' neither ended nor runs on the server: the server"
                " restarted, or the step's process died while it ran."
            )

    async def _read_detached(self, transport, directory):
        """Read what the step apart wrote since the last read, and its state.

        Its lines go to the job log, followed by the offset they end at
        (``_LOG_OFFSET``), which ``_flush_logs`` writes with them. The
        bytes travel in base64: the offset counts bytes, and output that
        is not UTF-8 must neither stop the read nor shift it.

        :param transport: the job's open transport
        :param str directory: :func:`detached_dir`
        :return: ``(state, exit_status, timedout, more)``: the state the
            script says (``running``, ``done``, ``lost`` or ``none``), the
            exit status once ``done`` (else None), whether the cap cut it,
            and whether the read was likely cut short by
            ``DETACHED_READ_MAX_BYTES``, so another should follow at once
        :raise RuntimeError: when the answer is not the script's
        """
        before = self._detached_offset
        result = await transport.run(
            _detached_ctl(directory, "read", before, DETACHED_READ_MAX_BYTES)
            + " | base64 -w0"
        )
        raw = base64.b64decode(result.stdout or "")
        header, _sep, data = raw.partition(b"\n")
        match = _DETACHED_HEADER_RE.match(header.decode("ascii", "replace"))
        if not match:
            raise RuntimeError(
                "The server did not answer as detached_step.sh does: "
                f"{header[:200].decode('utf-8', 'replace')!r}"
            )
        state, exit_status, timedout, offset = match.groups()
        for line in _stdout_lines(data.decode("utf-8", "replace")):
            await self.on_stdout(line)
        self._detached_offset = int(offset)
        if self._detached_offset != before:
            self._log_buffer.append((self._detached_offset, _LOG_OFFSET))
        return (
            state,
            None if exit_status == "-" else int(exit_status),
            timedout == "1",
            (self._detached_offset - before) * 2 > DETACHED_READ_MAX_BYTES,
        )

    async def _detached_stdout(self, transport, directory):
        """Return the stdout of the step apart, as ``execute`` returns it.

        Its last ``DETACHED_STDOUT_TAIL`` bytes, without the line they cut.

        :param transport: the job's open transport
        :param str directory: :func:`detached_dir`
        :rtype: str
        """
        result = await transport.run(
            _detached_ctl(directory, "result") + " | base64 -w0"
        )
        raw = base64.b64decode(result.stdout or "")
        if len(raw) >= DETACHED_STDOUT_TAIL:
            raw = raw.partition(b"\n")[2]
        return "\n".join(_stdout_lines(raw.decode("utf-8", "replace")))

    async def _clean_detached(self, transport, directory, index=None):
        """Remove the directory of a step apart that ended.

        One that cannot be removed — a process the step left behind still
        runs in its session, or the server cannot be reached — is left,
        with its checkpoint, to whoever reaches the server next: its index
        is kept in the checkpoint's ``unclean``, which every later close
        or resume tries again. The step's result stands.

        :param transport: the job's open transport
        :param str directory: :func:`detached_dir`
        :param int index: position of the step in the plan, to keep
        :return: whether the directory is gone
        :rtype: bool
        """
        self._detached_running = None
        self._on_server_index = None
        try:
            result = await transport.run(
                _detached_ctl(directory, "clean") + " 2>&1",
            )
        except Exception as exc:
            if not is_transient_connection_error(exc):
                raise
            result = CommandResult(
                stdout=f"{type(exc).__name__}: {exc}", exit_status=-1,
            )
        if result.exit_status == 0:
            self._mark_unclean(index, False)
            return True
        self._detached_left_alive = True
        self._mark_unclean(index, True)
        if self._say_again:
            self._sys(
                f"⚠ The step's directory {directory} stays on the server"
                f" (exit {result.exit_status}):"
                f" {self._script_message(result.stdout)}"
            )
        return False

    def _mark_unclean(self, index, unclean):
        """Keep, or drop, step *index* among the directories left to remove.

        Written at once: the row may be all that is left of the job when
        someone next reaches the server.

        :param int index: position of the step in the plan, or None
        :param bool unclean: whether its directory stays
        """
        if index is None or not self._checkpoint or self._checkpoint_data is None:
            return
        kept = list(self._checkpoint_data.get("unclean") or [])
        if unclean == (index in kept):
            return
        kept = sorted({*kept, index}) if unclean else [i for i in kept if i != index]
        self._checkpoint_data["unclean"] = kept
        self._write_checkpoint({"data": json.dumps(self._checkpoint_data)})

    async def _clean_ended_steps(self, transport, upto):
        """Remove the directories of the steps apart that ended before *upto*.

        The one just before *upto*, whose end was recorded before its
        directory went (``_run_steps``), and those ``clean`` had to leave
        (``unclean``). ``clean`` of a directory that is gone does nothing.

        :param transport: the open transport
        :param int upto: the first step not ended
        """
        plan = self._checkpoint_data.get("plan") or []
        indexes = set(self._checkpoint_data.get("unclean") or ())
        if 0 < upto <= len(plan) and (plan[upto - 1][2] or {}).get("detached"):
            indexes.add(upto - 1)
        for index in sorted(indexes):
            await self._clean_detached(transport, self._detached_dir(index), index)

    async def _stop_detached(
        self, transport, directory, wait_s=DETACHED_STOP_WAIT_S, index=None,
    ):
        """Stop the step apart in *directory*, keep its last lines, remove it.

        For a step ended by anything but its own end: a cancellation, a
        cap, an error. TERM to its whole session, ``DETACHED_STOP_WAIT_S``
        for its trap, KILL to what is left; what it wrote meanwhile goes
        to the job log. Never raises: the error on its way up is the one
        to report. What could not be stopped or removed is left, with the
        job's checkpoint, to whoever reaches the server next
        (``_detached_left_alive``).

        :param transport: the job's open transport
        :param str directory: :func:`detached_dir`
        :param int wait_s: how long its trap gets after TERM
        :param int index: position of the step in the plan
        """
        self._detached_running = None
        self._on_server_index = None
        stopped = False
        try:
            stop = await transport.run(
                _detached_ctl(directory, "stop", wait_s) + " 2>&1",
                timeout=wait_s + 60,
            )
            for line in _stdout_lines(stop.stdout or ""):
                await self.on_stderr(line)
            for _read in range(8):
                state, _exit, _cut, more = await self._read_detached(
                    transport, directory,
                )
                if state != "running" or not more:
                    break
            stopped = stop.exit_status == 0
            if not stopped:
                self._detached_left_alive = True
                self._hold_queue_if_past(index)
            await self._clean_detached(transport, directory, index)
        except BaseException as exc:
            self._detached_left_alive = True
            if not stopped:
                self._hold_queue_if_past(index)
            logging.getLogger("AbstractExecutor").warning(
                "[job %s] could not stop its step in %s: %s: %s",
                self.job.id, directory, type(exc).__name__, exc,
            )
            if not isinstance(exc, Exception):
                raise

    def _hold_queue_if_past(self, index):
        """Keep the job's queue busy if step *index* is past the point of no return.

        For a stop that may have left the step running (``_stop_detached``):
        its job fails, and the next job of its queue must not start beside
        it before the reconciliation looks at it. Not for a directory that
        only could not be removed.

        :param int index: position of the step in the plan, or None
        """
        if index is not None and self._checkpoint and self._past_no_return(index):
            self._hold_queue()

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
        with self._durable_env() as env:
            env['cloud.job.log.chunk'].sudo().create({
                'job_id': self.job.id,
                'content': _redact_tokens(msg),
                'source': 'system',
            })

    def pre_run_checks(self):
        """Called before connecting. Raises ``JobRefused`` to abort the job.

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
            raise JobRefused(msg)

    # ===============================
    # ALERT HELPERS
    # ===============================

    def _alert(self, code, message, level='warning'):
        """Create or refresh an active alert for this job's host.

        The dedup rule itself lives in ``cloud.alert.raise_alert`` (one
        implementation, shared with the job-less producers such as the
        metric-rule cron). What stays here is the private cursor: an
        alert about a failure must survive the rollback of the very
        transaction that failed.
        """
        with self._durable_env() as env:
            env['cloud.alert'].raise_alert(
                code, message, level=level,
                host=env['cloud.host'].browse(self.job.host_id.id),
                job=env['cloud.job'].browse(self.job.id),
            )

    def _resolve_alert(self, code):
        """Dismiss any active alert with the given code for this job's host."""
        with self._durable_env() as env:
            env['cloud.alert'].resolve_alert(
                code, host=env['cloud.host'].browse(self.job.host_id.id),
            )

    # ===============================
    # PATH HELPERS
    # ===============================

    def _inst_dir(self, inst):
        """Return the remote directory for an instance."""
        return inst.get_remote_dir()

    def _base_url(self):
        """Return the full HTTPS URL for web.base.url (scheme always https).

        Empty string when the job's instance has no domain — callers must
        treat that as "leave ``web.base.url`` alone", never as "write an
        empty parameter".

        Lives here rather than on the deploy executor because three
        different jobs need the same rule: deploy and rebuild set the
        parameter on a fresh stack, and restore has to overwrite whatever
        base URL travelled inside the restored dump.

        :return: ``https://<domain>`` or ``''``
        """
        domain = (self.job.instance_id.domain or "").strip()
        if not domain:
            return ""
        if domain.startswith(("http://", "https://")):
            return domain.rstrip("/")
        return f"https://{domain}"

    # ===============================
    # VERSIONED SCRIPT HELPERS
    # ===============================

    def _script_root(self):
        """Return the remote directory this job uploads its scripts to."""
        return self._job_path("scripts")

    #: This job's working directory on its host, once named (class
    #: defaults, so an executor built without ``__init__`` has them).
    _job_dir_path = None
    _job_dir_created = False

    def _job_dir(self):
        """Return this job's private working directory on its host.

        Named once per executor, from a random token: every attempt of a
        job has its own, and nothing outside this process knows the name
        before the directory exists. Known before the job connects, so
        ``get_commands()`` can name what is in it at any time.

        :rtype: str
        """
        if self._job_dir_path is None:
            self._job_dir_path = JOB_DIR_PREFIX + secrets.token_hex(8)
        return self._job_dir_path

    def _job_path(self, name):
        """Return the path of *name* in this job's working directory.

        :param str name: a file or directory name, relative
        :rtype: str
        """
        return f"{self._job_dir()}/{name}"

    async def _ensure_job_dir(self, transport):
        """Create this job's working directory on its host, once.

        ``mkdir -m 700`` without ``-p``: it fails when anything at all is
        at the path — a directory, a file or a link another account put
        there — and otherwise creates it this account's, unreadable by
        anyone else. Everything a job uploads goes in it.

        :param transport: the job's open transport
        :raise RuntimeError: when it could not be created
        """
        if self._job_dir_created:
            return
        directory = self._job_dir()
        result = await transport.run(f"mkdir -m 700 {shlex.quote(directory)}")
        if result.exit_status != 0:
            raise RuntimeError(
                f"Could not create this job's working directory {directory} "
                f"on the host (exit {result.exit_status}): something is "
                "already at that path, or /tmp cannot be written."
            )
        self._job_dir_created = True

    _drop_dir_created = False

    def _drop_dir(self):
        """Return where a container this job runs writes what it produces.

        :rtype: str
        """
        return self._job_path("out")

    async def _ensure_drop_dir(self, transport):
        """Create :meth:`_drop_dir` on the host, once.

        A container writes as whatever user its image runs as (``odoo``,
        ``root``), so the directory is writable by everyone — and
        reachable by nobody but this account: it is inside the job's own
        0700 directory. Mounted alone into the container, it is all of
        the host's ``/tmp`` the container sees.

        :param transport: the job's open transport
        :raise RuntimeError: when it could not be created
        """
        await self._ensure_job_dir(transport)
        if self._drop_dir_created:
            return
        drop = self._drop_dir()
        result = await transport.run(f"mkdir -m 777 {shlex.quote(drop)}")
        if result.exit_status != 0:
            raise RuntimeError(
                f"Could not create {drop} on the host "
                f"(exit {result.exit_status})."
            )
        self._drop_dir_created = True

    async def _check_regular_file(self, transport, path):
        """Refuse to download *path* unless it is a regular file.

        What a container left in the drop directory is downloaded by this
        account, and the download follows a link to wherever it points: a
        dump replaced by a link to this account's key would be shipped to
        the panel. The container has exited by then, and nobody else can
        reach the directory, so the answer holds until the download.

        :param transport: the job's open transport
        :param str path: the file about to be downloaded
        :raise RuntimeError: when it is a link, or not a file at all
        """
        quoted = shlex.quote(path)
        result = await transport.run(f"test -f {quoted} && ! test -L {quoted}")
        if result.exit_status != 0:
            raise RuntimeError(
                f"{path} is not a regular file on the host: not downloaded."
            )

    async def _upload_files(self, transport, files):
        """Upload files without a secret into this job's directory.

        :param transport: the job's open transport
        :param dict files: ``{remote_path: text_content}``, every path in
            :meth:`_job_dir`
        """
        await self._ensure_job_dir(transport)
        await transport.upload_text_files(files)

    @classmethod
    def _addon_chain(cls):
        """Return the addon of every class in the MRO, most derived first.

        Both the ``scripts/`` overlay and the ``ansible/`` tree resolve
        through this chain: an executor sees the assets of its own addon
        *and* those of the addons its base classes come from, which is
        how a subclass in another module reuses (or shadows) a core script.
        """
        addons = []
        for klass in cls.__mro__:
            parts = (klass.__module__ or "").split(".")
            if parts[:2] == ["odoo", "addons"] and len(parts) > 2:
                if parts[2] not in addons:
                    addons.append(parts[2])
        return addons

    def _addon_overlay(self, subdir, pattern="*"):
        """Return ``{relative path: absolute local path}`` for ``<addon>/<subdir>``.

        Merges that directory across every addon in ``_addon_chain()``,
        most derived first, so a subclass shadows a base-class file of
        the same relative name. Addons that don't ship the directory are
        skipped.
        """
        overlay = {}
        for addon in self._addon_chain():
            try:
                base = file_path(f"{addon}/{subdir}")
            except FileNotFoundError:
                continue
            for local in sorted(Path(base).rglob(pattern)):
                if local.is_file():
                    overlay.setdefault(
                        local.relative_to(base).as_posix(), str(local),
                    )
        return overlay

    def _script_overlay(self):
        """Return the merged ``scripts/`` overlay, cached per instance."""
        if self._script_overlay_cache is None:
            self._script_overlay_cache = self._addon_overlay("scripts", "*.sh")
        return self._script_overlay_cache

    def run_script(self, name, args=(), env=None):
        """Return the shell command that runs the versioned script *name*.

        Queues this executor's whole ``scripts/`` overlay for upload (so
        a script can ``source lib/common.sh``) and returns
        ``bash <remote>/<name> <args...>`` with every argument
        shell-quoted. Use it as the command of a ``get_commands()``
        tuple::

            ("Deploy", self.run_script("deploy.sh", [d, inst.name]))

        Passing values as *arguments* — never as text interpolated into
        the script — is what keeps remote execution injection-safe and
        the script itself reviewable and lintable in git. *env* is for
        what every operation of a script shares rather than one call
        takes, such as the instance's database tool
        (``cloud.instance._script_env``): it is set for the script's
        process only, quoted the same way.

        :param str name: path of the script relative to ``scripts/``
        :param args: positional arguments passed to the script
        :param env: ``{NAME: value}`` environment of the script's process
        :return: the command string to hand to ``transport.execute()``
        :raises FileNotFoundError: if no addon in the MRO ships *name*
        :raises ValueError: for an environment name a shell would not take
        """
        if name not in self._script_overlay():
            raise FileNotFoundError(
                f"No versioned script named {name!r} under scripts/ of: "
                f"{', '.join(self._addon_chain())}."
            )
        self._scripts_requested = True
        command = f"bash {self._script_root()}/{name}"
        quoted = shlex.join(str(a) for a in args)
        if quoted:
            command = f"{command} {quoted}"
        for key, value in (env or {}).items():
            if not _ENV_NAME_RE.fullmatch(key):
                raise ValueError(f"not an environment variable name: {key!r}")
            command = f"{key}={shlex.quote(str(value))} {command}"
        return command

    async def _upload_scripts(self, transport):
        """Upload the queued ``scripts/`` overlay to this job's remote dir.

        No-op unless ``run_script()`` was called, and no-op again once
        the upload has happened. The scripts go in the job's own
        directory (:meth:`_job_dir`), created 0700 for this run: nothing
        an earlier run or another account left can shadow them, and
        nobody else can read or replace them.

        ``_async_entry`` calls this between ``get_commands()`` and the
        command loop. An executor that runs a script from
        ``before_execute`` — where the upload has not happened yet —
        calls ``run_script()`` and then awaits this itself; the second
        call from ``_async_entry`` is then a no-op.
        """
        if not self._scripts_requested or self._scripts_uploaded:
            return
        root = self._script_root()
        files = {}
        dirs = {root}
        for rel, local in self._script_overlay().items():
            remote = f"{root}/{rel}"
            files[remote] = Path(local).read_text(encoding="utf-8")
            dirs.add(remote.rsplit("/", 1)[0])
        mkdir = " ".join(sorted(dirs))
        await self._ensure_job_dir(transport)
        result = await transport.run(f"mkdir -p -m 700 {mkdir}")
        if result.exit_status != 0:
            raise RuntimeError(
                f"Could not create the remote script directory {root} "
                f"(exit {result.exit_status})."
            )
        await transport.upload_text_files(files)
        self._scripts_uploaded = True
        self._sys(f"Uploaded {len(files)} script file(s) to {root}.")

    #: Remote paths of the secrets this job staged with
    #: :meth:`_upload_private_files` (a tuple, so an executor built
    #: without ``__init__`` still has one).
    _private_uploads = ()

    async def _upload_private_files(self, transport, files):
        """Stage secret files on the host, readable by their owner only.

        Created ``0600`` from the first byte, unlike
        ``upload_text_files``'s default — a copy an earlier run left at
        the path is replaced, not written into — and remembered: whatever
        a step has not moved into place by the end of the job is removed
        by :meth:`_discard_staging`, so a failed job leaves no key lying
        in ``/tmp``. The paths are in the job's directory, which is
        created first.

        :param transport: the job's open transport
        :param dict files: ``{remote_path: text_content}``
        """
        await self._ensure_job_dir(transport)
        # Listed first: an upload that fails half-way has created the
        # files before the one that failed, and those must go too.
        self._private_uploads = (*self._private_uploads, *files)
        await transport.upload_text_files(files, mode=0o600)

    async def _discard_staging(self, transport):
        """Remove the job's directory and the secrets it staged (best effort).

        Runs in the outermost ``finally`` of the run, so a failed job
        cleans up too. A path a step already moved is simply not there
        any more. Suppresses its own errors: the connection may already
        be gone, and a cleanup failure must never mask the real error
        that is on its way up.
        """
        paths = list(self._private_uploads)
        if self._job_dir_created:
            paths.append(self._job_dir())
        if not paths:
            return
        with contextlib.suppress(Exception):
            await transport.run(f"rm -rf {shlex.join(paths)}")

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

        Default policy is fail-closed: any command that exited non-zero is
        an error, so an executor that does not override this still fails
        the job (and reaches ``on_failure``) instead of silently reporting
        success. Executors whose steps intentionally tolerate a non-zero
        exit must override this and skip those labels.

        results: dict of {label: {'stdout': str, 'exit_status': int}}
        """
        return [
            f"'{label}' exited with status {data.get('exit_status')}"
            for label, data in results.items()
            if data.get('exit_status', 1) != 0
        ]

    def _accept_unchecked_compose_op(self, results, label):
        """Return *results* without step *label* if it ran but could not check.

        For a ``parse_results`` whose step *label* runs ``compose_op.sh``
        with ``COMPOSE_OP_EXIT_UNCHECKED`` as its ``continue_on``: that
        exit means the containers did what they were asked, so the step
        is left out of what counts as failed, and the job log says what
        was not checked. The script's own warnings, just above in the
        log, say which step.

        :param dict results: ``{label: {'stdout': str, 'exit_status': int}}``
        :param str label: the label of the step that runs ``compose_op.sh``
        :return: *results*, without *label* when it exited
            ``COMPOSE_OP_EXIT_UNCHECKED``
        :rtype: dict
        """
        step = results.get(label) or {}
        if step.get('exit_status') != COMPOSE_OP_EXIT_UNCHECKED:
            return results
        self._sys(
            f"⚠ '{label}' did its work, but the network steps that follow"
            " it could not be checked: the warnings above say why, and"
            " what may be wrong until the instance is restarted."
        )
        return {key: data for key, data in results.items() if key != label}

    def _update_restored_modules_step(self, d, inst):
        """Return the step that updates a restored database to this code.

        A database restored on an instance was written by other code than
        the one the instance runs: production's branch for a pull request
        preview or a staging, the code of its day for a backup, anybody's
        for an upload. Odoo started on it as it came serves the new code
        over the old schema (a stored field the code adds has no column,
        and every screen that reads it fails) until a rebuild updates it.
        This is the rebuild's own update, ``cron_guard.sh update``:
        ``click-odoo-update`` compares the checksum each module has stored
        in the database with the code and updates the modules that
        changed, all of them when the database stored none. It runs in a
        one-shot container of the instance's image, after any running
        cron, within the time limit of one command
        (``DEFAULT_SSH_COMMAND_TIMEOUT``). It stops the job when it
        fails: Odoo is not started over a database its code cannot read.

        :param str d: the instance's directory on its host
        :param inst: the ``cloud.instance`` whose database was restored
        :return: ``(label, command, options)``, labelled
            ``UPDATE_RESTORED_MODULES_LABEL``
        :rtype: tuple
        """
        return (
            UPDATE_RESTORED_MODULES_LABEL,
            self.run_script(
                "cron_guard.sh",
                [
                    "update", d,
                    inst.postgres_dbname or "prod",
                    inst.postgres_username or "odoo",
                ],
            ),
            {"stop_on_failure": True},
        )

    def _say_update_target(self, inst):
        """Say in the job log which code a restored database is updated to.

        The code the instance was last built from, which a restore does
        not change: a database restored to undo a bad deployment, with
        the code still the deployment's, is migrated by it again.

        :param inst: the ``cloud.instance`` whose database is restored
        """
        def _path(url):
            """Return the repository's path in *url*, without its server."""
            url = (url or "").rstrip("/").removesuffix(".git")
            if "://" in url:
                return url.split("://", 1)[1].partition("/")[2] or url
            return url.rpartition(":")[2]

        lines = [
            f"{_path(line.url)} on {line.branch or 'its default branch'}"
            + (f" pinned to {line.commit_sha[:12]}" if line.commit_sha else "")
            for line in inst.repo_ids
        ]
        odoo = f"Odoo {inst.odoo_version}" + (
            f" pinned to {inst.odoo_commit_sha[:12]}"
            if inst.odoo_commit_sha else ""
        )
        self._sys(
            f"The restored database's modules are updated to the code this"
            f" instance was last built from: {', '.join([odoo, *lines])}."
            f" To undo a deployment, revert its code first, or its"
            f" migration runs again."
        )

    def _report_failed_module_update(self, results):
        """Say in the job log what a failed update of a restored database left.

        A step that never answered (the connection dropped, its time ran
        out) counts as failed: ``on_abort`` hands it in with no status.

        :param dict results: what the steps answered
            (``{label: {'stdout': str, 'exit_status': int}}``)
        :return: what failed, a sentence safe to show on a pull or merge
            request, or ``""`` when the update was not the step that failed
        :rtype: str
        """
        step = (results or {}).get(UPDATE_RESTORED_MODULES_LABEL) or {}
        status = step.get('exit_status', 0)
        if status == 0:
            return ""
        if status is None:
            reason = (
                "Updating its modules to this code did not end: the"
                " connection to the server dropped or the step ran out of"
                " time."
            )
        else:
            reason = (
                f"Updating its modules to this code failed (exit status"
                f" {status}); the job's log in the panel names the module."
            )
        self._sys(
            f"✗ The restored database could not be updated to this"
            f" instance's code: the output of '{UPDATE_RESTORED_MODULES_LABEL}'"
            f" above names the module that failed. Odoo was left stopped,"
            f" so it does not serve a database its code cannot read. Fix"
            f" what it names, in the code or in the data. Then dismiss this"
            f" job's alert, which holds the instance's queue, and rebuild"
            f" the instance: the rebuild updates the modules and starts"
            f" Odoo. A rebuild with 'Auto-update modules on rebuild' off"
            f" does not update them, and Odoo would start on the database"
            f" as it is."
        )
        return reason

    async def _fail_cut_off_run(self, exc):
        """Close a run cut off halfway through ``on_failure``, for ``on_abort``.

        What the steps answered goes to ``on_failure``, with the step the
        host never answered (``_running_step``) counted as failed with no
        status, and an error naming it. A run cut before its steps only
        has *exc* written to the log: nothing changed on the host.

        :param Exception exc: what stopped the run
        """
        error = f"{type(exc).__name__}: {exc}"
        if self._step_results is None:
            self._sys(f"✗ {error}")
            return
        results = dict(self._step_results)
        step = self._running_step
        if step and step not in results:
            results[step] = {"stdout": "", "exit_status": None}
            error = f"'{step}' did not end: {error}"
        await self.on_failure(results, [error])

    async def on_success(self, results):
        """Called when parse_results returns an empty list."""

    async def on_failure(self, results, errors):
        """Called when parse_results returns one or more error strings."""

    async def on_abort(self, exc):
        """Called when the job ended with neither ``on_success`` nor ``on_failure``.

        A check that refused, before or inside the run, a failed
        connection, an exception in ``before_execute`` or between two
        steps, a terminal hook that raised: ``on_failure`` only follows
        steps that ran to their end. Runs on a durable cursor like the
        other terminal hooks. Not called for a run that will be retried;
        for one cancelled before its steps, through
        ``on_cancelled_running``. An override writes *exc* to the job log
        (``_handles_abort``).

        :param Exception exc: what stopped the run
        """

    async def on_cancelled_running(self, results, exc):
        """Called when a person cancelled the job while it ran.

        For what the job leaves behind, a cancellation is a failure: the
        same hook closes it, so whatever an executor puts back when it
        fails — an instance out of 'deploying', a host out of 'checking'
        — is put back too. ``on_failure`` with what the steps answered
        once they started, with ``CANCELLED_WHILE_RUNNING`` as the error;
        ``on_abort`` before them. Unlike a failure, the job ends
        ``cancelled`` and no alert is raised (``ic_job_cancelled``). An
        executor whose failure path does what a stop must not — follow
        work a person stopped, read a verdict into an unfinished run —
        overrides this. Runs on a durable cursor like the other terminal
        hooks.

        :param results: ``{label: {'stdout', 'exit_status'}}`` of the
            steps that ran, or None when the run had not reached them
        :param JobCancelled exc: the stop
        """
        if results is not None:
            await self.on_failure(results, [CANCELLED_WHILE_RUNNING])
        elif self._handles_abort():
            await self.on_abort(exc)
        else:
            self._sys(f"✗ {CANCELLED_WHILE_RUNNING}")

    def blocking_alert(self):
        """Return the alert this job must wait for, before anything runs.

        Called by ``cloud.job.execute`` before ``run``, in the job's own
        transaction — on a durable cursor for a step of a chain, which
        fails and rolls that transaction back. An alert returned blocks
        the job: it ends at once
        with ``BLOCKED_BY_ALERT``, ``blocked_alert_id`` points at the
        alert, and resolving the alert dispatches the job again. Whoever
        returns one has already raised it and said why in the job log.

        Only a job nothing runs after can wait. A step of a chain whose
        executor returns an alert fails with ``JobRefused`` instead, and
        the chain is cancelled.

        :return: an active ``cloud.alert``, or an empty recordset to run
        """
        return self.job.env['cloud.alert'].browse()

    def on_blocked(self, alert):
        """Called when ``blocking_alert`` stopped the job, in its transaction.

        :param alert: the ``cloud.alert`` the job now waits for
        """

    def on_unblocked(self):
        """Called when a job that waited is dispatched again, before it is.

        Runs in the transaction of whoever resolved the alert, the
        counterpart of ``on_blocked``.
        """

    def on_cancelled_queued(self):
        """Called when the job is cancelled while it waited in its queue.

        Nothing ran and nothing reached the host, but whoever asked for
        the job may have written ahead of it — an instance put in
        'deploying', a staging created, a move's markers set — and the
        executor that would have reverted that on failure never runs.
        An override leaves those as they were before the job was asked
        for. Runs in the transaction of whoever cancels: the user's
        request, a delete's enqueue, the dispatcher that found the
        instance gone. Writes with ``self.env``; the cancellation stands
        if it raises.
        """

    def on_never_run(self):
        """Called when the job left its queue but will never run.

        A step of a chain that a step before it ended, failed or
        cancelled (``queue.job._end_chain_after``), or a dispatched job
        cancelled before it started. Unlike ``on_cancelled_queued``, what
        was written when the job was asked for stays: after a partial
        run, recovering from it reads that (a move's markers). An
        override only lets go of what the steps that did run left on a
        host for this one — and never reaches a host itself: it runs in
        the transaction that records the end, so it queues a job that
        does. The end stands if it raises.
        """

    @classmethod
    def _merge_queued_payload(cls, waiting_job, payload):
        """Fold a new request into *waiting_job* instead of queueing another.

        Asked by ``cloud.job.enqueue`` for every job of this type that
        waits on the same target, oldest first, before a new one is
        created. An override that absorbs *payload* writes it into
        *waiting_job* and returns True; the request then becomes that
        job. Core absorbs nothing. Runs in the enqueuing transaction.

        :param waiting_job: a ``queued`` ``cloud.job`` of this type
        :param dict payload: what the caller passed to ``enqueue``
        :return: whether the request was absorbed
        :rtype: bool
        """
        return False

    @classmethod
    def _supersedes_failure(cls, waiting_job, failed_jobs):
        """Whether *waiting_job* replaces the failures holding its queue.

        Asked by ``cloud.job._supersedes_hold`` for the first job of an
        entry waiting in a held queue, once the hold is known to come
        from ``job_failed`` alerts alone, of lone jobs (no chain step
        among them). An override that answers True lets the job past
        the hold: it is a new attempt at what failed, not something that
        would only fail behind it. The alerts stay open until it ends
        ``done``. Core replaces nothing.

        :param waiting_job: a ``queued`` ``cloud.job`` of this type
        :param failed_jobs: the failed ``cloud.job`` records whose
            alerts hold the queue
        :rtype: bool
        """
        return False


# Backward-compat alias — kept so existing executor imports don't break.
AbstractSSHExecutor = AbstractExecutor
