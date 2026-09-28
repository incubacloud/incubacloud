"""The executor write rule, checked over source code.

    The job's transaction is not written. While an executor runs, every
    write that must last goes through ``_durable_env()`` or
    ``_persist()``; ``on_success``/``on_failure`` already run on a
    durable cursor and write with ``self.env``.

Why, in short (``AbstractExecutor._durable_env`` has the long version):
``cloud.job.execute`` ends with ``env.clear()``, which drops every write
not flushed yet; queue_job rolls the transaction back when the job fails
and forbids committing it midway; and a flushed write holds its row
until the job ends, so a hook writing that row waits forever. Core
1.0.135 lost the metrics deletion key that way, and a tenant claim left
an active OIDC client behind on every tenant.

This module is importable, not a test: each repository with executors
runs :func:`check` over its own model files from a test of its own, so
the rule is enforced wherever an executor can be written.

What it cannot see: a write made *inside* a model helper the executor
calls (``settings._ensure_operator_credential()`` writes, but its call
site looks like any other). The survival tests of
``test_executor_durable_writes`` cover those, and the convention is that
a model helper meant for executors writes through the environment of the
record it is called on — which the executor then binds to a durable one.
"""
import ast
from pathlib import Path

RULE = (
    "the job's transaction is not written: write through "
    "self._durable_env() or self._persist(), or from on_success/on_failure"
)

#: Where the durable primitives live: the one file allowed to open
#: cursors, and whose cursor blocks count as durable.
PRIMITIVES_FILE = "abstract_executor.py"

#: A class is an executor when one of its bases is one of these, or is
#: itself an executor. Mixins an executor inherits from are checked too.
EXECUTOR_ROOTS = frozenset({"AbstractExecutor", "AbstractSSHExecutor"})

#: Method names whose call writes to the database.
WRITE_CALLS = frozenset({
    "write", "create", "unlink", "_transition",
    "enqueue", "enqueue_chain",
    "raise_alert", "resolve_alert", "set_param",
})

#: The terminal hooks, which ``_dispatch_outcome`` runs on a durable cursor.
HOOKS = frozenset({"on_success", "on_failure"})

#: Methods that write with ``self.env`` and are only ever called from a
#: hook, so they write on the hook's durable cursor. Each is verified to
#: have no other caller.
HOOK_HELPERS = {
    "_record_shipped_accounts": "records the ACL a successful run shipped",
    "_sync_backup_records": "mirrors the listed backups after success",
    "_enqueue_coalesced_rebuild_if_pending": "chains a rebuild once done",
    "_alert_on_purge_failure": "alerts on a failed teardown",
    "_stamp_core_commit": "stamps the core release a run shipped",
    "_stamp_tenant_module_commit": "stamps the tenant module a run shipped",
}

#: Calls named like a write that do not touch the database, keyed by
#: ``(method, receiver)`` as written in the source.
NOT_THE_ORM = {
    ("_write_private", "fh"): "writes a local file, 0600, for Ansible",
    ("_conf_content", "cp"): "ConfigParser rendering odoo.conf to a string",
    ("_conf_content", "base_cp"): "ConfigParser rendering odoo.conf to a string",
}


def _base_names(cls):
    """Return the last dotted component of every base of *cls*."""
    names = set()
    for base in cls.bases:
        if isinstance(base, ast.Attribute):
            names.add(base.attr)
        elif isinstance(base, ast.Name):
            names.add(base.id)
    return names


def _executor_classes(trees):
    """Return the names of the classes the rule applies to.

    Executors first, to a fixpoint (a subclass of an executor is one),
    then every class of the scanned code that an executor inherits from:
    a mixin's methods run inside the job exactly like the executor's.

    :param trees: ``{filename: ast.Module}``
    """
    classes = {
        node.name: node
        for tree in trees.values()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }
    executors = set()
    changed = True
    while changed:
        changed = False
        for name, cls in classes.items():
            if name in executors:
                continue
            if _base_names(cls) & (EXECUTOR_ROOTS | executors):
                executors.add(name)
                changed = True
    mixins = {
        base
        for name in executors
        for base in _base_names(classes[name])
        if base in classes
    }
    return executors | mixins


