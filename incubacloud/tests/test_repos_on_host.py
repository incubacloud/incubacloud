"""The repositories are fetched on the host, never cloned in the image.

Every series downloads each repository of ``repos.yaml`` on the server,
in a helper container, before the image is built with ``AGGREGATE=false``
(``deploy.sh fetch-repos``): all but 19.0 from core 1.0.222, 19.0 too
from 1.0.224. The token of a private repository then enters neither the
image nor its build nor the instance's container, and the ``repos.yaml``
written on the server has no credential. The system parameter
``incubacloud.repos_in_image_series`` sends a series back to cloning in
the image, except 8.0 to 10.0, whose image git is not safe.

What ``deploy.sh`` does with the spec is covered by
``tests/shell/deploy.bats``.
"""
import asyncio
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from odoo.addons.incubacloud.models import _odoo_versions as versions
from odoo.addons.incubacloud.models import deploy_instance_executor as deploy_mod
from odoo.addons.incubacloud.models.abstract_executor import JobRefused
from odoo.addons.incubacloud.models.transport import SSHTransport
from odoo.addons.incubacloud.tests.test_executor_features import (
    _find_cmd,
    _make_deploy_executor,
    _make_rebuild_executor,
)
from odoo.tests.common import BaseCase

_TOKEN = "ghs_scopedTokenOfTheJob"
_PINNED = "0123456789abcdef0123456789abcdef01234567"
_FETCH = "Fetch the repositories"


def _repos():
    """One branch line and one pinned line of a private GitHub repository.

    :rtype: list
    """
    return [
        SimpleNamespace(url="https://github.com/acme/addons", branch="main",
                        commit_sha=False),
        SimpleNamespace(url="https://github.com/acme/fixed", branch="main",
                        commit_sha=_PINNED),
    ]


def _render(ex):
    """Render *ex*'s repos.yaml with GitHub reading every line as private.

    :return: ``(text, pinned)``
    :rtype: tuple
    """
    inst = ex._inst()
    inst.odoo_commit_sha = False
    inst.repo_ids = _repos()
    pinned = set()
    with patch.object(ex, "_get_github_tokens", return_value=("app", "")), \
            patch.object(ex, "_github_repo_access", return_value=(5, True, "app")), \
            patch.object(ex, "_scoped_github_token", return_value=_TOKEN):
        text = ex._repos_yaml_content(pinned)
    return text, pinned


