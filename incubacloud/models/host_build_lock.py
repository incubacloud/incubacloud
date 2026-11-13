"""Per-host advisory lock for image builds.

``cloud.job.enqueue`` serialises jobs per *instance*, deliberately: a
deploy on one instance must not block a deploy on another. But image
builds are not per-instance work in practice — every build on a host
runs ``docker compose build`` against the same Docker daemon, and
upstream doodba's Dockerfile mounts the apt cache with a BuildKit cache
id shared by every build on the machine. Two of them reaching
``apt-get install`` together produce::

    E: Could not get lock /var/cache/apt/archives/lock. It is held by process 0

which fails the build, fails the job, and — when the job came from the
release rollout — latches the rollout for the whole fleet until someone
re-runs it by hand. Measured in production on 2026-08-20: two rebuilds
enqueued in the same second on the same host, one built for 456 s and
the other died 75 s in.

This mixin serialises builds per host. The first build job on a host
takes a PostgreSQL transactional advisory lock keyed by ``host_id``; any
other build on the same host fails the try-lock and reschedules ~30 s
later. Deferring rather than failing is the point: a collision must cost
30 seconds, never a failed job.

The lock is transactional, so it is released when the worker commits at
job end — and also on rollback or a crashed worker, which is why nothing
can leave it held.

A job that parks between two attempts, its build running on the host
(``JobParked``), lets the transaction go and the lock with it. Its
checkpoint keeps the host instead (``build_lock_host_id``), written before
the lock goes: a build that takes the lock then finds the row and defers
as if the lock were held. The parked job takes the lock again when it
comes back; the row, and with it the hold, goes when the job ends.

The namespace is shared with the build lock a layer may take for
builds of its own (the first one solved this for its builds only). One
namespace is what makes "at most one build per host" true across all of
them: two families each holding their own lock would still collide.

A deferral says what it waits for (13-nov-2026, #68). Its message starts
with ``BUILD_LOCK_WAIT_MARKER`` and names the job when there is one
(``(job #N)``); queue_job keeps it in ``queue.job.result`` until the job
ends, and ``cloud.job._build_waits`` reads it there for the lists. The
job's log says it once per job it waits for.
"""
from odoo.addons.queue_job.exception import RetryableJobError

from .registry import executor_registry

# Single namespace for every per-host lock, whatever enqueued the work:
# manual rebuild, a layer's deploys and rebuilds, and the instance
# teardowns. Distinct from ``_JOB_LOCK_NAMESPACE`` in
# ``cloud.job`` so the two lock families never collide. Released on
# COMMIT/ROLLBACK automatically.
_HOST_BUILD_LOCK_NS = 0x0C70BB1D

#: How every deferral for the lock starts. ``cloud.job._build_waits``
#: reads it in ``queue.job.result``: the message comes from here alone,
#: ``(job #N)`` after it when a job is named.
BUILD_LOCK_WAIT_MARKER = "Another build is running on this host"

#: Seconds before a deferred job tries again.
_BUILD_LOCK_RETRY_S = 30


