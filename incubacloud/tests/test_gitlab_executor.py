"""Deploy and rebuild with GitLab repositories (plan-gitlab.md §4.4, J4, J5).

- ``repos.yaml`` carries each server's own credential and never another's;
- the heads spec has seven columns for a GitLab line (``cred_authority``
  always filled, so ``git-trust`` tells it from GitHub);
- 11.0 to 19.0 trust the server in the build (``git-trust``) and, under
  the ``public`` policy, pin its address with ``build.extra_hosts``;
- 8.0 to 10.0 fetch every repository before the build (``fetch-repos``,
  with a step timeout of N × 600 s and more), write ``repos.yaml``
  without credentials and build with ``AGGREGATE=false``;
- the pinned address is resolved once per job and never makes the
  instance look changed;
- J5: a GitLab the panel cannot reach is tolerated only when its
  requirements were read before.
"""
import asyncio
import shlex
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.models import abstract_executor
from odoo.addons.incubacloud.models import deploy_instance_executor as deploy_mod
from odoo.addons.incubacloud.models.abstract_executor import JobRefused
from odoo.addons.incubacloud.models.transport import CommandResult, SSHTransport
from odoo.addons.incubacloud.net import git_reader
from odoo.addons.incubacloud.tests import _certs
from odoo.addons.incubacloud.tests._template import pin_template
from odoo.addons.incubacloud.tests.test_executor_features import (
    _find_cmd,
    _make_deploy_executor,
    _make_rebuild_executor,
)

_GH_TOKEN = "ghs_githubtoken123"
_GL_USER = "gitlab+deploy-token-7"
_GL_TOKEN = "gldt-abcdefgh12"
_PIN = "203.0.113.7"


class _GitLabExecutorCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env["cloud.git.provider"].create({
            "name": "GitLab.com", "base_url": "https://gitlab.com",
        })
        cls.env["cloud.git.credential"].create({
            "provider_id": cls.provider.id, "namespace": "acme",
            "kind": "deploy", "username": _GL_USER, "token": _GL_TOKEN,
        })

    def _repos(self):
        """One GitHub line and one GitLab line, as the executor reads them.

        :rtype: list
        """
        return [
            SimpleNamespace(
                url="https://github.com/acme/gh-addons", branch="main",
                commit_sha=False, provider_id=self.env["cloud.git.provider"],
            ),
            SimpleNamespace(
                url="https://gitlab.com/acme/addons.git", branch="main",
                commit_sha=False, provider_id=self.provider,
            ),
        ]

    def _ready(self, ex, version="19.0"):
        """Give *ex* the test repositories and render its files.

        :return: ``(repos_yaml, pinned)``
        :rtype: tuple
        """
        inst = ex._inst()
        inst.odoo_version = version
        inst.odoo_commit_sha = False
        inst.repo_ids = self._repos()
        pinned = set()
        with patch.object(
            ex, "_get_github_tokens", return_value=(_GH_TOKEN, ""),
        ), patch.object(ex, "_get_token_for_repo", return_value=_GH_TOKEN):
            text = ex._repos_yaml_content(pinned)
        return text, pinned

    def _public(self):
        """Patch the ``public`` policy, answering the pinned address.

        :return: the ``_resolve_pin`` mock
        """
        Provider = type(self.env["cloud.git.provider"])
        policy = patch.object(Provider, "_network_policy", return_value="public")
        pin = patch.object(Provider, "_resolve_pin", return_value=_PIN)
        policy.start()
        self.addCleanup(policy.stop)
        resolved = pin.start()
        self.addCleanup(pin.stop)
        return resolved


