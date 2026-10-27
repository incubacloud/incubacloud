"""Continuous recovery: what it renders, when it acts, how it judges.

The host side (``pitr.sh``, ``pitr_agent.sh``, ``pitr_archive.sh``) has its
own bats tests and was run end to end against Alpine PostgreSQL and an S3
server; these cover the panel: which instances archive, what reaches their
stack (and what never reaches the database's container), who may change
it, the jobs the actions and crons queue, how a reading is judged, which
base copy a restore starts from, and what a job that did part of its work
records.
"""
import asyncio
import base64
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg2
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from lxml import etree

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import BaseCase, TransactionCase, new_test_user, tagged

from odoo.addons.incubacloud.models.delete_instance_executor import (
    DeleteInstanceExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport
from odoo.addons.incubacloud.tests.test_executor_durable_writes import (
    JobEndAssertions,
)
from odoo.addons.incubacloud_pitr import uninstall_hook
from odoo.addons.incubacloud_pitr.models import pitr_executors as ex
from odoo.addons.incubacloud_pitr.models._constants import (
    _RCLONE_OBSCURE_KEY,
    rclone_obscure,
)


def _base(name, finish):
    """A base copy as ``parse_bases`` returns it."""
    return {"backup_name": name, "finish_time": datetime.fromisoformat(finish)}


class TestPureHelpers(BaseCase):

    def test_an_obscured_password_reveals_to_itself(self):
        """rclone's ``reveal`` is AES-CTR with the IV in front."""
        raw = base64.urlsafe_b64decode(rclone_obscure("s3cret") + "==")
        decryptor = Cipher(
            algorithms.AES(_RCLONE_OBSCURE_KEY), modes.CTR(raw[:16]),
        ).decryptor()
        self.assertEqual(decryptor.update(raw[16:]) + decryptor.finalize(), b"s3cret")
        # Stable: an unchanged password does not recreate the container.
        self.assertEqual(rclone_obscure("s3cret"), rclone_obscure("s3cret"))

    def test_the_base_copies_line_is_read_oldest_first(self):
        cluster, bases = ex.parse_bases(
            'noise\nIC_PITR_BASES 7698 [{"backup_name":"base_2",'
            '"finish_time":"2026-10-18T22:30:45.419565Z","uncompressed_size":9},'
            '{"backup_name":"base_1","finish_time":"2026-10-18T22:30:18.263177Z"}]\n'
        )
        self.assertEqual(cluster, "7698")
        self.assertEqual([b["backup_name"] for b in bases], ["base_1", "base_2"])
        self.assertEqual(str(bases[0]["finish_time"]), "2026-10-18 22:30:18")

    def test_no_line_no_copies(self):
        self.assertEqual(ex.parse_bases("ERROR: nothing"), (None, []))
        self.assertEqual(ex.parse_status("ERROR"), {})
        self.assertEqual(ex.parse_restore("ERROR"), {})

    def test_the_status_and_restore_lines_are_read(self):
        status = ex.parse_status(
            "IC_PITR_STATUS mode=on last_archived=1792363522 failed=0 "
            "last_failed=0 cluster=7698 pending=2 oldest_pending=30 agent=1",
        )
        self.assertEqual(status["mode"], "on")
        self.assertEqual(status["agent"], "1")
        restore = ex.parse_restore(
            "log\nIC_PITR_RESTORE job=9 state=db-only stopped_at=filestore "
            "timeline=00000002 promoted_at=1792369056\n",
        )
        self.assertEqual(restore["state"], "db-only")
        self.assertEqual(restore["timeline"], "00000002")


class TestChoosingTheBaseCopy(BaseCase):
    """A restore to the past starts timeline 2 at P, branching off R.

    Measured with PostgreSQL 17 on 19-oct-2026: a restore to a moment after
    P from a timeline-1 copy taken before R, asked for timeline 2, followed
    timeline 1 to the branch and timeline 2 to the moment.
    """

    B1 = _base("base_000000010000000000000005", "2026-10-18 00:00:00")
    B2 = _base("base_000000010000000000000009", "2026-10-19 02:00:00")
    B3 = _base("base_00000002000000000000000C", "2026-10-19 12:30:00")
    SWITCH = {
        "timeline": "00000002", "since": "2026-10-19 12:00:00",
        "target": "2026-10-18 18:00:00",
    }

    def _choose(self, target, switches=(SWITCH,)):
        base, timeline = ex.choose_base(
            [self.B1, self.B2, self.B3], datetime.fromisoformat(target), list(switches),
        )
        return (base["backup_name"] if base else None), timeline

    def test_without_a_restore_the_newest_copy_before_the_moment(self):
        self.assertEqual(
            self._choose("2026-10-19 03:00:00", ()), (self.B2["backup_name"], 1),
        )

    def test_before_the_branch_both_histories_are_one(self):
        self.assertEqual(self._choose("2026-10-18 10:00:00"), (self.B1["backup_name"], 1))

    def test_between_the_branch_and_the_restore_it_is_the_abandoned_history(self):
        """At that moment the instance lived on timeline 1: B2 holds it."""
        self.assertEqual(self._choose("2026-10-19 06:00:00"), (self.B2["backup_name"], 1))

    def test_after_the_restore_a_copy_taken_after_the_branch_does_not_serve(self):
        """B2 holds the abandoned history: the restore follows B1 into timeline 2."""
        self.assertEqual(self._choose("2026-10-19 12:10:00"), (self.B1["backup_name"], 2))

    def test_once_the_new_timeline_has_a_copy_it_is_used(self):
        self.assertEqual(self._choose("2026-10-19 13:00:00"), (self.B3["backup_name"], 2))

    def test_nothing_serves_a_moment_before_every_copy(self):
        self.assertEqual(self._choose("2026-10-17 00:00:00"), (None, None))


class _PitrCase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "pitr-host", "ip_address": "192.0.2.90", "user": "ubuntu",
            "wildcard_domain": "pitr.example.com",
        })
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "pitr-bb", "backend_type": "s3", "s3_bucket": "bucket",
            "s3_path": "backups", "s3_endpoint_url": "https://r2.example.com",
            "s3_access_key_id": "AK", "s3_secret_access_key": "SK",
            "backup_retention": "14D",
        })
        self.project = self.env["cloud.project"].create({
            "name": "pitr-proj", "remote_folder": "pp",
            "backup_backend_id": self.backend.id,
        })
        self.inst = self.env["cloud.instance"].create({
            "name": "pitr-inst", "project_id": self.project.id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed", "running": True,
        })
        self.Instance = type(self.inst)
        self.Job = type(self.env["cloud.job"])
        sys_log = patch.object(ex._PitrExecutor, "_sys", autospec=True)
        self.sys_log = sys_log.start()
        self.addCleanup(sys_log.stop)

    def _executor(self, cls, code, payload=None, state=None):
        job_type = self.env["cloud.job.type"].search([("code", "=", code)], limit=1)
        job = self.env["cloud.job"].create({
            "name": code, "host_id": self.host.id, "instance_id": self.inst.id,
            "job_type_id": job_type.id, "payload": payload or {},
        })
        if state:
            job.state = state
        return cls(job, self.host)

    def _alerts(self, code):
        return self.env["cloud.alert"].search([
            ("code", "=", code), ("instance_id", "=", self.inst.id),
            ("state", "=", "active"),
        ])


class TestWhatArchives(_PitrCase):

    def test_off_until_switched_on(self):
        self.assertFalse(self.inst._pitr_active())
        self.assertEqual(self.inst._extra_services(), ())
        self.assertEqual(self.inst._compose_override_fragments(), {})
        self.assertEqual(self.inst._extra_env_files(), {})

    def test_a_production_with_a_backend_archives_once_on(self):
        self.inst.pitr_enabled = True
        self.assertTrue(self.inst._pitr_active())
        self.assertIn("pitr", self.inst.expected_services())

    def test_a_staging_never_archives(self):
        staging = self.env["cloud.instance"].create({
            "name": "pitr-stg", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
            "pitr_enabled": True,
        })
        self.assertFalse(staging._pitr_active())
        self.assertNotIn("pitr", staging.expected_services())

    def test_without_a_backend_nothing_archives(self):
        self.project.backup_backend_id = False
        self.inst.pitr_enabled = True
        self.assertFalse(self.inst._pitr_active())

    def test_every_instance_is_born_with_its_key(self):
        self.assertEqual(len(self.inst.pitr_key), 64)

    def test_the_window_is_the_backup_retention(self):
        self.assertEqual(self.inst._pitr_window_days(), 14)
        self.backend.backup_retention = "3M"
        self.assertEqual(self.inst._pitr_window_days(), 90)
        self.backend.backup_retention = "?"
        self.assertEqual(self.inst._pitr_window_days(), 7)

    def test_the_prefix_is_beside_the_duplicity_one_never_under_it(self):
        self.assertEqual(
            self.inst._pitr_prefix_base(), "s3://bucket/backups/pp/pitr-inst-pitr",
        )
        dup = self.inst.instance_backup_dst.replace("boto3+s3://", "s3://")
        self.assertFalse(self.inst._pitr_prefix_base().startswith(dup + "/"))


