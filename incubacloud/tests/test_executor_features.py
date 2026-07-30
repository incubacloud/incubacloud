"""
Tests for executor features: stop_on_failure, _prod_services,
SMTP stripping, safe boot test, and click-odoo-update integration.
"""
import shlex
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
    ex._backup_enabled = bool
    # Reads cloud.settings in production; stubbed here like the other
    # environment reads so get_commands() works without a database.
    ex._copier_template = lambda: ("gh:Tecnativa/doodba-copier-template", "")
    ex.job = SimpleNamespace(id=42)
    ex._scripts_requested = False
    ex._scripts_uploaded = False
    ex._script_overlay_cache = None
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
    ex._copier_template = lambda: ("gh:Tecnativa/doodba-copier-template", "")
    ex.job = SimpleNamespace(id=42)
    ex._scripts_requested = False
    ex._scripts_uploaded = False
    ex._script_overlay_cache = None
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

class TestRebuildCommandsBootTest(BaseCase):
    """The safe-boot step runs ``rebuild.sh boot-test``. Its pipeline —
    containerised chown, in-container backup_label removal, pre-cleanup on
    both sides of the boundary, exit-code propagation — is safety-critical
    and covered end to end in ``tests/shell/rebuild.bats``. Here we pin
    the wiring: the step invokes the right operation, with the instance id
    and postgres parameters, and stops the chain on failure so a bad boot
    never reaches ``up -d``."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ex = _make_rebuild_executor()
        cls.cmds = ex.get_commands()
        cls.cmd = _find_cmd(cls.cmds, "Test new image (safe boot check)")
        cls.argv = shlex.split(cls.cmd[1]) if cls.cmd else []

    def test_boot_test_command_present(self):
        self.assertIsNotNone(self.cmd, "Boot test command not found")

    def test_boot_test_invokes_the_rebuild_boot_test_op(self):
        self.assertTrue(self.argv[1].endswith("/rebuild.sh"), self.argv[1])
        self.assertEqual(self.argv[2], "boot-test")

    def test_boot_test_passes_the_instance_and_pg_parameters(self):
        # argv: bash <path> boot-test <dir> <inst_id> <project> <user> <pgver> <db>
        _, _, _op, _dir, inst_id, project, pg_user, pg_ver, dbname = self.argv
        self.assertEqual(inst_id, "42")
        self.assertEqual(project, "test_proj")
        self.assertEqual(pg_user, "odoo")
        self.assertEqual(pg_ver, "17")
        self.assertEqual(dbname, "prod")

    def test_boot_test_has_stop_on_failure(self):
        # A failed boot must abort before ``up -d`` so the instance keeps
        # running on the old image.
        self.assertEqual(len(self.cmd), 3)
        self.assertTrue(self.cmd[2].get("stop_on_failure"))

    def test_update_command_uses_click_odoo_update(self):
        # This step stayed inline (a one-liner docker compose run).
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
    """The env_file injection runs ``deploy.sh inject-secret-env`` on both
    deploy and rebuild. Here we pin the wiring — the step is present and
    invokes that operation; the behaviour it guarantees (targets prod.yaml
    and test.yaml, not common.yaml; idempotent) is covered by
    ``tests/shell/deploy.bats``.
    """

    def _deploy_inject_cmd(self, **kwargs):
        cmds = _make_deploy_executor(**kwargs).get_commands()
        return _find_cmd(cmds, "Inject incubacloud.env in prod.yaml and test.yaml")

    def _rebuild_inject_cmd(self, **kwargs):
        cmds = _make_rebuild_executor(**kwargs).get_commands()
        return _find_cmd(cmds, "Inject incubacloud.env in prod.yaml and test.yaml")

    def _assert_inject_op(self, cmd):
        self.assertIsNotNone(cmd, "Inject step must be present")
        argv = shlex.split(cmd[1])
        self.assertTrue(argv[1].endswith("/deploy.sh"), argv[1])
        self.assertEqual(argv[2], "inject-secret-env")

    def test_deploy_inject_step_invokes_the_script(self):
        self._assert_inject_op(self._deploy_inject_cmd(environment='production'))

    def test_rebuild_inject_step_invokes_the_script(self):
        self._assert_inject_op(self._rebuild_inject_cmd())


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


# ---------------------------------------------------------------------------
# Idempotent ~/.gitconfig guard
# ---------------------------------------------------------------------------

class TestGitConfigIdempotentGuard(BaseCase):
    """Pin the read-first guard on ``init.defaultBranch``.

    An unconditional ``git config --global init.defaultBranch master``
    takes the ``~/.gitconfig`` lock on every deploy/rebuild/warm-claim.
    When two jobs hit the same host in the same tick (warm pool cron
    enqueues N builds against one host, the dedicated ``root.warm``
    channel runs them in parallel), the second one fails with
    ``error: could not lock config file ~/.gitconfig: File exists``
    and the whole deploy unravels.

    The fix has two layers and this test class guards both:

    1. ``full_setup`` seeds the setting once per host, in a serialized
       step that cannot race with itself.
    2. The deploy / rebuild / warm-claim ``path_prefix`` reads first
       (``--get``) and only writes when the value is absent, so a host
       already seeded never touches the lock again.
    """

    # ``--get`` is a read, not a write — it does not take the ~/.gitconfig
    # lock, so concurrent siblings on the same host never collide.
    _READ_FIRST_TOKEN = (
        'git config --global --get init.defaultBranch >/dev/null 2>&1'
    )

    def _path_prefix_step(self, executor_factory, label):
        """Return the (label, cmd) tuple for the copier step from a
        freshly built executor's get_commands() output.

        ``_make_deploy_executor`` builds a SimpleNamespace that doesn't
        carry ``effective_backup_backend`` (a model field), so any path
        through ``_backup_enabled()`` blows up with AttributeError.
        Stub it to ``False`` — backup-stripping behaviour is unrelated
        to the gitconfig guard under test here.
        """
        ex = executor_factory()
        ex._backup_enabled = bool
        cmds = ex.get_commands()
        cmd = _find_cmd(cmds, label)
        self.assertIsNotNone(cmd, f"Step '{label}' missing from get_commands()")
        return cmd

    def test_deploy_copier_step_invokes_the_script(self):
        """The gitconfig read-first guard now lives inside ``deploy.sh
        copier-deploy`` (asserted in tests/shell/deploy.bats); here we
        pin that the deploy step routes through it."""
        cmd = self._path_prefix_step(_make_deploy_executor, "Deploy with copier")
        argv = shlex.split(cmd[1])
        self.assertTrue(argv[1].endswith("/deploy.sh"), argv[1])
        self.assertEqual(argv[2], "copier-deploy")

    def test_rebuild_copier_step_invokes_the_script(self):
        """Same guard, inside ``rebuild.sh copier-update`` (asserted in
        tests/shell/rebuild.bats)."""
        cmd = self._path_prefix_step(_make_rebuild_executor, "Update with copier")
        argv = shlex.split(cmd[1])
        self.assertTrue(argv[1].endswith("/rebuild.sh"), argv[1])
        self.assertEqual(argv[2], "copier-update")

    def _install_script_text(self):
        """Return the text of ``scripts/full_setup_install.sh``.

        The Phase-2 install catalog moved from the ``SETUP_COMMANDS``
        Python list to this versioned script; these guards now assert on
        the script itself.
        """
        import os

        from odoo.modules.module import get_module_path

        path = os.path.join(
            get_module_path("incubacloud"),
            "scripts", "full_setup_install.sh",
        )
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_full_setup_has_seed_step(self):
        """full_setup must seed init.defaultBranch once per host."""
        self.assertIn("init.defaultBranch", self._install_script_text())

    def test_full_setup_seed_step_is_idempotent(self):
        """The seed reads before writing — re-running Setup Host on an
        already-configured host must not rewrite (no ~/.gitconfig lock).
        """
        self.assertIn(self._READ_FIRST_TOKEN, self._install_script_text())

    def test_full_setup_seed_runs_after_install_git(self):
        """Git must be installed before ``git config`` runs — otherwise the
        first Setup Host run would call it before the binary exists.
        """
        text = self._install_script_text()
        git_install = text.find("command -v git")
        git_seed = text.find("git config --global --get init.defaultBranch")
        self.assertGreater(git_install, -1, "install-git step missing")
        self.assertGreater(git_seed, -1, "gitconfig seed step missing")
        self.assertLess(
            git_install, git_seed,
            "git must be installed before the gitconfig seed",
        )
