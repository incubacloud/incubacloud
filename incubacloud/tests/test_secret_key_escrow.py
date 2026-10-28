"""The panel keeps each instance's encryption key; hosts only receive it.

On 18-oct-2026 two panels moved to another host came up with a key the
move had generated there, and could not read anything their database had
encrypted with the old one: no step of a move or a refresh ever read
``.docker/incubacloud.env``. These pin what replaced that: a key born
with the instance, merged with what the host holds on every deploy and
rebuild (never dropping one), read from the origin during a move, taken
from production by a copy left live, and captured once for instances
deployed before.
"""
import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet

from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.models.capture_secret_key_executor import (
    ALERT_CODE_NOT_ESCROWED,
    CaptureSecretKeyExecutor,
)
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
from odoo.addons.incubacloud.models.password_utils import (
    merge_secret_key_lines,
    parse_secret_key_line,
)
from odoo.addons.incubacloud.models.restore_instance_executor import (
    RestoreInstanceExecutor,
)
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport

from .test_executor_durable_writes import JobEndAssertions

K1, K2, K3 = (Fernet.generate_key().decode() for _ in range(3))


def _env_file(chain):
    """Return an ``incubacloud.env`` holding *chain*."""
    return f"INCUBACLOUD_SECRET_KEY={chain}\n"


def _transport(stdout="", exit_status=0):
    """Return a transport whose ``run`` answers *stdout*."""
    transport = MagicMock(spec=SSHTransport)
    transport.run = AsyncMock(return_value=CommandResult(stdout, exit_status))
    transport.upload_text_files = AsyncMock()
    return transport


class TestKeyChains(BaseCase):
    """Tier 1 — reading and merging chains, without a database."""

    def test_an_env_file_is_read_and_anything_else_ignored(self):
        text = f"# comment\nOTHER=1\nINCUBACLOUD_SECRET_KEY=\"{K1},{K2}\"\n"
        self.assertEqual(parse_secret_key_line(text), [K1, K2])

    def test_a_bare_chain_is_read(self):
        self.assertEqual(parse_secret_key_line(f" {K1} , {K2} "), [K1, K2])

    def test_what_is_not_a_fernet_key_is_dropped(self):
        self.assertEqual(parse_secret_key_line(f"nope,{K1},{K1}"), [K1])
        self.assertEqual(parse_secret_key_line(""), [])
        self.assertEqual(parse_secret_key_line(None), [])

    def test_a_merge_keeps_every_key_and_the_first_primary(self):
        merged = merge_secret_key_lines(_env_file(K2), f"{K1},{K2}", K3)
        self.assertEqual(merged, f"{K2},{K1},{K3}")

    def test_nothing_valid_merges_to_nothing(self):
        self.assertEqual(merge_secret_key_lines("", "garbage", None), "")


class _EscrowCase(JobEndAssertions, TransactionCase):
    """An instance, its host, and a deploy executor on a real job."""

    def setUp(self):
        super().setUp()
        self.registry_enter_test_mode()
        self.host = self.env["cloud.host"].create({
            "name": "esc-host", "ip_address": "192.0.2.50", "user": "ubuntu",
            "wildcard_domain": "esc.example.com",
        })
        self.other = self.env["cloud.host"].create({
            "name": "esc-other", "ip_address": "192.0.2.51", "user": "ubuntu",
            "wildcard_domain": "esc2.example.com",
        })
        self.project = self.env["cloud.project"].create({"name": "esc-proj"})
        self.inst = self.env["cloud.instance"].create({
            "name": "esc-inst", "project_id": self.project.id,
            "environment": "production", "host_id": self.host.id,
        })
        self.env.flush_all()

    def _job(self, code, instance, host):
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", code)], limit=1,
        )
        return self.env["cloud.job"].create({
            "name": code, "job_type_id": job_type.id,
            "host_id": host.id, "instance_id": instance.id,
        })

    def _executor(self, cls, code, instance=None, host=None, payload=None):
        job = self._job(code, instance or self.inst, host or self.host)
        if payload:
            job.payload = payload
        executor = cls(job, job.host_id)
        sys_log = patch.object(executor, "_sys", autospec=True)
        sys_log.start()
        self.addCleanup(sys_log.stop)
        return executor

    def _deploy(self, host=None):
        return self._executor(
            DeployInstanceExecutor, "deploy_instance", host=host,
        )

    def _set_state(self, state):
        self.inst.with_context(cloud_state_transition=True).write(
            {"state": state},
        )
        self.env.flush_all()

    def _staged(self, transport):
        """Return the key file the executor staged on *transport*."""
        files, = transport.upload_text_files.await_args.args
        self.assertEqual(transport.upload_text_files.await_args.kwargs, {"mode": 0o600})
        (path, content), = files.items()
        self.assertTrue(path.startswith("/tmp/.incubacloud-"), path)
        return content