class TestWhatReachesTheStack(_PitrCase):

    def setUp(self):
        super().setUp()
        self.inst.pitr_enabled = True

    def test_the_database_gets_the_scripts_and_nothing_secret(self):
        """Odoo's superuser could read the db container's environment."""
        frag = self.inst._compose_override_fragments()
        self.assertEqual(frag["db"], {
            "volumes": ["./.walg:/opt/walg:ro"],
            # Born archiving: switching on reloads instead of restarting.
            "command": ["-c", "archive_mode=on"],
        })
        agent = frag["pitr"]
        self.assertTrue(agent["image"].startswith("rclone/rclone:"))
        self.assertIn("@sha256:", agent["image"])
        self.assertIn("db:/var/lib/postgresql/data:ro", agent["volumes"])
        self.assertIn("./.walg/spool:/opt/walg/spool", agent["volumes"])
        self.assertEqual(agent["env_file"], [".docker/db-access.env", ".docker/pitr.env"])
        self.assertIn(
            f"loop 5 {self.inst._pitr_sync_hour()} || exec sleep infinity",
            agent["command"][0],
        )

    def test_the_agent_s_environment_carries_the_credentials_and_the_key(self):
        files = self.inst._extra_env_files()
        env = files[".docker/pitr.env"]
        self.assertIn("IC_PITR_PREFIX=s3://bucket/backups/pp/pitr-inst-pitr\n", env)
        self.assertIn("AWS_SECRET_ACCESS_KEY=SK\n", env)
        self.assertIn(f"WALG_LIBSODIUM_KEY={self.inst.pitr_key}\n", env)
        self.assertIn("AWS_ENDPOINT=https://r2.example.com\n", env)
        self.assertIn("PGHOST=db\n", env)
        self.assertIn("RCLONE_CONFIG_PITR_REMOTE=pitrs3:bucket/backups/pp/pitr-inst-pitr\n", env)
        self.assertIn("RCLONE_CONFIG_PITRC_TYPE=crypt\n", env)
        self.assertEqual(
            sorted(files),
            [".docker/pitr.env", ".walg/README.txt", ".walg/spool/README.txt"],
        )

    def test_switched_off_the_credentials_stay_for_the_purge_not_the_agent(self):
        self.inst.write({"pitr_enabled": False, "pitr_cluster_id": "77"})
        self.assertEqual(self.inst._compose_override_fragments(), {})
        self.assertEqual(list(self.inst._extra_env_files()), [".docker/pitr.env"])

    def test_an_external_stack_gets_nothing_rendered(self):
        """It declares the agent itself; the probe still watches it."""
        self.inst.pitr_stack_external = True
        self.assertTrue(self.inst._pitr_active())
        self.assertEqual(self.inst._compose_override_fragments(), {})
        self.assertEqual(self.inst._extra_env_files(), {})
        self.assertIn("pitr", self.inst.expected_services())

    def test_a_copy_without_a_key_renders_nothing_until_it_gets_one(self):
        """Neutralization clears the key: it never opens production's archive."""
        self.inst.pitr_key = False
        self.assertEqual(self.inst._compose_override_fragments(), {})
        self.inst._pitr_ensure_key()
        self.assertEqual(len(self.inst.pitr_key), 64)
        self.assertIn("pitr", self.inst._compose_override_fragments())

    def test_deleting_purges_the_prefix_archiving_keeps_it(self):
        first, second = self.inst._teardown_pre_steps("~/pp/pitr-inst", False)
        self.assertIn("purge pitr:", first[1])
        self.assertTrue(first[2]["stop_on_failure"])
        # Once the agent and the database stopped, for what they sent
        # between the two (``TestDeletingPurgesTheArchive``).
        self.assertIn("purge pitr:", second[1])
        self.assertEqual(self.inst._teardown_pre_steps("~/pp/pitr-inst", True), [])