def _opens_cursor(call):
    """Return True if *call* opens a cursor of its own."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "read_committed_cursor"
    return (
        isinstance(func, ast.Attribute) and func.attr == "cursor"
        and ast.unparse(func.value).endswith("registry")
    )


def _is_durable_with(node, filename):
    """Return True if the ``with`` statement *node* opens a durable context."""
    for item in node.items:
        expr = item.context_expr
        if not isinstance(expr, ast.Call):
            continue
        if ast.unparse(expr.func).endswith("_durable_env"):
            return True
        if filename == PRIMITIVES_FILE and _opens_cursor(expr):
            return True
    return False


class _MethodVisitor(ast.NodeVisitor):
    """Collect cursor openings, unguarded writes and helper calls of a method."""

    def __init__(self, filename):
        """Start with nothing collected, outside any durable context."""
        self.filename = filename
        self.durable = 0
        self.cursors = []
        self.writes = []
        self.calls = []

    def visit_With(self, node):
        """Track whether the body runs inside a durable context."""
        durable = _is_durable_with(node, self.filename)
        self.durable += durable
        self.generic_visit(node)
        self.durable -= durable

    visit_AsyncWith = visit_With

    def visit_Call(self, node):
        """Classify one call."""
        func = node.func
        if _opens_cursor(node):
            self.cursors.append(node)
        if isinstance(func, ast.Attribute):
            if func.attr in WRITE_CALLS and not self.durable:
                self.writes.append(node)
            if ast.unparse(func.value) == "self":
                self.calls.append((func.attr, node))
        self.generic_visit(node)


def _parse(sources):
    """Return ``{filename: ast.Module}`` for ``{filename: source}``."""
    return {name: ast.parse(text, filename=name) for name, text in sources.items()}


def check_sources(sources, context=None):
    """Return the rule violations in *sources*, one readable line each.

    :param dict sources: ``{filename: source}`` to check
    :param dict context: ``{filename: source}`` read only to resolve
        base classes, e.g. core's executors when checking saas
    :return: a sorted list of ``"file:line Class.method: message"``
    """
    trees = _parse(sources)
    context_trees = _parse(context or {})
    executors = _executor_classes({**context_trees, **trees})
    violations = []
    helper_callers = {}
    for filename, tree in trees.items():
        base = Path(filename).name
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            if cls.name not in executors:
                continue
            for method in cls.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                visitor = _MethodVisitor(base)
                visitor.visit(method)
                where = f"{base}:{{}} {cls.name}.{method.name}"
                if base != PRIMITIVES_FILE:
                    violations.extend(
                        where.format(c.lineno) + ": opens its own cursor; "
                        "use self._durable_env()"
                        for c in visitor.cursors
                    )
                for name, call in visitor.calls:
                    helper_callers.setdefault(name, []).append(
                        (where.format(call.lineno), method.name),
                    )
                if method.name in HOOKS or method.name in HOOK_HELPERS:
                    continue
                for call in visitor.writes:
                    receiver = ast.unparse(call.func.value)
                    if receiver.startswith("Path("):
                        continue  # the filesystem, not the ORM
                    if (method.name, receiver) in NOT_THE_ORM:
                        continue
                    violations.append(
                        where.format(call.lineno)
                        + f": {receiver}.{call.func.attr}() — {RULE}"
                    )
    for helper in HOOK_HELPERS:
        for where, caller in helper_callers.get(helper, ()):
            if caller not in HOOKS:
                violations.append(
                    f"{where}: calls {helper}(), which writes on the hook's "
                    "cursor and is only allowed from on_success/on_failure"
                )
    return sorted(violations)


def check(paths, context_paths=()):
    """Return the rule violations in the Python files *paths*.

    :param paths: files to check
    :param context_paths: files read only to resolve base classes
    :return: see :func:`check_sources`
    """
    return check_sources(_read_sources(paths), _read_sources(context_paths))


def executors_in(paths, context_paths=()):
    """Return the names of the executor classes defined in *paths*.

    A check that found no executor at all passes by construction, so
    each caller asserts this is not empty.

    :param paths: files whose classes are reported
    :param context_paths: files read only to resolve base classes
    """
    trees = _parse(_read_sources(paths))
    names = _executor_classes({**_parse(_read_sources(context_paths)), **trees})
    return {
        node.name
        for tree in trees.values()
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name in names
    }


def _read_sources(files):
    """Return ``{filename: source}`` for the Python files *files*."""
    return {str(p): Path(p).read_text(encoding="utf-8") for p in files}


def model_files(addon_dir):
    """Return the Python files of *addon_dir*'s ``models`` package, sorted."""
    return sorted(Path(addon_dir, "models").glob("*.py"))
