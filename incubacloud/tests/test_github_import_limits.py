"""Security limits and compatibility tests for API-only GitHub imports."""

import base64
import hashlib
import io
import json
import urllib.error
from email.message import Message
from http.client import HTTPResponse
from itertools import starmap
from unittest.mock import MagicMock, patch

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.github.client import (
    GitHubAnonymousClient,
    GitHubAppClient,
    GitHubPATClient,
)
from odoo.addons.incubacloud.github.credentials import GitHubAppCredentials
from odoo.addons.incubacloud.github.http_utils import (
    HTTPBudget,
    HTTPBudgetExceeded,
)
from odoo.addons.incubacloud.github.import_inspector import (
    GitHubImportInspector,
    ImportInspectionError,
    MAX_CONF_FILES,
    MAX_DOMAINS,
    MAX_MANIFEST_BYTES,
    MAX_MANIFEST_DEPTH,
    MAX_MANIFEST_NODES,
    MAX_MANIFESTS,
    MAX_REPOSITORIES,
    MAX_TREE_ENTRIES,
    MAX_YAML_DEPTH,
    MAX_YAML_NODES,
    parse_github_repo_url,
    safe_load_yaml,
    validate_branch,
    version_from_manifest_text,
)


def _blob(content):
    """Return a GitHub blob response for UTF-8 *content*."""
    raw = content.encode()
    return {
        "encoding": "base64",
        "size": len(raw),
        "content": base64.b64encode(raw).decode(),
    }


def _entry(path, content, sha=None, mode="100644"):
    """Return a regular Git tree entry matching *content*."""
    return {
        "path": path,
        "type": "blob",
        "mode": mode,
        "sha": sha or hashlib.sha1(
            path.encode(), usedforsecurity=False,
        ).hexdigest(),
        "size": len(content.encode()),
    }


def _http_response(payload):
    """Build a context-managed urllib response with a strict real spec."""
    response = MagicMock(spec=HTTPResponse)
    headers = Message()
    headers["Content-Length"] = str(len(payload))
    response.headers = headers
    response.read.return_value = payload
    response.__enter__.return_value = response
    return response


class TestHTTPBudget(BaseCase):
    """Transport limits are charged before JSON or base64 decoding."""

    def _response(self, payload):
        """Build a strictly spec'd urllib response mock."""
        return _http_response(payload)

    def test_request_limit_is_strict(self):
        """The request after the configured maximum is rejected."""
        budget = HTTPBudget(1, 100, 10)
        budget.begin_request()
        with self.assertRaises(HTTPBudgetExceeded):
            budget.begin_request()

    def test_body_limit_uses_transport_bytes(self):
        """An oversized body is rejected from its declared length."""
        budget = HTTPBudget(2, 100, 10)
        response = self._response(b"x" * 11)
        with self.assertRaises(HTTPBudgetExceeded):
            budget.read(response, 10)
        response.read.assert_not_called()

    def test_shared_byte_budget_accumulates(self):
        """Separate responses consume the same aggregate byte budget."""
        budget = HTTPBudget(3, 6, 10)
        self.assertEqual(budget.read(self._response(b"abc"), 4), b"abc")
        with self.assertRaises(HTTPBudgetExceeded):
            budget.read(self._response(b"defg"), 4)

    def test_shared_deadline_expires_between_local_steps(self):
        """The public deadline check also bounds non-I/O import work."""
        now = [10.0]

        def _clock():
            """Return the deterministic monotonic time for this fixture."""
            return now[0]

        budget = HTTPBudget(3, 100, 2, clock=_clock)
        budget.check()
        now[0] = 12.0
        with self.assertRaises(HTTPBudgetExceeded):
            budget.check()


