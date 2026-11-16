"""A build that has no room on its server is refused before it changes anything.

16-nov-2026: a preview's rebuild filled its server's disk half-way, after
copier had rewritten its files, and the previous image and the build cache
had filled the disk until the daily prune. A rebuild that builds now reads
the room first (``_check_build_room``): the current image plus the reserve
the server's other instances keep. Short of it, it frees the dangling
images and the build cache and reads again, and refuses with the figures.
Once its new image is in service, a step of its own removes the previous
one, and caps the build cache on a disk short of room
(``after_build_prune``).
"""
import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from odoo.tests.common import BaseCase, TransactionCase

from ..models.abstract_executor import JobRefused
from ..models.docker_prune_executor import (
    BUILD_CACHE_RESERVED,
    BUILD_IMAGE_ESTIMATE_GB,
    BUILD_PEAK_FACTOR,
    after_build_prune,
    build_room_command,
    build_room_free_command,
    build_room_needed,
    build_room_refusal,
    parse_build_room,
)
from ..models.registry import executor_registry
from ..models.transport import CommandResult, SSHTransport
from ..restore_space import GIB, RESERVE_BYTES
from .test_executor_features import _make_rebuild_executor

#: The labels of the rebuild's restart and of the step after it.
_RESTART = "Restart instance"
_AFTER = "Remove the previous image"


def _run_with_fake_docker(command, fails_on=None):
    """Run *command* in bash with a ``docker`` that answers and records.

    ``docker info`` gives a directory of this machine's disk, so ``df``
    reads a real free space; ``compose images -q`` names an image,
    and ``image inspect`` gives it 123 bytes. Every other call is
    recorded, and fails when its arguments hold *fails_on* (``""``: every
    one).

    :param str command: the shell command under test
    :param str fails_on: the text of the calls that exit 1, or None
    :return: the exit status, the output, and the recorded calls
    :rtype: tuple[int, str, list[str]]
    """
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp, "calls")
        fake = Path(tmp, "docker")
        fail = ""
        if fails_on is not None:
            fail = f'case "$*" in *"{fails_on}"*) exit 1;; esac\n'
        fake.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = info ]; then echo {tmp}; exit 0; fi\n'
            'if [ "$1 $2" = "compose images" ]; then echo 0123abcd; exit 0; fi\n'
            'if [ "$1 $2" = "image inspect" ]; then echo "image 123"; exit 0; fi\n'
            f'echo "$*" >> {log}\n'
            'echo "noise $*"; echo "noise $*" >&2\n'
            f"{fail}exit 0\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        env = os.environ | {"PATH": f"{tmp}:{os.environ.get('PATH', '')}"}
        done = subprocess.run(
            ["sh", "-c", command], capture_output=True, text=True,
            env=env, timeout=30, check=False, cwd=tmp,
        )
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return done.returncode, done.stdout, calls


class TestTheRoomOfABuild(BaseCase):

    def test_the_room_is_read_in_a_posix_shell(self):
        status, out, _calls = _run_with_fake_docker(build_room_command("."))
        self.assertEqual(status, 0)
        free, image = parse_build_room(out)
        self.assertGreater(free, 0)
        self.assertEqual(image, 123)

    def test_what_cannot_be_read_is_left_out(self):
        self.assertEqual(parse_build_room(""), (None, None))
        self.assertEqual(parse_build_room("free_kb x\nimage\nnoise 3"), (None, None))
        self.assertEqual(parse_build_room("free_kb 2\n"), (2048, None))

    def test_the_need_is_the_image_and_the_reserve(self):
        image = 5 * GIB
        needed, share = build_room_needed(image)
        self.assertEqual(share, int(image * BUILD_PEAK_FACTOR))
        self.assertEqual(needed, share + RESERVE_BYTES)

    def test_without_an_image_the_estimate_counts(self):
        needed, share = build_room_needed(None)
        self.assertEqual(share, int(BUILD_IMAGE_ESTIMATE_GB * GIB * BUILD_PEAK_FACTOR))
        self.assertEqual(needed, share + RESERVE_BYTES)

    def test_the_refusal_names_both_parts(self):
        needed, share = build_room_needed(5 * GIB)
        text = build_room_refusal(int(1.2 * GIB), 5 * GIB, needed, share, "Nothing.")
        self.assertIn("1.2 GiB free", text)
        self.assertIn("as large as its current one", text)
        self.assertIn("5.0 GiB the server's other instances keep", text)
        self.assertIn("Nothing.", text)


