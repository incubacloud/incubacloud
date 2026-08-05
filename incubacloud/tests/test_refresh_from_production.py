"""Tests for ``cloud.instance.refresh_from_production`` and the two
executor changes it rides on.

The feature adds no executor and no job type: it is the tail of the
``clone_to_staging`` chain (``backup_download`` on production →
``restore_instance`` on the staging) without the deploy step. So what is
worth pinning here is the wiring:

  * the chain shape — codes, hosts per step, exact payloads;
  * the validations that keep data flowing production → staging only;
  * the degradation from ``'backup'`` to ``'live'`` when the source has
    no backup backend;
  * that ``backup_download`` now takes the live path on production when
    asked, without disturbing the duplicity path;
  * that ``restore_instance`` passes ``--neutralize`` and resets the base
    URL only when told to — and that the host move, which shares the same
    executor, still does neither.

The SSH executors themselves are mocked away; the shell they invoke is
covered by ``tests/shell/restore.bats``.
"""

import shlex
from types import SimpleNamespace
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import BaseCase, TransactionCase

from .test_cloud_security import CloudSecurityBase

INSTANCE_DIR = "~/projects/demo-inst"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _argv(step):
    """Return a script step's invocation as an argv list."""
    return shlex.split(step[1])


def _find(cmds, label):
    return next((c for c in cmds if c[0] == label), None)


def _make_restore_executor(payload, domain="staging.example.com",
                           dbname="prod", pg_user="odoo"):
    from odoo.addons.incubacloud.models.restore_instance_executor import (
        RestoreInstanceExecutor,
    )

    inst = SimpleNamespace(
        id=7,
        name="demo-inst",
        domain=domain,
        postgres_dbname=dbname,
        postgres_username=pg_user,
    )
    job = SimpleNamespace(id=42, instance_id=inst, payload=payload)

    ex = object.__new__(RestoreInstanceExecutor)
    ex.job = job
    ex._inst_dir = lambda i: INSTANCE_DIR
    ex._scripts_requested = False
    ex._script_overlay_cache = None
    return ex


def _make_download_executor(environment="production", time="latest",
                            mode="all"):
    from odoo.addons.incubacloud.models.backup_download_executor import (
        BackupDownloadExecutor,
    )

    inst = SimpleNamespace(
        id=7,
        name="demo-inst",
        environment=environment,
        postgres_dbname="prod",
    )
    job = SimpleNamespace(
        id=42,
        instance_id=inst,
        payload={"time": time, "download_type": mode},
    )

    ex = object.__new__(BackupDownloadExecutor)
    ex.job = job
    ex._inst_dir = lambda i: INSTANCE_DIR
    ex._scripts_requested = False
    ex._script_overlay_cache = None
    return ex


# ── Orchestration ────────────────────────────────────────────────────────────


