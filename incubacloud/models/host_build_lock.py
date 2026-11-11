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
"""
from odoo.addons.queue_job.exception import RetryableJobError

# Single namespace for every per-host lock, whatever enqueued the work:
# manual rebuild, a layer's deploys and rebuilds, and the instance
# teardowns. Distinct from ``_JOB_LOCK_NAMESPACE`` in
# ``cloud.job`` so the two lock families never collide. Released on
# COMMIT/ROLLBACK automatically.
_HOST_BUILD_LOCK_NS = 0x0C70BB1D


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
        """
        super().pre_run_checks()
        if not self._takes_host_build_lock:
            return
        host = self._host()
        if not host:
            return
        if not self._try_host_build_lock(host):
            raise RetryableJobError(
                "Another build is running on this host; deferring.",
                seconds=30,
                ignore_retry=True,
            )
        with self.env["cloud.job"]._fresh_env() as env:
            env.cr.execute(
                "SELECT job_id FROM cloud_job_checkpoint"
                " WHERE build_lock_host_id = %s AND job_id != %s"
                " ORDER BY job_id LIMIT 1",
                (host.id, self.job.id),
            )
            row = env.cr.fetchone()
        if row:
            raise RetryableJobError(
                f"Another build is running on this host (job #{row[0]});"
                " deferring.",
                seconds=30,
                ignore_retry=True,
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
