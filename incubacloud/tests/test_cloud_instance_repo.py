"""
Tier 2 — ORM integration tests for cloud.instance.repo.

Tests constraint validation, addons/excludes overlap, and
requirements sync behavior.
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestInstanceRepoConstraints(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({'name': 'RepoProj'})
        self.inst = self.env['cloud.instance'].create({
            'name': 'repo-test',
            'project_id': self.project.id,
            'environment': 'staging',
        })

    def _repo(self, **kw):
        base = {
            'instance_id': self.inst.id,
            'url': 'https://github.com/OCA/web.git',
            'branch': 'main',
        }
        return self.env['cloud.instance.repo'].with_context(
            skip_apply_requirements=True,
        ).create(base | kw)

    def test_create_basic_repo(self):
        repo = self._repo()
        self.assertTrue(repo.id)
        self.assertEqual(repo.branch, 'main')

    def test_addons_excludes_no_overlap_ok(self):
        repo = self._repo(
            addons='web, sale_order',
            excludes='purchase',
        )
        self.assertTrue(repo.id)

    def test_addons_excludes_overlap_raises(self):
        with self.assertRaises(ValidationError):
            self._repo(
                addons='web, sale_order',
                excludes='sale_order, purchase',
            )

    def test_empty_addons_empty_excludes_ok(self):
        repo = self._repo(addons='', excludes='')
        self.assertTrue(repo.id)

    def test_commit_sha_stored(self):
        repo = self._repo(commit_sha='abc123def')
        self.assertEqual(repo.commit_sha, 'abc123def')

    def test_sequence_default(self):
        repo = self._repo()
        self.assertEqual(repo.sequence, 10)

    def test_cascade_delete_with_instance(self):
        repo = self._repo()
        rid = repo.id
        self.inst.unlink()
        self.assertFalse(
            self.env['cloud.instance.repo'].browse(rid).exists()
        )


class TestInstanceRepoOrdering(TransactionCase):

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({'name': 'RepoOrd'})
        self.inst = self.env['cloud.instance'].create({
            'name': 'ord-test',
            'project_id': self.project.id,
            'environment': 'staging',
        })

    def test_ordered_by_sequence(self):
        Repo = self.env['cloud.instance.repo'].with_context(
            skip_apply_requirements=True,
        )
        Repo.create({
            'instance_id': self.inst.id,
            'url': 'https://github.com/OCA/web.git',
            'sequence': 30,
        })
        Repo.create({
            'instance_id': self.inst.id,
            'url': 'https://github.com/OCA/server-ux.git',
            'sequence': 10,
        })
        repos = Repo.search([('instance_id', '=', self.inst.id)])
        urls = [r.url for r in repos]
        self.assertEqual(urls[0], 'https://github.com/OCA/server-ux.git')
        self.assertEqual(urls[1], 'https://github.com/OCA/web.git')


class TestInstanceRepoInheritance(TransactionCase):
    """Test that project repos are inherited by new instances."""

    def setUp(self):
        super().setUp()
        self.project = self.env['cloud.project'].create({'name': 'InhProj'})
        self.env['cloud.project.repo'].with_context(
            skip_apply_requirements=True,
        ).create({
            'project_id': self.project.id,
            'url': 'https://github.com/OCA/web.git',
            'branch': '18.0',
            'commit_sha': 'abc1234',
        })

    def test_instance_inherits_project_repos(self):
        inst = self.env['cloud.instance'].create({
            'name': 'inherit-test',
            'project_id': self.project.id,
            'environment': 'staging',
        })
        self.assertEqual(len(inst.repo_ids), 1)
        repo = inst.repo_ids
        self.assertEqual(repo.url, 'https://github.com/OCA/web.git')
        self.assertEqual(repo.branch, '18.0')
        self.assertEqual(repo.commit_sha, 'abc1234')