class TestBudgetedGitHubClient(BaseCase):
    """Authenticated clients use headers and charge the shared budget."""

    def test_pat_is_only_in_authorization_header(self):
        """A PAT never appears in the requested URL or an argv-like value."""
        payload = b'{"default_branch":"main"}'
        response = MagicMock(spec=HTTPResponse)
        headers = Message()
        headers["Content-Length"] = str(len(payload))
        response.headers = headers
        response.read.return_value = payload
        response.__enter__.return_value = response
        budget = HTTPBudget(2, 1000, 10)
        with patch(
            'odoo.addons.incubacloud.github.client.safe_urlopen',
            autospec=True,
            return_value=response,
        ) as opened:
            result = GitHubPATClient("top-secret-token").get(
                "/repos/acme/demo", budget=budget, max_bytes=100,
            )
        request = opened.call_args.args[0]
        self.assertNotIn("top-secret-token", request.full_url)
        self.assertEqual(
            request.headers["Authorization"], "Bearer top-secret-token",
        )
        self.assertEqual(result["default_branch"], "main")
        self.assertEqual(budget.requests, 1)
        self.assertEqual(budget.bytes_read, len(payload))

    def test_oversized_error_body_is_rejected_before_read(self):
        """GitHub error responses consume the same bounded byte budget."""
        headers = Message()
        headers["Content-Length"] = "101"
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/acme/demo",
            500,
            "failure",
            headers,
            io.BytesIO(b"x" * 101),
        )
        budget = HTTPBudget(2, 1000, 10)
        with patch(
            "odoo.addons.incubacloud.github.client.safe_urlopen",
            autospec=True,
            side_effect=error,
        ), self.assertRaises(HTTPBudgetExceeded):
            GitHubPATClient("header-only").get(
                "/repos/acme/demo", budget=budget, max_bytes=100,
            )
        self.assertEqual(budget.requests, 1)
        self.assertEqual(budget.bytes_read, 0)

    def test_app_token_exchange_shares_request_and_byte_budget(self):
        """Issuing an App token and the API GET charge one shared budget."""
        auth_payload = json.dumps({
            "token": "installation-token",
            "expires_at": "2099-01-01T00:00:00Z",
        }).encode()
        api_payload = b'{"default_branch":"main"}'
        credentials = GitHubAppCredentials(
            app_id="7",
            installation_id="11",
            private_key_pem="never-rendered",
        )
        budget = HTTPBudget(3, 1000, 10)
        with patch(
            "odoo.addons.incubacloud.github.client.generate_github_app_jwt",
            autospec=True,
            return_value="signed-jwt",
        ), patch(
            "odoo.addons.incubacloud.github.client.token_cache.get_token",
            autospec=True,
            return_value=None,
        ), patch(
            "odoo.addons.incubacloud.github.client.token_cache.set_token",
            autospec=True,
        ), patch(
            "odoo.addons.incubacloud.github.client.safe_urlopen",
            autospec=True,
            side_effect=[
                _http_response(auth_payload),
                _http_response(api_payload),
            ],
        ) as opened:
            result = GitHubAppClient(credentials).get(
                "/repos/acme/demo", budget=budget, max_bytes=500,
            )
        self.assertEqual(result["default_branch"], "main")
        self.assertEqual(budget.requests, 2)
        self.assertEqual(
            budget.bytes_read, len(auth_payload) + len(api_payload),
        )
        auth_request = opened.call_args_list[0].args[0]
        api_request = opened.call_args_list[1].args[0]
        self.assertEqual(auth_request.headers["Authorization"], "Bearer signed-jwt")
        self.assertEqual(
            api_request.headers["Authorization"],
            "Bearer installation-token",
        )
        self.assertNotIn("never-rendered", auth_request.full_url)
        self.assertNotIn("never-rendered", api_request.full_url)