class TestTheCleanupBeforeABuild(BaseCase):

    def test_it_prunes_both_and_answers_only_the_room(self):
        status, out, calls = _run_with_fake_docker(build_room_free_command("."))
        self.assertEqual(status, 0)
        self.assertEqual(calls[0], "image prune -f")
        self.assertEqual(calls[1], 'builder prune -af --filter until=1h')
        self.assertEqual(len(calls), 3)
        self.assertNotIn("noise", out)
        free, image = parse_build_room(out)
        self.assertGreater(free, 0)
        self.assertEqual(image, 123)

    def test_it_never_fails(self):
        status, out, _calls = _run_with_fake_docker(
            build_room_free_command("."), fails_on="",
        )
        self.assertEqual(status, 0)
        self.assertNotIn("noise", out)


class TestTheCleanupAfterABuild(BaseCase):

    def test_with_room_only_the_dangling_images_go(self):
        status, out, calls = _run_with_fake_docker(after_build_prune(0))
        self.assertEqual(status, 0)
        self.assertEqual(calls, ["image prune -f"])
        self.assertIn("The build cache stays", out)

    def test_short_of_room_the_cache_is_capped(self):
        # No disk has a petabyte free: always under.
        status, out, calls = _run_with_fake_docker(after_build_prune(10 ** 6))
        self.assertEqual(status, 0)
        self.assertEqual(calls, [
            "image prune -f",
            f"builder prune -f --reserved-space {BUILD_CACHE_RESERVED}",
        ])
        self.assertIn("capped", out)

    def test_a_docker_without_reserved_space_keeps_storage(self):
        status, _out, calls = _run_with_fake_docker(
            after_build_prune(10 ** 6), fails_on="--reserved-space",
        )
        self.assertEqual(status, 0)
        self.assertEqual(calls[-1], f"builder prune -f --keep-storage {BUILD_CACHE_RESERVED}")

    def test_it_never_fails(self):
        status, _out, _calls = _run_with_fake_docker(after_build_prune(10 ** 6), fails_on="")
        self.assertEqual(status, 0)


def _transport(*answers):
    """A transport whose ``run`` gives *answers* in order.

    :return: ``MagicMock(spec=SSHTransport)``
    """
    transport = MagicMock(spec=SSHTransport)
    transport.run = AsyncMock(side_effect=[CommandResult(a, 0) for a in answers])
    return transport


class TestTheRebuildChecksItsRoom(BaseCase):

    def _check(self, transport):
        """Run the rebuild's check through *transport*."""
        ex = _make_rebuild_executor()
        asyncio.run(ex._check_build_room(transport, "Nothing was changed."))

    def test_enough_room_reads_once(self):
        transport = _transport(f"free_kb {50 * 1024 * 1024}\nimage {5 * GIB}\n")
        self._check(transport)
        transport.run.assert_awaited_once()

    def test_short_then_enough_after_the_cleanup(self):
        transport = _transport(
            f"free_kb {6 * 1024 * 1024}\nimage {5 * GIB}\n",
            f"free_kb {20 * 1024 * 1024}\n",
        )
        self._check(transport)
        self.assertEqual(transport.run.await_count, 2)
        cleanup = transport.run.await_args_list[1].args[0]
        self.assertIn("docker image prune -f", cleanup)
        self.assertIn("docker builder prune -af", cleanup)

    def test_still_short_is_refused_with_the_figures(self):
        transport = _transport(
            f"free_kb {6 * 1024 * 1024}\nimage {5 * GIB}\n",
            f"free_kb {7 * 1024 * 1024}\n",
        )
        with self.assertRaises(JobRefused) as caught:
            self._check(transport)
        self.assertIn("No room on the server to build the image", str(caught.exception))
        self.assertIn("7.0 GiB free", str(caught.exception))
        self.assertIn("Nothing was changed.", str(caught.exception))

    def test_a_disk_that_cannot_be_read_lets_the_build_go(self):
        transport = _transport("")
        self._check(transport)
        transport.run.assert_awaited_once()