class HostBuildLockMixin:
    """Serialise heavy docker work per host via a try-advisory-xact-lock.

    Builds first, and since 2026-09-19 instance teardowns too: two
    ``delete_instance`` jobs fired on one host in the same millisecond,
    and one of them saw ``docker compose config`` die in under 110 ms
    (the same call answered correctly 11 minutes later). ``enqueue``
    serialises per *instance*, so two deletes of two instances on one
    host passed straight through; this lock is what closes that gap.
    One namespace for builds and teardowns on purpose — they contend for
    the same daemon.
    """

    #: Executors that wait for something before doing their remote work
    #: opt out: holding the host's lock while merely waiting would park
    #: every build and teardown on that machine behind the wait.
    _takes_host_build_lock = True

    def pre_run_checks(self):
        """Acquire the per-host lock; defer the job if busy.

        Runs before the SSH connection is opened, so a deferred job pays
        no remote cost at all. Any other build or teardown already
        executing on this host holds the lock; the loser raises
        ``RetryableJobError`` and queue_job reschedules it after 30 s.

        ``ignore_retry`` keeps a deferral off the job's retry budget: the
        host being busy is not the job failing, and a long queue on one
        host must not exhaust the retries a real error deserves.

        Keyed on the host the *job* runs against, not the instance's:
        the move cleanups tear down a copy on a host the instance does
        not live on (the abandoned source, or a rolled-back target), and
        keying on ``inst.host_id`` would lock the machine they never
        touch while leaving the one they do touch open. For every other
        executor the two are the same host.

        A job parked between two attempts holds the host too: its
        checkpoint says so (``build_lock_host_id``), read on a fresh
        cursor, as the parked job committed it on a cursor of its own.
        A row a closed job kept (``closed``) holds it only while a step
        apart of it was left started there (``detached_started``): one
        kept between two steps, for a directory it could not remove or a
        server not reached, builds nothing.
        """
        super().pre_run_checks()
        if not self._takes_host_build_lock:
            return
        host = self._host()
        if not host:
            return
        if not self._try_host_build_lock(host):
            raise self._build_lock_deferral(self._build_running_on(host))
        with self.env["cloud.job"]._fresh_env() as env:
            env.cr.execute(
                "SELECT job_id FROM cloud_job_checkpoint"
                " WHERE build_lock_host_id = %s AND job_id != %s"
                " AND (closed IS NOT TRUE OR detached_started)"
                " ORDER BY job_id LIMIT 1",
                (host.id, self.job.id),
            )
            row = env.cr.fetchone()
        if row:
            raise self._build_lock_deferral(row[0])

    def _build_running_on(self, host):
        """Return the job that holds *host*'s lock, when only one may hold it.

        The jobs of the host, this one aside, whose ``queue.job`` runs and
        whose type has an executor that takes the lock: one of them holds
        it. Read on a fresh cursor.

        :param host: the ``cloud.host``
        :return: its id when there is exactly one such job, else None
        :rtype: int
        """
        with self.env["cloud.job"]._fresh_env() as env:
            env.cr.execute(
                "SELECT c.id, t.code FROM cloud_job c"
                " JOIN queue_job q ON q.uuid = c.queue_job_uuid"
                " JOIN cloud_job_type t ON t.id = c.job_type_id"
                " WHERE c.host_id = %s AND c.id != %s AND q.state = 'started'",
                (host.id, self.job.id),
            )
            rows = env.cr.fetchall()
        building = []
        for job_id, code in rows:
            cls = executor_registry.get(code)
            if cls and issubclass(cls, HostBuildLockMixin) and cls._takes_host_build_lock:
                building.append(job_id)
        return building[0] if len(building) == 1 else None

    def _build_lock_deferral(self, job_id):
        """Say in the log what the job waits for, and return its deferral.

        The line goes once per job waited for (``cloud.job._note_once``,
        on a cursor of its own: the deferral rolls the job's transaction
        back). The message starts with ``BUILD_LOCK_WAIT_MARKER``, which
        the lists read.

        :param int job_id: the job waited for, or None when none is known
        :return: the ``RetryableJobError`` to raise; it spends no retry
        """
        if job_id:
            note = self.job.env._(
                "Waiting for job #%(job)s to finish building on this server:"
                " tried again every 30 s.",
                job=job_id,
            )
            message = f"{BUILD_LOCK_WAIT_MARKER} (job #{job_id}); deferring."
        else:
            note = self.job.env._(
                "Waiting for another build on this server to finish:"
                " tried again every 30 s."
            )
            message = f"{BUILD_LOCK_WAIT_MARKER}; deferring."
        self.job._note_once(note)
        return RetryableJobError(
            message, seconds=_BUILD_LOCK_RETRY_S, ignore_retry=True,
        )

    def _try_host_build_lock(self, host):
        """Try the build lock of *host* in the job's transaction.

        :param host: the ``cloud.host``
        :return: whether this job holds it now
        :rtype: bool
        """
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)",
            (_HOST_BUILD_LOCK_NS, host.id),
        )
        if not self.env.cr.fetchone()[0]:
            return False
        self._held_build_lock_host_id = host.id
        return True

    def _retake_host_build_lock(self):
        """Take the host's lock again as a parked job comes back.

        Its checkpoint kept the host while it was parked, so a build that
        took the lock meanwhile is deferring itself: when it is held for
        that instant, the job waits 5 s (``_wait_for_build_lock``).

        :return: whether the job holds the lock, or takes none
        :rtype: bool
        """
        if not self._takes_host_build_lock:
            return True
        host = self._host()
        return not host or self._try_host_build_lock(host)
