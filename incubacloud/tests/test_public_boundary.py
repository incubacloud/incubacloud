"""Core names nothing of one particular deployment.

Core is public and ships to anyone who runs a panel. A module that
extends the panel may serve customers of its own from servers of its
own, but core does not know it: no tenants, no hosted service, no names
of somebody's servers, Compose stacks, private plans or subdomains — not
in code, comments, docstrings, help texts, tests or documentation. Each
such word was harmless where it crept in; together they described one
deployment's inside to anyone reading the repository, and to the
assistant, which searches core's code.

The repository is walked from disk, so a file not committed yet is read
too. What is still allowed is listed below with its reason. A file's
allowance is an exact count: one match more fails, and so does one
fewer, so a cleaned file loses its entry instead of keeping a door open.
A file the walk does not find (an image keeps only the modules) is not
checked.
"""
import pathlib
import re

from odoo.tests.common import BaseCase

#: What core must not say, by the name a failure reports.
_RULES = {
    "saas": re.compile(r"saas", re.IGNORECASE),
    "tenant": re.compile(r"tenant", re.IGNORECASE),
    "warm pool": re.compile(r"warm[ _-]?pool", re.IGNORECASE),
    "server name": re.compile(r"(?<![A-Za-z])Cloud ?1(?![0-9])"),
    "stack name": re.compile(r"incubacloud-(?:production|qa)\b", re.IGNORECASE),
    "private plan": re.compile(
        r"planes-mejoras|\bplan-[a-z0-9-]+\.md", re.IGNORECASE,
    ),
    "subdomain": re.compile(
        r"(?<![\w.-])((?:[a-z0-9-]+\.)+)incubacloud\.io", re.IGNORECASE,
    ),
}

#: The project's public names: its documentation, its site, its status page.
_PUBLIC_SUBDOMAINS = frozenset({"docs.", "www.", "status."})

#: Tokens removed before matching, each with its reason.
_ALLOWED_TOKENS = {
    # A CSS class the screens of modules on top share with core's
    # Settings. Renaming it needs an alias in those modules in the same
    # release, so it goes on its own.
    "rl-saas-fsec": "a shared CSS class, renamed together with its users",
}

#: Folders whose files may use some of the words, and why.
_ALLOWED_TREES = {
    "images/relay-oauth/": (
        {"tenant"},
        (
            "Microsoft 365's tenant (its Entra ID directory): the relay's "
            "own vocabulary, not a customer of anyone's"
        ),
    ),
    "docs/user/": (
        {"saas", "tenant", "subdomain"},
        (
            "the user guide of both editions, the hosted one included, kept "
            "in core on purpose (10-nov-2026); it says what the hosted "
            "service offers, never how it is run"
        ),
    ),
    "incubacloud_pitr/": (
        set(_RULES),
        "in a release candidate under way; cleaned once it lands",
    ),
}

#: Released migrations: they ran once and are history. 1.0.102 reads a
#: layer's parameters on purpose (kept as it is, 10-nov-2026).
_MIGRATIONS = re.compile(r"^[a-z_]+/migrations/")
_MIGRATION_RULES = {"saas", "tenant", "warm pool"}

_CANDIDATE = "in a release candidate under way; cleaned once it lands"
_LABELS = (
    "the observability labels tenant and tenant_id, renamed with the "
    "dashboards of their own"
)
_VISIBLE = "help texts and screens, rewritten with their Spanish"
_RUNBOOK = "a runbook both editions use, pending its rewrite"
_LAYER_TESTS = (
    "a check core runs over the modules of a layer, naming them; it moves "
    "to that layer"
)

#: Files allowed an exact number of matches, and why.
_ALLOWED_FILES = {
    "docs/architecture.md": (47, _CANDIDATE),
    "incubacloud/i18n/es.po": (9, _CANDIDATE),
    "incubacloud/models/cloud_instance.py": (27, _CANDIDATE),
    "incubacloud/models/cloud_instance_domain.py": (2, _CANDIDATE),
    "incubacloud/models/cloud_project.py": (2, _CANDIDATE),
    "incubacloud/models/cloud_repo_mixin.py": (2, _CANDIDATE),
    "incubacloud/models/deploy_instance_executor.py": (7, _CANDIDATE),
    "incubacloud/scripts/deploy.sh": (3, _CANDIDATE),
    "incubacloud/ansible/playbooks/host_observability.yml": (11, _LABELS),
    "incubacloud/models/observability_agents_executor.py": (7, _LABELS),
    "incubacloud/models/cloud_settings.py": (3, _VISIBLE),
    "incubacloud/models/cloud_host.py": (1, _VISIBLE),
    "incubacloud/models/cloud_backup_backend.py": (1, _VISIBLE),
    "incubacloud/static/src/components/settings/settings.xml": (1, _VISIBLE),
    "docs/runbooks/RB-02-restore-master-db.md": (2, _RUNBOOK),
    "docs/runbooks/RB-11-metrics-backend-unreachable.md": (5, _RUNBOOK),
    "docs/runbooks/RB-24-direct-path.md": (3, _RUNBOOK),
    "docs/runbooks/RB-25-visitor-address-behind-a-cdn.md": (2, _RUNBOOK),
    "incubacloud/tests/test_form_field_width.py": (2, _LAYER_TESTS),
    "incubacloud/tests/test_responsive_css.py": (5, _LAYER_TESTS),
    "incubacloud/tests/test_platform_actor_invariant.py": (5, _LAYER_TESTS),
    "incubacloud/tests/executor_write_rules.py": (2, _LAYER_TESTS),
    "incubacloud/tests/idempotency_harness.py": (2, _LAYER_TESTS),
    "incubacloud/static/src/utils/icons.js": (
        2, "an icon's name, which the screens of modules on top ask for",
    ),
}

