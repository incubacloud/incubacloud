"""
Tests for executor features: stop_on_failure, _prod_services,
SMTP stripping, safe boot test, and click-odoo-update integration.
"""
import unittest

from odoo.tests.common import BaseCase

from types import SimpleNamespace


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_deploy_executor(smtp_relay_host='', environment='production',
                          odoo_version='19.0', **extra):
    """Build a minimal DeployInstanceExecutor without an Odoo environment."""
    from odoo.addons.incubacloud.models.deploy_instance_executor import (
        DeployInstanceExecutor,
    )

    inst = SimpleNamespace(
        name='test-inst',
        doodba_project_name='test_proj',
        postgres_username='odoo',
        postgres_dbname='prod',
        environment=environment,
        smtp_relay_host=smtp_relay_host,
        odoo_version=odoo_version,
        domain='test.example.com',
        project_id=SimpleNamespace(remote_folder='projects'),
        odoo_initial_lang=None,
        **extra,
    )

    ex = object.__new__(DeployInstanceExecutor)
    ex._inst = lambda: inst
    ex._inst_dir = lambda i: f"~/projects/{i.name}"
    ex._tmp = lambda suffix: f"/tmp/.incubacloud-test_proj-{suffix}"
    ex._base_url = lambda: "https://test.example.com"
    return ex


def _make_rebuild_executor(smtp_relay_host='', environment='production',
                           odoo_version='19.0', auto_update=True, **extra):
    """Build a minimal RebuildInstanceExecutor without an Odoo environment."""
    from odoo.addons.incubacloud.models.rebuild_instance_executor import (
        RebuildInstanceExecutor,
    )

    inst = SimpleNamespace(
        id=42,
        name='test-inst',
        doodba_project_name='test_proj',
        postgres_username='odoo',
        postgres_dbname='prod',
        postgres_version='17',
        environment=environment,
        smtp_relay_host=smtp_relay_host,
        odoo_version=odoo_version,
        domain='test.example.com',
        project_id=SimpleNamespace(remote_folder='projects'),
        odoo_initial_lang=None,
        auto_update=auto_update,
        rebuild_fingerprint='fp1',
        last_rebuild_fingerprint='fp0',
        **extra,
    )

    ex = object.__new__(RebuildInstanceExecutor)
    ex._inst = lambda: inst
    ex._inst_dir = lambda i: f"~/projects/{i.name}"
    ex._tmp = lambda suffix: f"/tmp/.incubacloud-test_proj-{suffix}"
    ex._base_url = lambda: "https://test.example.com"
    ex._sys = lambda *_a, **_k: None
    ex._backup_enabled = bool
    ex._backup_retention = lambda: '3M'
    return ex


def _find_cmd(cmds, label_substring):
    """Return the first command tuple whose label contains *label_substring*."""
    return next(
        (c for c in cmds if label_substring in c[0]),
        None,
    )


# ---------------------------------------------------------------------------
# Class 1: TestStopOnFailure
# ---------------------------------------------------------------------------

class TestStopOnFailure(unittest.TestCase):
    """Test that command tuples support 2-element and 3-element formats."""

    def test_two_tuple_has_no_opts(self):
        item = ('label', 'cmd')
        self.assertEqual(len(item), 2)
        with self.assertRaises(IndexError):
            item[2]  # noqa: B018 — intentional access

    def test_three_tuple_has_opts(self):
        item = ('label', 'cmd', {"stop_on_failure": True})
        self.assertTrue(item[2]["stop_on_failure"])

    def test_opts_default_empty(self):
        item = ('label', 'cmd')
        opts = item[2] if len(item) == 3 else {}
        self.assertEqual(opts, {})


# ---------------------------------------------------------------------------
# Class 2: TestProdServices
# ---------------------------------------------------------------------------

