"""What an addon may add to an instance's stack, through model hooks.

The deploy executor renders the whole stack and has eight subclasses; an
addon that needed one more container, env file or teardown step had to
patch executors and break with each of their changes. These hooks are
where it adds instead (``cloud.instance._extra_services`` and friends).
Pinned here: without an addon nothing changes, and what an addon adds
never replaces what the platform renders.
"""
from unittest.mock import patch

import yaml

from odoo.tests.common import BaseCase, TransactionCase

from odoo.addons.incubacloud.models.delete_instance_executor import (
    DeleteInstanceExecutor,
)
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
    _merge_override_fragment,
)
from odoo.addons.incubacloud.models.move_cutover_executor import (
    MoveCleanupSourceExecutor,
)
from odoo.addons.incubacloud.models.rebuild_instance_executor import (
    RebuildInstanceExecutor,
)


class TestMergingAFragment(BaseCase):

    def test_nothing_the_platform_set_is_replaced(self):
        entry = {
            "command": ["odoo"], "mem_limit": "1g",
            "environment": {"A": "platform"}, "volumes": ["./logs:/l"],
        }
        _merge_override_fragment(entry, {
            "command": ["evil"], "mem_limit": "9g",
            "environment": {"A": "addon", "B": "addon"},
            "volumes": ["./logs:/l", "/opt/x:/x:ro"],
            "env_file": [".docker/x.env"],
        })
        self.assertEqual(entry["command"], ["odoo"])
        self.assertEqual(entry["mem_limit"], "1g")
        self.assertEqual(entry["environment"], {"A": "platform", "B": "addon"})
        self.assertEqual(entry["volumes"], ["./logs:/l", "/opt/x:/x:ro"])
        self.assertEqual(entry["env_file"], [".docker/x.env"])


class TestAddonHooks(TransactionCase):

    def setUp(self):
        super().setUp()
        self.host = self.env["cloud.host"].create({
            "name": "hk-host", "ip_address": "192.0.2.71", "user": "ubuntu",
            "wildcard_domain": "hk.example.com",
        })
        self.project = self.env["cloud.project"].create({"name": "hk-proj"})
        self.inst = self.env["cloud.instance"].create({
            "name": "hk-inst", "project_id": self.project.id,
            "environment": "production", "host_id": self.host.id,
        })
        self.Instance = type(self.inst)

    def _executor(self, cls, code):
        job_type = self.env["cloud.job.type"].search(
            [("code", "=", code)], limit=1,
        )
        job = self.env["cloud.job"].create({
            "name": code, "host_id": self.host.id,
            "instance_id": self.inst.id, "job_type_id": job_type.id,
        })
        return cls(job, self.host)

    def _labels(self, executor):
        return [c[0] for c in executor.get_commands()]

    # ── Without an addon ──────────────────────────────────────────────

    def test_core_adds_nothing(self):
        self.assertEqual(self.inst._extra_services(), ())
        self.assertEqual(self.inst._compose_override_fragments(), {})
        self.assertEqual(self.inst._extra_env_files(), {})
        self.assertEqual(self.inst._teardown_pre_steps("~/d", False), [])
        self.assertIsNone(self.inst._after_restore_hook("restore"))

    def test_without_an_addon_no_step_is_added(self):
        for cls, code in (
            (DeployInstanceExecutor, "deploy_instance"),
            (RebuildInstanceExecutor, "rebuild_instance"),
        ):
            self.assertNotIn(
                "Write extra env files", self._labels(self._executor(cls, code)),
            )

    # ── With one ──────────────────────────────────────────────────────

    def test_an_extra_service_is_rendered_labelled_and_expected(self):
        fragments = {
            "sidecar": {"image": "example/sidecar:1", "volumes": ["a:/a"]},
            "db": {"volumes": ["/opt/bin/tool:/opt/tool:ro"]},
            "odoo": {"command": ["replaced?"]},
        }
        with patch.object(
            self.Instance, "_extra_services", return_value=("sidecar",),
        ), patch.object(
            self.Instance, "_compose_override_fragments",
            return_value=fragments,
        ):
            self.assertIn("sidecar", self.inst.expected_services())
            data = yaml.safe_load(
                self._executor(DeployInstanceExecutor, "deploy_instance")
                ._resource_override_content()
            )["services"]
        self.assertEqual(data["sidecar"]["image"], "example/sidecar:1")
        self.assertIn("incubacloud.protect", data["sidecar"]["labels"])
        self.assertEqual(data["sidecar"]["logging"]["driver"], "json-file")
        self.assertIn("/opt/bin/tool:/opt/tool:ro", data["db"]["volumes"])
        self.assertNotEqual(data["odoo"]["command"], ["replaced?"])

    def test_an_env_file_is_staged_private_and_moved_into_place(self):
        files = {".docker/sidecar.env": "TOKEN=s3cret\n"}
        with patch.object(
            self.Instance, "_extra_env_files", return_value=files,
        ):
            for cls, code in (
                (DeployInstanceExecutor, "deploy_instance"),
                (RebuildInstanceExecutor, "rebuild_instance"),
            ):
                executor = self._executor(cls, code)
                commands = dict(c[:2] for c in executor.get_commands())
                step = commands["Write extra env files"]
                tmp = executor._extra_env_tmp(".docker/sidecar.env")
                d = self.inst.get_remote_dir()
                self.assertIn(f"f={tmp};", step)
                self.assertIn(f"chmod 600 {d}/.docker/sidecar.env", step)
                self.assertNotIn("s3cret", step)
                labels = list(commands)
                self.assertLess(
                    labels.index("Write extra env files"),
                    labels.index("Write compose override"),
                )

    def test_a_teardown_step_runs_before_the_stack_goes_down(self):
        step = ("Purge sidecar data", "true")
        with patch.object(
            self.Instance, "_teardown_pre_steps", return_value=[step],
        ) as hook:
            labels = self._labels(
                self._executor(DeleteInstanceExecutor, "delete_instance"),
            )
            self.assertLess(
                labels.index("Purge sidecar data"),
                labels.index("Stop and remove containers"),
            )
            hook.assert_called_once_with(self.inst.get_remote_dir(), False)
            # A move's cleanup tears down a copy of an instance that
            # lives on elsewhere: its addon state is not touched.
            labels = self._labels(
                self._executor(MoveCleanupSourceExecutor, "move_cleanup_source"),
            )
        self.assertNotIn("Purge sidecar data", labels)

    def test_each_env_file_move_must_succeed(self):
        files = {".docker/a.env": "A=1\n", ".docker/b.env": "B=1\n"}
        with patch.object(
            self.Instance, "_extra_env_files", return_value=files,
        ):
            executor = self._executor(DeployInstanceExecutor, "deploy_instance")
            step = dict(c[:2] for c in executor.get_commands())[
                "Write extra env files"
            ]
        # Chained, so a failed first move is not hidden by the second.
        self.assertEqual(step.count("; } && { "), 1)

    def test_an_addon_hook_that_raises_leaves_the_job_s_outcome(self):
        before = self.inst.apt_dependencies

        def _broken(inst):
            inst.write({"apt_dependencies": "written by the hook"})
            raise RuntimeError("addon bug")

        executor = self._executor(DeployInstanceExecutor, "deploy_instance")
        with patch.object(
            self.Instance, "_after_stack_rendered", autospec=True,
            side_effect=_broken,
        ):
            executor._call_addon_hook(self.inst, "_after_stack_rendered")
        self.inst.invalidate_recordset(["apt_dependencies"])
        self.assertEqual(self.inst.apt_dependencies, before)