class TestRefreshFromProduction(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "refresh-proj"})
        self.other_project = self.env["cloud.project"].create({"name": "other"})
        self.host_a = self.env["cloud.host"].create({
            "name": "host-a", "ip_address": "10.0.0.1", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "a.example.com",
            "status": "compatible", "traefik_deployed": True,
        })
        self.host_b = self.env["cloud.host"].create({
            "name": "host-b", "ip_address": "10.0.0.2", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "b.example.com",
            "status": "compatible", "traefik_deployed": True,
        })
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "refresh-bb",
            "backend_type": "s3",
            "s3_bucket": "refresh-bucket",
        })
        self.prod = self.env["cloud.instance"].create({
            "name": "prod-inst", "project_id": self.project.id,
            "environment": "production", "host_id": self.host_a.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
        })
        self.staging = self.env["cloud.instance"].create({
            "name": "staging-inst", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host_a.id,
            "state": "deployed",
        })

    def _patch_chain(self):
        return patch.object(
            type(self.env["cloud.job"]), "enqueue_chain",
            return_value=[11, 12],
        )

    # ── Chain shape ────────────────────────────────────────────────────

    def test_chain_is_download_then_restore(self):
        with self._patch_chain() as m:
            res = self.staging.refresh_from_production(self.prod.id)
        self.assertTrue(res["ok"])
        self.assertEqual(res["job_ids"], [11, 12])
        steps = m.call_args[0][0]
        self.assertEqual(
            [s["job_type_code"] for s in steps],
            ["backup_download", "restore_instance"],
        )

    def test_download_runs_on_the_production_instance(self):
        with self._patch_chain() as m:
            self.staging.refresh_from_production(self.prod.id)
        download = m.call_args[0][0][0]
        self.assertEqual(download["instance_id"], self.prod.id)
        self.assertEqual(download["host_id"], self.host_a.id)
        self.assertEqual(
            download["payload"],
            {"time": "latest", "download_type": "all", "handoff": "host"},
        )

    def test_restore_runs_on_the_staging_instance(self):
        with self._patch_chain() as m:
            self.staging.refresh_from_production(self.prod.id)
        restore = m.call_args[0][0][1]
        self.assertEqual(restore["instance_id"], self.staging.id)
        self.assertEqual(restore["host_id"], self.host_a.id)
        self.assertEqual(
            restore["payload"],
            {
                "mode": "from_host",
                "source_job_id": "__chain_job_0__",
                "neutralize": True,
                "reset_base_url": True,
            },
        )

    def test_hosts_are_per_step_when_they_differ(self):
        self.staging.host_id = self.host_b
        with self._patch_chain() as m:
            self.staging.refresh_from_production(self.prod.id)
        steps = m.call_args[0][0]
        self.assertEqual(steps[0]["host_id"], self.host_a.id)
        self.assertEqual(steps[1]["host_id"], self.host_b.id)

    def test_restore_references_the_download_job(self):
        with self._patch_chain() as m:
            self.staging.refresh_from_production(self.prod.id)
        restore = m.call_args[0][0][1]
        self.assertEqual(restore["payload"]["source_job_id"], "__chain_job_0__")

    # ── Data source ────────────────────────────────────────────────────

    def test_live_source_asks_for_a_live_dump(self):
        with self._patch_chain() as m:
            res = self.staging.refresh_from_production(
                self.prod.id, source="live",
            )
        self.assertEqual(res["source"], "live")
        self.assertEqual(m.call_args[0][0][0]["payload"]["time"], "live")

    def test_backup_source_without_a_backend_degrades_to_live(self):
        self.prod.backup_backend_id = False
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id", "0",
        )
        self.prod.invalidate_recordset(["effective_backup_backend"])
        self.assertFalse(self.prod.effective_backup_backend)
        with self._patch_chain() as m:
            res = self.staging.refresh_from_production(
                self.prod.id, source="backup",
            )
        self.assertEqual(res["source"], "live")
        self.assertEqual(m.call_args[0][0][0]["payload"]["time"], "live")

    def test_backup_source_with_a_backend_stays_on_backup(self):
        with self._patch_chain() as m:
            res = self.staging.refresh_from_production(
                self.prod.id, source="backup",
            )
        self.assertEqual(res["source"], "backup")
        self.assertEqual(m.call_args[0][0][0]["payload"]["time"], "latest")

    def test_unknown_source_is_refused(self):
        with self.assertRaises(UserError):
            self.staging.refresh_from_production(self.prod.id, source="rsync")

    # ── Neutralize ─────────────────────────────────────────────────────

    def test_neutralize_defaults_to_true(self):
        with self._patch_chain() as m:
            self.staging.refresh_from_production(self.prod.id)
        self.assertTrue(m.call_args[0][0][1]["payload"]["neutralize"])

    def test_neutralize_can_be_switched_off(self):
        with self._patch_chain() as m:
            self.staging.refresh_from_production(
                self.prod.id, neutralize=False,
            )
        self.assertFalse(m.call_args[0][0][1]["payload"]["neutralize"])

    # ── Validations ────────────────────────────────────────────────────

    def test_a_production_cannot_be_refreshed(self):
        # A project holds at most one production (exclusion constraint),
        # so the second one lives in the other project. The environment
        # check fires before the same-project one either way.
        other_prod = self.env["cloud.instance"].create({
            "name": "prod-two", "project_id": self.other_project.id,
            "environment": "production", "host_id": self.host_a.id,
            "state": "deployed",
        })
        with self.assertRaises(UserError):
            other_prod.refresh_from_production(self.prod.id)

    def test_target_must_be_deployed(self):
        draft = self.env["cloud.instance"].create({
            "name": "draft-inst", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host_a.id,
        })
        with self.assertRaises(UserError):
            draft.refresh_from_production(self.prod.id)

    def test_target_must_have_a_host(self):
        self.staging.host_id = False
        with self.assertRaises(UserError):
            self.staging.refresh_from_production(self.prod.id)

    def test_missing_source_is_refused(self):
        with self.assertRaises(UserError):
            self.staging.refresh_from_production(999999)

    def test_source_must_be_production(self):
        sibling = self.env["cloud.instance"].create({
            "name": "sibling-staging", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host_a.id,
            "state": "deployed",
        })
        with self.assertRaises(UserError):
            self.staging.refresh_from_production(sibling.id)

    def test_source_must_be_deployed(self):
        # ``state`` is only writable through _transition(), and the only
        # way off 'deployed' is via 'deleting'.
        self.prod._transition("deleting")
        self.prod._transition("draft")
        self.assertFalse(self.prod.deployed)
        with self.assertRaises(UserError):
            self.staging.refresh_from_production(self.prod.id)

    def test_source_must_have_a_host(self):
        self.prod.host_id = False
        with self.assertRaises(UserError):
            self.staging.refresh_from_production(self.prod.id)

    def test_cross_project_refresh_is_refused(self):
        foreign = self.env["cloud.instance"].create({
            "name": "foreign-prod", "project_id": self.other_project.id,
            "environment": "production", "host_id": self.host_a.id,
            "state": "deployed",
        })
        with self.assertRaises(UserError):
            self.staging.refresh_from_production(foreign.id)

    def test_a_running_job_blocks_the_refresh(self):
        # Inherited from enqueue_chain's per-instance advisory lock: a
        # smoke test, since cloud.job owns the behaviour.
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", "deploy_instance")], limit=1,
        )
        job = self.env["cloud.job"].create({
            "host_id": self.host_a.id,
            "instance_id": self.staging.id,
            "job_type_id": job_type.id,
            "name": "Blocking job",
        })
        # ``state`` is a stored related on queue.job, so writing it
        # through the ORM on a job with no queue.job is a no-op.
        self.env.cr.execute(
            "UPDATE cloud_job SET state = %s WHERE id = %s",
            ("started", job.id),
        )
        self.env["cloud.job"].invalidate_model(["state"])
        with self.assertRaises(UserError):
            self.staging.refresh_from_production(self.prod.id)