class TestReposYamlAndSpec(_GitLabExecutorCase):

    def test_a_server_off_the_public_policy_refuses_the_job_with_its_cause(self):
        # Rendered outside any ``try`` when the resync of requirements is
        # off: a UserError there failed the job as an unexpected error
        # (Fable, 8-nov-2026).
        Provider = type(self.env["cloud.git.provider"])
        message = "gitlab.com must resolve to public addresses only"
        with patch.object(Provider, "_network_policy", return_value="public"), \
                patch.object(Provider, "_resolve_pin", side_effect=UserError(message)), \
                self.assertRaises(JobRefused) as caught:
            self._ready(_make_deploy_executor())
        self.assertIn(message, str(caught.exception))

    def test_each_server_gets_its_own_credential_with_git_suffix(self):
        text, _unused = self._ready(_make_deploy_executor())
        remotes = {
            alias: next(iter(entry["remotes"].values()))
            for alias, entry in yaml.safe_load(text).items()
        }
        self.assertEqual(
            remotes["addons"],
            f"https://{_GL_USER}:{_GL_TOKEN}@gitlab.com/acme/addons.git",
        )
        self.assertIn(f"x-access-token:{_GH_TOKEN}@github.com", remotes["gh-addons"])
        self.assertNotIn(_GH_TOKEN, remotes["addons"])
        self.assertNotIn(_GL_TOKEN, remotes["gh-addons"])

    def test_a_gitlab_line_has_seven_columns_and_github_four(self):
        ex = _make_deploy_executor()
        text, pinned = self._ready(ex)
        lines = ex._repo_heads_spec(text, pinned).splitlines()
        self.assertEqual(len(lines[1].split("\t")), 4)
        self.assertEqual(lines[2].split("\t")[4:], ["gitlab.com", "", ""])

    def test_a_ca_travels_as_a_private_file_named_in_the_spec(self):
        pem = _certs.ca_pem()
        self.provider.write({"ca_bundle": pem})
        ex = _make_deploy_executor()
        text, pinned = self._ready(ex)
        ca_file = ex._repo_heads_spec(text, pinned).splitlines()[2].split("\t")[5]
        self.assertEqual(ca_file, ex._tmp(f"git-ca-{self.provider.id}.pem"))
        self.assertEqual(ex._git_private_files(), {ca_file: self.provider.ca_bundle})

    def test_the_public_policy_pins_once_in_the_spec_and_the_build(self):
        resolved = self._public()
        ex = _make_deploy_executor()
        text, pinned = self._ready(ex)
        spec = ex._repo_heads_spec(text, pinned).splitlines()
        self.assertEqual(spec[2].split("\t")[6], _PIN)
        self.assertEqual(
            ex._odoo_build_override(),
            {"build": {"extra_hosts": [f"gitlab.com:{_PIN}"]}},
        )
        self.assertEqual(resolved.call_count, 1)
        heads = _find_cmd(ex.get_commands(), "Record repository heads")[1]
        self.assertIn(deploy_mod.GIT_HELPER_IMAGE, shlex.split(heads))

    def test_without_gitlab_nothing_is_pinned_and_the_override_has_no_build(self):
        self._public()
        ex = _make_deploy_executor()
        inst = ex._inst()
        inst.odoo_commit_sha = False
        inst.repo_ids = self._repos()[:1]
        with patch.object(
            ex, "_get_github_tokens", return_value=("", ""),
        ), patch.object(ex, "_get_token_for_repo", return_value=None):
            ex._repos_yaml_content(set())
        self.assertEqual(ex._odoo_build_override(), {})


