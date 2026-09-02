"""Bounded, API-only inspection of GitHub repositories for project imports."""

import ast
import base64
import binascii
import posixpath
import re
from urllib.parse import quote, urlparse

import yaml

from .client import GitHubAPIError
from .http_utils import HTTPBudget, HTTPBudgetExceeded
from ..models._odoo_versions import ODOO_VERSIONS


MAX_URL_LENGTH = 2048
MAX_BRANCH_LENGTH = 255
MAX_TREE_BYTES = 8 * 1024 * 1024
MAX_TREE_ENTRIES = 20_000
MAX_HTTP_REQUESTS = 250
MAX_HTTP_BYTES = 32 * 1024 * 1024
MAX_REPOSITORIES = 100
MAX_CONF_FILES = 100
MAX_MANIFESTS = 100
MAX_DOMAINS = 100
MAX_YAML_NODES = 10_000
MAX_YAML_DEPTH = 50
MAX_MANIFEST_BYTES = 128 * 1024
MAX_MANIFEST_NODES = 10_000
MAX_MANIFEST_DEPTH = 50
MAX_BLOB_BYTES = 1024 * 1024
MAX_BLOB_TRANSPORT_BYTES = 1536 * 1024
MAX_CONFIG_BYTES = 5 * 1024 * 1024
IMPORT_TIMEOUT_SECONDS = 120

_ALLOWED_GIT_HOSTS = frozenset({"github.com", "www.github.com"})
_SUPPORTED_VERSIONS = frozenset(ODOO_VERSIONS)
_SSH_URL_RE = re.compile(r"^git@([^:]+):(.+?)(?:\.git)?$")
_VERSION_BRANCH_RE = re.compile(r"^\d+\.\d$")
_REGULAR_BLOB_MODES = frozenset({"100644", "100755"})


class ImportInspectionError(Exception):
    """Raised for a safe, user-facing repository inspection failure."""


def parse_github_repo_url(url):
    """Return ``(owner, repository)`` for an allow-listed GitHub URL.

    :param str url: HTTPS, HTTP, bare-host or SCP-style GitHub URL
    :raises ImportInspectionError: when the URL is invalid or too long
    """
    value = (url or "").strip()
    if not value or len(value) > MAX_URL_LENGTH:
        raise ImportInspectionError("Invalid repository URL.")
    match = _SSH_URL_RE.fullmatch(value)
    if match:
        host, path = match.groups()
    else:
        candidate = value if "://" in value else f"https://{value}"
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lstrip("/")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ImportInspectionError("Invalid repository URL.")
    if host.lower() not in _ALLOWED_GIT_HOSTS:
        raise ImportInspectionError("Invalid repository URL.")
    parts = path.rstrip("/").removesuffix(".git").split("/")
    if len(parts) != 2 or not all(parts):
        raise ImportInspectionError("Invalid repository URL.")
    return parts[0], parts[1]


def to_https(url):
    """Normalize a supported GitHub remote to a canonical HTTPS URL."""
    owner, repo = parse_github_repo_url(url)
    return f"https://github.com/{owner}/{repo}.git"


def validate_branch(branch):
    """Return a normalized safe branch name or raise a stable error."""
    value = (branch or "main").strip()
    if (
        not value
        or len(value) > MAX_BRANCH_LENGTH
        or value.startswith("-")
        or ".." in value
        or not re.fullmatch(r"[A-Za-z0-9._/\-]+", value)
    ):
        raise ImportInspectionError("Invalid branch name.")
    return value