class TestProdServices(unittest.TestCase):
    """Test that _prod_services() reflects SMTP configuration."""

    def test_smtp_included_when_host_set(self):
        ex = _make_deploy_executor(smtp_relay_host='mail.example.com')
        self.assertIn('smtp', ex._prod_services())

    def test_smtp_excluded_when_host_empty(self):
        ex = _make_deploy_executor(smtp_relay_host='')
        self.assertNotIn('smtp', ex._prod_services())

    def test_smtp_excluded_when_host_none(self):
        ex = _make_deploy_executor(smtp_relay_host=None)
        self.assertNotIn('smtp', ex._prod_services())

    def test_always_includes_odoo_db_backup(self):
        result = _make_deploy_executor(smtp_relay_host='')._prod_services()
        self.assertIn('odoo', result)
        self.assertIn('db', result)
        self.assertIn('backup', result)


# ---------------------------------------------------------------------------
# Class 3: TestRebuildCommandsBootTest
# ---------------------------------------------------------------------------

class TestRebuildCommandsBootTest(unittest.TestCase):
    """Test rebuild get_commands() includes boot test + click-odoo-update."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ex = _make_rebuild_executor()
        cls.cmds = ex.get_commands()

    def test_boot_test_command_present(self):
        cmd = _find_cmd(self.cmds, "Test new image (safe boot check)")
        self.assertIsNotNone(cmd, "Boot test command not found")

    def test_boot_test_uses_click_odoo_update(self):
        cmd = _find_cmd(self.cmds, "Test new image (safe boot check)")
        self.assertIn("click-odoo-update", cmd[1])

    def test_boot_test_uses_test_db(self):
        cmd = _find_cmd(self.cmds, "Test new image (safe boot check)")
        self.assertIn("__ic_boot_test", cmd[1])

    def test_boot_test_has_stop_on_failure(self):
        cmd = _find_cmd(self.cmds, "Test new image (safe boot check)")
        self.assertEqual(len(cmd), 3)
        self.assertTrue(cmd[2].get("stop_on_failure"))

    def test_boot_test_cleanup_always_runs(self):
        """dropdb runs after ';' (not '&&') so it executes on failure too."""
        cmd = _find_cmd(self.cmds, "Test new image (safe boot check)")
        body = cmd[1]
        # Find dropdb and verify it's preceded by ';' not '&&'
        dropdb_idx = body.index("dropdb")
        preceding = body[:dropdb_idx]
        # The last connector before dropdb should be ';'
        self.assertIn("; docker compose exec -T db", preceding)

    def test_update_command_uses_click_odoo_update(self):
        cmd = _find_cmd(self.cmds, "Update changed modules")
        self.assertIsNotNone(cmd, "Update command not found")
        self.assertIn("click-odoo-update", cmd[1])

    def test_build_has_stop_on_failure(self):
        cmd = _find_cmd(self.cmds, "Rebuild Odoo image")
        self.assertIsNotNone(cmd, "Rebuild command not found")
        self.assertEqual(len(cmd), 3)
        self.assertTrue(cmd[2].get("stop_on_failure"))


# ---------------------------------------------------------------------------
# Class 4: TestDeployCommandsChecksums
# ---------------------------------------------------------------------------

class TestDeployCommandsChecksums(unittest.TestCase):
    """Test that deploy get_commands() includes checksums baseline step."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ex = _make_deploy_executor()
        cls.cmds = ex.get_commands()

    def test_checksums_step_present(self):
        cmd = _find_cmd(self.cmds, "Initialize module checksums")
        self.assertIsNotNone(cmd, "Checksums step not found")

    def test_checksums_uses_only_compute_hashes(self):
        cmd = _find_cmd(self.cmds, "Initialize module checksums")
        self.assertIn("--only-compute-hashes", cmd[1])

    def test_init_db_has_stop_on_failure(self):
        cmd = _find_cmd(self.cmds, "Initialize database")
        self.assertIsNotNone(cmd, "Init DB command not found")
        self.assertEqual(len(cmd), 3)
        self.assertTrue(cmd[2].get("stop_on_failure"))


# ---------------------------------------------------------------------------
# Class 5: TestSmtpStripping
# ---------------------------------------------------------------------------

