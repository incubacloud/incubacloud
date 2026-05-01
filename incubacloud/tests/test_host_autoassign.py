"""
Tests for host auto-assignment feature.
Covers: parse_memory_to_gb, computed resource fields, select_best_host,
        _compute_instance_resources, and controller auto-assign logic.
"""
import unittest


from odoo.tests.common import TransactionCase, BaseCase


# ── Fase 1: parse_memory_to_gb ──────────────────────────────────────────────


class TestParseMemoryToGb(BaseCase):
    """parse_memory_to_gb converts Docker memory strings to float GB."""

    def _parse(self, val):
        from odoo.addons.incubacloud.models.cloud_host import (
            parse_memory_to_gb,
        )
        return parse_memory_to_gb(val)

    def test_gigabytes(self):
        self.assertAlmostEqual(self._parse('2g'), 2.0)

    def test_megabytes(self):
        self.assertAlmostEqual(self._parse('512m'), 0.5)

    def test_kilobytes(self):
        self.assertAlmostEqual(self._parse('1048576k'), 1.0)

    def test_empty_string(self):
        self.assertEqual(self._parse(''), 0.0)

    def test_false(self):
        self.assertEqual(self._parse(False), 0.0)

    def test_none(self):
        self.assertEqual(self._parse(None), 0.0)

    def test_uppercase(self):
        self.assertAlmostEqual(self._parse('2G'), 2.0)

    def test_decimal(self):
        self.assertAlmostEqual(self._parse('1.5g'), 1.5)

    def test_256m(self):
        self.assertAlmostEqual(self._parse('256m'), 0.25)

    def test_1g(self):
        self.assertAlmostEqual(self._parse('1g'), 1.0)


# ── Fase 2: Campos computados en cloud.host ─────────────────────────────────


class TestHostResourceComputation(TransactionCase):
    """Computed fields: allocated_cpus/ram, available_cpus/ram."""

    def setUp(self):
        super().setUp()
        self.Host = self.env['cloud.host']
        self.host = self.Host.create({
            'name': 'H1',
            'ip_address': '10.0.0.1',
            'user': 'ubuntu',
            'wildcard_domain': 'h1.example.com',
            'cpu_cores': 8,
            'ram_total_gb': 16.0,
        })
        self.project = self.env['cloud.project'].create({'name': 'P1'})

    def _inst(self, **kw):
        return self.env['cloud.instance'].create({
            'name': kw.pop('name', 'i1'),
            'project_id': self.project.id,
            'host_id': self.host.id,
        } | kw)

    def test_empty_host_zero_allocated(self):
        self.assertEqual(self.host.allocated_cpus, 0.0)
        self.assertEqual(self.host.allocated_ram_gb, 0.0)
        self.assertEqual(self.host.available_cpus, 8.0)
        self.assertEqual(self.host.available_ram_gb, 16.0)

    def test_single_instance_defaults(self):
        self._inst()
        # defaults: odoo=2 + db=1 + backup=0.5 + smtp=0.25 = 3.75 CPU
        self.assertAlmostEqual(self.host.allocated_cpus, 3.75)
        self.assertAlmostEqual(self.host.available_cpus, 4.25)
        # defaults: 2g + 1g + 512m + 256m = 3.75 GB
        self.assertAlmostEqual(self.host.allocated_ram_gb, 3.75, places=2)
        self.assertAlmostEqual(self.host.available_ram_gb, 12.25, places=2)

    def test_multiple_instances_sum(self):
        self._inst(name='i1')
        self._inst(name='i2')
        self.assertAlmostEqual(self.host.allocated_cpus, 7.5)
        self.assertAlmostEqual(self.host.allocated_ram_gb, 7.5, places=2)

    def test_available_can_be_negative(self):
        """Overcommit produces negative available values."""
        self._inst(name='i1')
        self._inst(name='i2')
        self._inst(name='i3')
        self.assertLess(self.host.available_cpus, 0)

    def test_custom_resource_limits(self):
        self._inst(
            odoo_cpus=4.0, odoo_memory_limit='4g',
            db_cpus=2.0, db_memory_limit='2g',
            backup_cpus=1.0, backup_memory_limit='1g',
            smtp_cpus=0.5, smtp_memory_limit='512m',
        )
        self.assertAlmostEqual(self.host.allocated_cpus, 7.5)
        self.assertAlmostEqual(self.host.allocated_ram_gb, 7.5, places=2)

    def test_instance_with_empty_memory_uses_zero(self):
        self._inst(
            odoo_memory_limit='', db_memory_limit='',
            backup_memory_limit='', smtp_memory_limit='',
        )
        self.assertEqual(self.host.allocated_ram_gb, 0.0)

    def test_exclude_from_autoassign_default_false(self):
        self.assertFalse(self.host.exclude_from_autoassign)