def version_from_manifest_text(text):
    """Return the supported Odoo series from a bounded literal manifest.

    ``ast.literal_eval`` is only called after the parsed AST has passed node
    and depth limits.  This matters even for a byte-limited source: Python
    literals can otherwise expand into a much larger in-memory object or
    exhaust the interpreter stack while being evaluated.
    """
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ImportInspectionError("Repository manifest is too large.")
    try:
        tree = ast.parse(text, mode="eval")
    except (RecursionError, SyntaxError, TypeError, ValueError):
        return ""
    nodes = 0
    stack = [(tree, 1)]
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_MANIFEST_NODES or depth > MAX_MANIFEST_DEPTH:
            raise ImportInspectionError("Repository manifest is too complex.")
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    try:
        data = ast.literal_eval(tree)
    except (RecursionError, SyntaxError, TypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    parts = str(data.get("version", "")).split(".")
    if len(parts) < 2:
        return ""
    candidate = f"{parts[0]}.{parts[1]}"
    return candidate if candidate in _SUPPORTED_VERSIONS else ""


def safe_load_yaml(content, check=None):
    """Parse YAML after bounding document nodes, depth and aliases.

    Aliases are rejected because a small document can otherwise expand into a
    much larger structure when later traversed, even when its byte size and
    syntactic node count are both within bounds.
    """
    if check:
        check()
    nodes = 0
    depth = 0
    try:
        for event in yaml.parse(content):
            if check and nodes % 256 == 0:
                check()
            if isinstance(event, yaml.events.AliasEvent):
                raise ImportInspectionError(
                    "YAML aliases are not supported in imported configuration."
                )
            if isinstance(
                event,
                (
                    yaml.events.MappingStartEvent,
                    yaml.events.SequenceStartEvent,
                    yaml.events.ScalarEvent,
                ),
            ):
                nodes += 1
                if nodes > MAX_YAML_NODES:
                    raise ImportInspectionError(
                        "Imported YAML configuration is too complex."
                    )
            if isinstance(
                event,
                (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent),
            ):
                depth += 1
                if depth > MAX_YAML_DEPTH:
                    raise ImportInspectionError(
                        "Imported YAML configuration is too deeply nested."
                    )
            elif isinstance(
                event,
                (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent),
            ):
                depth -= 1
        if check:
            check()
        result = yaml.safe_load(content) or {}
        if check:
            check()
        return result
    except ImportInspectionError:
        raise
    except yaml.YAMLError as exc:
        raise ImportInspectionError(
            "Could not parse repository configuration."
        ) from exc


def _parse_gitmodules(content, main_url):
    """Parse and validate a ``.gitmodules`` document."""
    sections = []
    current = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("[submodule"):
            if current:
                sections.append(current)
            current = {}
        elif "=" in line:
            key, _separator, value = line.partition("=")
            current[key.strip()] = value.strip()
    if current:
        sections.append(current)
    if len(sections) > MAX_REPOSITORIES:
        raise ImportInspectionError("Repository declares too many submodules.")
    result = []
    for section in sections:
        if not section.get("url"):
            continue
        path = posixpath.normpath(section.get("path", "").strip())
        if path in ("", ".") or path.startswith("../") or path.startswith("/"):
            raise ImportInspectionError("Invalid submodule path.")
        result.append({
            "url": _resolve_submodule_url(main_url, section["url"]),
            "branch": section.get("branch", "").strip(),
            "path": path,
        })
    return result


def _resolve_submodule_url(main_url, submodule_url):
    """Resolve a relative submodule reference and enforce the GitHub host."""
    value = (submodule_url or "").strip()
    if value.startswith(("https://", "http://", "git@")):
        return to_https(value)
    owner, _repo = parse_github_repo_url(main_url)
    base_path = f"/{owner}/placeholder"
    parts = (
        posixpath.normpath(posixpath.join(base_path, value))
        .strip("/")
        .split("/")
    )
    if len(parts) != 2:
        raise ImportInspectionError("Invalid submodule URL.")
    return to_https(f"https://github.com/{parts[0]}/{parts[1]}")


def _parse_repos_yaml(content, odoo_version, check=None):
    """Parse bounded git-aggregator configuration into repository values."""
    data = safe_load_yaml(content, check=check)
    if not isinstance(data, dict):
        raise ImportInspectionError("Invalid repos.yaml structure.")
    if len(data) > MAX_REPOSITORIES + 1:
        raise ImportInspectionError("Repository declares too many repositories.")
    repos = []
    for alias, config in data.items():
        if alias in ("./odoo", "odoo"):
            continue
        if not isinstance(alias, str) or not isinstance(config, dict):
            continue
        remotes = config.get("remotes", {})
        if not isinstance(remotes, dict):
            continue
        repo_url = next(iter(remotes.values()), "")
        if not isinstance(repo_url, str) or not repo_url:
            continue
        branch = ""
        target = config.get("target", "")
        if isinstance(target, str) and target:
            parts = target.split()
            branch = parts[-1]
        merges = config.get("merges", [])
        if not branch and isinstance(merges, list) and merges:
            if isinstance(merges[0], str):
                branch = merges[0].split()[-1]
        branch = branch.replace("$ODOO_VERSION", odoo_version) if branch else "main"
        repos.append({
            "url": to_https(repo_url),
            "branch": validate_branch(branch),
            "alias": alias[:255],
        })
    if len(repos) > MAX_REPOSITORIES:
        raise ImportInspectionError("Repository declares too many repositories.")
    return repos


def _parse_addons_yaml(content, check=None):
    """Parse bounded addons configuration into alias-to-addons text."""
    data = safe_load_yaml(content, check=check)
    if not isinstance(data, dict):
        raise ImportInspectionError("Invalid addons.yaml structure.")
    if len(data) > MAX_REPOSITORIES:
        raise ImportInspectionError("Repository declares too many addon aliases.")
    result = {}
    for alias, addons in data.items():
        if isinstance(addons, list):
            result[str(alias)] = (
                "" if "*" in addons else ",".join(str(item) for item in addons)
            )
        elif addons == "*":
            result[str(alias)] = ""
        else:
            result[str(alias)] = str(addons) if addons else ""
    return result


def _parse_copier_answers(content, check=None):
    """Parse bounded copier answers into values consumed by the importer."""
    data = safe_load_yaml(content, check=check)
    if not isinstance(data, dict):
        raise ImportInspectionError("Invalid copier answers structure.")
    domains = []
    raw_domains = data.get("domains_prod", []) or []
    if not isinstance(raw_domains, list):
        raise ImportInspectionError("Invalid production domains configuration.")
    for domain in raw_domains:
        if not isinstance(domain, dict):
            continue
        hosts = domain.get("hosts", []) or []
        if not isinstance(hosts, list):
            raise ImportInspectionError("Invalid production domains configuration.")
        for host in hosts:
            domains.append({
                "hostname": str(host),
                "redirect_to": str(domain.get("redirect_to", "") or ""),
            })
            if len(domains) > MAX_DOMAINS:
                raise ImportInspectionError("Repository declares too many domains.")
    raw_version = str(data.get("odoo_version", "") or "")
    match = re.match(r"^(\d+\.\d+)", raw_version)
    version = match.group(1) if match else ""
    if version not in _SUPPORTED_VERSIONS:
        version = ""
    return {
        "odoo_version": version,
        "project_author": str(data.get("project_author", "") or ""),
        "project_license": str(data.get("project_license", "") or ""),
        "project_name": str(data.get("project_name", "") or ""),
        "postgres_version": str(data.get("postgres_version", "") or ""),
        "postgres_dbname": str(data.get("postgres_dbname", "prod") or "prod"),
        "postgres_username": str(data.get("postgres_username", "odoo") or "odoo"),
        "odoo_proxy": str(data.get("odoo_proxy", "traefik") or "traefik"),
        "odoo_initial_lang": str(data.get("odoo_initial_lang", "") or ""),
        "smtp_relay_host": str(data.get("smtp_relay_host", "") or ""),
        "smtp_relay_port": data.get("smtp_relay_port", 587),
        "smtp_relay_user": str(data.get("smtp_relay_user", "") or ""),
        "domains": domains,
    }


class GitHubImportInspector:
    """Inspect imports through bounded GitHub API tree/blob requests."""

    def __init__(self, clients, budget=None):
        """Initialize the inspector with ordered authentication clients.

        :param list clients: App, PAT and anonymous clients in fallback order
        :param HTTPBudget budget: shared budget override used by tests
        """
        self.clients = list(clients)
        self.budget = budget or HTTPBudget(
            MAX_HTTP_REQUESTS, MAX_HTTP_BYTES, IMPORT_TIMEOUT_SECONDS,
        )
        self.config_bytes = 0
        self._repo_clients = {}

    def inspect_preview(self, url, branch="main"):
        """Return the bounded submodule preview for one repository."""
        self._check_budget()
        view = self._open_repository(url, branch)
        submodules, warning = self._submodules(view)
        version = self._version_from_tree(
            view, {sub["path"] for sub in submodules}, branch,
        )
        for submodule in submodules:
            normalized, normalized_warning = self._normalize_submodule_branch(
                submodule, version,
            )
            submodule["branch"] = normalized
            warning = warning or normalized_warning
        result = {
            "ok": True,
            "repo_name": view["repo"],
            "submodules": submodules,
            "odoo_version": version,
            "main_commit_sha": view["sha"],
        }
        if warning:
            result["sha_warning"] = warning
        self._check_budget()
        return result

    def inspect_import(self, url, branch="main"):
        """Return all bounded configuration needed to create a project."""
        self._check_budget()
        view = self._open_repository(url, branch)
        paths = view["paths"]
        doodba_path = "odoo/custom/src/repos.yaml"
        if doodba_path in paths and self._is_regular(paths[doodba_path]):
            result = self._inspect_doodba(view)
        elif ".gitmodules" in paths and self._is_regular(paths[".gitmodules"]):
            result = self._inspect_odoosh(view)
        else:
            result = self._inspect_simple(view)
        result.update({
            "ok": True,
            "repo_name": view["repo"],
            "main_commit_sha": view["sha"],
        })
        self._check_budget()
        return result

    def ensure_budget(self):
        """Raise the stable import error when the shared deadline expired."""
        self._check_budget()

    def _open_repository(self, url, branch):
        """Select an accessible client and load a bounded recursive tree."""
        owner, repo = parse_github_repo_url(url)
        branch = validate_branch(branch)
        last_error = None
        for client in self.clients:
            try:
                metadata = self._get(
                    client, f"/repos/{self._seg(owner)}/{self._seg(repo)}",
                    MAX_BLOB_TRANSPORT_BYTES,
                )
                branch_data = self._get(
                    client,
                    f"/repos/{self._seg(owner)}/{self._seg(repo)}"
                    f"/branches/{self._seg(branch)}",
                    MAX_BLOB_TRANSPORT_BYTES,
                )
                sha = str(branch_data.get("commit", {}).get("sha", ""))
                if not re.fullmatch(r"[a-fA-F0-9]{7,64}", sha):
                    raise ImportInspectionError("Repository branch has no valid commit.")
                tree = self._get(
                    client,
                    f"/repos/{self._seg(owner)}/{self._seg(repo)}"
                    f"/git/trees/{self._seg(sha)}?recursive=1",
                    MAX_TREE_BYTES,
                )
                entries = tree.get("tree", [])
                if tree.get("truncated"):
                    raise ImportInspectionError("Repository tree is too large.")
                if not isinstance(entries, list) or len(entries) > MAX_TREE_ENTRIES:
                    raise ImportInspectionError("Repository tree is too large.")
                paths = {}
                for index, entry in enumerate(entries):
                    if index % 256 == 0:
                        self._check_budget()
                    if not isinstance(entry, dict):
                        continue
                    path = entry.get("path")
                    if isinstance(path, str) and path:
                        paths[path] = entry
                key = (owner.lower(), repo.lower())
                self._repo_clients[key] = (client, metadata)
                return {
                    "url": to_https(url),
                    "owner": owner,
                    "repo": repo,
                    "branch": branch,
                    "sha": sha,
                    "client": client,
                    "metadata": metadata,
                    "entries": entries,
                    "paths": paths,
                }
            except (GitHubAPIError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                continue
        raise ImportInspectionError(
            "Could not access the repository. Check the URL and GitHub credentials."
        ) from last_error

    def _inspect_doodba(self, view):
        """Inspect Doodba configuration without checking out repository files."""
        copier = {}
        if ".copier-answers.yml" in view["paths"]:
            copier = _parse_copier_answers(
                self._read_blob(view, ".copier-answers.yml"),
                check=self._check_budget,
            )
        version = copier.get("odoo_version", "")
        if not version and _VERSION_BRANCH_RE.fullmatch(view["branch"]):
            version = (
                view["branch"] if view["branch"] in _SUPPORTED_VERSIONS else ""
            )
        repos = _parse_repos_yaml(
            self._read_blob(view, "odoo/custom/src/repos.yaml"), version,
            check=self._check_budget,
        )
        addons_path = "odoo/custom/src/addons.yaml"
        addons = {}
        if addons_path in view["paths"]:
            addons = _parse_addons_yaml(
                self._read_blob(view, addons_path),
                check=self._check_budget,
            )
        for repo in repos:
            repo["addons"] = addons.get(repo["alias"], "")
            repo["requirements"] = ""
        if not version:
            version = self._version_from_tree(view, set(), view["branch"])
        pip_deps = self._read_optional(
            view, "odoo/custom/dependencies/pip.txt",
        ).strip()
        apt_deps = self._read_optional(
            view, "odoo/custom/dependencies/apt.txt",
        ).strip()
        conf_paths = sorted(
            path for path, entry in view["paths"].items()
            if path.startswith("odoo/custom/conf.d/")
            and path.endswith(".conf") and self._is_regular(entry)
        )
        if len(conf_paths) > MAX_CONF_FILES:
            raise ImportInspectionError("Repository declares too many config files.")
        odoo_conf = "\n".join(
            self._read_blob(view, path).strip() for path in conf_paths
        )
        return {
            "repo_type": "doodba",
            "repos_data": repos,
            "copier": copier,
            "pip_deps": pip_deps,
            "apt_deps": apt_deps,
            "odoo_conf": odoo_conf,
            "odoo_version": version,
            "submodules": [],
        }

    def _inspect_odoosh(self, view):
        """Inspect an Odoo.sh-style main repository and its submodules."""
        submodules, warning = self._submodules(view)
        version = (
            view["branch"]
            if view["branch"] in _SUPPORTED_VERSIONS
            else self._version_from_tree(
                view, {sub["path"] for sub in submodules}, view["branch"],
            )
        )
        repos = [{
            "url": view["url"],
            "branch": view["branch"],
            "alias": view["repo"],
            "addons": "",
        }]
        for submodule in submodules:
            normalized, normalized_warning = self._normalize_submodule_branch(
                submodule, version,
            )
            warning = warning or normalized_warning
            repos.append({
                "url": submodule["url"],
                "branch": normalized,
                "alias": submodule["path"].split("/")[-1],
                "addons": "",
                "commit_sha": submodule.get("commit_sha", ""),
            })
        if len(repos) > MAX_REPOSITORIES:
            raise ImportInspectionError("Repository declares too many repositories.")
        for index, repo in enumerate(repos):
            reference = (
                view["sha"] if index == 0
                else repo.get("commit_sha") or repo["branch"]
            )
            repo["requirements"] = self._read_repository_file(
                repo["url"], reference, "requirements.txt",
            )
        result = {
            "repo_type": "odoosh",
            "repos_data": repos,
            "copier": {},
            "pip_deps": "",
            "apt_deps": "",
            "odoo_conf": "",
            "odoo_version": version,
            "submodules": submodules,
        }
        if warning:
            result["sha_warning"] = warning
        return result

    def _inspect_simple(self, view):
        """Inspect a simple repository and fetch its requirements once."""
        version = self._version_from_tree(view, set(), view["branch"])
        repo = {
            "url": view["url"],
            "branch": view["branch"],
            "alias": view["repo"],
            "addons": "",
        }
        repo["requirements"] = self._read_repository_file(
            repo["url"], view["sha"], "requirements.txt",
        )
        return {
            "repo_type": "simple",
            "repos_data": [repo],
            "copier": {},
            "pip_deps": "",
            "apt_deps": "",
            "odoo_conf": "",
            "odoo_version": version,
            "submodules": [],
        }

    def _submodules(self, view):
        """Read submodule declarations and attach pinned SHAs from the tree."""
        if ".gitmodules" not in view["paths"]:
            return [], ""
        content = self._read_blob(view, ".gitmodules")
        submodules = _parse_gitmodules(content, view["url"])
        for submodule in submodules:
            entry = view["paths"].get(submodule["path"], {})
            if entry.get("mode") == "160000" and entry.get("type") == "commit":
                submodule["commit_sha"] = str(entry.get("sha", ""))
        return submodules, ""

    def _normalize_submodule_branch(self, submodule, project_version):
        """Normalize a stale version branch using at most three API compares."""
        declared = (submodule.get("branch") or "").strip()
        if not declared or declared == ".":
            return project_version or "main", ""
        if not (
            _VERSION_BRANCH_RE.fullmatch(declared)
            and project_version
            and declared != project_version
        ):
            return validate_branch(declared), ""
        sha = submodule.get("commit_sha", "")
        if not sha:
            return project_version, self._normalization_warning()
        owner, repo = parse_github_repo_url(submodule["url"])
        client, metadata = self._repository_client(owner, repo)
        candidates = [
            candidate
            for candidate in dict.fromkeys((
                project_version,
                declared,
                str(metadata.get("default_branch", "") or ""),
            ))
            if candidate
        ]
        for candidate in candidates[:3]:
            try:
                compare = self._get(
                    client,
                    f"/repos/{self._seg(owner)}/{self._seg(repo)}"
                    f"/compare/{self._seg(sha)}...{self._seg(candidate)}",
                    MAX_BLOB_TRANSPORT_BYTES,
                )
            except GitHubAPIError:
                continue
            if compare.get("status") in ("ahead", "identical"):
                return validate_branch(candidate), ""
        return project_version, self._normalization_warning()

    def _version_from_tree(self, view, skip_paths, branch):
        """Detect Odoo version from a bounded set of own manifest blobs."""
        manifests = []
        for index, (path, entry) in enumerate(view["paths"].items()):
            if index % 256 == 0:
                self._check_budget()
            if not path.endswith("/__manifest__.py") or not self._is_regular(entry):
                continue
            if any(path == skip or path.startswith(f"{skip}/") for skip in skip_paths):
                continue
            manifests.append(path)
        if len(manifests) > MAX_MANIFESTS:
            raise ImportInspectionError("Repository contains too many manifests.")
        manifests.sort(key=lambda path: (path.count("/") != 1, path))
        for path in manifests:
            self._check_budget()
            version = version_from_manifest_text(
                self._read_blob(
                    view, path, max_decoded_bytes=MAX_MANIFEST_BYTES,
                    too_large_message="Repository manifest is too large.",
                )
            )
            self._check_budget()
            if version:
                return version
        return branch if branch in _SUPPORTED_VERSIONS else ""

    def _read_optional(self, view, path):
        """Read a regular blob when present, otherwise return empty text."""
        entry = view["paths"].get(path)
        if not entry:
            return ""
        return self._read_blob(view, path)

    def _read_blob(self, view, path, *, max_decoded_bytes=MAX_BLOB_BYTES,
                   too_large_message="Repository configuration file is too large."):
        """Read and decode one selected regular Git blob under all caps."""
        entry = view["paths"].get(path)
        if not entry or not self._is_regular(entry):
            raise ImportInspectionError("Repository configuration is not a regular file.")
        size = entry.get("size", 0)
        if (
            not isinstance(size, int)
            or size < 0
            or size > max_decoded_bytes
        ):
            raise ImportInspectionError(too_large_message)
        data = self._get(
            view["client"],
            f"/repos/{self._seg(view['owner'])}/{self._seg(view['repo'])}"
            f"/git/blobs/{self._seg(str(entry.get('sha', '')))}",
            MAX_BLOB_TRANSPORT_BYTES,
        )
        return self._decode_blob(
            data, size, max_decoded_bytes=max_decoded_bytes,
            too_large_message=too_large_message,
        )

    def _read_repository_file(self, url, reference, path):
        """Read an optional repository file once through the bounded contents API."""
        owner, repo = parse_github_repo_url(url)
        client, _metadata = self._repository_client(owner, repo)
        try:
            data = self._get(
                client,
                f"/repos/{self._seg(owner)}/{self._seg(repo)}"
                f"/contents/{self._seg(path)}?ref={self._seg(reference)}",
                MAX_BLOB_TRANSPORT_BYTES,
            )
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return ""
            raise
        declared = data.get("size", 0)
        if not isinstance(declared, int) or declared > MAX_BLOB_BYTES:
            raise ImportInspectionError("Repository requirements file is too large.")
        return self._decode_blob(data, declared)

    def _decode_blob(self, data, declared_size, *,
                     max_decoded_bytes=MAX_BLOB_BYTES,
                     too_large_message="Repository content size is invalid."):
        """Decode a GitHub base64 blob and charge its decoded configuration size."""
        if data.get("encoding") != "base64":
            raise ImportInspectionError("Unsupported repository content encoding.")
        encoded = "".join(str(data.get("content", "")).split())
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImportInspectionError("Invalid repository content encoding.") from exc
        if len(raw) > max_decoded_bytes:
            raise ImportInspectionError(too_large_message)
        if (
            isinstance(declared_size, int) and declared_size >= 0
            and len(raw) != declared_size
        ):
            raise ImportInspectionError("Repository content size is invalid.")
        self.config_bytes += len(raw)
        if self.config_bytes > MAX_CONFIG_BYTES:
            raise ImportInspectionError("Repository configuration is too large.")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ImportInspectionError("Repository configuration is not UTF-8.") from exc

    def _repository_client(self, owner, repo):
        """Return and cache a client that can access repository metadata."""
        key = (owner.lower(), repo.lower())
        if key in self._repo_clients:
            return self._repo_clients[key]
        last_error = None
        for client in self.clients:
            try:
                metadata = self._get(
                    client, f"/repos/{self._seg(owner)}/{self._seg(repo)}",
                    MAX_BLOB_TRANSPORT_BYTES,
                )
                self._repo_clients[key] = (client, metadata)
                return client, metadata
            except GitHubAPIError as exc:
                last_error = exc
        raise ImportInspectionError(
            "Could not access a repository declared by the project."
        ) from last_error

    def _get(self, client, endpoint, max_bytes):
        """Issue one GET and translate exhausted shared budgets safely."""
        try:
            self.budget.check()
            result = client.get(
                endpoint, budget=self.budget, max_bytes=max_bytes,
            )
            self.budget.check()
            return result
        except HTTPBudgetExceeded as exc:
            raise ImportInspectionError(
                "Repository inspection exceeded its safety budget."
            ) from exc

    def _check_budget(self):
        """Translate an expired shared deadline to the stable import error."""
        try:
            self.budget.check()
        except HTTPBudgetExceeded as exc:
            raise ImportInspectionError(
                "Repository inspection exceeded its safety budget."
            ) from exc

    @staticmethod
    def _is_regular(entry):
        """Return whether a tree entry is a regular Git blob, not a symlink."""
        return (
            isinstance(entry, dict)
            and entry.get("type") == "blob"
            and entry.get("mode") in _REGULAR_BLOB_MODES
        )

    @staticmethod
    def _seg(value):
        """Quote one untrusted GitHub API path segment."""
        return quote(str(value), safe="")

    @staticmethod
    def _normalization_warning():
        """Return the stable warning for an unproven submodule branch."""
        return (
            "A submodule branch could not be verified within the safety "
            "budget and was normalized to the project version. Review it "
            "before deploying."
        )
