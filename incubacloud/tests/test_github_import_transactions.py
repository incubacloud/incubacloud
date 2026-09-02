"""Transactional guarantees for bounded GitHub preview and import routes."""

from unittest.mock import MagicMock, patch

from odoo import api
from odoo import http as odoo_http
from odoo.http import Request
from odoo.tests.common import TransactionCase

from odoo.addons.incubacloud.controllers import data_load
from odoo.addons.incubacloud.controllers._data_load import (
    _routes_github as routes,
)
from odoo.addons.incubacloud.controllers.data_load import (
    CloudDataLoadController,
)
from odoo.addons.incubacloud.github.client import GitHubAnonymousClient
from odoo.addons.incubacloud.github.import_inspector import (
    ImportInspectionError,
)
from odoo.addons.incubacloud.models._concurrency import (
    try_advisory_xact_lock,
)


class TestGitHubImportTransactions(TransactionCase):
    """Locks, durable quotas and ORM creation keep their route contract."""

    def setUp(self):
        """Bind the mounted controller to an admin request with limits enabled."""
        super().setUp()
        self.controller = CloudDataLoadController()
        self.route_env = self.env(
            context=self.env.context | {"force_rate_limit": True},
        )
        self.request = MagicMock(spec=Request)
        self.request.env = self.route_env
        self.buckets = [
            f"github_preview_user:{self.route_env.user.id}",
            f"github_import_user:{self.route_env.user.id}",
            f"hard004_durable:{self.route_env.user.id}",
            f"hard004_user_a:{self.route_env.user.id}",
            f"hard004_user_b:{self.route_env.user.id}",
        ]
        self._clear_buckets()
        self.addCleanup(self._clear_buckets)

    def _clear_buckets(self):
        """Delete independently committed test counters without touching others."""
        with self.env.registry.cursor() as cr:
            cr.execute(
                "DELETE FROM cloud_rate_limit WHERE bucket = ANY(%s)",
                (self.buckets,),
            )
            cr.commit()

    def _route_patches(self):
        """Patch every request binding and avoid reading configured credentials.

        ``odoo.http.request`` is patched as well because ``Controller.env``
        derives from it; without it Odoo's translation fallback dereferences a
        ``None`` environment while rendering the limit and contention replies.
        """
        client = MagicMock(spec=GitHubAnonymousClient)
        return (
            patch.object(routes, "request", self.request),
            patch.object(data_load, "request", self.request),
            patch.object(odoo_http, "request", self.request),
            patch.object(
                routes, "_github_import_clients", autospec=True,
                return_value=[client],
            ),
        )

    def _bucket_count(self, bucket):
        """Read a durable bucket count from a separate transaction."""
        with self.env.registry.cursor() as cr:
            cr.execute(
                "SELECT count FROM cloud_rate_limit WHERE bucket = %s",
                (bucket,),
            )
            row = cr.fetchone()
        return row[0] if row else 0

    def test_default_route_thresholds_are_separate(self):
        """Preview admits 10/hour and import 5/hour in distinct buckets."""
        request_patch, facade_patch, http_patch, clients_patch = (
            self._route_patches()
        )
        with request_patch, facade_patch, http_patch, clients_patch, patch.object(
            routes, "GitHubImportInspector", autospec=True,
        ) as Inspector, patch.object(
            CloudDataLoadController,
            "_create_bounded_github_import",
            autospec=True,
            return_value={"ok": True},
        ) as create:
            inspector = Inspector.return_value
            inspector.inspect_preview.return_value = {"ok": True}
            inspector.inspect_import.return_value = {
                "ok": True,
                "repo_name": "demo",
                "repo_type": "simple",
                "repos_data": [],
            }
            previews = [
                self.controller.cloud_fetch_odoojs_submodules(
                    "https://github.com/acme/demo", "main",
                )
                for _index in range(11)
            ]
            imports = [
                self.controller.cloud_import_project(
                    "https://github.com/acme/demo", "main",
                )
                for _index in range(6)
            ]
        self.assertTrue(previews[9]["ok"])
        self.assertFalse(previews[10]["ok"])
        self.assertTrue(imports[4]["ok"])
        self.assertFalse(imports[5]["ok"])
        self.assertEqual(inspector.inspect_preview.call_count, 10)
        self.assertEqual(inspector.inspect_import.call_count, 5)
        self.assertEqual(create.call_count, 5)
        self.assertEqual(self._bucket_count(self.buckets[0]), 11)
        self.assertEqual(self._bucket_count(self.buckets[1]), 6)

    def test_busy_global_lock_does_not_consume_quota(self):
        """A real conflicting lock rejects before bucket creation or inspection."""
        request_patch, facade_patch, http_patch, clients_patch = (
            self._route_patches()
        )
        with self.env.registry.cursor() as winner, request_patch, \
                facade_patch, http_patch, clients_patch, patch.object(
                    routes, "GitHubImportInspector", autospec=True,
                ) as Inspector:
            self.assertTrue(try_advisory_xact_lock(
                winner, routes._GITHUB_IMPORT_LOCK_NAMESPACE,
            ))
            result = self.controller.cloud_fetch_odoojs_submodules(
                "https://github.com/acme/demo", "main",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(self._bucket_count(self.buckets[0]), 0)
        Inspector.assert_not_called()

    def test_invalid_syntax_does_not_lock_count_or_inspect(self):
        """URL/ref validation short-circuits before every costly route stage."""
        request_patch, facade_patch, http_patch, clients_patch = (
            self._route_patches()
        )
        with request_patch, facade_patch, http_patch, clients_patch, patch.object(
            routes, "GitHubImportInspector", autospec=True,
        ) as Inspector:
            result = self.controller.cloud_fetch_odoojs_submodules(
                "https://example.test/acme/demo", "main",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(self._bucket_count(self.buckets[0]), 0)
        Inspector.assert_not_called()

    def test_admitted_failure_survives_main_cursor_rollback(self):
        """A failed parse still counts after the request transaction rolls back."""
        with self.env.registry.cursor() as main:
            route_env = api.Environment(
                main,
                self.env.uid,
                self.env.context | {"force_rate_limit": True},
            )
            route_request = MagicMock(spec=Request)
            route_request.env = route_env
            client = MagicMock(spec=GitHubAnonymousClient)
            with patch.object(
                routes, "request", route_request,
            ), patch.object(
                data_load, "request", route_request,
            ), patch.object(
                routes, "_github_import_clients", autospec=True,
                return_value=[client],
            ), patch.object(
                routes, "GitHubImportInspector", autospec=True,
            ) as Inspector:
                Inspector.return_value.inspect_preview.side_effect = (
                    ImportInspectionError("Repository tree is too large.")
                )
                result = self.controller.cloud_fetch_odoojs_submodules(
                    "https://github.com/acme/demo", "main",
                )
            main.rollback()
        self.assertFalse(result["ok"])
        self.assertEqual(self._bucket_count(self.buckets[0]), 1)

    def test_quota_buckets_are_independent_per_user_key(self):
        """Exhausting one user-shaped bucket does not consume another."""
        for _index in range(10):
            self.assertTrue(routes._consume_github_quota(
                self.route_env,
                self.buckets[3],
                "rate_limit_github_previews_per_hour",
            ))
        self.assertFalse(routes._consume_github_quota(
            self.route_env,
            self.buckets[3],
            "rate_limit_github_previews_per_hour",
        ))
        self.assertTrue(routes._consume_github_quota(
            self.route_env,
            self.buckets[4],
            "rate_limit_github_previews_per_hour",
        ))

    def test_complete_simple_creation_applies_downloaded_requirements(self):
        """One inspected fixture creates project, repo and instance without I/O."""
        inspected = {
            "ok": True,
            "repo_name": "bounded-demo",
            "repo_type": "simple",
            "repos_data": [{
                "url": "https://github.com/acme/bounded-demo.git",
                "branch": "19.0",
                "alias": "bounded-demo",
                "addons": "",
                "requirements": "requests==2.32.0\n",
            }],
            "copier": {},
            "pip_deps": "",
            "apt_deps": "",
            "odoo_conf": "",
            "odoo_version": "19.0",
            "submodules": [],
        }
        with patch.object(routes, "request", self.request):
            result = self.controller._create_bounded_github_import(inspected)
        project = self.env["cloud.project"].browse(result["project_id"])
        instance = self.env["cloud.instance"].browse(result["instance_id"])
        self.assertEqual(len(project.repo_ids), 1)
        self.assertEqual(project.repo_ids.branch, "19.0")
        self.assertIn("requests==2.32.0", project.pip_dependencies)
        self.assertEqual(instance.project_id, project)

    def test_creation_deadline_rolls_back_partial_records(self):
        """An expired local deadline inside the savepoint leaves no project."""
        request_patch, facade_patch, http_patch, clients_patch = (
            self._route_patches()
        )
        with request_patch, facade_patch, http_patch, clients_patch, patch.object(
            routes, "GitHubImportInspector", autospec=True,
        ) as Inspector:
            inspector = Inspector.return_value
            inspector.inspect_import.return_value = {
                "ok": True,
                "repo_name": "deadline-project",
                "repo_type": "simple",
                "repos_data": [],
                "copier": {},
                "odoo_version": "19.0",
            }
            inspector.ensure_budget.side_effect = [None, None, None,
                                                   ImportInspectionError(
                                                       "Repository inspection "
                                                       "exceeded its safety "
                                                       "budget."
                                                   )]
            result = self.controller.cloud_import_project(
                "https://github.com/acme/deadline-project", "main",
            )
        self.assertFalse(result["ok"])
        self.assertFalse(self.env["cloud.project"].search([
            ("name", "=", "deadline-project"),
        ]))