class TestANewInstanceIsBornWithItsKey(_EscrowCase):

    def test_create_generates_a_key_the_panel_keeps(self):
        self.assertEqual(len(parse_secret_key_line(self.inst.secret_key)), 1)

    def test_an_instance_born_on_its_host_waits_for_the_host_s_key(self):
        """An import already has a key on its host: inventing another
        would encrypt nothing and hide the real one."""
        imported = self.env["cloud.instance"].create({
            "name": "esc-imported", "project_id": self.project.id,
            "host_id": self.host.id, "state": "deployed",
        })
        self.assertFalse(imported.secret_key)

    def test_a_copy_gets_a_key_of_its_own(self):
        twin = self.inst.copy({"name": "esc-twin", "environment": "staging"})
        self.assertTrue(twin.secret_key)
        self.assertNotEqual(twin.secret_key, self.inst.secret_key)

    def test_the_key_stays_out_of_the_config_snapshot_and_answers(self):
        """``applied_config_snapshot`` is stored in clear, and the copier
        answers travel to the host as a plain file."""
        key = self.inst.secret_key
        self.assertNotIn(key, str(self.inst._render_config_snapshot()))
        self.assertNotIn(key, str(self.inst._render_copier_answers()))


class TestADeployShipsThePanelsKey(_EscrowCase):

    def test_a_first_deploy_ships_the_panel_s_key_and_reads_no_host(self):
        transport = _transport()
        executor = self._deploy()
        asyncio.run(executor._ship_secret_key(transport))
        # Nothing read: the host only gets the job's directory.
        self.assertEqual(
            [c.args[0] for c in transport.run.await_args_list],
            [f"mkdir -m 700 {executor._job_dir()}"],
        )
        self.assertEqual(
            self._staged(transport), _env_file(self.inst.secret_key),
        )

    def test_a_deployed_instance_keeps_what_its_host_holds_first(self):
        """The running Odoo encrypts with the host's first key: it stays
        the primary one, and the panel's copy is not lost either."""
        self._set_state("deployed")
        kept = self.inst.secret_key
        transport = _transport(_env_file(K1))
        asyncio.run(self._deploy()._ship_secret_key(transport))
        commands = [c.args[0] for c in transport.run.await_args_list]
        self.assertTrue([
            c for c in commands
            if f"{self.inst.get_remote_dir()}/.docker/incubacloud.env" in c
        ], commands)
        self.assertEqual(self.stored(self.inst, "secret_key"), f"{K1},{kept}")
        self.assertEqual(self._staged(transport), _env_file(f"{K1},{kept}"))

    def test_a_move_reads_the_key_on_the_origin(self):
        """The job runs on the target, where there is nothing yet; the
        only copy of the key is on the host the instance still lives on."""
        self._set_state("deployed")
        self.inst.secret_key = False
        self.env.flush_all()
        origin = _transport(_env_file(K2))
        target = _transport()

        @asynccontextmanager
        async def _open(host):
            self.assertEqual(host, self.host)
            yield origin

        executor = self._deploy(host=self.other)
        with patch.object(type(self.host), "get_transport", _open):
            asyncio.run(executor._ship_secret_key(target))
        # Nothing read on the target: it only gets the job's directory.
        self.assertEqual(
            [c.args[0] for c in target.run.await_args_list],
            [f"mkdir -m 700 {executor._job_dir()}"],
        )
        self.assertEqual(self.stored(self.inst, "secret_key"), K2)
        self.assertEqual(self._staged(target), _env_file(K2))

    def test_a_host_that_cannot_be_read_ships_nothing(self):
        self._set_state("deployed")
        transport = _transport(exit_status=1)
        with self.assertRaises(RuntimeError):
            asyncio.run(self._deploy()._ship_secret_key(transport))
        transport.upload_text_files.assert_not_awaited()

    def test_a_host_file_with_no_readable_key_is_not_overwritten(self):
        self._set_state("deployed")
        kept = self.inst.secret_key
        transport = _transport("INCUBACLOUD_SECRET_KEY=not-a-fernet-key\n")
        with self.assertRaises(RuntimeError):
            asyncio.run(self._deploy()._ship_secret_key(transport))
        transport.upload_text_files.assert_not_awaited()
        self.assertEqual(self.stored(self.inst, "secret_key"), kept)

    def test_an_instance_with_no_key_anywhere_gets_one(self):
        self._set_state("deployed")
        self.inst.secret_key = False
        self.env.flush_all()
        transport = _transport("")
        asyncio.run(self._deploy()._ship_secret_key(transport))
        self.assertEqual(
            len(parse_secret_key_line(self.stored(self.inst, "secret_key"))),
            1,
        )

    def test_the_staged_file_is_removed_when_the_job_ends(self):
        transport = _transport()
        executor = self._deploy()
        asyncio.run(executor._ship_secret_key(transport))
        asyncio.run(executor._discard_staging(transport))
        self.assertEqual(
            transport.run.await_args.args[0],
            f"rm -rf {executor._job_dir()}/incubacloud.env"
            f" {executor._job_dir()}",
        )


