"""A deploy builds the image in a step of its own, repeated once if it fails.

On 8-nov-2026 an instance's move to another host failed in «Start instance»:
the image was built there, by the first ``docker compose`` that needed it,
and a passing failure of GitHub in ``100-repos-aggregate`` failed the
whole deploy. The rebuild has repeated a failed build once since
1-nov-2026; the deploy now builds in «Build Odoo image» with the same
helper. A first attempt that ran 900 s or more is not repeated: the job
runner kills a job at 3600 s, and its SIGKILL leaves the job orphaned.

The repetition is decided on the host, so the command is run here with a
stand-in ``docker`` and ``sleep`` on the ``PATH`` that record their calls.

The step runs in whatever shell the host's user has: bash for root, but a
user made with ``useradd`` and no ``-s`` has ``/bin/sh``, which is dash on
Debian. The shell tests therefore run in bash, ``sh`` and, if installed,
dash. The clock is a stand-in ``date``, so a long attempt costs no time.
"""
import asyncio
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from odoo.tests.common import BaseCase

from ..models.deploy_instance_executor import (
    _BUILD_RETRY_MAX_FIRST_S,
    _BUILD_RETRY_WAIT_S,
    _FETCH_SECONDS_FILE,
    DeployInstanceExecutor,
    _build_retried_once,
    _then_context_prune,
)
from ..models.transport import CommandResult, SSHTransport
from .test_blocked_and_aborted_jobs import _DeployJobCase
from .test_executor_features import _make_deploy_executor, _make_rebuild_executor

_BUILD = "Build Odoo image"
_COMPOSE_BUILD = "docker compose build odoo"
#: The removal of the build contexts no build is using (``build_context_prune``).
_CONTEXT_PRUNE = "docker builder prune -af --filter type=source.local"
#: The real ``sleep``, for the stand-ins that have to hang until the TERM.
_REAL_SLEEP = shutil.which("sleep")


class _FakeHost:
    """A directory with a stand-in ``docker``, ``sleep`` and ``date``.

    ``docker`` logs each call, moves the clock forward by *docker_secs*
    and exits with the next status of *statuses* (0 once they run out);
    ``sleep`` logs its argument and returns at once; ``date`` prints the
    clock, whatever its arguments. Either of the first two hangs instead
    when its ``*_hangs`` says so, for a test that sends the TERM while it
    runs.
    """

    def __init__(
        self, tmp, statuses=(), docker_hangs=False, sleep_hangs=False,
        docker_secs=0,
    ):
        """Write the stand-ins into *tmp*.

        :param str tmp: an empty directory
        :param statuses: the exit status of each ``docker`` call, in order
        :param bool docker_hangs: the first ``docker`` call runs until killed
        :param bool sleep_hangs: ``sleep`` runs until killed
        :param int docker_secs: the seconds each ``docker`` call «takes»
        """
        self.log = Path(tmp, "calls")
        counter = Path(tmp, "count")
        clock = Path(tmp, "clock")
        clock.write_text("1800000000\n", encoding="utf-8")
        cases = "".join(
            f"  {n}) exit {status} ;;\n" for n, status in enumerate(statuses, 1)
        )
        hang = f"{_REAL_SLEEP} 30\n" if docker_hangs else ""
        self._write(tmp, "docker", (
            f'echo "docker $*" >> {self.log}\n'
            f"n=$(( $(cat {counter} 2>/dev/null || echo 0) + 1 ))\n"
            f"echo $n > {counter}\n"
            f"echo $(( $(cat {clock}) + {docker_secs} )) > {clock}\n"
            f"{hang}"
            'case "$n" in\n'
            f"{cases}"
            "esac\nexit 0\n"
        ))
        self._write(tmp, "date", f"cat {clock}\n")
        self._write(tmp, "sleep", (
            f'echo "sleep $*" >> {self.log}\n'
            + (f"exec {_REAL_SLEEP} 30\n" if sleep_hangs else "exit 0\n")
        ))
        self.env = os.environ | {"PATH": f"{tmp}:{os.environ.get('PATH', '')}"}

    @staticmethod
    def _write(tmp, name, body):
        """Write the executable stand-in *name* with shell *body*."""
        path = Path(tmp, name)
        path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
        path.chmod(0o755)

    def calls(self):
        """The calls recorded so far, one line each.

        :rtype: list[str]
        """
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def run(self, command, shell="bash"):
        """Run *command* with ``<shell> -c``, as sshd runs a step.

        :param str shell: the user's login shell
        :return: the exit status and the output
        :rtype: tuple[int, str]
        """
        done = subprocess.run(
            [shell, "-c", command], capture_output=True, text=True,
            env=self.env, timeout=30, check=False,
        )
        return done.returncode, done.stdout