# ── clone_to_staging now neutralizes ─────────────────────────────────────────


class TestCloneNeutralizes(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "clone-proj"})
        self.host = self.env["cloud.host"].create({
            "name": "clone-host", "ip_address": "10.0.0.3", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "c.example.com",
            "status": "compatible", "traefik_deployed": True,
        })
        self.prod = self.env["cloud.instance"].create({
            "name": "clone-prod", "project_id": self.project.id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed",
        })

    def test_clone_restore_step_neutralizes_and_resets_the_base_url(self):
        with patch.object(
            type(self.env["cloud.job"]), "enqueue_chain",
            return_value=[1, 2, 3],
        ) as m:
            self.prod.clone_to_staging("clone-staging")
        restore = m.call_args[0][0][2]
        self.assertEqual(restore["job_type_code"], "restore_instance")
        self.assertTrue(restore["payload"]["neutralize"])
        self.assertTrue(restore["payload"]["reset_base_url"])

    def test_clone_still_chains_deploy_download_restore(self):
        with patch.object(
            type(self.env["cloud.job"]), "enqueue_chain",
            return_value=[1, 2, 3],
        ) as m:
            self.prod.clone_to_staging("clone-staging-2")
        self.assertEqual(
            [s["job_type_code"] for s in m.call_args[0][0]],
            ["deploy_instance", "backup_download", "restore_instance"],
        )

    def test_clone_without_backend_takes_a_live_dump(self):
        # Previously the download step hardcoded 'latest', so cloning a
        # production with no backup destination failed mid-chain on the
        # duplicity path, leaving the fresh staging deployed but empty.
        self.env["ir.config_parameter"].sudo().set_param(
            "incubacloud.backup_backend_id", "0",
        )
        self.prod.invalidate_recordset(["effective_backup_backend"])
        self.assertFalse(self.prod.effective_backup_backend)
        with patch.object(
            type(self.env["cloud.job"]), "enqueue_chain",
            return_value=[1, 2, 3],
        ) as m:
            self.prod.clone_to_staging("clone-live")
        download = m.call_args[0][0][1]
        self.assertEqual(download["job_type_code"], "backup_download")
        self.assertEqual(download["payload"]["time"], "live")

    def test_clone_with_backend_stays_on_latest(self):
        backend = self.env["cloud.backup.backend"].create({
            "name": "clone-bb", "backend_type": "s3", "s3_bucket": "clone-bkt",
        })
        self.prod.backup_backend_id = backend
        with patch.object(
            type(self.env["cloud.job"]), "enqueue_chain",
            return_value=[1, 2, 3],
        ) as m:
            self.prod.clone_to_staging("clone-latest")
        download = m.call_args[0][0][1]
        self.assertEqual(download["payload"]["time"], "latest")