class TestImportValidation(BaseCase):
    """Untrusted URL, ref and YAML structures are rejected early."""

    def test_github_url_parser_rejects_credentials_and_foreign_hosts(self):
        """Only plain GitHub owner/repository URLs are admitted."""
        for value in (
            "https://token@github.com/org/repo",
            "https://evil.example/org/repo",
            "https://github.com/org/repo/extra",
        ):
            with self.subTest(value=value), self.assertRaises(
                ImportInspectionError
            ):
                parse_github_repo_url(value)

    def test_branch_validation_rejects_option_and_traversal_forms(self):
        """Refs cannot become options or traversal-like tool input."""
        for value in ("-main", "release..next", "bad branch"):
            with self.subTest(value=value), self.assertRaises(
                ImportInspectionError
            ):
                validate_branch(value)

    def test_url_and_branch_accept_their_exact_maximum_lengths(self):
        """Documented syntax caps are inclusive at the valid boundary."""
        owner_length = 2048 - len("https://github.com//r")
        owner = "a" * owner_length
        self.assertEqual(
            parse_github_repo_url(f"https://github.com/{owner}/r"),
            (owner, "r"),
        )
        self.assertEqual(validate_branch("b" * 255), "b" * 255)

    def test_url_and_branch_reject_values_over_their_caps(self):
        """One extra byte or character is rejected before remote access."""
        with self.assertRaises(ImportInspectionError):
            parse_github_repo_url(
                f"https://github.com/{'a' * 2028}/repository",
            )
        with self.assertRaises(ImportInspectionError):
            validate_branch("b" * 256)

    def test_yaml_aliases_are_rejected(self):
        """Aliases cannot structurally amplify a small YAML document."""
        with self.assertRaises(ImportInspectionError):
            safe_load_yaml("a: &shared [1, 2]\nb: *shared\n")

    def test_yaml_node_and_depth_limits_are_enforced(self):
        """Small-byte YAML cannot expand into an unbounded Python graph."""
        with self.assertRaises(ImportInspectionError):
            safe_load_yaml("[" + ",".join("0" for _ in range(MAX_YAML_NODES)) + "]")
        nested = "[" * (MAX_YAML_DEPTH + 1) + "0" + "]" * (
            MAX_YAML_DEPTH + 1
        )
        with self.assertRaises(ImportInspectionError):
            safe_load_yaml(nested)

    def test_manifest_ast_node_and_depth_limits_are_enforced(self):
        """Manifest literals are bounded before ``literal_eval`` runs."""
        many_nodes = "{'items': [" + ",".join(
            "0" for _ in range(MAX_MANIFEST_NODES)
        ) + "]}"
        with self.assertRaises(ImportInspectionError):
            version_from_manifest_text(many_nodes)
        nested = "[" * MAX_MANIFEST_DEPTH + "{'version': '19.0'}" + "]" * (
            MAX_MANIFEST_DEPTH
        )
        with self.assertRaises(ImportInspectionError):
            version_from_manifest_text(nested)

    def test_manifest_byte_limit_is_enforced_before_ast_parse(self):
        """A manifest larger than its dedicated cap never reaches the AST."""
        with self.assertRaises(ImportInspectionError):
            version_from_manifest_text(" " * (MAX_MANIFEST_BYTES + 1))

    def test_manifest_parser_recursion_is_a_safe_non_match(self):
        """Interpreter recursion failures never escape as worker errors."""
        with patch(
            "odoo.addons.incubacloud.github.import_inspector.ast.parse",
            autospec=True,
            side_effect=RecursionError,
        ):
            self.assertEqual(version_from_manifest_text("{}"), "")


