"""cloud.gitlab.event: a delivery recorded, dispatched and purged (G3).

plan-gitlab.md §2.4, §4.7.3, §4.7.6, §4.8 and Fable's review of G3.
"""
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tools import mute_logger
from psycopg2 import errors as pg_errors

from ._gitlab_hooks import AFTER, BEFORE, ZERO, GitLabHookCase, push_body


class TestGitLabPush(GitLabHookCase):

    def test_a_push_queues_the_rebuild_with_its_push_keys(self):
        event = self._event(push_body())
        event._dispatch_safely()
        self.assertTrue(event.processed)
        self.assertFalse(event.error)
        job = self._rebuilds()
        self.assertEqual(len(job), 1)
        self.assertEqual(job.payload["trigger"], "webhook")
        self.assertEqual(job.payload["push_provider"], "gitlab")
        self.assertEqual(job.payload["push_repo"], "acme/web")
        self.assertEqual(job.payload["push_sha"], AFTER[:7])
        self.assertEqual(job.payload["push_by"], "dev")
        self.assertEqual(job.payload["push_message"], "fix: the thing")
        self.assertTrue(self.instance.last_auto_rebuild)

    def test_the_job_type_comes_from_the_mixin(self):
        with patch.object(
            type(self.Event), "_rebuild_job_type", return_value=False,
        ):
            self._event(push_body())._dispatch_safely()
        self.assertFalse(self._rebuilds())

    def test_deletes_tags_and_unsafe_branches_are_ignored(self):
        for i, body in enumerate((
            push_body(after=ZERO),
            push_body(ref="refs/tags/v1"),
            push_body(branch="main;rm"),
        )):
            with self.subTest(i=i):
                event = self._event(body, key=f"k{i}")
                event._dispatch_safely()
                self.assertTrue(event.processed)
        self.assertFalse(self._rebuilds())

    def test_a_frozen_line_or_auto_rebuild_off_is_respected(self):
        self.line.commit_sha = "b" * 40
        self._event(push_body(), key="f1")._dispatch_safely()
        self.assertFalse(self._rebuilds())
        self.line.commit_sha = False
        self.instance.auto_rebuild = False
        self._event(push_body(after="c" * 40), key="f2")._dispatch_safely()
        self.assertFalse(self._rebuilds())

    def test_a_tag_push_counts_as_a_delivery_and_dispatches_nothing(self):
        self.assertEqual(self.hook.state, "pending")
        event = self._event(push_body(ref="refs/tags/v1.0.0"), "Tag Push Hook")
        event._dispatch_safely()
        self.assertTrue(event.processed)
        self.assertEqual(self.hook.state, "active")
        self.assertFalse(self._rebuilds())

    def test_active_is_written_only_when_it_changes(self):
        self.hook.state = "active"
        with patch.object(type(self.Hook), "write", autospec=True) as write:
            self._event(push_body(ref="refs/tags/v2"), "Tag Push Hook")._dispatch_safely()
        self.assertFalse([c for c in write.call_args_list if "state" in c.args[1]])

    def test_a_renamed_project_raises_an_alert_and_does_nothing(self):
        self.hook.gitlab_id = 101
        event = self._event(push_body(path="acme/site"))
        event._dispatch_safely()
        self.assertTrue(event.processed)
        self.assertFalse(self._rebuilds())
        alert = self.env["cloud.alert"].search([
            ("code", "=", "gitlab_repo_moved"), ("state", "=", "active"),
        ])
        self.assertTrue(alert)
        self.assertIn("acme/site", alert.message)

    def test_a_group_hook_covers_its_paths_by_segments(self):
        group = self.Hook.create({
            "provider_id": self.provider.id, "scope": "group", "path": "acme",
        })
        self._event(push_body(), hook=group, key="g1")._dispatch_safely()
        self.assertEqual(len(self._rebuilds()), 1)
        other = self.Hook.create({
            "provider_id": self.provider.id, "scope": "group", "path": "acm",
        })
        event = self._event(push_body(after="d" * 40), hook=other, key="g2")
        event._dispatch_safely()
        self.assertEqual(len(self._rebuilds()), 1, "acm does not cover acme/web")

    def test_the_summary_keeps_no_email(self):
        event = self._event(push_body())
        text = event.summary
        self.assertNotIn("@example.com", text)
        self.assertNotIn("Dev Eloper", text)
        summary = json.loads(text)
        self.assertEqual(summary["user_username"], "dev")
        self.assertEqual(summary["last_commit"], {"id": AFTER, "title": "fix: the thing"})

    def test_the_semantic_key_of_a_push(self):
        event = self._event(push_body())
        self.assertEqual(
            event.semantic_key, f"push:acme/web:refs/heads/main:{BEFORE}:{AFTER}",
        )
        tag = self._event(push_body(ref="refs/tags/v1"), "Tag Push Hook", key="t")
        self.assertFalse(tag.semantic_key)

    def test_the_same_delivery_or_push_twice_is_refused(self):
        self._event(push_body())
        for key, body in (("idem-1", push_body(after="e" * 40)),
                          ("idem-2", push_body())):
            with self.subTest(key=key), mute_logger("odoo.sql_db"), \
                    self.assertRaises(pg_errors.UniqueViolation), \
                    self.env.cr.savepoint():
                self._event(body, key=key)

    def test_deliveries_without_an_idempotency_key(self):
        self._event(push_body(), key="")
        self._event(push_body(after="f" * 40), key="")
        with mute_logger("odoo.sql_db"), \
                self.assertRaises(pg_errors.UniqueViolation), \
                self.env.cr.savepoint():
            self._event(push_body(), key="")


