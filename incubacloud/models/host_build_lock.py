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

The namespace is shared with the warm-pool build lock (originally
``WarmHostLockMixin``, which solved this for warm builds only). One
namespace is what makes "at most one build per host" true across all of
them: two families each holding their own lock would still collide.
"""
from odoo.addons.queue_job.exception import RetryableJobError

# Single namespace for every per-host build lock, whatever enqueued the
# build: manual rebuild, tenant rebuild, warm deploy, warm rebuild.
# Distinct from ``_JOB_LOCK_NAMESPACE`` in ``cloud.job`` so the two lock
# families never collide. Released on COMMIT/ROLLBACK automatically.
_HOST_BUILD_LOCK_NS = 0x0C70BB1D


class HostBuildLockMixin:
    """Serialise image builds per host via a try-advisory-xact-lock."""

    def pre_run_checks(self):
        """Acquire the per-host build lock; defer the job if busy.

        Runs before the SSH connection is opened, so a deferred job pays
        no remote cost at all. Any other build already executing on this
        host holds the lock; the loser raises ``RetryableJobError`` and
        queue_job reschedules it after 30 s.

        ``ignore_retry`` keeps a deferral off the job's retry budget: the
        host being busy is not the job failing, and a long queue on one
        host must not exhaust the retries a real error deserves.
        """
        super().pre_run_checks()
        inst = self._inst()
        if not inst or not inst.host_id:
            return
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s, %s)",
            (_HOST_BUILD_LOCK_NS, inst.host_id.id),
        )
        if not self.env.cr.fetchone()[0]:
            raise RetryableJobError(
                "Another build is running on this host; deferring.",
                seconds=30,
                ignore_retry=True,
            )