class TestTheSeriesThatFetchFirst(_GitLabExecutorCase):

    def test_8_0_fetches_every_repository_before_the_build(self):
        for version in ("8.0", "9.0", "10.0"):
            with self.subTest(version=version):
                ex = _make_deploy_executor(odoo_version=version)
                text, _unused = self._ready(ex, version)
                stripped = ex._strip_repo_credentials(text)
                self.assertNotIn("@", stripped)
                self.assertNotIn(_GL_TOKEN, stripped)
                cmds = ex.get_commands()
                labels = [c[0] for c in cmds]
                self.assertNotIn("Record repository heads", labels)
                self.assertNotIn("Trust the Git servers", labels)
                fetch = _find_cmd(cmds, "Fetch the repositories")
                argv = shlex.split(fetch[1])
                self.assertEqual(argv[2], "fetch-repos")
                self.assertEqual(argv[5:], [deploy_mod.GIT_HELPER_IMAGE, "60"])
                self.assertEqual(
                    fetch[2]["timeout"], deploy_mod._fetch_timeout(3),
                )
                self.assertLess(
                    labels.index("Fetch the repositories"),
                    labels.index("Initialize database"),
                )
                self.assertEqual(
                    ex._odoo_build_override(),
                    {"build": {"args": {"AGGREGATE": "false"}}},
                )

    def test_a_pin_carries_its_url_when_the_series_fetches_first(self):
        ex = _make_deploy_executor(odoo_version="8.0")
        text, _unused = self._ready(ex, "8.0")
        spec = ex._repo_heads_spec(text, {"addons"}).splitlines()
        self.assertEqual(spec[2].split("\t")[1], "pin")
        self.assertTrue(spec[2].split("\t")[3].endswith("gitlab.com/acme/addons.git"))

    def test_19_0_trusts_the_servers_and_records_the_heads(self):
        ex = _make_deploy_executor()
        self._ready(ex)
        cmds = ex.get_commands()
        labels = [c[0] for c in cmds]
        self.assertIn("Record repository heads", labels)
        trust = _find_cmd(cmds, "Trust the Git servers")[1]
        self.assertEqual(shlex.split(trust)[2], "git-trust")
        self.assertNotIn("Fetch the repositories", labels)

    def test_the_fetch_budget_covers_each_download_and_its_retry(self):
        self.assertEqual(deploy_mod._fetch_timeout(3), 3 * (2 * 600 + 60))
        self.assertGreater(deploy_mod._fetch_timeout(4), deploy_mod._fetch_timeout(3))

    def test_a_rebuild_of_8_0_fetches_in_its_own_step_before_the_build(self):
        # One step for both had the transport's TERM kill the outer
        # ``bash -c`` and leave deploy.sh running (Fable, 8-nov-2026).
        ex = _make_rebuild_executor(odoo_version="8.0")
        self._ready(ex, "8.0")
        cmds = ex.get_commands()
        labels = [c[0] for c in cmds]
        _label, fetch, fetch_opts = _find_cmd(cmds, "Fetch the repositories")
        self.assertEqual(shlex.split(fetch)[2], "fetch-repos")
        self.assertNotIn("docker compose build", fetch)
        self.assertEqual(fetch_opts["timeout"], deploy_mod._fetch_timeout(3))
        self.assertTrue(fetch_opts["stop_on_failure"])
        build = _find_cmd(cmds, "Rebuild Odoo image")
        self.assertEqual(labels.index("Fetch the repositories") + 1,
                         labels.index("Rebuild Odoo image"))
        self.assertTrue(build[1].startswith(f"cd {ex._inst_dir(ex._inst())} && "))
        self.assertNotIn("fetch-repos", build[1])
        self.assertNotIn("record-heads", build[1])
        self.assertEqual(build[2], {"stop_on_failure": True})

    def test_a_rebuild_of_19_0_records_the_heads_in_its_build_step(self):
        ex = _make_rebuild_executor()
        self._ready(ex)
        cmds = ex.get_commands()
        self.assertNotIn("Fetch the repositories", [c[0] for c in cmds])
        build = _find_cmd(cmds, "Rebuild Odoo image")
        self.assertIn("record-heads", build[1])
        self.assertEqual(build[2], {"stop_on_failure": True})


class TestStepTimeout(TransactionCase):

    def test_a_step_timeout_reaches_the_transport(self):
        ex = _make_deploy_executor()
        ex.on_stdout = ex.on_stderr = None
        transport = MagicMock(spec=SSHTransport)
        transport.execute.return_value = CommandResult(stdout="", exit_status=0)
        with patch.object(type(ex), "_sys", autospec=True):
            asyncio.run(abstract_executor.AbstractSSHExecutor._run_steps(
                ex, transport, [("A", "true", {"timeout": 7200}), ("B", "true")],
            ))
        first, second = transport.execute.call_args_list
        self.assertEqual(first.kwargs["timeout"], 7200)
        self.assertNotIn("timeout", second.kwargs)