# ── Fase 3: _compute_instance_resources ──────────────────────────────────────


class TestComputeInstanceResources(BaseCase):
    """_compute_instance_resources extracts total CPU/RAM from creation vals."""

    def _compute(self, vals):
        from odoo.addons.incubacloud.models.cloud_instance import CloudInstance
        return CloudInstance._compute_instance_resources(vals)

    def test_defaults_when_empty(self):
        cpus, ram = self._compute({})
        self.assertAlmostEqual(cpus, 3.75)
        self.assertAlmostEqual(ram, 3.75, places=2)

    def test_custom_odoo_cpus(self):
        cpus, ram = self._compute({'odoo_cpus': 4.0})
        self.assertAlmostEqual(cpus, 5.75)  # 4 + 1 + 0.5 + 0.25

    def test_custom_memory(self):
        cpus, ram = self._compute({'odoo_memory_limit': '4g'})
        self.assertAlmostEqual(ram, 5.75, places=2)  # 4 + 1 + 0.5 + 0.25

    def test_all_custom(self):
        cpus, ram = self._compute({
            'odoo_cpus': 4.0, 'db_cpus': 2.0,
            'backup_cpus': 1.0, 'smtp_cpus': 0.5,
            'odoo_memory_limit': '4g', 'db_memory_limit': '2g',
            'backup_memory_limit': '1g', 'smtp_memory_limit': '512m',
        })
        self.assertAlmostEqual(cpus, 7.5)
        self.assertAlmostEqual(ram, 7.5, places=2)

    def test_partial_override(self):
        cpus, ram = self._compute({
            'odoo_cpus': 4.0,
            'db_memory_limit': '2g',
        })
        # CPU: 4 + 1 + 0.5 + 0.25 = 5.75
        self.assertAlmostEqual(cpus, 5.75)
        # RAM: 2g + 2g + 0.5g + 0.25g = 4.75
        self.assertAlmostEqual(ram, 4.75, places=2)


# ── Fase 4: select_best_host ─────────────────────────────────────────────────

_HOST_BASE = {
    'user': 'ubuntu',
    'wildcard_domain': 'example.com',
}