class TestDeletingPurgesTheArchive(_PitrCase):
    """Deleting the instance leaves nothing of its archive in the bucket.

    The two purges run as the host runs them, through ``sh``, with a
    ``docker`` that answers as told: the first with the stack up, the
    second once the agent and the database stopped. Either one failing
    stops the delete, and the instance, its directory and the agent's
    credentials stay for the delete to be run again.
    """

    #: Records each call in ``$CALLS``, declares ``$STUB_SERVICES`` and
    #: exits with ``$STUB_CONFIG``, ``$STUB_STOP``, ``$STUB_PURGE`` or
    #: ``$STUB_START`` for the call that names it.
    DOCKER = """#!/bin/sh
echo "docker $*" >> "$CALLS"
case "$*" in
    "compose config --services")
        [ "${STUB_CONFIG:-0}" -eq 0 ] || exit "$STUB_CONFIG"
        printf '%s' "${STUB_SERVICES-db
odoo
pitr
}" ;;
    "compose stop"*) exit "${STUB_STOP:-0}" ;;
    "run "*) exit "${STUB_PURGE:-0}" ;;
    "compose start"*) exit "${STUB_START:-0}" ;;
esac
exit 0
"""

    def setUp(self):
        super().setUp()
        self.inst.pitr_enabled = True
        tmp = Path(tempfile.mkdtemp(prefix="pitr-teardown-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.dir = tmp / "inst"
        (self.dir / ".docker").mkdir(parents=True)
        self.env_file = self.dir / ".docker" / "pitr.env"
        self.env_file.write_text("AWS_REGION=auto\n")
        self.calls = tmp / "calls"
        self.calls.write_text("")
        (tmp / "bin").mkdir()
        docker = tmp / "bin" / "docker"
        docker.write_text(self.DOCKER)
        docker.chmod(0o755)
        self.path = f"{tmp / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}"
        self.first, self.second = self.inst._teardown_pre_steps(str(self.dir), False)

    def _run(self, step, **stubs):
        """Run *step*'s command through ``sh``, as the host would.

        :param tuple step: ``(label, command[, opts])``
        :param stubs: what docker answers: ``services`` (what the compose
            declares, one per line), and the exit status of ``config``
            (reading the compose), ``stop``, ``purge`` (the ephemeral
            rclone) and ``start``
        :return: the command's exit status; what it wrote is left in
            ``self.output``
        :rtype: int
        """
        env = {"PATH": self.path, "CALLS": str(self.calls)}
        env.update({f"STUB_{name.upper()}": str(v) for name, v in stubs.items()})
        proc = subprocess.run(
            ["sh", "-c", step[1]], env=env, capture_output=True, text=True,
            timeout=60, check=False,
        )
        self.output = proc.stdout + proc.stderr
        return proc.returncode

    def _calls(self, prefix):
        """The docker calls the steps made that start with *prefix*.

        :rtype: list
        """
        return [
            call for call in self.calls.read_text().splitlines()
            if call.startswith(prefix)
        ]

    def test_a_second_purge_that_fails_stops_the_delete(self):
        """What the agent sent between the two would outlive the instance."""
        self.assertNotEqual(self._run(self.second, purge=1), 0)
        opts = self.second[2] if len(self.second) > 2 else {}
        self.assertTrue(opts.get("stop_on_failure"))
        self.assertEqual(
            self._calls("docker compose stop"), ["docker compose stop pitr db"],
        )
        # Running again, as a first purge that fails leaves it.
        self.assertEqual(
            self._calls("docker compose start"), ["docker compose start pitr db"],
        )

    def test_a_stop_that_fails_is_not_purged_over(self):
        """An agent that may still be sending makes no purge the last one."""
        self.assertNotEqual(self._run(self.second, stop=1), 0)
        self.assertEqual(self._calls("docker run"), [])
        self.assertEqual(
            self._calls("docker compose start"), ["docker compose start pitr db"],
        )

    def test_a_compose_it_cannot_read_stops_the_step_untouched(self):
        """Read as "no agent", the database alone was stopped and the
        purge ran with the agent still sending."""
        self.assertNotEqual(self._run(self.second, config=1), 0)
        self.assertEqual(self._calls("docker compose stop"), [])
        self.assertEqual(self._calls("docker run"), [])
        self.assertEqual(self._calls("docker compose start"), [])

    def test_the_log_says_whether_what_stopped_runs_again(self):
        self._run(self.second, purge=1)
        self.assertIn("pitr db running again", self.output)
        self._run(self.second, purge=1, start=1)
        self.assertIn("ERROR: pitr db could not be started again", self.output)
        self.assertNotIn("running again", self.output)

    def test_without_the_agent_only_the_database_is_stopped_and_started(self):
        """Switched off, the credentials stay for the purge; the agent does not.

        Compose refuses to stop or start a service it does not declare,
        and does neither to the others then.
        """
        self.assertEqual(self._run(self.second, services="db\nodoo\n"), 0)
        self.assertNotEqual(
            self._run(self.second, services="db\nodoo\n", purge=1), 0,
        )
        self.assertEqual(self._calls("docker compose stop"), [
            "docker compose stop db", "docker compose stop db",
        ])
        self.assertEqual(
            self._calls("docker compose start"), ["docker compose start db"],
        )

    def test_an_empty_prefix_or_no_credentials_let_the_delete_go_on(self):
        for purge in (0, 3):
            self.assertEqual(self._run(self.second, purge=purge), 0)
        self.env_file.unlink()
        self.assertEqual(self._run(self.second, purge=1), 0)
        self.assertEqual(len(self._calls("docker run")), 2)
        self.assertEqual(self._calls("docker compose start"), [])

    def test_the_first_purge_stops_the_delete_with_the_stack_up(self):
        self.assertTrue(self.first[2]["stop_on_failure"])
        self.assertNotEqual(self._run(self.first, purge=1), 0)
        self.assertEqual(self._run(self.first, purge=3), 0)
        self.assertEqual(self._calls("docker compose"), [])

    def test_the_delete_keeps_the_instance_when_the_second_purge_fails(self):
        """Nothing is torn down, and the instance is back to 'deployed'."""
        executor = self._executor(DeleteInstanceExecutor, "delete_instance")
        steps = executor.get_commands()
        failing = dict(step[:2] for step in steps)[
            "Stop archiving and purge what arrived meanwhile"
        ]
        transport = MagicMock(spec=SSHTransport)
        transport.execute.side_effect = (
            lambda command, *_handlers: CommandResult("", int(command == failing))
        )
        results, skipped = asyncio.run(executor._run_steps(transport, steps))
        self.assertIn("Stop and remove containers", skipped)
        self.assertIn("Remove instance directory", skipped)
        errors = executor._outcome_errors(results, skipped)
        self.assertTrue(errors)
        self.inst._transition("deleting")
        asyncio.run(executor.on_failure(results, errors))
        self.assertEqual(self.inst.state, "deployed")
        self.assertEqual(self.inst.status, "error")
        self.assertFalse(self.inst.removal_finalized)


# post_install: creating a user at_install trips over account's
# ``res_partner.autopost_bills`` NOT NULL column (as in core's tests).
@tagged("post_install", "-at_install")
class TestOnlyAManagerChangesIt(_PitrCase):

    def setUp(self):
        super().setUp()
        self.dev = new_test_user(
            self.env, login="pitr-dev", groups="incubacloud.group_cloud_developer",
        )
        # A member of the project: the record rules let them write it.
        self.project.member_ids = [(4, self.dev.id)]

    def test_a_developer_cannot_use_the_actions(self):
        with self.assertRaises(AccessError):
            self.inst.with_user(self.dev).action_pitr_enable()
        with self.assertRaises(AccessError):
            self.inst.with_user(self.dev).action_pitr_restore("2026-10-18 22:31")

    def test_nor_write_what_the_actions_guard(self):
        """Switched off, or pointed at a cluster to purge, by a plain write."""
        for vals in (
            {"pitr_enabled": False},
            {"pitr_previous_clusters": [{"id": "1", "until": "2000-01-01"}]},
            {"pitr_stack_external": True},
        ):
            with self.assertRaisesRegex(AccessError, "group_cloud_manager"):
                self.inst.with_user(self.dev).write(vals)

    def test_other_fields_stay_theirs(self):
        self.inst.with_user(self.dev).write({"apt_dependencies": "curl"})
        self.assertEqual(self.inst.apt_dependencies, "curl")


class TestWhatTheCardIsTold(_PitrCase):

    def test_the_status_says_when_the_host_was_last_read(self):
        """"Last archived" is as fresh as the watch's reading, every ten
        minutes: shown alone it passed for archiving behind."""
        when = datetime(2026, 10, 20, 22, 34, 55)
        self.inst.write({
            "pitr_state": "ok", "pitr_enabled": True,
            "pitr_oldest_base_at": datetime(2026, 10, 20, 17, 24, 53),
            "pitr_last_archived_at": when, "pitr_checked_at": when,
        })
        vals = self.inst._pitr_status_vals()
        self.assertEqual(vals["checked"], "2026-10-20 22:34:55")
        self.assertEqual(vals["last_archived"], "2026-10-20 22:34:55")
        self.assertEqual(vals["from"], "2026-10-20 17:24:53")
        self.inst.pitr_checked_at = False
        self.assertFalse(self.inst._pitr_status_vals()["checked"])


class TestTheActions(_PitrCase):

    def _chain(self, call):
        with patch.object(self.Job, "enqueue_chain", autospec=True, return_value=[7, 8]) as chain:
            call()
        return [s["job_type_code"] for s in chain.call_args.args[1]], chain.call_args.args[1]

    def test_switching_on_rebuilds_then_archives(self):
        codes, _steps = self._chain(lambda: self.inst.action_pitr_enable("15"))
        self.assertEqual(codes, ["rebuild_instance", "pitr_enable"])
        self.assertTrue(self.inst.pitr_enabled)
        self.assertEqual(self.inst.pitr_objective, "15")
        self.assertEqual(self.inst.pitr_state, "enabling")

    def test_a_copy_switched_on_gets_a_key_of_its_own(self):
        self.inst.pitr_key = False
        self._chain(self.inst.action_pitr_enable)
        self.assertEqual(len(self.inst.pitr_key), 64)

    def test_an_unknown_objective_is_refused(self):
        with self.assertRaises(UserError):
            self.inst.action_pitr_enable("7")

    def test_an_external_stack_is_switched_by_its_runbook(self):
        """On the panel's own instance the job would stop its own Odoo."""
        self.inst.pitr_stack_external = True
        for action in (
            self.inst.action_pitr_enable, self.inst.action_pitr_disable,
            lambda: self.inst.action_pitr_set_objective("15"),
        ):
            with self.assertRaises(UserError):
                action()

    def test_a_job_that_only_waits_is_not_said_to_run(self):
        """Refused either way, saying which: queued has not started."""
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "pitr_enable")], limit=1,
        )
        job = self.env["cloud.job"].create({
            "name": "pitr_enable", "host_id": self.host.id,
            "instance_id": self.inst.id, "job_type_id": job_type.id,
            "state": "queued",
        })
        with self.assertRaisesRegex(UserError, "waiting in this instance's queue"):
            self.inst.action_pitr_enable()
        job.state = "started"
        with self.assertRaisesRegex(UserError, "already running"):
            self.inst.action_pitr_enable()

    def test_switching_off_stops_archiving_then_rebuilds(self):
        self.inst.write({"pitr_enabled": True, "pitr_state": "ok"})
        codes, _steps = self._chain(self.inst.action_pitr_disable)
        self.assertEqual(codes, ["pitr_disable", "rebuild_instance"])
        self.assertFalse(self.inst.pitr_enabled)

    def test_a_staging_cannot_switch_it_on(self):
        self.inst.environment = "staging"
        with self.assertRaises(UserError):
            self.inst.action_pitr_enable()

    def test_a_restore_takes_a_backup_of_the_present_first(self):
        now = fields.Datetime.now()
        self.inst.write({
            "pitr_enabled": True, "pitr_state": "ok",
            "pitr_oldest_base_at": now - timedelta(days=3),
        })
        target = fields.Datetime.to_string(now - timedelta(days=1))[:16]
        codes, steps = self._chain(lambda: self.inst.action_pitr_restore(target))
        self.assertEqual(codes, ["backup_create", "pitr_restore"])
        self.assertEqual(steps[1]["payload"]["target"], target + ":00")

    def test_a_moment_outside_the_window_is_refused(self):
        now = fields.Datetime.now()
        self.inst.write({
            "pitr_enabled": True, "pitr_state": "ok",
            "pitr_oldest_base_at": now - timedelta(days=1),
        })
        with self.assertRaises(UserError):
            self.inst.action_pitr_restore(
                fields.Datetime.to_string(now - timedelta(days=2)),
            )
        with self.assertRaises(UserError):
            self.inst.action_pitr_restore("yesterday at noon")

    def test_uninstalling_is_refused_while_an_instance_archives(self):
        self.inst.pitr_enabled = True
        with self.assertRaises(UserError):
            uninstall_hook(self.env)