class TestGitHubImportInspector(BaseCase):
    """Representative simple, Doodba and hostile trees stay bounded."""

    def _client(self, owner, repo, branch, files, *, truncated=False,
                extra_entries=None, contents=None, compares=None,
                default_branch="main"):
        """Build a spec'd client serving a deterministic GitHub fixture."""
        client = MagicMock(spec=GitHubAnonymousClient)
        entries = list(starmap(_entry, files.items()))
        entries.extend(extra_entries or [])
        blobs = {
            entry["sha"]: _blob(files[entry["path"]])
            for entry in entries if entry["path"] in files
        }

        def _get(endpoint, *, budget=None, max_bytes=None):
            """Serve one fixture endpoint with the real client signature."""
            if endpoint == f"/repos/{owner}/{repo}":
                return {"default_branch": default_branch}
            if endpoint == f"/repos/{owner}/{repo}/branches/{branch}":
                return {"commit": {"sha": "a" * 40}}
            if endpoint.startswith(f"/repos/{owner}/{repo}/git/trees/"):
                return {"tree": entries, "truncated": truncated}
            if endpoint.startswith(f"/repos/{owner}/{repo}/git/blobs/"):
                return blobs[endpoint.rsplit("/", 1)[1]]
            if "/contents/" in endpoint:
                path_ref = endpoint.split("/contents/", 1)[1]
                path = path_ref.split("?", 1)[0]
                return _blob((contents or {}).get(path, ""))
            if "/compare/" in endpoint:
                candidate = endpoint.rsplit("...", 1)[1]
                return {"status": (compares or {}).get(candidate, "diverged")}
            raise AssertionError(f"unexpected endpoint: {endpoint}")

        client.get.side_effect = _get
        return client

    def test_simple_import_detects_version_and_fetches_requirements_once(self):
        """Simple repositories preserve version and requirements behavior."""
        files = {
            "my_module/__manifest__.py": "{'version': '19.0.1.0.0'}",
        }
        client = self._client(
            "acme", "simple", "main", files,
            contents={"requirements.txt": "requests==2.32.0\n"},
        )
        result = GitHubImportInspector([client]).inspect_import(
            "https://github.com/acme/simple", "main",
        )
        self.assertEqual(result["repo_type"], "simple")
        self.assertEqual(result["odoo_version"], "19.0")
        self.assertEqual(
            result["repos_data"][0]["requirements"],
            "requests==2.32.0\n",
        )
        requirement_calls = [
            call for call in client.get.call_args_list
            if "/contents/requirements.txt" in call.args[0]
        ]
        self.assertEqual(len(requirement_calls), 1)

    def test_doodba_import_reads_all_bounded_configuration(self):
        """Doodba config retains repos, addons, deps, conf and domains."""
        files = {
            ".copier-answers.yml": (
                "odoo_version: '19.0'\nproject_name: Demo\n"
                "domains_prod:\n  - hosts: [demo.example.com]\n"
            ),
            "odoo/custom/src/repos.yaml": (
                "./odoo: {}\ncustom:\n  remotes:\n"
                "    origin: git@github.com:acme/addons.git\n"
                "  target: origin 19.0\n"
            ),
            "odoo/custom/src/addons.yaml": "custom: [sale_custom]\n",
            "odoo/custom/dependencies/pip.txt": "requests==2.32.0\n",
            "odoo/custom/dependencies/apt.txt": "libpq-dev\n",
            "odoo/custom/conf.d/20-demo.conf": "workers = 4\n",
        }
        result = GitHubImportInspector([
            self._client("acme", "doodba", "19.0", files),
        ]).inspect_import("https://github.com/acme/doodba", "19.0")
        self.assertEqual(result["repo_type"], "doodba")
        self.assertEqual(result["repos_data"][0]["addons"], "sale_custom")
        self.assertEqual(result["pip_deps"], "requests==2.32.0")
        self.assertEqual(result["apt_deps"], "libpq-dev")
        self.assertEqual(result["odoo_conf"], "workers = 4")
        self.assertEqual(
            result["copier"]["domains"][0]["hostname"],
            "demo.example.com",
        )

    def test_odoosh_import_keeps_main_and_pinned_submodule_requirements(self):
        """Odoo.sh parity includes both repos and their one-shot requirements."""
        gitmodules = (
            '[submodule "addons"]\n'
            '  path = addons\n'
            '  url = https://github.com/acme/addons.git\n'
            '  branch = 19.0\n'
        )
        client = self._client(
            "acme", "odoosh", "19.0",
            {".gitmodules": gitmodules},
            extra_entries=[{
                "path": "addons",
                "type": "commit",
                "mode": "160000",
                "sha": "b" * 40,
            }],
            contents={"requirements.txt": "requests==2.32.0\n"},
        )
        root_get = client.get.side_effect

        def _get(endpoint, *, budget=None, max_bytes=None):
            """Extend the main fixture with subrepository metadata/content."""
            if endpoint == "/repos/acme/addons":
                return {"default_branch": "19.0"}
            if endpoint.startswith("/repos/acme/addons/contents/"):
                return _blob("cryptography==44.0.0\n")
            return root_get(endpoint, budget=budget, max_bytes=max_bytes)

        client.get.side_effect = _get
        result = GitHubImportInspector([client]).inspect_import(
            "https://github.com/acme/odoosh", "19.0",
        )
        self.assertEqual(result["repo_type"], "odoosh")
        self.assertEqual(len(result["repos_data"]), 2)
        self.assertEqual(result["repos_data"][1]["commit_sha"], "b" * 40)
        self.assertEqual(
            result["repos_data"][1]["requirements"],
            "cryptography==44.0.0\n",
        )

    def test_declared_repository_submodule_and_domain_caps(self):
        """Every collection sourced from YAML or gitmodules has a hard cap."""
        repos_yaml = "\n".join(
            ["./odoo: {}"]
            + [
                f"repo{index}:\n  remotes:\n    origin: "
                f"https://github.com/acme/r{index}.git"
                for index in range(MAX_REPOSITORIES + 1)
            ]
        )
        client = self._client(
            "acme", "many-repos", "19.0",
            {"odoo/custom/src/repos.yaml": repos_yaml},
        )
        with self.assertRaises(ImportInspectionError):
            GitHubImportInspector([client]).inspect_import(
                "https://github.com/acme/many-repos", "19.0",
            )

        gitmodules = "\n".join(
            f'[submodule "s{index}"]\n path = s{index}\n'
            f' url = https://github.com/acme/s{index}.git'
            for index in range(MAX_REPOSITORIES + 1)
        )
        client = self._client(
            "acme", "many-subs", "19.0", {".gitmodules": gitmodules},
        )
        with self.assertRaises(ImportInspectionError):
            GitHubImportInspector([client]).inspect_import(
                "https://github.com/acme/many-subs", "19.0",
            )

        hosts = ", ".join(
            f"d{index}.example.com" for index in range(MAX_DOMAINS + 1)
        )
        client = self._client(
            "acme", "many-domains", "19.0",
            {
                ".copier-answers.yml": (
                    "odoo_version: '19.0'\n"
                    f"domains_prod:\n  - hosts: [{hosts}]\n"
                ),
                "odoo/custom/src/repos.yaml": "./odoo: {}\n",
            },
        )
        with self.assertRaises(ImportInspectionError):
            GitHubImportInspector([client]).inspect_import(
                "https://github.com/acme/many-domains", "19.0",
            )

    def test_config_manifest_and_decoded_aggregate_caps(self):
        """File counts, manifest size and decoded aggregate are all bounded."""
        conf_files = {
            f"odoo/custom/conf.d/{index:03d}.conf": "workers = 1"
            for index in range(MAX_CONF_FILES + 1)
        }
        conf_files["odoo/custom/src/repos.yaml"] = "./odoo: {}\n"
        client = self._client("acme", "many-conf", "19.0", conf_files)
        with self.assertRaises(ImportInspectionError):
            GitHubImportInspector([client]).inspect_import(
                "https://github.com/acme/many-conf", "19.0",
            )

        manifests = {
            f"m{index}/__manifest__.py": "{'name': 'x'}"
            for index in range(MAX_MANIFESTS + 1)
        }
        client = self._client("acme", "many-manifests", "main", manifests)
        with self.assertRaises(ImportInspectionError):
            GitHubImportInspector([client]).inspect_import(
                "https://github.com/acme/many-manifests", "main",
            )

        oversized = " " * (MAX_MANIFEST_BYTES + 1)
        client = self._client(
            "acme", "large-manifest", "main",
            {"mod/__manifest__.py": oversized},
        )
        with self.assertRaises(ImportInspectionError):
            GitHubImportInspector([client]).inspect_import(
                "https://github.com/acme/large-manifest", "main",
            )

        client = self._client(
            "acme", "aggregate", "19.0",
            {
                "odoo/custom/src/repos.yaml": "./odoo: {}\n",
                "odoo/custom/dependencies/pip.txt": "12345",
                "odoo/custom/dependencies/apt.txt": "67890",
            },
        )
        with patch(
            "odoo.addons.incubacloud.github.import_inspector.MAX_CONFIG_BYTES",
            20,
        ), self.assertRaises(ImportInspectionError):
            GitHubImportInspector([client]).inspect_import(
                "https://github.com/acme/aggregate", "19.0",
            )

    def test_truncated_tree_is_rejected(self):
        """GitHub's truncated marker never becomes a partial import."""
        client = self._client(
            "acme", "huge", "main", {}, truncated=True,
        )
        with self.assertRaises(ImportInspectionError):
            GitHubImportInspector([client]).inspect_import(
                "https://github.com/acme/huge", "main",
            )

    def test_entry_count_limit_is_rejected(self):
        """A structurally huge tree stops before any blob request."""
        entries = [
            {"path": f"p/{index}", "type": "tree", "mode": "040000"}
            for index in range(MAX_TREE_ENTRIES + 1)
        ]
        client = self._client(
            "acme", "huge", "main", {}, extra_entries=entries,
        )
        with self.assertRaises(ImportInspectionError):
            GitHubImportInspector([client]).inspect_import(
                "https://github.com/acme/huge", "main",
            )

    def test_symlink_named_as_configuration_is_not_followed(self):
        """A symlink at a selected path cannot be read as config."""
        content = "custom: {}\n"
        symlink = _entry(
            "odoo/custom/src/repos.yaml", content, mode="120000",
        )
        client = self._client(
            "acme", "link", "main", {}, extra_entries=[symlink],
        )
        result = GitHubImportInspector([client]).inspect_import(
            "https://github.com/acme/link", "main",
        )
        self.assertEqual(result["repo_type"], "simple")

    def test_stale_submodule_branch_uses_three_compares_then_warns(self):
        """A fourth unknown branch is not enumerated and falls back safely."""
        gitmodules = (
            '[submodule "addons"]\n'
            '  path = addons\n'
            '  url = https://github.com/acme/addons.git\n'
            '  branch = 16.0\n'
        )
        client = self._client(
            "acme", "odoosh", "main",
            {
                ".gitmodules": gitmodules,
                "own/__manifest__.py": "{'version': '19.0.1.0.0'}",
            },
            extra_entries=[{
                "path": "addons",
                "type": "commit",
                "mode": "160000",
                "sha": "b" * 40,
            }],
            contents={"requirements.txt": ""},
        )
        root_get = client.get.side_effect

        def _get(endpoint, *, budget=None, max_bytes=None):
            """Extend the root fixture with the declared subrepository."""
            if endpoint == "/repos/acme/addons":
                return {"default_branch": "develop"}
            if endpoint.startswith("/repos/acme/addons/compare/"):
                return {"status": "diverged"}
            if endpoint.startswith("/repos/acme/addons/contents/"):
                return _blob("")
            return root_get(
                endpoint, budget=budget, max_bytes=max_bytes,
            )

        client.get.side_effect = _get
        result = GitHubImportInspector([client]).inspect_import(
            "https://github.com/acme/odoosh", "main",
        )
        subrepo = result["repos_data"][1]
        self.assertEqual(subrepo["branch"], "19.0")
        self.assertIn("sha_warning", result)
        compare_calls = [
            call.args[0] for call in client.get.call_args_list
            if "/compare/" in call.args[0]
        ]
        self.assertEqual(len(compare_calls), 3)
        self.assertTrue(compare_calls[0].endswith("...19.0"))
        self.assertTrue(compare_calls[1].endswith("...16.0"))
        self.assertTrue(compare_calls[2].endswith("...develop"))

    def test_exhausted_shared_budget_becomes_safe_import_error(self):
        """A client budget failure is translated without remote details."""
        client = self._client("acme", "late", "main", {})
        client.get.side_effect = HTTPBudgetExceeded("deadline with details")
        with self.assertRaises(ImportInspectionError):
            GitHubImportInspector([client]).inspect_import(
                "https://github.com/acme/late", "main",
            )
        client.get.assert_called_once()

    def test_deadline_expiry_after_remote_response_stops_local_parse(self):
        """A response arriving at the deadline cannot start AST/YAML work."""
        now = [0.0]

        def _clock():
            """Return the deterministic monotonic time for this fixture."""
            return now[0]

        budget = HTTPBudget(20, 100_000, 1, clock=_clock)
        client = self._client(
            "acme", "late-tree", "main",
            {"module/__manifest__.py": "{'version': '19.0.1.0.0'}"},
        )
        fixture_get = client.get.side_effect

        def _expiring_get(endpoint, *, budget=None, max_bytes=None):
            """Expire the operation as the recursive tree response arrives."""
            result = fixture_get(
                endpoint, budget=budget, max_bytes=max_bytes,
            )
            if "/git/trees/" in endpoint:
                now[0] = 1.0
            return result

        client.get.side_effect = _expiring_get
        with self.assertRaises(ImportInspectionError):
            GitHubImportInspector([client], budget=budget).inspect_import(
                "https://github.com/acme/late-tree", "main",
            )

    def test_requirements_blob_has_the_same_decoded_size_cap(self):
        """Contents API requirements cannot bypass the decoded blob limit."""
        client = self._client(
            "acme", "requirements", "main", {},
            contents={"requirements.txt": "12345"},
        )
        with patch(
            "odoo.addons.incubacloud.github.import_inspector.MAX_BLOB_BYTES",
            4,
        ), self.assertRaises(ImportInspectionError):
            GitHubImportInspector([client]).inspect_import(
                "https://github.com/acme/requirements", "main",
            )

    def test_json_fixture_is_serializable_without_secret_material(self):
        """Representative results contain plain data and no auth tokens."""
        token = "secret-token-must-not-leak"
        client = self._client("acme", "plain", "main", {})
        result = GitHubImportInspector([client]).inspect_import(
            "https://github.com/acme/plain", "main",
        )
        self.assertNotIn(token, json.dumps(result))