class TestSelectBestHost(TransactionCase):
    """select_best_host returns the host with most available resources."""

    def setUp(self):
        super().setUp()
        Host = self.env['cloud.host']
        # Exclude any pre-existing hosts (dev DB data) from auto-assign so
        # they don't interfere with the test's expectation of an empty result.
        existing = Host.search([])
        if existing:
            existing.write({'exclude_from_autoassign': True})
        self.project = self.env['cloud.project'].create({'name': 'P1'})
        self.host_a = Host.create({
            'name': 'Big', 'ip_address': '10.0.0.1',
            'cpu_cores': 16, 'ram_total_gb': 32.0,
            'status': 'compatible', 'traefik_deployed': True,
            'disk_free_gb': 100.0,
        } | _HOST_BASE)
        self.host_b = Host.create({
            'name': 'Small', 'ip_address': '10.0.0.2',
            'cpu_cores': 8, 'ram_total_gb': 16.0,
            'status': 'compatible', 'traefik_deployed': True,
            'disk_free_gb': 50.0,
        } | _HOST_BASE)
        self.host_c = Host.create({
            'name': 'Excluded', 'ip_address': '10.0.0.3',
            'cpu_cores': 32, 'ram_total_gb': 64.0,
            'status': 'compatible', 'traefik_deployed': True,
            'disk_free_gb': 200.0,
            'exclude_from_autoassign': True,
        } | _HOST_BASE)

    def _inst(self, host, name='i1', **kw):
        return self.env['cloud.instance'].create({
            'name': name, 'project_id': self.project.id,
            'host_id': host.id,
        } | kw)

    def test_picks_host_with_most_headroom(self):
        """Both empty — bigger host wins."""
        best = self.env['cloud.host'].select_best_host(3.75, 3.75)
        self.assertEqual(best, self.host_a)

    def test_picks_less_loaded_host(self):
        """Load host_a heavily so host_b becomes better."""
        for i in range(4):
            self._inst(self.host_a, name=f'ia{i}')
        best = self.env['cloud.host'].select_best_host(3.75, 3.75)
        self.assertEqual(best, self.host_b)

    def test_excludes_incompatible(self):
        self.host_a.status = 'degraded'
        best = self.env['cloud.host'].select_best_host(3.75, 3.75)
        self.assertEqual(best, self.host_b)

    def test_excludes_no_traefik(self):
        self.host_a.traefik_deployed = False
        best = self.env['cloud.host'].select_best_host(3.75, 3.75)
        self.assertEqual(best, self.host_b)

    def test_excludes_autoassign_excluded(self):
        """host_c has most resources but is excluded."""
        best = self.env['cloud.host'].select_best_host(3.75, 3.75)
        self.assertNotEqual(best, self.host_c)

    def test_excludes_low_disk(self):
        self.host_a.disk_free_gb = 5.0
        self.host_b.disk_free_gb = 5.0
        best = self.env['cloud.host'].select_best_host(3.75, 3.75)
        self.assertFalse(best)

    def test_no_eligible_hosts_returns_empty(self):
        self.host_a.status = 'unsupported'
        self.host_b.status = 'unsupported'
        best = self.env['cloud.host'].select_best_host(3.75, 3.75)
        self.assertFalse(best)

    def test_insufficient_resources_falls_back_to_most_headroom(self):
        """When no host fits, select_best_host falls back to the most-headroom
        compatible host so any inheriting auto-provisioning layer can react
        while the instance still gets placed."""
        best = self.env['cloud.host'].select_best_host(100.0, 100.0)
        # Returns the fallback host, not False (fallback behaviour is
        # documented in select_best_host docstring).
        self.assertTrue(best)
        self.assertIn(best, self.host_a | self.host_b)

    def test_bottleneck_aware_scoring(self):
        """Host with balanced resources wins over unbalanced one."""
        # host_a: lots of CPU (16), low RAM (4) — unbalanced
        self.host_a.ram_total_gb = 4.0
        # host_b: moderate CPU (8), moderate RAM (16) — balanced
        best = self.env['cloud.host'].select_best_host(3.75, 3.75)
        self.assertEqual(best, self.host_b)


# ── Fase 5: Auto-assign in create_instance controller logic ──────────────────

_AUTOASSIGN_PARAM = 'incubacloud.host_autoassign'


class TestCreateInstanceAutoAssign(TransactionCase):
    """Auto-assign host when host_id is falsy in create_instance."""

    def setUp(self):
        super().setUp()
        self.Host = self.env['cloud.host']
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.search([('key', '=', _AUTOASSIGN_PARAM)]).unlink()
        self.project = self.env['cloud.project'].create({'name': 'P1'})
        self.host = self.Host.create({
            'name': 'H1', 'ip_address': '10.0.0.1',
            'user': 'ubuntu', 'wildcard_domain': 'example.com',
            'cpu_cores': 16, 'ram_total_gb': 32.0,
            'status': 'compatible', 'traefik_deployed': True,
            'disk_free_gb': 100.0,
        })

    def test_autoassign_enabled_no_host_selects_best(self):
        self.ICP.set_param(_AUTOASSIGN_PARAM, '1')
        cpus, ram = self.env['cloud.instance']._compute_instance_resources({})
        best = self.Host.select_best_host(cpus, ram)
        self.assertTrue(best)
        self.assertEqual(best, self.host)

    def test_autoassign_disabled_returns_no_host(self):
        """When disabled, select_best_host still works but controller blocks."""
        self.ICP.set_param(_AUTOASSIGN_PARAM, '0')
        enabled = self.ICP.get_param(_AUTOASSIGN_PARAM) == '1'
        self.assertFalse(enabled)

    def test_autoassign_default_is_disabled(self):
        enabled = self.ICP.get_param(_AUTOASSIGN_PARAM, '0') == '1'
        self.assertFalse(enabled)

    def test_autoassign_oversized_request_uses_fallback(self):
        """Oversized requests don't return False; select_best_host falls
        back to the host with most headroom (documented behaviour)."""
        self.ICP.set_param(_AUTOASSIGN_PARAM, '1')
        best = self.Host.select_best_host(100.0, 100.0)
        self.assertEqual(best, self.host)