class TestReconciling(_PitrCase):
    """The watch and a rebuild decide the same way (``_pitr_reconcile``)."""

    def _watch(self):
        with patch.object(self.Job, "enqueue_chain", autospec=True, return_value=[1, 2]) as chain, \
                patch.object(self.Job, "enqueue", autospec=True, return_value=3) as enqueue:
            self.env["cloud.instance"]._cron_pitr_watch()
        chains = [[s["job_type_code"] for s in c.args[1]] for c in chain.call_args_list]
        singles = [c.args[3] for c in enqueue.call_args_list]
        return chains, singles

    def _rebuilt(self):
        with patch.object(self.Job, "enqueue_chain", autospec=True, return_value=[1, 2]) as chain, \
                patch.object(self.Job, "enqueue", autospec=True, return_value=3) as enqueue:
            self.inst._after_stack_rendered()
        chains = [[s["job_type_code"] for s in c.args[1]] for c in chain.call_args_list]
        return chains, [c.args[3] for c in enqueue.call_args_list]

    def test_an_instance_wanted_on_is_switched_on(self):
        self.inst.pitr_enabled = True
        self.assertEqual(self._watch(), ([["rebuild_instance", "pitr_enable"]], []))
        self.assertEqual(self.inst.pitr_state, "enabling")

    def test_a_lone_switch_goes_to_the_head_only_for_the_rebuild_that_asks(self):
        """What a rebuild queues from ``_after_stack_rendered`` follows it;
        the watch's job waits its turn in the instance's queue."""
        with patch.object(self.Job, "enqueue", autospec=True, return_value=3) as enqueue:
            self.inst._pitr_enqueue(["pitr_enable"])
        self.assertFalse(enqueue.call_args.kwargs["bypass_running_check"])
        with patch.object(self.Job, "enqueue", autospec=True, return_value=3) as enqueue:
            self.inst._pitr_enqueue(["pitr_enable"], head=True)
        self.assertTrue(enqueue.call_args.kwargs["bypass_running_check"])
        self.inst.pitr_enabled = True
        with patch.object(type(self.inst), "_pitr_renders", autospec=True, return_value=True), \
                patch.object(self.Job, "enqueue", autospec=True, return_value=3) as enqueue:
            self.inst._after_stack_rendered()
        self.assertEqual(enqueue.call_args.args[3], "pitr_enable")
        self.assertTrue(enqueue.call_args.kwargs["bypass_running_check"])

    def test_a_switch_that_failed_waits_a_day_both_ways(self):
        """Neither a rebuild nor a restart of the database every ten minutes."""
        self.inst.pitr_enabled = True
        self._executor(ex.PitrEnableExecutor, "pitr_enable", state="failed")
        self.assertEqual(self._watch(), ([], []))
        self.assertEqual(self._rebuilt(), ([], []))
        self.inst.write({"pitr_enabled": False, "pitr_state": "ok"})
        self._executor(ex.PitrDisableExecutor, "pitr_disable", state="failed")
        self.assertEqual(self._watch(), ([], ["pitr_status"]))

    def test_an_archiving_instance_is_read(self):
        self.inst.write({
            "pitr_enabled": True, "pitr_state": "ok",
            "pitr_last_base_at": fields.Datetime.now() - timedelta(hours=1),
        })
        self.assertEqual(self._watch(), ([], ["pitr_status"]))

    def test_a_due_base_copy_is_queued_in_place_of_the_reading(self):
        """The reading is taken at the next watch, ten minutes later."""
        self.inst.write({"pitr_enabled": True, "pitr_state": "ok"})
        self.assertEqual(self._watch(), ([], ["pitr_base_backup"]))
        self.inst.pitr_last_base_at = fields.Datetime.now() - timedelta(hours=40)
        self.assertEqual(self._watch(), ([], ["pitr_base_backup"]))

    def test_a_base_copy_that_failed_is_tried_again_an_hour_later(self):
        """Not at every watch."""
        self.inst.write({
            "pitr_enabled": True, "pitr_state": "ok",
            "pitr_last_base_at": fields.Datetime.now() - timedelta(hours=40),
        })
        failed = self._executor(
            ex.PitrBaseBackupExecutor, "pitr_base_backup", state="failed",
        ).job
        self.assertEqual(self._watch(), ([], ["pitr_status"]))
        self.env.cr.execute(
            "UPDATE cloud_job SET create_date = %s WHERE id = %s",
            (fields.Datetime.now() - timedelta(minutes=61), failed.id),
        )
        failed.invalidate_recordset(["create_date"])
        self.assertEqual(self._watch(), ([], ["pitr_base_backup"]))

    def test_one_no_longer_wanted_is_switched_off(self):
        self.inst.write({"pitr_enabled": False, "pitr_state": "ok"})
        self.assertEqual(self._watch(), ([["pitr_disable", "rebuild_instance"]], []))

    def test_a_stopped_instance_is_left_alone(self):
        self.inst.write({"pitr_enabled": True, "pitr_state": "ok", "running": False})
        self.assertEqual(self._watch(), ([], []))

    def test_an_external_stack_is_only_watched(self):
        """Never switched; read, and its base copy queued when due: with the
        hourly cron gone, the watch is the only way to one (the panel's own
        instance runs such a stack)."""
        self.inst.write({
            "pitr_enabled": True, "pitr_stack_external": True, "pitr_state": "off",
            "pitr_last_base_at": fields.Datetime.now() - timedelta(hours=1),
        })
        self.assertEqual(self._watch(), ([], []))
        self.inst.pitr_state = "ok"
        self.assertEqual(self._watch(), ([], ["pitr_status"]))
        self.inst.pitr_last_base_at = fields.Datetime.now() - timedelta(hours=40)
        self.assertEqual(self._watch(), ([], ["pitr_base_backup"]))

    def test_a_rebuild_that_shipped_the_pieces_is_followed_by_the_switch_alone(self):
        self.inst.pitr_enabled = True
        self.assertEqual(self._rebuilt(), ([], ["pitr_enable"]))
        self.inst.write({"pitr_enabled": False, "pitr_state": "ok"})
        self.assertEqual(self._rebuilt(), ([], ["pitr_disable"]))

    def test_a_rebuild_without_the_pieces_needs_another(self):
        """A copy without a key rendered nothing: it gets one, and a rebuild."""
        self.inst.write({"pitr_enabled": True, "pitr_key": False})
        self.assertEqual(self._rebuilt(), ([["rebuild_instance", "pitr_enable"]], []))

    def test_a_rebuild_whose_chain_goes_on_leaves_the_switch_to_the_watch(self):
        """A revival restores its backup after the deploy: switching would
        restart the database under it."""
        self.inst.pitr_enabled = True
        for code in ("rebuild_instance", "backup_restore"):
            self._executor(ex.PitrEnableExecutor, code, state="pending")
        self.assertEqual(self._rebuilt(), ([], []))

    def test_nothing_to_do_when_it_already_matches(self):
        self.inst.write({"pitr_enabled": True, "pitr_state": "ok"})
        self.assertEqual(self._rebuilt(), ([], []))
        self.inst.write({"pitr_enabled": False, "pitr_state": "off"})
        self.assertEqual(self._rebuilt(), ([], []))

    def test_not_during_a_move(self):
        self.inst.write({"pitr_enabled": True, "move_origin_host_id": self.host.id})
        self.assertEqual(self._rebuilt(), ([], []))

    def test_the_daily_copy_is_queued_at_its_hour(self):
        """By the watch, the only one that queues it: at the instance's
        hour once the last copy is 20 hours old, not at another hour,
        not with a fresher copy."""
        self.inst.write({
            "pitr_enabled": True, "pitr_state": "ok",
            "pitr_last_base_at": fields.Datetime.now() - timedelta(hours=22),
        })

        def hour_in(offset):
            """Patch the instance's hour to *offset* hours from now, UTC."""
            return patch.object(
                type(self.inst), "_pitr_sync_hour", autospec=True,
                side_effect=lambda _inst: (
                    datetime.now(timezone.utc).hour + offset
                ) % 24,
            )

        with hour_in(0):
            self.assertEqual(self._watch(), ([], ["pitr_base_backup"]))
        with hour_in(12):
            self.assertEqual(self._watch(), ([], ["pitr_status"]))
        self.inst.pitr_last_base_at = fields.Datetime.now() - timedelta(hours=2)
        with hour_in(0):
            self.assertEqual(self._watch(), ([], ["pitr_status"]))