class TestSmtpStripping(unittest.TestCase):
    """Test SMTP service stripping from prod.yaml when appropriate."""

    def test_smtp_strip_when_no_host_production(self):
        cmds = _make_deploy_executor(smtp_relay_host='', environment='production').get_commands()
        cmd = _find_cmd(cmds, "Strip smtp service")
        self.assertIsNotNone(cmd, "Strip step should be present for prod without smtp")

    def test_no_smtp_strip_when_host_set(self):
        cmds = _make_deploy_executor(
            smtp_relay_host='mail.example.com', environment='production',
        ).get_commands()
        cmd = _find_cmd(cmds, "Strip smtp service")
        self.assertIsNone(cmd, "Strip step should NOT be present when smtp is configured")

    def test_no_smtp_strip_for_staging(self):
        cmds = _make_deploy_executor(smtp_relay_host='', environment='staging').get_commands()
        cmd = _find_cmd(cmds, "Strip smtp service")
        self.assertIsNone(cmd, "Strip step should NOT be present for staging")


# ---------------------------------------------------------------------------
# Smart Rebuild (fingerprint-based cache decision)
# ---------------------------------------------------------------------------

class TestSmartRebuildCommands(unittest.TestCase):
    """Test that rebuild uses --no-cache only when fingerprint changed."""

    def test_full_rebuild_when_fingerprint_differs(self):
        """When fingerprints differ, build should use --no-cache --pull."""
        cmds = _make_rebuild_executor(
            rebuild_fingerprint='aaa',
            last_rebuild_fingerprint='bbb',
        ).get_commands()
        cmd = _find_cmd(cmds, "Rebuild Odoo image")
        self.assertIsNotNone(cmd)
        self.assertIn('--no-cache', cmd[1])
        self.assertIn('--pull', cmd[1])

    def test_cached_rebuild_when_fingerprint_matches(self):
        """When fingerprints match, build should NOT use --no-cache."""
        cmds = _make_rebuild_executor(
            rebuild_fingerprint='same',
            last_rebuild_fingerprint='same',
        ).get_commands()
        cmd = _find_cmd(cmds, "Rebuild Odoo image")
        self.assertIsNotNone(cmd)
        self.assertNotIn('--no-cache', cmd[1])
        self.assertNotIn('--pull', cmd[1])

    def test_first_rebuild_is_always_full(self):
        """When last_rebuild_fingerprint is None, build must be full."""
        cmds = _make_rebuild_executor(
            rebuild_fingerprint='abc',
            last_rebuild_fingerprint=None,
        ).get_commands()
        cmd = _find_cmd(cmds, "Rebuild Odoo image")
        self.assertIsNotNone(cmd)
        self.assertIn('--no-cache', cmd[1])

    def test_first_rebuild_when_empty_string(self):
        """When last_rebuild_fingerprint is empty, build must be full."""
        cmds = _make_rebuild_executor(
            rebuild_fingerprint='abc',
            last_rebuild_fingerprint='',
        ).get_commands()
        cmd = _find_cmd(cmds, "Rebuild Odoo image")
        self.assertIsNotNone(cmd)
        self.assertIn('--no-cache', cmd[1])


# ---------------------------------------------------------------------------
# incubacloud.env injection
# ---------------------------------------------------------------------------