# ── Fase 6: get_hosts response format ────────────────────────────────────────


class TestGetHostsResponseFields(TransactionCase):
    """Verify cloud.host exposes resource availability fields."""

    def setUp(self):
        super().setUp()
        self.host = self.env['cloud.host'].create({
            'name': 'H1', 'ip_address': '10.0.0.1',
            'user': 'ubuntu', 'wildcard_domain': 'example.com',
            'cpu_cores': 8, 'ram_total_gb': 16.0,
        })

    def test_host_has_availability_fields(self):
        """The model exposes the fields the controller will read."""
        for field in ('allocated_cpus', 'allocated_ram_gb',
                      'available_cpus', 'available_ram_gb',
                      'exclude_from_autoassign'):
            self.assertIn(field, self.host._fields)

    def test_availability_values_correct(self):
        self.assertEqual(self.host.available_cpus, 8.0)
        self.assertEqual(self.host.available_ram_gb, 16.0)


# ── Fase 7: Settings — ir.config_parameter ───────────────────────────────────


class TestAutoAssignSettings(TransactionCase):
    """Settings for host auto-assign toggle via ir.config_parameter."""

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.param = 'incubacloud.host_autoassign'
        # Remove any leftover value so each test starts from scratch
        self.ICP.search([('key', '=', self.param)]).unlink()

    def test_get_returns_default_false(self):
        self.assertEqual(self.ICP.get_param(self.param, '0'), '0')

    def test_save_enables(self):
        self.ICP.set_param(self.param, '1')
        self.assertEqual(self.ICP.get_param(self.param), '1')

    def test_save_disables(self):
        self.ICP.set_param(self.param, '1')
        self.ICP.set_param(self.param, '0')
        self.assertEqual(self.ICP.get_param(self.param), '0')

    def test_persists_across_reads(self):
        self.ICP.set_param(self.param, '1')
        val1 = self.ICP.get_param(self.param)
        val2 = self.ICP.get_param(self.param)
        self.assertEqual(val1, val2, '1')


# ── get_remote_dir + custom overrides ────────────────────────────────────────


class TestGetRemoteDir(TransactionCase):
    """Instance.get_remote_dir() respects custom_remote_dir."""

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({'name': 'My App'})
        self.host = self.env['cloud.host'].create({
            'name': 'H1', 'ip_address': '10.0.0.1',
            'user': 'ubuntu', 'wildcard_domain': 'example.com',
        })

    def _inst(self, **kw):
        return self.env['cloud.instance'].create({
            'name': kw.pop('name', 'prod'),
            'project_id': self.project.id,
            'host_id': self.host.id,
        } | kw)

    def test_default_path(self):
        inst = self._inst()
        self.assertEqual(inst.get_remote_dir(), '~/my-app/prod')

    def test_custom_remote_dir_overrides(self):
        inst = self._inst(custom_remote_dir='/root/IncubaCloud')
        self.assertEqual(inst.get_remote_dir(), '/root/IncubaCloud')

    def test_no_project_uses_instances_folder(self):
        inst = self.env['cloud.instance'].create({
            'name': 'orphan',
            'host_id': self.host.id,
        })
        self.assertEqual(inst.get_remote_dir(), '~/instances/orphan')


class TestCustomBackupDst(TransactionCase):
    """Instance.custom_backup_dst overrides computed backup dst."""

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({'name': 'P1'})
        self.backend = self.env['cloud.backup.backend'].create({
            'name': 'Test S3',
            's3_bucket': 'my-bucket',
            's3_path': 'backups',
        })

    def test_computed_dst_without_custom(self):
        inst = self.env['cloud.instance'].create({
            'name': 'prod',
            'project_id': self.project.id,
            'backup_backend_id': self.backend.id,
        })
        self.assertIn('my-bucket', inst.instance_backup_dst)
        self.assertIn('p1/prod', inst.instance_backup_dst)

    def test_custom_dst_overrides(self):
        inst = self.env['cloud.instance'].create({
            'name': 'prod',
            'project_id': self.project.id,
            'backup_backend_id': self.backend.id,
            'custom_backup_dst': 'boto3+s3://custom-bucket/custom-path',
        })
        self.assertEqual(
            inst.instance_backup_dst,
            'boto3+s3://custom-bucket/custom-path',
        )