class TestSwitchingOnAndBaseCopies(_PitrCase):

    def setUp(self):
        super().setUp()
        self.inst.write({"pitr_enabled": True, "pitr_state": "enabling"})

    def test_switching_on_records_the_cluster_and_its_window(self):
        executor = self._executor(ex.PitrEnableExecutor, "pitr_enable")
        self.assertEqual(
            [c[0] for c in executor.get_commands()],
            ["Install wal-g", "Switch archiving on", "First base copy"],
        )
        out = (
            "IC_PITR_CLUSTER 77 restarted=1\nIC_PITR_BASES 77 "
            '[{"backup_name":"base_1","finish_time":"2026-10-18T22:30:18Z"}]'
        )
        asyncio.run(executor.on_success({
            "Switch archiving on": {"stdout": out, "exit_status": 0},
        }))
        self.assertEqual(self.inst.pitr_state, "ok")
        self.assertEqual(self.inst.pitr_cluster_id, "77")
        self.assertEqual(str(self.inst.pitr_oldest_base_at), "2026-10-18 22:30:18")

    def test_archiving_on_without_a_first_copy_is_archiving(self):
        """Not 'failed': the next base copy job is due at once."""
        executor = self._executor(ex.PitrEnableExecutor, "pitr_enable")
        asyncio.run(executor.on_failure({
            "Install wal-g": {"stdout": "", "exit_status": 0},
            "Switch archiving on": {"stdout": "IC_PITR_CLUSTER 77 restarted=1", "exit_status": 0},
            "First base copy": {"stdout": "boom", "exit_status": 1},
        }, ["First base copy failed"]))
        self.assertEqual(self.inst.pitr_state, "ok")
        self.assertEqual(self.inst.pitr_cluster_id, "77")
        # A first sighting: the watch's next reading confirms it.
        self.assertTrue(self.env["cloud.alert.suspicion"].search([
            ("code", "=", "pitr_base_missing"), ("instance_id", "=", self.inst.id),
        ]))
        self.assertFalse(self._alerts("pitr_failing"))

    def test_a_switch_that_never_reached_the_database_is_off_and_critical(self):
        executor = self._executor(ex.PitrEnableExecutor, "pitr_enable")
        asyncio.run(executor.on_failure({
            "Install wal-g": {"stdout": "checksum mismatch", "exit_status": 1},
        }, ["Install failed"]))
        self.assertEqual(self.inst.pitr_state, "off")
        self.assertEqual(self._alerts("pitr_failing").level, "critical")

    def test_a_new_cluster_keeps_the_old_one_until_the_window_passes(self):
        self.inst.pitr_cluster_id = "66"
        executor = self._executor(ex.PitrEnableExecutor, "pitr_enable")
        asyncio.run(executor.on_success({"Switch archiving on": {
            "stdout": "IC_PITR_CLUSTER 77 restarted=1", "exit_status": 0,
        }}))
        self.assertEqual(self.inst.pitr_previous_clusters[0]["id"], "66")
        self.inst.pitr_previous_clusters = [
            {"id": "66", "until": "2020-01-01 00:00:00"},
            # A history that names the current cluster never purges it.
            {"id": "77", "until": "2020-01-01 00:00:00"},
        ]
        base = self._executor(ex.PitrBaseBackupExecutor, "pitr_base_backup")
        housekeep = dict(c[:2] for c in base.get_commands())["Drop what left the window"]
        self.assertIn(" 66", housekeep)
        self.assertNotIn(" 77", housekeep)

    def test_a_base_copy_whose_housekeeping_failed_is_still_recorded(self):
        self.inst.write({
            "pitr_cluster_id": "77", "pitr_state": "ok",
            "pitr_previous_clusters": [{"id": "66", "until": "2020-01-01 00:00:00"}],
        })
        executor = self._executor(ex.PitrBaseBackupExecutor, "pitr_base_backup")
        bases = (
            'IC_PITR_BASES 77 [{"backup_name":"base_1","finish_time":"2026-10-17T02:00:00Z"},'
            '{"backup_name":"base_2","finish_time":"2026-10-18T02:00:00Z"}]'
        )
        asyncio.run(executor.on_failure({
            "Base copy": {"stdout": bases, "exit_status": 0},
            "Drop what left the window": {
                "stdout": f"IC_PITR_PURGED 66\n{bases}", "exit_status": 1,
            },
        }, ["housekeeping failed"]))
        self.assertEqual(str(self.inst.pitr_last_base_at), "2026-10-18 02:00:00")
        self.assertFalse(self.inst.pitr_previous_clusters)

    def test_with_no_base_copy_left_the_window_is_empty(self):
        """A purge took them all: no moment is offered, a copy is due at once.

        ``[]`` is what wal-g 3.0.9 prints for a prefix with no copy.
        """
        self.inst.write({
            "pitr_cluster_id": "77", "pitr_state": "ok",
            "pitr_oldest_base_at": fields.Datetime.now() - timedelta(days=3),
            "pitr_last_base_at": fields.Datetime.now() - timedelta(hours=2),
        })
        self.assertFalse(self.inst._pitr_base_due())
        executor = self._executor(ex.PitrBaseBackupExecutor, "pitr_base_backup")
        asyncio.run(executor.on_success({
            "Drop what left the window": {
                "stdout": "IC_PITR_BASES 77 []", "exit_status": 0,
            },
        }))
        self.assertFalse(self.inst.pitr_oldest_base_at)
        self.assertFalse(self.inst.pitr_last_base_at)
        self.assertIsNone(self.inst._pitr_window())
        status = self.inst._pitr_status_vals()
        self.assertEqual((status["from"], status["last_base"]), (False, False))
        with self.assertRaisesRegex(UserError, "outside the window"):
            self.inst.action_pitr_restore("2026-10-18 22:31")
        self.assertTrue(self.inst._pitr_base_due())

    def test_a_listing_that_failed_leaves_the_window(self):
        oldest = fields.Datetime.now() - timedelta(days=3)
        self.inst.write({"pitr_cluster_id": "77", "pitr_oldest_base_at": oldest})
        executor = self._executor(ex.PitrBaseBackupExecutor, "pitr_base_backup")
        asyncio.run(executor.on_failure({
            "Drop what left the window": {
                "stdout": "IC_PITR_BASES 77 ", "exit_status": 1,
            },
        }, ["housekeeping failed"]))
        self.assertEqual(self.inst.pitr_oldest_base_at, oldest)

    def test_after_a_restore_the_next_base_copy_is_full(self):
        base = self._executor(
            ex.PitrBaseBackupExecutor, "pitr_base_backup", payload={"full": True},
        )
        self.assertTrue(base.get_commands()[0][1].endswith(" full"))

    def test_a_move_switches_it_on_on_the_new_cluster(self):
        with patch.object(self.Job, "enqueue", autospec=True) as enqueue:
            self.inst._after_restore_hook("move")
            self.inst._after_restore_hook("backup_restore")
            self.inst.move_origin_host_id = self.host
            self.inst._after_restore_hook("restore")
        self.assertEqual(
            [c.args[3] for c in enqueue.call_args_list],
            ["pitr_enable", "pitr_base_backup"],
        )

    def test_a_restore_to_a_moment_takes_no_second_base_copy(self):
        """Its executor took a full one, on the timeline the restore
        started (``PitrRestoreExecutor._restored``): the hook's would be a
        second, running beside it."""
        with patch.object(self.Job, "enqueue", autospec=True) as enqueue:
            self.inst._after_restore_hook("pitr")
            self.inst._after_restore_hook("restore")
        self.assertEqual(
            [c.args[3] for c in enqueue.call_args_list], ["pitr_base_backup"],
        )