class TestEverySeriesFetchesOnTheHost(BaseCase):

    def test_every_card_fetches(self):
        """No series clones in the image by default, 19.0 included."""
        self.assertEqual(
            [v for v in versions.VERSIONS if not versions.prefetch_repos(v)],
            [],
        )

    def test_only_8_0_to_10_0_may_not_go_back_to_the_image(self):
        """Their git 2.1.4 sends a token in clear after a redirect."""
        self.assertEqual(
            [v for v in versions.VERSIONS if not versions.image_git_safe(v)],
            ["8.0", "9.0", "10.0"],
        )

    def test_a_deploy_fetches_and_neither_records_heads_nor_trusts_servers(self):
        """The deploy's steps, with a fixed timeout and the build after."""
        for version in ("11.0", "18.0", "19.0", "20.0"):
            with self.subTest(version=version):
                ex = _make_deploy_executor(odoo_version=version)
                cmds = ex.get_commands()
                labels = [c[0] for c in cmds]
                self.assertNotIn("Record repository heads", labels)
                self.assertNotIn("Trust the Git servers", labels)
                _label, command, opts = _find_cmd(cmds, _FETCH)
                # Run apart (phase 3, 12-nov-2026): the same step as the
                # rebuild's, from the same helper.
                self.assertEqual(
                    opts,
                    {"stop_on_failure": True, "timeout": 1260, "detached": True},
                )
                argv = shlex.split(command)
                self.assertEqual(argv[2:5], [
                    "fetch-repos", "~/projects/test-inst", ex._tmp("repo-heads.tsv"),
                ])
                self.assertEqual(
                    argv[5:], [deploy_mod.GIT_HELPER_IMAGE, version, "60", "1200"],
                )
                self.assertLess(labels.index("Write repos.yaml"), labels.index(_FETCH))
                self.assertLess(labels.index(_FETCH), labels.index("Build Odoo image"))
                build = _find_cmd(cmds, "Build Odoo image")[1]
                self.assertIn(deploy_mod._FETCH_SECONDS_FILE, build)
                self.assertEqual(
                    ex._odoo_build_override(),
                    {"build": {"args": {"AGGREGATE": "false"}}},
                )

    def test_a_rebuild_fetches_in_its_own_step_and_its_build_records_nothing(self):
        """Cached or not, the download is a step of its own before the build."""
        for version, last in (
            ("18.0", "fp1"), ("18.0", "fp0"), ("19.0", "fp1"), ("19.0", "fp0"),
        ):
            with self.subTest(version=version, last=last):
                ex = _make_rebuild_executor(
                    odoo_version=version, last_rebuild_fingerprint=last,
                )
                cmds = ex.get_commands()
                labels = [c[0] for c in cmds]
                self.assertNotIn("Trust the Git servers", labels)
                self.assertEqual(
                    labels.index(_FETCH) + 1, labels.index("Rebuild Odoo image"),
                )
                self.assertEqual(_find_cmd(cmds, _FETCH)[2]["timeout"], 1260)
                build = _find_cmd(cmds, "Rebuild Odoo image")[1]
                self.assertNotIn("record-heads", build)
                self.assertIn(deploy_mod._FETCH_SECONDS_FILE, build)

    def test_19_0_goes_back_to_the_image_with_the_parameter(self):
        """19.0 named in the parameter keeps the steps it had before 1.0.224."""
        for ex in (_make_deploy_executor(), _make_rebuild_executor()):
            with self.subTest(ex=type(ex).__name__):
                self.assertIn(_FETCH, [c[0] for c in ex.get_commands()])
                ex._repos_in_image_series = lambda: frozenset({"19.0"})
                cmds = ex.get_commands()
                self.assertNotIn(_FETCH, [c[0] for c in cmds])
                self.assertEqual(ex._odoo_build_override(), {})

    def test_the_parameter_sends_a_series_back_to_the_image_but_not_8_0(self):
        """``incubacloud.repos_in_image_series`` is the way back, not for 8.0."""
        ex = _make_deploy_executor(odoo_version="18.0")
        ex._repos_in_image_series = lambda: frozenset({"18.0", "8.0"})
        labels = [c[0] for c in ex.get_commands()]
        self.assertNotIn(_FETCH, labels)
        self.assertIn("Record repository heads", labels)
        self.assertIn("Trust the Git servers", labels)
        old = _make_deploy_executor(odoo_version="8.0")
        old._repos_in_image_series = lambda: frozenset({"18.0", "8.0"})
        self.assertIn(_FETCH, [c[0] for c in old.get_commands()])

    def test_the_parameter_is_read_as_a_list_of_series(self):
        """Comma separated, spaces and empty items ignored."""
        ex = _make_deploy_executor(odoo_version="18.0")
        del ex._repos_in_image_series
        params = MagicMock()
        params.sudo.return_value.get_param.return_value = " 17.0, 18.0 ,,"
        ex.env = {"ir.config_parameter": params}
        self.assertEqual(ex._repos_in_image_series(), frozenset({"17.0", "18.0"}))
        self.assertFalse(ex._prefetches())