class TestRefreshGate(CloudSecurityBase):
    """Both methods are public and therefore reachable over RPC
    (call_kw), where the controller's Developer gate never runs, so the
    gate must hold on the model itself. su callers (tests, the GitHub
    webhook's sudo'd PR previews) bypass inside ``_check_cloud_group``.

    Extends ``CloudSecurityBase`` for its ``new_test_user`` workaround
    (NOT NULL partner columns added by optional modules).
    """

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "gate-proj"})
        self.host = self.env["cloud.host"].create({
            "name": "gate-host", "ip_address": "10.0.2.1", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "g.example.com",
            "status": "compatible", "traefik_deployed": True,
        })
        self.prod = self.env["cloud.instance"].create({
            "name": "gate-prod", "project_id": self.project.id,
            "environment": "production", "host_id": self.host.id,
            "state": "deployed",
        })
        self.staging = self.env["cloud.instance"].create({
            "name": "gate-staging", "project_id": self.project.id,
            "environment": "staging", "host_id": self.host.id,
            "state": "deployed",
        })
        self.consultant = self._create_user(
            "gate-consultant", "group_cloud_consultant",
        )
        self.developer = self._create_user(
            "gate-developer", "group_cloud_developer",
        )

    def test_a_consultant_cannot_refresh_over_rpc(self):
        with self.assertRaises(AccessError):
            self.staging.with_user(
                self.consultant,
            ).refresh_from_production(self.prod.id)

    def test_a_consultant_cannot_clone_over_rpc(self):
        with self.assertRaises(AccessError):
            self.prod.with_user(self.consultant).clone_to_staging("gate-x")

    def test_a_developer_passes_the_gate(self):
        with patch.object(
            type(self.env["cloud.job"]), "enqueue_chain",
            return_value=[11, 12],
        ):
            res = self.staging.with_user(
                self.developer,
            ).refresh_from_production(self.prod.id)
        self.assertTrue(res["ok"])


class TestMoveDoesNotNeutralize(TransactionCase):
    """A move is the same production instance changing machines.

    Neutralizing there would silently kill the customer's crons and
    outgoing mail, so the move chain must never carry the flag.
    """

    def setUp(self):
        super().setUp()
        self.project = self.env["cloud.project"].create({"name": "move-proj"})
        self.source = self.env["cloud.host"].create({
            "name": "move-src", "ip_address": "10.0.1.1", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "src.example.com",
            "status": "compatible", "traefik_deployed": True,
        })
        self.target = self.env["cloud.host"].create({
            "name": "move-tgt", "ip_address": "10.0.1.2", "port": 22,
            "user": "root", "login_type": "ssh_key",
            "wildcard_domain": "tgt.example.com",
            "status": "compatible", "traefik_deployed": True,
        })
        self.backend = self.env["cloud.backup.backend"].create({
            "name": "move-bb", "backend_type": "s3", "s3_bucket": "move-bkt",
        })
        self.inst = self.env["cloud.instance"].create({
            "name": "moving", "project_id": self.project.id,
            "environment": "production", "host_id": self.source.id,
            "state": "deployed", "backup_backend_id": self.backend.id,
        })

    def test_move_restore_step_carries_no_neutralize(self):
        with patch.object(
            type(self.env["cloud.job"]), "enqueue_chain",
            return_value=[1, 2, 3, 4, 5, 6, 7],
        ) as m:
            self.inst.move_to_host(self.target)
        restore = next(
            s for s in m.call_args[0][0]
            if s["job_type_code"] == "restore_instance"
        )
        self.assertNotIn("neutralize", restore["payload"])
        self.assertNotIn("reset_base_url", restore["payload"])


# ── backup_download: live path on production ─────────────────────────────────


class TestBackupDownloadLive(BaseCase):

    def test_production_live_takes_the_live_dump_path(self):
        cmds = _make_download_executor(
            environment="production", time="live",
        ).get_commands()
        self.assertEqual([c[0] for c in cmds], ["Create live backup"])
        argv = _argv(cmds[0])
        self.assertEqual(argv[2], "live-dump")

    def test_production_latest_still_goes_through_duplicity(self):
        cmds = _make_download_executor(
            environment="production", time="latest",
        ).get_commands()
        self.assertEqual(
            [c[0] for c in cmds],
            ["Restore full from backup", "Extract and package"],
        )

    def test_production_timestamp_still_goes_through_duplicity(self):
        cmds = _make_download_executor(
            environment="production", time="12h_ago",
        ).get_commands()
        argv = _argv(cmds[0])
        self.assertEqual(argv[2], "restore-full")
        self.assertIn("12h_ago", argv)

    def test_non_production_is_unchanged(self):
        cmds = _make_download_executor(
            environment="staging", time="latest",
        ).get_commands()
        self.assertEqual([c[0] for c in cmds], ["Create live backup"])

    def test_live_dump_scope_follows_download_type(self):
        cmds = _make_download_executor(
            environment="production", time="live", mode="all",
        ).get_commands()
        self.assertEqual(_argv(cmds[0])[-1], "all")
        cmds = _make_download_executor(
            environment="production", time="live", mode="dump",
        ).get_commands()
        self.assertEqual(_argv(cmds[0])[-1], "db")