class TestTheBuildIsRepeatedOnce(BaseCase):
    """``_build_retried_once`` on the host, with stand-in commands."""

    #: The shell that runs the step.
    _SHELL = "bash"

    def _run(self, statuses, docker_secs=0):
        """Run the wrapped build with ``docker`` exiting *statuses*.

        :param int docker_secs: the seconds each ``docker`` call «takes»
        :return: the exit status, the output and the calls
        :rtype: tuple[int, str, list[str]]
        """
        with tempfile.TemporaryDirectory() as tmp:
            host = _FakeHost(tmp, statuses, docker_secs=docker_secs)
            status, out = host.run(
                _build_retried_once(_COMPOSE_BUILD), self._SHELL,
            )
            return status, out, host.calls()

    def test_a_build_that_goes_through_runs_once(self):
        status, _out, calls = self._run([0])
        self.assertEqual(status, 0)
        self.assertEqual(calls, ["docker compose build odoo"])

    def test_a_failure_then_a_success_waits_and_goes_through(self):
        status, out, calls = self._run([1, 0])
        self.assertEqual(status, 0)
        self.assertEqual(calls, [
            "docker compose build odoo",
            f"sleep {_BUILD_RETRY_WAIT_S}",
            "docker compose build odoo",
        ])
        self.assertIn("trying once more", out)

    def test_two_failures_fail_with_the_seconds_status(self):
        status, _out, calls = self._run([1, 17])
        self.assertEqual(status, 17)
        self.assertEqual(
            [c for c in calls if c.startswith("docker")],
            ["docker compose build odoo"] * 2,
        )

    def test_a_long_first_attempt_is_not_repeated(self):
        """900 s or more: the job runner's 3600 s would not hold another."""
        status, out, calls = self._run([3, 0], docker_secs=_BUILD_RETRY_MAX_FIRST_S)
        self.assertEqual(status, 3)
        self.assertEqual(calls, ["docker compose build odoo"])
        self.assertIn("not trying again", out)

    def test_a_first_attempt_just_under_the_cap_is_repeated(self):
        status, _out, calls = self._run(
            [3, 0], docker_secs=_BUILD_RETRY_MAX_FIRST_S - 2,
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            [c for c in calls if c.startswith("docker")],
            ["docker compose build odoo"] * 2,
        )

    def _terminated(self, **hangs):
        """Send TERM to the step's process group while a stand-in hangs.

        As sshd does to a cancelled command's session.

        :return: the exit status and the calls
        :rtype: tuple[int, list[str]]
        """
        with tempfile.TemporaryDirectory() as tmp:
            host = _FakeHost(tmp, [1, 0], **hangs)
            proc = subprocess.Popen(
                [self._SHELL, "-c", _build_retried_once(_COMPOSE_BUILD)],
                env=host.env, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            expected = 2 if hangs.get("sleep_hangs") else 1
            deadline = time.monotonic() + 10
            while len(host.calls()) < expected and time.monotonic() < deadline:
                time.sleep(0.05)
            os.killpg(proc.pid, signal.SIGTERM)
            status = proc.wait(timeout=10)
            # What a repetition would have run had it been reached.
            time.sleep(0.3)
            return status, host.calls()

    def test_a_term_during_the_first_attempt_stops_it_for_good(self):
        status, calls = self._terminated(docker_hangs=True)
        self.assertNotEqual(status, 0)
        self.assertEqual(calls, ["docker compose build odoo"])

    def test_a_term_during_the_wait_stops_it_for_good(self):
        status, calls = self._terminated(sleep_hangs=True)
        self.assertNotEqual(status, 0)
        self.assertEqual(
            calls, ["docker compose build odoo", f"sleep {_BUILD_RETRY_WAIT_S}"],
        )


class TestTheBuildIsRepeatedOnceInSh(TestTheBuildIsRepeatedOnce):
    """The same, in ``/bin/sh``: the shell of a user made without ``-s``."""

    #: Odoo collects only the methods a class defines, unless told otherwise.
    allow_inherited_tests_method = True
    _SHELL = "sh"


@skipUnless(shutil.which("dash"), "dash is not installed")
class TestTheBuildIsRepeatedOnceInDash(TestTheBuildIsRepeatedOnce):
    """The same, in dash, which has no ``SECONDS``."""

    allow_inherited_tests_method = True
    _SHELL = "dash"


class TestTheDownloadCountsForTheRepetition(BaseCase):
    """After ``fetch-repos``, its seconds count with the first attempt's.

    The download left the build for a step of its own (10-nov-2026): the
    repetition still keeps the job under the runner's 3600 s only if the
    time it took counts, as it did when the build cloned.
    """

    def _run(self, seconds, docker_secs, shell="sh"):
        """Run the wrapped build after a download that wrote *seconds*.

        :param seconds: what the seconds file holds, or None for no file
        :param int docker_secs: the seconds each ``docker`` call «takes»
        :return: the exit status and the ``docker`` calls
        :rtype: tuple
        """
        with tempfile.TemporaryDirectory() as tmp:
            host = _FakeHost(tmp, [3, 0], docker_secs=docker_secs)
            work = Path(tmp, "inst")
            target = work / _FETCH_SECONDS_FILE
            target.parent.mkdir(parents=True)
            if seconds is not None:
                target.write_text(f"{seconds}\n", encoding="utf-8")
            status, _out = host.run(
                f"cd {work} && "
                + _build_retried_once(_COMPOSE_BUILD, fetched=True),
                shell,
            )
            return status, [c for c in host.calls() if c.startswith("docker")]

    def test_a_long_download_and_a_short_build_are_not_repeated(self):
        """890 s of download and 20 s of build reach the cap."""
        status, calls = self._run(_BUILD_RETRY_MAX_FIRST_S - 10, 20)
        self.assertEqual(status, 3)
        self.assertEqual(calls, [_COMPOSE_BUILD])

    def test_a_short_download_and_a_short_build_are_repeated(self):
        """Under the cap together: repeated, in bash and in sh."""
        for shell in ("bash", "sh"):
            with self.subTest(shell=shell):
                status, calls = self._run(100, 20, shell)
                self.assertEqual(status, 0)
                self.assertEqual(calls, [_COMPOSE_BUILD] * 2)

    def test_no_file_or_one_that_is_not_a_number_counts_as_nothing(self):
        """A missing or tampered file is zero, never a command."""
        for seconds in (None, "", "12; touch pwned", "-5"):
            with self.subTest(seconds=seconds):
                status, calls = self._run(seconds, 20)
                self.assertEqual(status, 0)
                self.assertEqual(calls, [_COMPOSE_BUILD] * 2)


class TestTheBuildContextsGoAfterTheBuild(BaseCase):
    """A series that fetches on the host removes the build contexts after
    its build, built or not, keeping the build's status (10-nov-2026).

    Its context holds the repositories' trees, about 1.2 GB for 19.0, and
    Docker kept one per instance until the daily prune: a night that
    rebuilt the 25 instances of one host would have taken some 30 GB.
    """

    def _run(self, statuses, shell="sh"):
        """Run the wrapped build with ``docker`` exiting *statuses*.

        :return: the exit status and the ``docker`` calls
        :rtype: tuple
        """
        with tempfile.TemporaryDirectory() as tmp:
            host = _FakeHost(tmp, statuses)
            status, _out = host.run(
                _then_context_prune(
                    f"cd {tmp} && "
                    + _build_retried_once(_COMPOSE_BUILD, fetched=True),
                ),
                shell,
            )
            return status, [c for c in host.calls() if c.startswith("docker")]

    def test_after_a_build_that_goes_through(self):
        for shell in ("bash", "sh"):
            with self.subTest(shell=shell):
                status, calls = self._run([0], shell)
                self.assertEqual(status, 0)
                self.assertEqual(calls, [_COMPOSE_BUILD, _CONTEXT_PRUNE])

    def test_after_a_build_that_fails_with_the_build_s_status(self):
        status, calls = self._run([3, 4])
        self.assertEqual(status, 4)
        self.assertEqual(calls, [_COMPOSE_BUILD, _COMPOSE_BUILD, _CONTEXT_PRUNE])

    def test_a_removal_that_fails_does_not_fail_the_build(self):
        status, calls = self._run([0, 1])
        self.assertEqual(status, 0)
        self.assertEqual(calls, [_COMPOSE_BUILD, _CONTEXT_PRUNE])

    def test_only_a_series_that_fetches_removes_them(self):
        """A deploy's and a rebuild's build, with 19.0 on the host and sent
        back to the image by ``incubacloud.repos_in_image_series``."""
        for make, label in (
            (_make_deploy_executor, _BUILD),
            (_make_rebuild_executor, "Rebuild Odoo image"),
        ):
            for in_image in ((), ("19.0",)):
                with self.subTest(label=label, in_image=in_image):
                    ex = make()
                    ex._repos_in_image_series = lambda v=in_image: frozenset(v)
                    command = {c[0]: c for c in ex.get_commands()}[label][1]
                    self.assertEqual(_CONTEXT_PRUNE in command, not in_image)


class TestTheDeployBuildsInAStepOfItsOwn(BaseCase):
    """«Build Odoo image» comes between «Pull images» and «Start database»."""

    def _labels(self, **inst):
        """The labels of a deploy of an instance with fields *inst*."""
        return [c[0] for c in _make_deploy_executor(**inst).get_commands()]

    def test_the_step_repeats_a_cached_build_and_stops_the_deploy(self):
        steps = {c[0]: c for c in _make_deploy_executor().get_commands()}
        _label, command, opts = steps[_BUILD]
        self.assertEqual(opts, {"stop_on_failure": True})
        self.assertEqual(command.count(_COMPOSE_BUILD), 2)
        self.assertIn(f"sleep {_BUILD_RETRY_WAIT_S}", command)
        # The build of today's first ``compose run``: cached, no pull.
        self.assertNotIn("--pull", command)
        self.assertNotIn("--no-cache", command)
        self.assertTrue(command.startswith("{ cd ~/projects/test-inst && "))

    def test_it_builds_after_the_context_and_before_anything_starts(self):
        # Every series fetches every repository instead of recording the
        # heads, 19.0 too from core 1.0.224: the build takes the fetched
        # trees.
        for name, inst, heads in (
            ("production", {}, "Fetch the repositories"),
            ("staging", {"environment": "staging"}, "Fetch the repositories"),
            ("8.0", {"odoo_version": "8.0"}, "Fetch the repositories"),
        ):
            with self.subTest(name):
                labels = self._labels(**inst)
                build = labels.index(_BUILD)
                self.assertEqual(labels[build - 1], "Pull images")
                self.assertEqual(labels[build + 1], "Start database")
                for before in (
                    heads,
                    "Install incubacloud_connect",
                    "Free the build cache if the disk is low",
                ):
                    self.assertLess(labels.index(before), build, before)
                for after in ("Initialize database", "Start instance"):
                    self.assertGreater(labels.index(after), build, after)

    def test_an_old_series_reads_apt_from_the_archive_before_it_builds(self):
        labels = self._labels(odoo_version="8.0")
        self.assertLess(
            labels.index("Read apt from the Debian archive"),
            labels.index(_BUILD),
        )

    def test_a_moves_deploy_builds_before_it_starts(self):
        labels = self._labels(move_origin_host_id=7)
        build = labels.index(_BUILD)
        self.assertEqual(labels[build - 1], "Pull images")
        self.assertLess(build, labels.index("Start instance"))


class TestAFailedBuildStopsTheDeploy(_DeployJobCase):
    """Nothing after «Build Odoo image» runs when it fails for good."""

    def test_no_later_step_runs_and_the_error_names_the_build(self):
        executor = DeployInstanceExecutor(self.job, self.host)
        commands = _make_deploy_executor().get_commands()
        build = [c[0] for c in commands].index(_BUILD)

        async def execute(command, _on_stdout, _on_stderr, timeout=None):
            """End every step well but the build, which fails."""
            status = 1 if _COMPOSE_BUILD in command else 0
            return CommandResult(stdout="", exit_status=status)

        transport = MagicMock(spec=SSHTransport)
        transport.execute.side_effect = execute
        results, skipped = asyncio.run(executor._run_steps(transport, commands))
        self.assertEqual(list(results)[-1], _BUILD)
        self.assertEqual(skipped, [c[0] for c in commands[build + 1:]])
        self.assertIn(
            f"'{_BUILD}' exited with status 1",
            executor._outcome_errors(results, skipped),
        )


class TestAConflictIsNamedOnce(_DeployJobCase):
    """An ``AddonsConfigError`` the repeated build printed twice is one conflict."""

    def _chunk(self, content):
        """Log *content* on the job's stderr."""
        self.env["cloud.job.log.chunk"].create({
            "job_id": self.job.id, "source": "stderr", "content": content,
        })

    def test_each_addon_is_named_once(self):
        error = (
            "doodbalib.AddonsConfigError: Addon {addon} defined in several"
            " repos {{'acme/odoo-addons', 'OCA/web'}}"
        )
        for addon in ("web_x", "web_x", "web_y"):
            self._chunk(error.format(addon=addon))
        executor = DeployInstanceExecutor(self.job, self.host)
        Instance = type(self.env["cloud.instance"])
        with patch.object(
            Instance, "_raise_addon_conflict_alert", autospec=True,
        ) as raise_alert:
            executor._detect_addon_conflicts()
        conflicts = raise_alert.call_args.args[1]
        self.assertEqual([c["addon"] for c in conflicts], ["web_x", "web_y"])
        self.assertEqual(
            sorted(conflicts[0]["repos"]), ["OCA/web", "acme/odoo-addons"],
        )