class TestTheRebuildsPlan(BaseCase):

    def test_the_cleanup_follows_the_restart(self):
        labels = [c[0] for c in _make_rebuild_executor().get_commands()]
        self.assertEqual(labels[labels.index(_RESTART) + 1], _AFTER)

    def test_its_step_never_stops_the_run(self):
        step = next(c for c in _make_rebuild_executor().get_commands() if c[0] == _AFTER)
        self.assertEqual(len(step), 2)
        self.assertTrue(step[1].endswith("|| true"))

    def test_a_flow_that_builds_no_image_has_no_cleanup(self):
        ex = _make_rebuild_executor()
        ex._builds_image = False
        self.assertNotIn(_AFTER, [c[0] for c in ex.get_commands()])


class TestTheRebuildRefusesBeforeCopier(TransactionCase):

    def setUp(self):
        super().setUp()
        host = self.env["cloud.host"].create({
            "name": "room-host", "ip_address": "192.0.2.97", "user": "root",
            "wildcard_domain": "room.example.com",
        })
        project = self.env["cloud.project"].create({"name": "Room Proj"})
        inst = self.env["cloud.instance"].create({
            "name": "roominst", "project_id": project.id,
            "environment": "production", "host_id": host.id,
            "state": "deployed", "status": "ok",
        })
        job = self.env["cloud.job"].create({
            "name": "rebuild", "host_id": host.id, "instance_id": inst.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "rebuild_instance")], limit=1,
            ).id,
        })
        self.inst = inst
        self.executor = executor_registry.get("rebuild_instance")(job, host)

    def _before(self, builds=True):
        """Run ``before_execute`` with a full disk; return the upload mock."""
        cls = type(self.executor)
        self.executor._builds_image = builds
        transport = _transport(
            f"free_kb {1024 * 1024}\nimage {5 * GIB}\n", f"free_kb {1024 * 1024}\n",
        )
        with patch.object(cls, "_upload_copier_files", new_callable=AsyncMock) as upload, \
                patch.object(cls, "_ship_secret_key", new_callable=AsyncMock), \
                patch.object(cls, "_stage_extra_env_files", new_callable=AsyncMock):
            if builds:
                with self.assertRaises(JobRefused) as caught:
                    asyncio.run(self.executor.before_execute(transport))
                self.refusal = caught.exception
            else:
                asyncio.run(self.executor.before_execute(transport))
        return upload, transport

    def test_a_rebuild_without_room_changes_nothing(self):
        upload, _transport_used = self._before()
        upload.assert_not_called()
        asyncio.run(self.executor.on_abort(self.refusal))
        self.assertEqual(self.inst.status, "ok")
        log = [line for line, _source in self.executor._log_buffer]
        self.assertTrue(log[-1].startswith("✗ JobRefused: No room on the server"))

    def test_a_flow_that_builds_no_image_does_not_check(self):
        upload, transport = self._before(builds=False)
        upload.assert_awaited_once()
        transport.run.assert_not_awaited()


