"""Structural invariants of the enqueue role gate.

``cloud.job.enqueue`` is public and reachable over JSON-RPC, so the map
returned by ``_get_job_type_min_group()`` — not the HTTP controllers — is
what decides who may ask for an action. These tests make the map
impossible to forget: every installed job type must be listed explicitly
(the fail-closed default is a backstop, not a substitute for mapping), the
values must be real cloud groups, and the destructive types must not drift
below the role their only legitimate caller already requires.
"""
from odoo.tests.common import TransactionCase, tagged


_VALID_GROUPS = {
    'group_cloud_user',
    'group_cloud_consultant',
    'group_cloud_project_manager',
    'group_cloud_developer',
    'group_cloud_manager',
}


@tagged('post_install', '-at_install')
class TestJobTypeGateInvariant(TransactionCase):
    """post_install on purpose: dependent modules register their own
    ``_get_job_type_min_group()`` override only once they are loaded, and
    the job types removed from the data files are orphan-cleaned at the
    end of the whole load (``_process_end``). An at_install run would see
    a half-built map against a stale set of records.
    """


    def setUp(self):
        super().setUp()
        self.mapping = self.env['cloud.job']._get_job_type_min_group()

    def test_every_installed_job_type_is_mapped(self):
        """A declared job type with no entry would fall to the default."""
        codes = set(
            self.env['cloud.job.type'].search([]).mapped('code')
        ) - {False, ''}
        missing = sorted(codes - set(self.mapping))
        self.assertFalse(
            missing,
            "Job types with no role mapped: %s. Add them to "
            "cloud.job._job_type_min_group (core) or to the "
            "_get_job_type_min_group() override of the module that "
            "declares them." % ', '.join(missing),
        )

    def test_mapping_has_no_stale_entries(self):
        """A mapped code with no job type record is dead configuration."""
        codes = set(
            self.env['cloud.job.type'].search([]).mapped('code')
        ) - {False, ''}
        # ``delete_project`` is deliberately kept: the executor is gone but
        # historical jobs still reference the type record.
        stale = sorted(set(self.mapping) - codes)
        self.assertFalse(
            stale, "Mapped job types that no longer exist: %s"
            % ', '.join(stale),
        )

    def test_all_values_are_real_groups(self):
        bad = sorted(
            f"{code}={group}"
            for code, group in self.mapping.items()
            if group not in _VALID_GROUPS
        )
        self.assertFalse(bad, "Unknown groups in the map: %s" % ', '.join(bad))

    def test_destructive_types_stay_manager_only(self):
        """These have no guard of their own downstream.

        ``move_cutover`` rewrites ``host_id`` and ``move_cleanup_source``
        runs the delete teardown without ever reaching ``unlink()``, so the
        enqueue gate is the only thing standing in front of them.
        """
        for code in (
            'move_cutover', 'move_cleanup_source', 'move_rollback_cleanup',
            'delete_host', 'full_setup', 'setup_whitelist', 'host_probe',
            'docker_prune', 'host_hardening',
            'install_observability', 'deploy_metrics_central',
        ):
            with self.subTest(code=code):
                self.assertEqual(self.mapping.get(code), 'group_cloud_manager')

    def test_data_moving_types_require_developer_at_least(self):
        for code in (
            'backup_list', 'backup_create', 'backup_download',
            'backup_download_neutralized', 'backup_restore',
            'restore_instance', 'export_instance',
        ):
            with self.subTest(code=code):
                self.assertIn(
                    self.mapping.get(code),
                    ('group_cloud_developer', 'group_cloud_manager'),
                )