class TestACopyLeftLiveTakesItsProductionsKey(_EscrowCase):
    """A neutralized copy has production's secrets cleared: its own key
    reads everything it keeps. One left live reads them, so it takes
    production's key with the data."""

    def setUp(self):
        super().setUp()
        self._set_state("deployed")

    def _clone(self):
        with patch.object(
            type(self.env["cloud.job"]), "enqueue_chain",
            autospec=True, return_value=[1, 2, 3, 4],
        ) as chain:
            result = self.inst.clone_to_staging("esc-staging")
        steps = chain.call_args.args[1]
        return self.env["cloud.instance"].browse(result["staging_id"]), steps

    def test_a_clone_is_neutralized_and_keeps_a_key_of_its_own(self):
        staging, steps = self._clone()
        self.assertTrue(staging.secret_key)
        self.assertNotEqual(staging.secret_key, self.inst.secret_key)
        self.assertEqual(
            [s["job_type_code"] for s in steps],
            ["deploy_instance", "backup_download", "restore_instance"],
        )
        restore = steps[2]["payload"]
        self.assertTrue(restore["neutralize"])
        self.assertFalse(restore.get("share_secret_key"))

    def _refresh(self, neutralize):
        """Refresh a staging from production; return the restore's payload."""
        staging = self.env["cloud.instance"].create({
            "name": f"esc-stg-{int(neutralize)}", "project_id": self.project.id,
            "environment": "staging", "host_id": self.other.id,
            "state": "deployed",
        })
        with patch.object(
            type(self.env["cloud.job"]), "enqueue_chain",
            autospec=True, return_value=[1, 2],
        ) as chain:
            staging.refresh_from_production(
                self.inst.id, source="live", neutralize=neutralize,
            )
        return chain.call_args.args[1][1]["payload"]

    def test_a_refresh_left_live_takes_production_s_keys(self):
        self.assertTrue(self._refresh(neutralize=False)["share_secret_key"])

    def test_a_neutralized_refresh_keeps_the_staging_s_key(self):
        self.assertFalse(self._refresh(neutralize=True)["share_secret_key"])

    def test_the_refresh_restore_writes_the_keys_and_recreates_odoo(self):
        staging = self.env["cloud.instance"].create({
            "name": "esc-stg2", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
            "state": "deployed",
        })
        download = self._job("backup_download", self.inst, self.host)
        executor = self._executor(
            RestoreInstanceExecutor, "restore_instance", instance=staging,
            payload={"mode": "from_host", "source_job_id": download.id,
                     "share_secret_key": True},
        )
        transport = _transport(_env_file(K3))
        asyncio.run(executor._take_source_secret_key(transport))
        prod_key = self.inst.secret_key
        self.assertEqual(
            self.stored(staging, "secret_key"),
            merge_secret_key_lines(prod_key, K3, staging.secret_key),
        )
        commands = dict(c[:2] for c in executor.get_commands())
        self.assertIn("Write incubacloud.env", commands)
        self.assertIn(" up odoo", commands["Start Odoo service"])

    def test_a_restore_that_takes_no_keys_starts_odoo_as_before(self):
        download = self._job("backup_download", self.inst, self.host)
        executor = self._executor(
            RestoreInstanceExecutor, "restore_instance",
            payload={"mode": "from_host", "source_job_id": download.id},
        )
        commands = dict(c[:2] for c in executor.get_commands())
        self.assertNotIn("Write incubacloud.env", commands)
        self.assertIn(" start odoo", commands["Start Odoo service"])


class TestTheKeysOfOlderInstancesAreCaptured(_EscrowCase):

    def setUp(self):
        super().setUp()
        self._set_state("deployed")
        self.inst.secret_key = False
        self.env.flush_all()

    def _capture(self, payload=None):
        return self._executor(
            CaptureSecretKeyExecutor, "capture_secret_key", payload=payload,
        )

    def test_the_cron_queues_only_instances_the_panel_lacks_a_key_for(self):
        kept = self.env["cloud.instance"].create({
            "name": "esc-kept", "project_id": self.project.id,
            "host_id": self.host.id, "state": "deployed",
            "secret_key": K1,
        })
        Job = type(self.env["cloud.job"])
        with patch.object(Job, "enqueue", autospec=True) as enqueue:
            self.env["cloud.instance"].cron_capture_secret_keys(limit=100000)
        queued = {c.args[2] for c in enqueue.call_args_list}
        self.assertIn(self.inst.id, queued)
        self.assertNotIn(kept.id, queued)
        self.assertEqual(
            {c.args[3] for c in enqueue.call_args_list}, {"capture_secret_key"},
        )

    def test_a_capture_keeps_the_host_s_key(self):
        executor = self._capture()
        asyncio.run(executor.before_execute(_transport(_env_file(K2))))
        self.assertEqual(self.stored(self.inst, "secret_key"), K2)

    def test_a_failed_capture_warns_on_the_second_day(self):
        Alert = self.env["cloud.alert"]
        for _day in range(2):
            asyncio.run(self._capture().on_failure({}, ["host down"]))
        alert = Alert.search([
            ("code", "=", ALERT_CODE_NOT_ESCROWED),
            ("instance_id", "=", self.inst.id), ("state", "=", "active"),
        ])
        self.assertEqual(len(alert), 1)
        asyncio.run(self._capture().on_success({}))
        alert.invalidate_recordset(["state"])
        self.assertEqual(alert.state, "dismissed")