class TestThePinNeverChangesTheInstance(_GitLabExecutorCase):
    """The override carries the build's pin; the instance never sees it."""

    def setUp(self):
        super().setUp()
        pin_template(self.env)
        project = self.env["cloud.project"].create({"name": "Pin"})
        self.host = self.env["cloud.host"].create({
            "name": "pin-host", "ip_address": "192.0.2.63", "user": "ubuntu",
            "wildcard_domain": "pin.example.com",
        })
        self.instance = self.env["cloud.instance"].create({
            "name": "pininst", "project_id": project.id,
            "environment": "production", "host_id": self.host.id,
        })
        self.env["cloud.instance.repo"].with_context(
            skip_apply_requirements=True,
        ).create({
            "instance_id": self.instance.id,
            "url": "https://gitlab.com/acme/addons", "branch": "19.0",
        })

    def _override(self):
        """Render the instance's repos.yaml and override; return the odoo service.

        :rtype: dict
        """
        job = self.env["cloud.job"].create({
            "name": "Pin deploy", "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.env["cloud.job.type"].search(
                [("code", "=", "deploy_instance")], limit=1,
            ).id,
        })
        ex = deploy_mod.DeployInstanceExecutor(job, self.host)
        with patch.object(ex, "_get_github_tokens", return_value=("", "")):
            ex._repos_yaml_content(set())
        return yaml.safe_load(ex._resource_override_content())["services"]["odoo"]

    def test_fingerprint_and_snapshot_ignore_the_pin_and_aggregate(self):
        before = (
            self.instance.rebuild_fingerprint,
            self.instance._config_snapshot_hash(),
        )
        resolved = self._public()
        for address in ("203.0.113.7", "198.51.100.9"):
            resolved.return_value = address
            odoo = self._override()
            self.assertEqual(odoo["build"], {"extra_hosts": [f"gitlab.com:{address}"]})
            self.instance.invalidate_recordset()
            self.assertEqual(
                (self.instance.rebuild_fingerprint,
                 self.instance._config_snapshot_hash()),
                before,
            )


class TestRequirementsOfAnUnreachableGitLab(_GitLabExecutorCase):
    """J5 (§4.4-7, Fable's point 7)."""

    def setUp(self):
        super().setUp()
        # The job's durable cursor rides on the test's transaction.
        self.registry_enter_test_mode()
        project = self.env["cloud.project"].create({"name": "J5"})
        self.instance = self.env["cloud.instance"].create({
            "name": "j5-inst", "project_id": project.id,
            "environment": "staging", "state": "draft",
        })
        self.line = self.env["cloud.instance.repo"].with_context(
            skip_apply_requirements=True,
        ).create({
            "instance_id": self.instance.id,
            "url": "https://gitlab.com/acme/addons", "branch": "19.0",
        })
        ex = deploy_mod.DeployInstanceExecutor.__new__(
            deploy_mod.DeployInstanceExecutor,
        )
        ex.env = self.env
        ex._log_buffer = []
        job = MagicMock(spec=type(self.env["cloud.job"]))
        job.id = 1
        job.instance_id = self.instance
        job.env = self.env
        ex.job = job
        self.executor = ex
        # The durable cursor reads the database, not this env's cache.
        self.env.flush_all()

    def _resync(self, outcome):
        """Run the re-sync with the GitLab read answering *outcome*."""
        kwargs = (
            {"side_effect": outcome} if isinstance(outcome, Exception)
            else {"return_value": {"requirements.txt": outcome}}
        )
        self.env.flush_all()
        with patch.object(
            git_reader.GitReader, "read_files", autospec=True, **kwargs,
        ), patch.object(
            deploy_mod, "resolve_github_client", return_value=None,
        ):
            asyncio.run(self.executor._resync_repo_requirements())

    def _alerts(self):
        """Return the open ``gitlab_unreachable`` alerts.

        :rtype: cloud.alert
        """
        self.env.invalidate_all()
        return self.env["cloud.alert"].sudo().search([
            ("code", "=", "gitlab_unreachable"), ("state", "=", "active"),
        ])

    def test_never_read_and_unreachable_fails(self):
        with self.assertRaisesRegex(JobRefused, "never read the requirements"):
            self._resync(git_reader.GitReaderError("unreachable"))

    def test_read_before_and_unreachable_goes_on_with_an_alert(self):
        self.line.sudo().write({"requirements_read_at": fields.Datetime.now()})
        self._resync(git_reader.GitReaderError("timeout"))
        log = " ".join(line for line, _src in self.executor._log_buffer)
        self.assertIn("building with the last ones known", log)
        self.assertEqual(len(self._alerts()), 1)

    def test_a_correct_read_stamps_the_line_and_closes_the_alert(self):
        self.env["cloud.alert"].sudo().raise_alert(
            "gitlab_unreachable", "unreachable",
            key=f"git_provider:{self.provider.id}",
        )
        self.assertEqual(len(self._alerts()), 1)
        self._resync(None)
        self.env.invalidate_all()
        self.assertTrue(self.line.requirements_read_at)
        self.assertFalse(self._alerts())

    def test_a_private_address_fails(self):
        self.line.sudo().write({"requirements_read_at": fields.Datetime.now()})
        with self.assertRaises(JobRefused):
            self._resync(git_reader.GitReaderError("private_address"))