class TestTheFilesAServerGets(BaseCase):

    def _uploaded(self, version, in_image=()):
        """Run ``_upload_copier_files`` for *version*; return what it uploads.

        :param tuple in_image: the series ``incubacloud.repos_in_image_series``
            names
        :return: ``(executor, private files, plain files)``, each
            ``{path: text}``
        :rtype: tuple
        """
        ex = _make_deploy_executor(odoo_version=version)
        ex._repos_in_image_series = lambda: frozenset(in_image)
        inst = ex._inst()
        inst.odoo_commit_sha = False
        inst.repo_ids = _repos()
        inst.pip_dependencies = inst.apt_dependencies = ""
        sent = {}

        async def keep(_transport, files):
            """Record *files*, as the upload would send them."""
            sent.setdefault("private" if not sent else "plain", files)

        ex._read_host_cpus = AsyncMock()
        ex._resync_repo_requirements = AsyncMock()
        ex._build_answers = dict
        ex._addons_yaml_content = AsyncMock(return_value="")
        ex._conf_content = str
        ex._backup_env_content = lambda: None
        ex._resource_override_content = lambda: None
        ex._copier_settings_content = lambda: None
        ex._upload_private_files = keep
        ex._upload_files = keep
        ex._make_modules_stage_dir = AsyncMock()
        ex._sys = lambda *_a, **_k: None
        with patch.object(ex, "_get_github_tokens", return_value=("app", "")), \
                patch.object(ex, "_github_repo_access", return_value=(5, True, "app")), \
                patch.object(ex, "_scoped_github_token", return_value=_TOKEN):
            asyncio.run(ex._upload_copier_files(MagicMock(spec=SSHTransport)))
        return ex, sent["private"], sent["plain"]

    def test_the_repos_yaml_has_no_credential_and_the_spec_carries_them(self):
        """What the host gets: credentials in the owner-only spec alone."""
        for version in ("18.0", "19.0"):
            with self.subTest(version=version):
                ex, private, _plain = self._uploaded(version)
                repos_yaml = private[ex._tmp("repos.yaml")]
                for alias, entry in yaml.safe_load(repos_yaml).items():
                    for url in entry["remotes"].values():
                        with self.subTest(alias=alias):
                            self.assertNotIn("@", url)
                self.assertNotIn(_TOKEN, repos_yaml)
                spec = private[ex._tmp("repo-heads.tsv")].splitlines()
                self.assertIn(
                    f"fixed\tpin\t{_PINNED}\thttps://x-access-token:{_TOKEN}@github.com/acme/fixed",
                    spec,
                )
                self.assertIn(
                    f"addons\tref\tmain\thttps://x-access-token:{_TOKEN}@github.com/acme/addons",
                    spec,
                )

    def test_19_0_sent_back_to_the_image_writes_the_scoped_token_in_repos_yaml(self):
        """The way back clones in the image, with the scoped token in its URL."""
        ex, private, _plain = self._uploaded("19.0", in_image=("19.0",))
        self.assertIn(_TOKEN, private[ex._tmp("repos.yaml")])

    def test_a_repos_yaml_entry_with_a_command_refuses_the_job(self):
        """git-aggregator runs a ``shell_command_after`` in the container that
        holds the credential: only the keys core renders are allowed."""
        ex = _make_deploy_executor(odoo_version="18.0")
        text, _pinned = _render(ex)
        data = yaml.safe_load(text)
        data["addons"]["shell_command_after"] = ["cat ~/.gitconfig"]
        with self.assertRaises(JobRefused) as caught:
            ex._strip_repo_credentials(yaml.dump(data))
        self.assertIn("shell_command_after", str(caught.exception))
        # What core renders goes through.
        self.assertNotIn("@", ex._strip_repo_credentials(text))

    def test_a_repos_yaml_entry_that_is_not_a_mapping_refuses_the_job(self):
        """An entry a layer leaves empty, or a list, refuses the job rather
        than fail it with an ``AttributeError`` (10-nov-2026)."""
        ex = _make_deploy_executor(odoo_version="18.0")
        for text in ("addons:\n", "addons: [a, b]\n", "addons: text\n"):
            with self.subTest(text=text), self.assertRaises(JobRefused) as caught:
                ex._strip_repo_credentials(text)
            self.assertIn("addons", str(caught.exception))