class TestOdooRestartedAroundTheDatabase(JobEndAssertions, _PitrCase):
    """Switching archiving on or off may restart the database, with odoo
    stopped around it (``restart_db``). Odoo's series is then gone for
    about a minute, which the metrics cron read as a stop: the panel said
    "Stopped" for up to two minutes. The job confirms the instance running
    as soon as the script says odoo is back, durably, while its steps go
    on: switching on takes a first base copy after it, for minutes, and
    a confirmation at the job's end came after the grace was over."""

    def setUp(self):
        """Archiving being switched; the cron lowered ``running`` meanwhile."""
        super().setUp()
        self.registry_enter_test_mode()
        self.inst.write({
            "pitr_enabled": True, "pitr_state": "enabling", "running": False,
        })

    def _line(self, cls, code, line):
        """Hand *line* to a new job's executor, as a step prints it.

        :return: the executor, its job not ended
        """
        executor = self._executor(cls, code)
        # The durable cursor reads the database, not this env's cache.
        self.env.flush_all()
        asyncio.run(executor.on_stdout(line))
        return executor

    def test_the_switch_on_confirms_the_instance_as_the_line_comes(self):
        """Stored before the job ends: no outcome hook has run."""
        executor = self._line(
            ex.PitrEnableExecutor, "pitr_enable", "IC_PITR_ODOO_RESTARTED",
        )
        self.assertIn(("IC_PITR_ODOO_RESTARTED", "stdout"), executor._log_buffer)
        self.end_job()
        self.assertTrue(self.stored(self.inst, "running"))
        self.assertTrue(self.stored(self.inst, "running_confirmed_at"))

    def test_the_switch_off_confirms_it_too(self):
        """The same when switching off restarts the database."""
        self._line(
            ex.PitrDisableExecutor, "pitr_disable", "IC_PITR_ODOO_RESTARTED",
        )
        self.end_job()
        self.assertTrue(self.stored(self.inst, "running"))
        self.assertTrue(self.stored(self.inst, "running_confirmed_at"))

    def test_a_restart_with_odoo_already_stopped_confirms_nothing(self):
        """The database restarted (``restarted=1``), odoo was not running."""
        self._line(
            ex.PitrEnableExecutor, "pitr_enable", "IC_PITR_CLUSTER 77 restarted=1",
        )
        self.end_job()
        self.assertFalse(self.stored(self.inst, "running"))
        self.assertFalse(self.stored(self.inst, "running_confirmed_at"))

    def test_the_job_s_end_confirms_nothing_again(self):
        """A stop read after the grace is not undone when the job ends."""
        executor = self._line(
            ex.PitrEnableExecutor, "pitr_enable", "IC_PITR_ODOO_RESTARTED",
        )
        self.inst.write({"running": False})
        asyncio.run(executor.on_success({"Switch archiving on": {
            "stdout": "IC_PITR_ODOO_RESTARTED\nIC_PITR_CLUSTER 77 restarted=1",
            "exit_status": 0,
        }}))
        self.assertEqual(self.inst.pitr_state, "ok")
        self.assertFalse(self.inst.running)

    def test_a_busy_row_does_not_stop_the_switch(self):
        """Left unconfirmed, said in the job's log; the step goes on."""
        with patch.object(
            ex._PitrExecutor, "_persist", autospec=True,
            side_effect=psycopg2.errors.LockNotAvailable(),
        ):
            self._line(
                ex.PitrEnableExecutor, "pitr_enable", "IC_PITR_ODOO_RESTARTED",
            )
        self.assertIn("could not be confirmed running", self.sys_log.call_args.args[1])
        self.assertFalse(self.inst.running)

    def test_any_other_failure_of_the_write_still_raises(self):
        """Only a busy row is forgiven: anything else is a fault to see."""
        executor = self._executor(ex.PitrEnableExecutor, "pitr_enable")
        with patch.object(
            ex._PitrExecutor, "_persist", autospec=True,
            side_effect=psycopg2.errors.SerializationFailure(),
        ), self.assertRaises(psycopg2.errors.SerializationFailure):
            asyncio.run(executor.on_stdout("IC_PITR_ODOO_RESTARTED"))


class TestReadingTheArchiver(_PitrCase):

    def setUp(self):
        super().setUp()
        self.inst.write({
            "pitr_enabled": True, "pitr_state": "ok", "pitr_cluster_id": "77",
            "pitr_last_base_at": fields.Datetime.now(),
        })

    def _read(self, line, times=1):
        """Judge *line* as the watch would, *times* readings in a row."""
        for _reading in range(times):
            executor = self._executor(ex.PitrStatusExecutor, "pitr_status")
            asyncio.run(executor.on_success({"r": {"stdout": line, "exit_status": 0}}))

    def test_archiving_off_switches_it_on_again(self):
        self._read(
            "IC_PITR_STATUS mode=off last_archived=0 failed=0 last_failed=0 "
            "cluster=88 pending=0 oldest_pending=0 agent=1",
        )
        self.assertEqual(self.inst.pitr_state, "off")
        self.assertTrue(self._alerts("pitr_failing"))

    def test_an_idle_database_is_not_behind(self):
        """Nothing waiting means nothing owed, however old the last one."""
        self._read(
            "IC_PITR_STATUS mode=on last_archived=1000 failed=0 last_failed=0 "
            "cluster=77 pending=0 oldest_pending=0 agent=1 fs_ok=5000 now=5100",
        )
        self.assertEqual(self.inst.pitr_state, "ok")

    def test_uploads_failing_is_critical(self):
        self._read(
            "IC_PITR_STATUS mode=on last_archived=1000 failed=4 last_failed=2000 "
            "cluster=77 pending=3 oldest_pending=900 agent=1",
        )
        self.assertEqual(self.inst.pitr_state, "failed")
        self.assertEqual(self._alerts("pitr_failing").level, "critical")

    def test_an_agent_that_is_not_running_is_critical(self):
        self._read(
            "IC_PITR_STATUS mode=on last_archived=1000 failed=0 last_failed=0 "
            "cluster=77 pending=1 oldest_pending=20 agent=0",
        )
        self.assertEqual(self.inst.pitr_state, "failed")
        self.assertIn("agent", self._alerts("pitr_failing").message)

    def test_files_not_copied_for_a_while_are_behind(self):
        self._read(
            "IC_PITR_STATUS mode=on last_archived=1000 failed=0 last_failed=0 "
            "cluster=77 pending=0 oldest_pending=0 agent=1 fs_ok=1000 now=10000",
            times=2,
        )
        self.assertEqual(self.inst.pitr_state, "lagging")
        self.assertIn("files", self._alerts("pitr_archive_lagging").message)

    def test_archiving_with_no_base_copy_yet_is_said(self):
        self.inst.pitr_last_base_at = False
        self._read(
            "IC_PITR_STATUS mode=on last_archived=1000 failed=0 last_failed=0 "
            "cluster=77 pending=0 oldest_pending=0 agent=1",
            times=2,
        )
        self.assertIn("no base copy yet", self._alerts("pitr_base_missing").message)

    def test_a_restore_whose_process_is_gone_is_critical(self):
        self._read("IC_PITR_STATUS restoring=lost job=9")
        message = self._alerts("pitr_failing").message
        self.assertIn("job 9", message)
        self.assertIn("lock", message)

    ENDED = (
        "IC_PITR_STATUS restoring=1 restore_job=9 restore_state=done "
        "restore_timeline=00000002 restore_promoted_at=1792369056 "
        "restore_target=2026-10-19T00:06:30+00"
    )

    def test_during_a_restore_the_reading_waits_and_records_one_that_ended(self):
        with patch.object(self.Job, "enqueue", autospec=True):
            self._read(self.ENDED)
        self.assertEqual(self.inst.pitr_state, "ok")
        (entry,) = self.inst.pitr_timelines
        self.assertEqual(entry["timeline"], "00000002")
        self.assertEqual(entry["target"], "2026-10-19 00:06:30")
        self.assertEqual(entry["cluster"], "77")

    def test_a_restore_that_outlived_its_jobs_is_followed_up_once(self):
        """Nobody followed it to its end: the watch, which records it, takes
        its full base copy and tells the addons (``pitr``) as its job would
        have, and the next reading repeats neither."""
        with patch.object(
            self.Job, "enqueue", autospec=True,
        ) as enqueue, patch.object(
            self.Instance, "_after_restore_hook", autospec=True,
        ) as hook:
            self._read(self.ENDED, times=2)
        self.assertEqual(
            [(c.args[3], c.kwargs.get("payload")) for c in enqueue.call_args_list],
            [("pitr_base_backup", {"full": True})],
        )
        hook.assert_called_once_with(self.inst, "pitr")