class TestIncubaclouEnvInjection(unittest.TestCase):
    """The env_file injection must target prod.yaml and test.yaml, not common.yaml.

    common.yaml has no env_file block; the .docker/odoo.env reference lives
    in prod.yaml and test.yaml.  A sed targeting common.yaml never matches
    and silently leaves INCUBACLOUD_SECRET_KEY out of the compose stack.
    """

    def _deploy_inject_cmd(self, **kwargs):
        cmds = _make_deploy_executor(**kwargs).get_commands()
        return _find_cmd(cmds, "Inject incubacloud.env in prod.yaml and test.yaml")

    def _rebuild_inject_cmd(self, **kwargs):
        cmds = _make_rebuild_executor(**kwargs).get_commands()
        return _find_cmd(cmds, "Inject incubacloud.env in prod.yaml and test.yaml")

    def test_deploy_inject_step_present(self):
        cmd = self._deploy_inject_cmd(environment='production')
        self.assertIsNotNone(cmd, "Inject step must be present in deploy commands")

    def test_rebuild_inject_step_present(self):
        cmd = self._rebuild_inject_cmd()
        self.assertIsNotNone(cmd, "Inject step must be present in rebuild commands")

    def test_deploy_inject_targets_prod_and_test(self):
        cmd = self._deploy_inject_cmd(environment='production')
        self.assertIn('prod.yaml', cmd[1])
        self.assertIn('test.yaml', cmd[1])

    def test_rebuild_inject_targets_prod_and_test(self):
        cmd = self._rebuild_inject_cmd()
        self.assertIn('prod.yaml', cmd[1])
        self.assertIn('test.yaml', cmd[1])

    def test_deploy_inject_does_not_target_common(self):
        """Regression: must NOT inject into common.yaml (has no env_file)."""
        cmd = self._deploy_inject_cmd(environment='production')
        # The sed/grep should not mention common.yaml as the target
        self.assertNotIn(
            "sed -i '/\\.docker\\/odoo\\.env/a\\      - .docker/incubacloud.env' common.yaml",
            cmd[1],
        )

    def test_rebuild_inject_does_not_target_common(self):
        """Regression: must NOT inject into common.yaml (has no env_file)."""
        cmd = self._rebuild_inject_cmd()
        self.assertNotIn(
            "sed -i '/\\.docker\\/odoo\\.env/a\\      - .docker/incubacloud.env' common.yaml",
            cmd[1],
        )

    def test_deploy_inject_is_idempotent(self):
        """Command must use grep -q guard so it does not double-inject."""
        cmd = self._deploy_inject_cmd(environment='production')
        self.assertIn('grep -q', cmd[1])

    def test_rebuild_inject_is_idempotent(self):
        """Command must use grep -q guard so it does not double-inject."""
        cmd = self._rebuild_inject_cmd()
        self.assertIn('grep -q', cmd[1])


# ---------------------------------------------------------------------------
# Class 8: TestAutoUpdateFlag
# ---------------------------------------------------------------------------

class TestAutoUpdateFlag(BaseCase):
    """Rebuild skips boot test + update when ``auto_update`` is off.

    Default is ``True`` (preserves current behavior for all pre-existing
    instances). When an operator flips it off — e.g. a production with a
    change-management policy — both ``click-odoo-update`` steps go away
    but the image rebuild, the ``incubacloud_connect`` reinstall and the
    final ``up -d`` still run, so the operator can ship new image bits
    without forcing module migrations.
    """

    def test_auto_update_true_keeps_both_click_odoo_steps(self):
        cmds = _make_rebuild_executor(auto_update=True).get_commands()
        self.assertIsNotNone(
            _find_cmd(cmds, "Test new image (safe boot check)"),
            "boot test must be present when auto_update=True",
        )
        self.assertIsNotNone(
            _find_cmd(cmds, "Update changed modules"),
            "click-odoo-update must be present when auto_update=True",
        )

    def test_auto_update_false_skips_boot_test_and_update(self):
        cmds = _make_rebuild_executor(auto_update=False).get_commands()
        self.assertIsNone(
            _find_cmd(cmds, "Test new image (safe boot check)"),
            "boot test must be skipped when auto_update=False",
        )
        self.assertIsNone(
            _find_cmd(cmds, "Update changed modules"),
            "click-odoo-update must be skipped when auto_update=False",
        )

    def test_auto_update_false_keeps_tail_steps(self):
        """Skipping updates must not strip the incubacloud_connect install
        and the final restart — those guarantee the new image actually
        runs even without module migrations."""
        cmds = _make_rebuild_executor(auto_update=False).get_commands()
        self.assertIsNotNone(
            _find_cmd(cmds, "Ensure incubacloud_connect"),
            "incubacloud_connect reinstall must still run",
        )
        self.assertIsNotNone(
            _find_cmd(cmds, "Restart instance"),
            "docker compose up -d must still run",
        )

    def test_auto_update_false_keeps_image_rebuild(self):
        """The image itself must still be rebuilt; auto_update only
        gates the DB-side update, not the Docker-side one."""
        cmds = _make_rebuild_executor(auto_update=False).get_commands()
        self.assertIsNotNone(
            _find_cmd(cmds, "Rebuild Odoo image"),
            "image rebuild must still run when auto_update=False",
        )