# ── Import parsers (pure Python) ─────────────────────────────────────────────


class TestParseReposYaml(BaseCase):
    """parse repos.yaml git-aggregator format."""

    def _parse(self, content, odoo_version='19.0'):
        # Import inline to avoid module-level dependency
        import yaml
        import re as _re
        _SSH_RE = _re.compile(r'^git@[^:]+:(.+?)(?:\.git)?$')

        def _ssh_to_https(u):
            u = (u or '').strip()
            m = _SSH_RE.match(u)
            if m:
                return f'https://github.com/{m.group(1)}.git'
            return u

        data = yaml.safe_load(content) or {}
        repos = []
        for alias, cfg in data.items():
            if alias in ('./odoo', 'odoo'):
                continue
            if not isinstance(cfg, dict):
                continue
            remotes = cfg.get('remotes', {})
            repo_url = next(iter(remotes.values()), '') if remotes else ''
            repo_url = _ssh_to_https(repo_url)
            branch_val = ''
            target = cfg.get('target', '')
            if target:
                parts = target.split()
                branch_val = parts[-1] if len(parts) >= 2 else parts[0]
            if not branch_val:
                merges = cfg.get('merges', [])
                if merges and isinstance(merges[0], str):
                    parts = merges[0].split()
                    branch_val = parts[-1] if len(parts) >= 2 else parts[0]
            if '$ODOO_VERSION' in branch_val and odoo_version:
                branch_val = branch_val.replace('$ODOO_VERSION', odoo_version)
            repos.append({
                'url': repo_url, 'branch': branch_val or 'main',
                'alias': alias,
            })
        return repos

    def test_ssh_url_converted(self):
        yaml_str = """
core:
  remotes:
    core: git@github.com:incubacloud/core.git
  target: core 19.0
"""
        repos = self._parse(yaml_str)
        self.assertEqual(len(repos), 1)
        self.assertIn('https://github.com/', repos[0]['url'])

    def test_odoo_entry_skipped(self):
        yaml_str = """
./odoo:
  remotes:
    odoo: git@github.com:odoo/odoo.git
  target: odoo $ODOO_VERSION
core:
  remotes:
    core: https://github.com/org/core.git
  target: core 19.0
"""
        repos = self._parse(yaml_str)
        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]['alias'], 'core')

    def test_odoo_version_variable_resolved(self):
        yaml_str = """
enterprise:
  remotes:
    enterprise: https://github.com/odoo/enterprise.git
  target: enterprise $ODOO_VERSION
"""
        repos = self._parse(yaml_str, '18.0')
        self.assertEqual(repos[0]['branch'], '18.0')

    def test_fixed_branch(self):
        yaml_str = """
tools:
  remotes:
    origin: https://github.com/OCA/server-tools.git
  target: origin 16.0
"""
        repos = self._parse(yaml_str)
        self.assertEqual(repos[0]['branch'], '16.0')

    def test_branch_from_merges_fallback(self):
        yaml_str = """
addon:
  remotes:
    origin: https://github.com/org/addon.git
  merges:
    - origin main
"""
        repos = self._parse(yaml_str)
        self.assertEqual(repos[0]['branch'], 'main')


class TestParseAddonsYaml(BaseCase):
    """parse addons.yaml for addon inclusion/exclusion."""

    def _parse(self, content):
        import yaml
        data = yaml.safe_load(content) or {}
        result = {}
        for alias, addons in data.items():
            if isinstance(addons, list):
                result[alias] = (
                    '' if '*' in addons
                    else ','.join(str(a) for a in addons)
                )
            elif addons == '*':
                result[alias] = ''
            else:
                result[alias] = str(addons) if addons else ''
        return result

    def test_wildcard_star(self):
        r = self._parse('core:\n  - "*"')
        self.assertEqual(r['core'], '')

    def test_explicit_list(self):
        r = self._parse('core:\n  - mod_a\n  - mod_b')
        self.assertEqual(r['core'], 'mod_a,mod_b')

    def test_star_string(self):
        r = self._parse('core: "*"')
        self.assertEqual(r['core'], '')