#: Text files by extension; anything else is not read.
_TEXT_SUFFIXES = frozenset({
    ".py", ".js", ".xml", ".scss", ".css", ".md", ".yml", ".yaml", ".sh",
    ".bats", ".txt", ".po", ".pot", ".toml", ".cfg", ".ini", ".json",
    ".html", ".conf", ".csv", ".rst", ".j2", ".sql",
})
_TEXT_NAMES = frozenset({"Dockerfile"})

#: Folders never walked: version control, caches, built or vendored files.
_SKIPPED_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "site", ".pytest_cache",
})

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SELF = pathlib.Path(__file__).resolve()


def _text_files():
    """Yield ``(relative posix path, text)`` for every text file of core.

    :return: an iterator over the files the rules apply to.
    """
    for path in sorted(_REPO.rglob("*")):
        relative = path.relative_to(_REPO)
        if _SKIPPED_DIRS.intersection(relative.parts[:-1]):
            continue
        if "lib" in relative.parts and "static" in relative.parts:
            continue
        if not path.is_file() or path == _SELF:
            continue
        if path.suffix not in _TEXT_SUFFIXES and path.name not in _TEXT_NAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        yield relative.as_posix(), text


def _matches(text):
    """Return ``[(rule, line number, line)]`` for every match in *text*.

    :param str text: a file's content.
    :rtype: list[tuple[str, int, str]]
    """
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for token in _ALLOWED_TOKENS:
            line = line.replace(token, "")
        for rule, pattern in _RULES.items():
            for match in pattern.finditer(line):
                if rule == "subdomain" and (
                    match.group(1).lower() in _PUBLIC_SUBDOMAINS
                ):
                    continue
                found.append((rule, number, line.strip()))
    return found


def _tree_allows(path, rule):
    """Whether a folder exception covers *rule* in *path*.

    :param str path: relative posix path of the file.
    :param str rule: a key of ``_RULES``.
    :rtype: bool
    """
    if _MIGRATIONS.match(path) and rule in _MIGRATION_RULES:
        return True
    return any(
        path.startswith(prefix) and rule in rules
        for prefix, (rules, _reason) in _ALLOWED_TREES.items()
    )


class TestCoreNamesNoDeployment(BaseCase):
    """Nothing of one deployment in core, beyond what is listed."""

    @classmethod
    def setUpClass(cls):
        """Read the repository once: ``{path: matches}`` of what is left."""
        super().setUpClass()
        cls.found = {}
        for path, text in _text_files():
            matches = [
                match for match in _matches(text)
                if not _tree_allows(path, match[0])
            ]
            if matches or path in _ALLOWED_FILES:
                cls.found[path] = matches

    def test_no_file_says_what_core_must_not(self):
        """Every match is in a file allowed it, and within its count."""
        problems = []
        for path, matches in sorted(self.found.items()):
            allowed, _reason = _ALLOWED_FILES.get(path, (0, ""))
            if len(matches) > allowed:
                problems.append(
                    f"{path}: {len(matches)} matches, {allowed} allowed"
                )
                problems.extend(
                    f"  {line_no}: [{rule}] {line}"
                    for rule, line_no, line in matches
                )
        self.assertFalse(
            problems,
            "core must not name a deployment, its tenants or its hosts; "
            "write it in general terms (a module that extends the panel, a "
            "managed panel, a production host, example.com):\n"
            + "\n".join(problems),
        )

    def test_every_allowance_is_still_needed(self):
        """A file cleaned below its count must lower or lose its entry."""
        stale = [
            f"{path}: {len(self.found[path])} matches, {allowed} allowed"
            for path, (allowed, _reason) in sorted(_ALLOWED_FILES.items())
            if path in self.found and len(self.found[path]) < allowed
        ]
        self.assertFalse(
            stale,
            "lower these allowances to what is left, or drop them:\n"
            + "\n".join(stale),
        )

    def test_the_rules_catch_what_they_are_for(self):
        """Each rule fires on its own example and spares the public names."""
        examples = {
            "saas": "the SaaS manager overrides this",
            "tenant": "a tenant panel",
            "warm pool": "a warm_pool spare",
            "server name": "on Cloud1 and on Cloud 1",
            "stack name": "incubacloud-production-odoo-1",
            "private plan": "see planes-mejoras/plan-x.md",
            "subdomain": "https://metrics.incubacloud.io/",
        }
        for rule, example in examples.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, {m[0] for m in _matches(example)})
        public = (
            "https://docs.incubacloud.io/ www.incubacloud.io "
            "status.incubacloud.io incubacloud 1.0.11 rl-saas-fsec"
        )
        self.assertEqual(_matches(public), [])