class TestARestore(_PitrCase):

    LIST = (
        'IC_PITR_BASES 77 [{"backup_name":"base_000000010000000000000005",'
        '"finish_time":"2026-10-18T22:00:00Z"},{"backup_name":'
        '"base_000000010000000000000009","finish_time":"2026-10-18T22:30:00Z"},'
        '{"backup_name":"base_00000001000000000000000C","finish_time":"2026-10-18T23:00:00Z"}]'
    )

    def setUp(self):
        super().setUp()
        self.inst.write({
            "pitr_enabled": True, "pitr_state": "ok", "pitr_cluster_id": "77",
        })

    def _planned(self, target="2026-10-18 22:31:00", answer=None):
        executor = self._executor(
            ex.PitrRestoreExecutor, "pitr_restore", payload={"target": target},
        )
        transport = MagicMock(spec=SSHTransport)
        transport.upload_text_files = AsyncMock()
        transport.run = AsyncMock(return_value=CommandResult(answer or self.LIST, 0))
        asyncio.run(executor.before_execute(transport))
        return executor

    def test_it_starts_on_the_host_from_the_newest_copy_and_is_followed(self):
        executor = self._planned()
        commands = dict(c[:2] for c in executor.get_commands())
        self.assertEqual(list(commands), ["Start the restore", "Wait for the restore"])
        start = commands["Start the restore"]
        self.assertIn(
            f" {executor.job.id} 77 base_000000010000000000000009 1"
            " '2026-10-18 22:31:00+00' 2026-10-18",
            start,
        )
        self.assertIn(
            f" {executor.job.id} {ex.RESTORE_WAIT_S}", commands["Wait for the restore"],
        )

    def test_no_copy_before_the_moment_touches_nothing(self):
        executor = self._executor(
            ex.PitrRestoreExecutor, "pitr_restore",
            payload={"target": "2026-10-18 21:00:00"},
        )
        transport = MagicMock(spec=SSHTransport)
        transport.upload_text_files = AsyncMock()
        transport.run = AsyncMock(return_value=CommandResult(self.LIST, 0))
        with self.assertRaises(ValueError):
            asyncio.run(executor.before_execute(transport))
        with self.assertRaises(ValueError):
            executor.get_commands()
        asyncio.run(executor.on_abort(ValueError("no copy")))
        self.assertFalse(self._alerts("pitr_failing"))

    def test_done_records_the_timeline_and_copies_it_at_once(self):
        executor = self._planned()
        with patch.object(self.Job, "enqueue", autospec=True) as enqueue:
            asyncio.run(executor.on_success({"Wait for the restore": {"stdout": (
                f"IC_PITR_RESTORE job={executor.job.id} state=done "
                "timeline=00000002 promoted_at=1792369056 target=2026-10-18T22:31:00+00"
            ), "exit_status": 0}}))
        self.assertEqual(enqueue.call_args.args[3], "pitr_base_backup")
        self.assertEqual(enqueue.call_args.kwargs["payload"], {"full": True})
        self.assertEqual(self.inst.pitr_timelines[0]["job"], str(executor.job.id))

    def test_done_confirms_the_instance_running(self):
        """And opens the grace the metrics cron gives a job that has just
        started odoo (QA d76c: this restore's instance read "Stopped"
        half a minute later)."""
        self.inst.write({"running": False})
        executor = self._planned()
        with patch.object(self.Job, "enqueue", autospec=True):
            asyncio.run(executor.on_success({"Wait for the restore": {"stdout": (
                f"IC_PITR_RESTORE job={executor.job.id} state=done "
                "timeline=00000002 promoted_at=1792369056 target=2026-10-18T22:31:00+00"
            ), "exit_status": 0}}))
        self.assertTrue(self.inst.running)
        self.assertTrue(self.inst.running_confirmed_at)

    def _done(self, executor):
        """What the host answers once *executor*'s restore is done."""
        return {"Wait for the restore": {"stdout": (
            f"IC_PITR_RESTORE job={executor.job.id} state=done "
            "timeline=00000002 promoted_at=1792369056 target=2026-10-18T22:31:00+00"
        ), "exit_status": 0}}

    def test_a_restore_that_ended_tells_the_addons_once(self):
        """Its database was replaced, as by any restore (``pitr``): SaaS
        hands a tenant's panel new secrets then, since a moment before
        its last deploy holds ones the manager no longer accepts."""
        executor = self._planned()
        with patch.object(self.Job, "enqueue", autospec=True), patch.object(
            self.Instance, "_after_restore_hook", autospec=True,
        ) as hook:
            asyncio.run(executor.on_success(self._done(executor)))
        hook.assert_called_once_with(self.inst, "pitr")

    def test_a_restore_still_running_tells_the_addons_nothing(self):
        """The next job follows it, and tells them once it ends."""
        executor = self._planned()
        with patch.object(self.Job, "enqueue", autospec=True), patch.object(
            self.Instance, "_after_restore_hook", autospec=True,
        ) as hook:
            asyncio.run(executor.on_success({"Wait for the restore": {
                "stdout": "IC_PITR_RESTORE state=running", "exit_status": 0,
            }}))
        hook.assert_not_called()

    def test_a_database_restored_without_its_files_tells_the_addons(self):
        """The job fails, but the database was replaced all the same."""
        executor = self._planned()
        with patch.object(self.Job, "enqueue", autospec=True), patch.object(
            self.Instance, "_after_restore_hook", autospec=True,
        ) as hook:
            asyncio.run(executor.on_failure({"Wait for the restore": {"stdout": (
                f"IC_PITR_RESTORE job={executor.job.id} state=db-only "
                "timeline=00000002 promoted_at=1792369056 filestore=failed"
            ), "exit_status": 2}}, ["exit 2"]))
        hook.assert_called_once_with(self.inst, "pitr")

    def test_a_restore_put_back_tells_the_addons_nothing(self):
        """Its database is as it was."""
        executor = self._planned()
        with patch.object(
            self.Instance, "_after_restore_hook", autospec=True,
        ) as hook:
            asyncio.run(executor.on_failure({
                "Start the restore": {"stdout": "", "exit_status": 0},
                "Wait for the restore": {"stdout": (
                    "IC_PITR_RESTORE job=1 state=failed stopped_at=recovering"
                ), "exit_status": 1},
            }, ["exit 1"]))
        hook.assert_not_called()

    def test_a_restore_that_ended_takes_one_base_copy(self):
        """Its own, full; the hook it calls takes none besides."""
        executor = self._planned()
        with patch.object(self.Job, "enqueue", autospec=True) as enqueue:
            asyncio.run(executor.on_success(self._done(executor)))
        self.assertEqual(
            [(c.args[3], c.kwargs.get("payload")) for c in enqueue.call_args_list],
            [("pitr_base_backup", {"full": True})],
        )

    def _watch_reads_it_ended(self, executor, timeline="00000002"):
        """The watch reads *executor*'s restore as ended, before or after it."""
        watch = self._executor(ex.PitrStatusExecutor, "pitr_status")
        asyncio.run(watch.on_success({"r": {"stdout": (
            f"IC_PITR_STATUS restoring=1 restore_job={executor.job.id} "
            f"restore_state=done restore_timeline={timeline} "
            "restore_promoted_at=1792369056 restore_target=2026-10-18T22:31:00+00"
        ), "exit_status": 0}}))

    def test_a_restore_its_job_followed_up_is_not_followed_up_by_the_watch(self):
        """The job records it first: the watch's reading repeats nothing."""
        executor = self._planned()
        with patch.object(
            self.Job, "enqueue", autospec=True,
        ) as enqueue, patch.object(
            self.Instance, "_after_restore_hook", autospec=True,
        ) as hook:
            asyncio.run(executor.on_success(self._done(executor)))
            self._watch_reads_it_ended(executor)
        self.assertEqual(
            [c.args[3] for c in enqueue.call_args_list], ["pitr_base_backup"],
        )
        hook.assert_called_once_with(self.inst, "pitr")

    def test_a_restore_the_watch_followed_up_is_not_followed_up_by_its_job(self):
        """The watch records it first; the job, ending later, repeats
        nothing and still says the instance runs."""
        self.inst.write({"running": False})
        executor = self._planned()
        with patch.object(
            self.Job, "enqueue", autospec=True,
        ) as enqueue, patch.object(
            self.Instance, "_after_restore_hook", autospec=True,
        ) as hook:
            self._watch_reads_it_ended(executor)
            asyncio.run(executor.on_success(self._done(executor)))
        self.assertEqual(
            [c.args[3] for c in enqueue.call_args_list], ["pitr_base_backup"],
        )
        hook.assert_called_once_with(self.inst, "pitr")
        self.assertEqual(len(self.inst.pitr_timelines), 1)
        self.assertTrue(self.inst.running)

    def test_a_restore_without_its_timeline_is_still_followed_up_once(self):
        """``pitr.sh`` reads the new timeline after the promotion, and that
        read may fail: the restore is done but cannot be recorded. Its job
        still copies it and tells the addons; the watch, which cannot
        record it either, repeats neither."""
        executor = self._planned()
        with patch.object(
            self.Job, "enqueue", autospec=True,
        ) as enqueue, patch.object(
            self.Instance, "_after_restore_hook", autospec=True,
        ) as hook:
            asyncio.run(executor.on_success({"Wait for the restore": {"stdout": (
                f"IC_PITR_RESTORE job={executor.job.id} state=done timeline= "
                "promoted_at=1792369056 target=2026-10-18T22:31:00+00"
            ), "exit_status": 0}}))
            self._watch_reads_it_ended(executor, timeline="")
        self.assertEqual(
            [(c.args[3], c.kwargs.get("payload")) for c in enqueue.call_args_list],
            [("pitr_base_backup", {"full": True})],
        )
        hook.assert_called_once_with(self.inst, "pitr")
        self.assertFalse(self.inst.pitr_timelines)

    def test_one_still_running_is_handed_to_the_next_job(self):
        executor = self._planned()
        with patch.object(self.Job, "enqueue", autospec=True) as enqueue:
            asyncio.run(executor.on_success({"Wait for the restore": {
                "stdout": "IC_PITR_RESTORE state=running", "exit_status": 0,
            }}))
        call = enqueue.call_args
        self.assertEqual(call.args[3], "pitr_restore")
        self.assertEqual(call.kwargs["payload"]["follow"], executor.job.id)
        follower = self._executor(
            ex.PitrRestoreExecutor, "pitr_restore", payload=call.kwargs["payload"],
        )
        (only,) = follower.get_commands()
        self.assertEqual(only[0], "Wait for the restore")
        self.assertIn(f" {executor.job.id} ", only[1])

    def test_files_that_failed_still_leave_the_database_restored(self):
        executor = self._planned()
        with patch.object(self.Job, "enqueue", autospec=True) as enqueue:
            asyncio.run(executor.on_failure({"Wait for the restore": {"stdout": (
                f"IC_PITR_RESTORE job={executor.job.id} state=db-only "
                "timeline=00000002 promoted_at=1792369056 filestore=failed"
            ), "exit_status": 2}}, ["exit 2"]))
        self.assertEqual(enqueue.call_args.args[3], "pitr_base_backup")
        self.assertIn("not its files", self._alerts("pitr_failing").message)

    def test_one_put_back_says_so(self):
        executor = self._planned()
        asyncio.run(executor.on_failure({
            "Start the restore": {"stdout": "", "exit_status": 0},
            "Wait for the restore": {"stdout": (
                "IC_PITR_RESTORE job=1 state=failed stopped_at=recovering"
            ), "exit_status": 1},
        }, ["exit 1"]))
        message = self._alerts("pitr_failing").message
        self.assertIn("'recovering'", message)
        self.assertIn("as it was", message)

    def test_one_put_back_is_confirmed_running(self):
        """The script brings the stack up whatever happened, so the
        instance runs again and the metrics cron gives its odoo the same
        grace as after a restore that went through."""
        self.inst.write({"running": False})
        executor = self._planned()
        asyncio.run(executor.on_failure({
            "Start the restore": {"stdout": "", "exit_status": 0},
            "Wait for the restore": {"stdout": (
                "IC_PITR_RESTORE job=1 state=failed stopped_at=recovering"
            ), "exit_status": 1},
        }, ["exit 1"]))
        self.assertTrue(self.inst.running)
        self.assertTrue(self.inst.running_confirmed_at)

    def test_one_that_could_not_start_changed_nothing(self):
        executor = self._planned()
        asyncio.run(executor.on_failure({
            "Start the restore": {"stdout": "another restore", "exit_status": 1},
        }, ["exit 1"]))
        self.assertIn("nothing was changed", self._alerts("pitr_failing").message)

    def test_a_job_that_stopped_while_following_hands_it_on(self):
        executor = self._planned()
        with patch.object(self.Job, "enqueue", autospec=True) as enqueue:
            asyncio.run(executor.on_abort(TimeoutError("ssh")))
        self.assertEqual(enqueue.call_args.kwargs["payload"]["follow"], executor.job.id)

    def test_a_restore_whose_process_runs_is_followed_however_long(self):
        """No job holding the instance would let a rebuild start a second
        database on the same data; a long one is only said to be long."""
        executor = self._executor(
            ex.PitrRestoreExecutor, "pitr_restore",
            payload={"target": "2026-10-18 22:31:00", "follow": 5,
                     "round": ex.RESTORE_FOLLOW_MAX - 1},
        )
        with patch.object(self.Job, "enqueue", autospec=True) as enqueue:
            asyncio.run(executor.on_success({"w": {
                "stdout": "IC_PITR_RESTORE state=running", "exit_status": 0,
            }}))
        self.assertEqual(enqueue.call_args.kwargs["payload"]["follow"], 5)
        self.assertIn("running for hours", self._alerts("pitr_failing").message)

    def test_one_the_host_cannot_be_asked_about_is_given_up_in_the_end(self):
        executor = self._executor(
            ex.PitrRestoreExecutor, "pitr_restore",
            payload={"target": "2026-10-18 22:31:00", "follow": 5,
                     "missed": ex.RESTORE_UNREACHABLE_MAX},
        )
        with patch.object(self.Job, "enqueue", autospec=True) as enqueue:
            asyncio.run(executor.on_abort(TimeoutError("ssh")))
        enqueue.assert_not_called()
        self.assertIn("may still be running", self._alerts("pitr_failing").message)

    def test_a_job_run_again_follows_the_restore_it_started(self):
        """queue_job runs a job that died once more: its restore is on the
        host, the database maybe stopped, and listing would fail."""
        executor = self._executor(
            ex.PitrRestoreExecutor, "pitr_restore",
            payload={"target": "2026-10-18 22:31:00"},
        )
        transport = MagicMock(spec=SSHTransport)
        transport.upload_text_files = AsyncMock()
        transport.run = AsyncMock(return_value=CommandResult(
            "IC_PITR_RESTORE state=running", 0,
        ))
        asyncio.run(executor.before_execute(transport))
        commands = [c.args[0] for c in transport.run.await_args_list]
        self.assertTrue(any("restore-wait" in c for c in commands))
        self.assertFalse(any(" list " in c for c in commands))
        (only,) = executor.get_commands()
        self.assertEqual(only[0], "Wait for the restore")
        self.assertIn(f" {executor.job.id} ", only[1])


class TestThePanelAnchor(BaseCase):

    def test_the_backups_toolbar_the_panel_follows_is_there(self):
        xml = (
            Path(__file__).resolve().parents[2] / "incubacloud"
            / "static/src/components/instance_detail/instance_detail.xml"
        )
        body = etree.parse(str(xml)).xpath(
            "//t[@t-name='incubacloud.InstanceDetail.tabBody']",
        )
        self.assertTrue(body)
        self.assertTrue(body[0].xpath(".//div[contains(@class, 'ic-backups-toolbar')]"))