class TestALaterAttemptOfTheRebuild(TransactionCase):
    """The retry after «Update changed modules» answered 75 (crons busy).

    Its first attempt already wrote the image and rewrote the files, so
    the free space is lower and the check would refuse, falsely, a job
    that finishes if left alone.
    """

    def setUp(self):
        super().setUp()
        host = self.env["cloud.host"].create({
            "name": "room-host-r", "ip_address": "192.0.2.99", "user": "root",
            "wildcard_domain": "roomr.example.com",
        })
        project = self.env["cloud.project"].create({"name": "Room Retry"})
        self.inst = self.env["cloud.instance"].create({
            "name": "roomretry", "project_id": project.id,
            "environment": "production", "host_id": host.id,
            "state": "deployed", "status": "ok",
        })
        self.job = self.env["cloud.job"].create({
            "name": "rebuild", "host_id": host.id, "instance_id": self.inst.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "rebuild_instance")], limit=1,
            ).id,
        })
        self.executor = executor_registry.get("rebuild_instance")(self.job, host)

    def _first_attempt_ended_in_the_update(self):
        """Log what the first attempt started, up to the update it left at 75."""
        self.env["cloud.job.log.chunk"].create([
            {"job_id": self.job.id, "source": "system", "content": f"Running: {label}"}
            for label in (
                "Update with copier", "Rebuild Odoo image", "Update changed modules",
            )
        ])
        self.env.flush_all()

    def _before(self, full):
        """Run ``before_execute`` of a second attempt on a full disk.

        :param bool full: whether the rebuild is a full one
        :return: the transport, and the refusal or None
        """
        cls = type(self.executor)
        transport = _transport(
            f"free_kb {1024 * 1024}\nimage {5 * GIB}\n", f"free_kb {1024 * 1024}\n",
        )
        refusal = None
        with patch.object(cls, "_needs_full_rebuild", return_value=full), \
                patch.object(cls, "_upload_copier_files", new_callable=AsyncMock), \
                patch.object(cls, "_ship_secret_key", new_callable=AsyncMock), \
                patch.object(cls, "_stage_extra_env_files", new_callable=AsyncMock):
            try:
                asyncio.run(self.executor.before_execute(transport))
            except JobRefused as exc:
                refusal = exc
        return transport, refusal

    def test_the_first_attempt_is_checked(self):
        transport, refusal = self._before(full=False)
        self.assertIsNotNone(refusal)
        self.assertIn("Nothing was changed", str(refusal))
        self.assertEqual(transport.run.await_count, 2)

    def test_a_cached_second_attempt_is_not_refused_nor_read(self):
        self._first_attempt_ended_in_the_update()
        transport, refusal = self._before(full=False)
        self.assertIsNone(refusal)
        transport.run.assert_not_awaited()

    def test_a_full_second_attempt_is_checked_without_saying_nothing_changed(self):
        self._first_attempt_ended_in_the_update()
        transport, refusal = self._before(full=True)
        self.assertIsNotNone(refusal)
        self.assertIn("No room on the server to build the image", str(refusal))
        self.assertNotIn("Nothing was changed", str(refusal))
        self.assertIn("An earlier attempt of this job had begun", str(refusal))


class TestTheDeployRefusesBeforeCopier(TransactionCase):
    """A deploy short of room fails before it writes anything, as any abort."""

    def setUp(self):
        super().setUp()
        host = self.env["cloud.host"].create({
            "name": "room-host-d", "ip_address": "192.0.2.98", "user": "root",
            "wildcard_domain": "roomd.example.com",
        })
        project = self.env["cloud.project"].create({"name": "Room Deploy"})
        self.inst = self.env["cloud.instance"].create({
            "name": "roomdep", "project_id": project.id,
            "environment": "production", "host_id": host.id,
            "state": "deploying", "status": "ok",
        })
        job = self.env["cloud.job"].create({
            "name": "deploy", "host_id": host.id, "instance_id": self.inst.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "deploy_instance")], limit=1,
            ).id,
        })
        self.executor = executor_registry.get("deploy_instance")(job, host)

    def test_a_deploy_without_room_writes_nothing(self):
        cls = type(self.executor)
        # A new instance: no image to read, the estimate counts.
        transport = _transport(f"free_kb {1024 * 1024}\n", f"free_kb {1024 * 1024}\n")
        with patch.object(cls, "_upload_copier_files", new_callable=AsyncMock) as upload:
            with self.assertRaises(JobRefused) as caught:
                asyncio.run(self.executor.before_execute(transport))
        upload.assert_not_called()
        self.assertIn("estimated, as the instance has none yet", str(caught.exception))
        self.assertIn("Nothing was changed on the server.", str(caught.exception))

    def test_a_deploy_with_room_goes_on(self):
        cls = type(self.executor)
        transport = _transport(f"free_kb {100 * 1024 * 1024}\n")
        with patch.object(cls, "_upload_copier_files", new_callable=AsyncMock) as upload, \
                patch.object(cls, "_ship_secret_key", new_callable=AsyncMock), \
                patch.object(cls, "_stage_extra_env_files", new_callable=AsyncMock):
            asyncio.run(self.executor.before_execute(transport))
        upload.assert_awaited_once()