class TestBackupDownloadValidation(BaseCase):

    def _run_before_execute(self, ex):
        import asyncio
        return asyncio.run(ex.before_execute(None))

    def test_non_production_accepts_live(self):
        ex = _make_download_executor(environment="staging", time="live")
        self._run_before_execute(ex)  # must not raise

    def test_non_production_accepts_latest(self):
        ex = _make_download_executor(environment="staging", time="latest")
        self._run_before_execute(ex)  # must not raise

    def test_non_production_still_refuses_timestamps(self):
        ex = _make_download_executor(environment="staging", time="12h_ago")
        with self.assertRaises(ValueError):
            self._run_before_execute(ex)

    def test_production_accepts_live(self):
        ex = _make_download_executor(environment="production", time="live")
        self._run_before_execute(ex)  # must not raise


# ── restore_instance: neutralize + base URL ──────────────────────────────────


class TestRestoreNeutralize(BaseCase):

    def _restore_step(self, payload):
        return _find(
            _make_restore_executor(payload).get_commands(),
            "Restore database",
        )

    def test_neutralize_flag_reaches_the_script(self):
        argv = _argv(self._restore_step({
            "mode": "from_job", "neutralize": True,
        }))
        self.assertEqual(argv[2], "restore-db")
        self.assertEqual(argv[-1], "1")

    def test_without_the_flag_the_script_is_told_not_to(self):
        argv = _argv(self._restore_step({"mode": "from_job"}))
        self.assertEqual(argv[-1], "0")

    def test_explicit_false_is_not_neutralized(self):
        argv = _argv(self._restore_step({
            "mode": "from_job", "neutralize": False,
        }))
        self.assertEqual(argv[-1], "0")


class TestRestoreBaseUrl(BaseCase):

    def test_reset_base_url_adds_the_step(self):
        cmds = _make_restore_executor({
            "mode": "from_job", "reset_base_url": True,
        }).get_commands()
        step = _find(cmds, "Reset base URL")
        self.assertIsNotNone(step)
        argv = _argv(step)
        self.assertEqual(argv[2], "set-base-url")
        self.assertEqual(
            argv[3:],
            [
                INSTANCE_DIR, "odoo", "prod",
                "https://staging.example.com", "http://localhost:8069",
            ],
        )

    def test_without_the_flag_there_is_no_step(self):
        cmds = _make_restore_executor({"mode": "from_job"}).get_commands()
        self.assertIsNone(_find(cmds, "Reset base URL"))

    def test_no_domain_means_no_step(self):
        # Writing an empty web.base.url would be worse than leaving the
        # one that came inside the dump.
        cmds = _make_restore_executor(
            {"mode": "from_job", "reset_base_url": True}, domain="",
        ).get_commands()
        self.assertIsNone(_find(cmds, "Reset base URL"))

    def test_step_runs_before_the_connect_module_boots_odoo(self):
        cmds = _make_restore_executor({
            "mode": "from_job", "reset_base_url": True,
        }).get_commands()
        labels = [c[0] for c in cmds]
        self.assertLess(
            labels.index("Reset base URL"),
            labels.index("Ensure incubacloud_connect"),
        )
        self.assertGreater(
            labels.index("Reset base URL"),
            labels.index("Restore database"),
        )

    def test_the_rest_of_the_chain_is_untouched(self):
        cmds = _make_restore_executor({"mode": "from_job"}).get_commands()
        self.assertEqual(
            [c[0] for c in cmds],
            [
                "Verify backup file",
                "Stop Odoo service",
                "Restore database",
                "Ensure incubacloud_connect",
                "Start Odoo service",
                "Remove remote backup file",
            ],
        )

    def test_a_quote_in_the_domain_is_sql_escaped(self):
        argv = _argv(_find(
            _make_restore_executor(
                {"mode": "from_job", "reset_base_url": True},
                domain="it's.example.com",
            ).get_commands(),
            "Reset base URL",
        ))
        self.assertIn("https://it''s.example.com", argv)
