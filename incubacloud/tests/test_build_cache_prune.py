"""Docker's build cache is emptied after a delete and before a build on a full disk.

The daily prune (``host_maintenance.yml``) is what frees the build cache
and the images nothing uses; a host can fill well before it runs. So a
delete empties them at once, and a deploy or a rebuild first empties
them when the disk Docker lives on has less than
``BUILD_CACHE_MIN_FREE_GB`` free. Both are the daily prune's own safe
commands — the build cache, and the images no container uses with the
protected ones spared — never ``docker system prune``, which takes
networks too. And neither may fail the job that runs it.

The threshold is decided on the host, so the command is run here with a
stand-in ``docker`` on the ``PATH`` that records what it was asked.
"""
import os
import subprocess
import tempfile
from pathlib import Path

from odoo.tests.common import BaseCase

from ..models.docker_prune_executor import (
    BUILD_CACHE_KEEP,
    BUILD_CACHE_MIN_FREE_GB,
    PROTECT_LABEL,
    build_cache_prune,
    build_cache_prune_if_low,
)
from ..models.host_probe_executor import MIN_DISK_GB
from .test_backup_purge_step import _PurgeBase
from .test_executor_features import (
    _make_deploy_executor,
    _make_rebuild_executor,
)

_LOW_DISK_STEP = "Free the build cache if the disk is low"
#: What the stand-in ``docker`` records of the cleanup, call by call.
_CLEANUP_CALLS = [
    "builder prune -af --filter until=1h",
    f"image prune -af --filter label!={PROTECT_LABEL} --filter until=1h",
]
_DELETE_STEP = "Empty the Docker build cache"


def _run_with_fake_docker(command, docker_fails=False):
    """Run *command* in bash with a ``docker`` that records its calls.

    The stand-in answers ``docker info`` with a directory on this
    machine's disk, so ``df`` reads a real free space, and logs every
    other call to a file.

    :param str command: the shell command under test
    :param bool docker_fails: whether every ``docker`` call but ``info``
        exits non-zero
    :return: the exit status, the output, and the ``docker`` calls (one
        argument line each, ``info`` left out)
    :rtype: tuple[int, str, list[str]]
    """
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp, "calls")
        fake = Path(tmp, "docker")
        fake.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = info ]; then echo {tmp}; exit 0; fi\n'
            f'echo "$*" >> {log}\n'
            f"exit {1 if docker_fails else 0}\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        env = os.environ | {"PATH": f"{tmp}:{os.environ.get('PATH', '')}"}
        done = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True,
            env=env, timeout=30, check=False,
        )
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return done.returncode, done.stdout, calls


class TestTheCleanupCommands(BaseCase):

    def test_the_cleanup_is_the_daily_prunes_without_networks(self):
        command = build_cache_prune()
        self.assertIn('docker builder prune -af --filter "until=1h"', command)
        self.assertIn(
            f'docker image prune -af --filter "label!={PROTECT_LABEL}"'
            ' --filter "until=1h"',
            command,
        )
        self.assertNotIn("system prune", command)
        self.assertNotIn("network", command)
        self.assertNotIn("container prune", command)

    def test_the_cleanup_runs_both_and_never_fails(self):
        for fails in (False, True):
            with self.subTest(docker_fails=fails):
                status, _out, calls = _run_with_fake_docker(build_cache_prune(), fails)
                self.assertEqual(status, 0)
                self.assertEqual(calls, _CLEANUP_CALLS)

    def test_what_the_last_hour_built_or_used_stays(self):
        """A deploy holds no build lock: its cleanup, or a delete's, may run
        beside a build, whose cache and fresh image are that recent."""
        self.assertEqual(BUILD_CACHE_KEEP, "1h")

    def test_the_threshold_is_the_checks(self):
        """Under the Check's figure the host is degraded: a deploy or a
        rebuild empties the cache from there, not 2 GB later."""
        self.assertGreaterEqual(BUILD_CACHE_MIN_FREE_GB, MIN_DISK_GB)
        self.assertIn(
            str(BUILD_CACHE_MIN_FREE_GB * 1024 * 1024), build_cache_prune_if_low(),
        )

    def test_a_disk_under_the_threshold_is_cleaned(self):
        # No disk has a petabyte free: always under.
        status, out, calls = _run_with_fake_docker(build_cache_prune_if_low(10 ** 6))
        self.assertEqual(status, 0)
        self.assertIn("emptying the build cache", out)
        self.assertEqual(calls, _CLEANUP_CALLS)

    def test_a_disk_over_the_threshold_is_left_alone(self):
        status, out, calls = _run_with_fake_docker(build_cache_prune_if_low(0))
        self.assertEqual(status, 0)
        self.assertIn("the build cache stays", out)
        self.assertEqual(calls, [])

    def test_a_failing_cleanup_does_not_fail_the_step(self):
        status, _out, calls = _run_with_fake_docker(
            build_cache_prune_if_low(10 ** 6), docker_fails=True,
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 2)

    def test_a_free_space_it_cannot_read_cleans_nothing(self):
        command = build_cache_prune_if_low(10 ** 6).replace(
            '"${root:-/var/lib/docker}"', "/no/such/dir/anywhere",
        )
        status, out, calls = _run_with_fake_docker(command)
        self.assertEqual(status, 0)
        self.assertIn("Free disk unknown", out)
        self.assertEqual(calls, [])


class TestBuildsCleanFirstOnALowDisk(BaseCase):

    def _labels(self, executor):
        return [step[0] for step in executor.get_commands()]

    def test_a_deploy_checks_before_it_pulls_and_builds(self):
        labels = self._labels(_make_deploy_executor())
        self.assertIn(_LOW_DISK_STEP, labels)
        self.assertLess(labels.index(_LOW_DISK_STEP), labels.index("Build Odoo image"))
        self.assertLess(labels.index(_LOW_DISK_STEP), labels.index("Initialize database"))

    def test_a_rebuild_checks_before_it_builds(self):
        for full in (True, False):
            with self.subTest(full_rebuild=full):
                ex = _make_rebuild_executor(
                    last_rebuild_fingerprint="fp0" if full else "fp1",
                )
                labels = self._labels(ex)
                self.assertLess(
                    labels.index(_LOW_DISK_STEP), labels.index("Rebuild Odoo image"),
                )

    def test_the_step_is_the_threshold_command_and_cannot_stop_the_chain(self):
        for ex in (_make_deploy_executor(), _make_rebuild_executor()):
            step = next(s for s in ex.get_commands() if s[0] == _LOW_DISK_STEP)
            self.assertEqual(step[1], build_cache_prune_if_low())
            self.assertEqual(len(step), 2)


class TestADeleteEmptiesTheCache(_PurgeBase):

    def test_the_teardown_ends_by_emptying_the_cache(self):
        steps = self._executor(self._job()).get_commands()
        labels = [step[0] for step in steps]
        self.assertEqual(labels[-1], _DELETE_STEP)
        self.assertLess(labels.index("Stop and remove containers"), labels.index(_DELETE_STEP))
        self.assertEqual(steps[-1][1], build_cache_prune())
        self.assertEqual(len(steps[-1]), 2)