class TestGitLabDispatchFailure(GitLabHookCase):

    def test_a_failing_dispatch_keeps_the_event_and_the_cron_reprocesses_it(self):
        event = self._event(push_body())
        with patch.object(
            type(self.Event), "_process_push", side_effect=RuntimeError("boom"),
        ), mute_logger("odoo.addons.incubacloud.models.cloud_gitlab_event"):
            event._dispatch_safely()
        self.assertFalse(event.processed)
        self.assertIn("boom", event.error)
        self.assertEqual(event.dispatch_attempts, 1)
        self.assertFalse(self._rebuilds())
        self.Event._cron_purge_gitlab_events()
        self.assertTrue(event.processed)
        self.assertFalse(event.error)
        self.assertEqual(len(self._rebuilds()), 1)

    def test_the_cron_gives_up_after_three_attempts(self):
        event = self._event(push_body())
        event.write({"error": "x", "dispatch_attempts": 3})
        self.Event._cron_purge_gitlab_events()
        self.assertFalse(event.processed)
        self.assertFalse(self._rebuilds())

    def test_a_failing_enqueue_does_not_stop_the_others(self):
        """Each enqueue in its savepoint: a database error on one instance spares the rest."""
        second = self.env["cloud.instance"].create({
            "name": "gl-staging-2", "project_id": self.project.id,
            "host_id": self.host.id, "environment": "staging",
            "auto_rebuild": True, "state": "deployed",
        })
        self.env["cloud.instance.repo"].create({
            "instance_id": second.id,
            "url": f"https://gitlab.com/{self.PATH}.git", "branch": "main",
        })
        Job = type(self.env["cloud.job"])
        real = Job.enqueue

        def enqueue(job_model, host_id, instance_id, *args, **kwargs):
            """Break the transaction for the first instance only."""
            if instance_id == self.instance.id:
                job_model.env.cr.execute("SELECT 1/0")
            return real(job_model, host_id, instance_id, *args, **kwargs)

        with patch.object(Job, "enqueue", autospec=True, side_effect=enqueue), \
                mute_logger("odoo.sql_db", "odoo.addons.incubacloud.models.cloud_repo_event_mixin"):
            event = self._event(push_body())
            event._dispatch_safely()
        self.assertTrue(event.processed)
        self.assertFalse(self._rebuilds())
        self.assertEqual(len(self._rebuilds(second)), 1)


class TestGitLabPurge(GitLabHookCase):

    def test_old_processed_events_go_and_failed_ones_stay(self):
        old = fields.Datetime.now() - timedelta(days=31)
        done = self._event(push_body(), key="p1")
        done.processed = True
        failed = self._event(push_body(after="9" * 40), key="p2")
        failed.write({"error": "x", "dispatch_attempts": 3})
        self.env.cr.execute(
            "UPDATE cloud_gitlab_event SET create_date = %s WHERE id IN %s",
            (old, (done.id, failed.id)),
        )
        self.Event.invalidate_model()
        self.Event._cron_purge_gitlab_events()
        self.assertFalse(done.exists())
        self.assertTrue(failed.exists())


class TestMixinOverride(GitLabHookCase):

    def test_neither_event_model_redefines_the_mixin_hooks(self):
        """An override layered on the mixin must reach both providers (§2.7)."""
        for model in ("cloud.github.event", "cloud.gitlab.event"):
            cls = type(self.env[model])
            for name in ("_rebuild_job_type", "_pr_preview_allowed"):
                with self.subTest(model=model, name=name):
                    owner = next(
                        k for k in cls.__mro__ if name in vars(k)
                    )
                    self.assertEqual(
                        getattr(owner, "_name", None) or owner._inherit,
                        "cloud.repo.event.mixin",
                    )
