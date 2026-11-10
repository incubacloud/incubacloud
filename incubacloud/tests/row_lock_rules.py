"""The row lock rule, checked over source code.

    Our code does not take ``SELECT … FOR UPDATE`` (nor
    ``lock_for_update()`` / ``try_lock_for_update()`` without
    ``allow_referencing=True``) on a row other rows point to, except to
    delete the row or change a column of one of its unique indexes. It
    takes ``FOR NO KEY UPDATE``.

Why: both keep any other transaction from deleting or writing the row,
but ``FOR UPDATE`` followed by an ``UPDATE`` in the same transaction
marks the tuple as a change of its key. Any REPEATABLE READ transaction
begun before that one commits that then inserts or updates a row with a
foreign key to it fails with ``could not serialize access due to
concurrent update``: its check of the key (``FOR KEY SHARE``) collides
with a key it believes changed. ``FOR NO KEY UPDATE`` does not collide
with that check. A move failed in production that way on 7-nov-2026:
the probe of running panels held the instance ``FOR UPDATE`` and wrote
it, and the store of ``done`` of the move's job, keyed on that
instance, failed. Measured on PostgreSQL with a REPEATABLE READ
transaction opened first and an insert keyed on the row after: a plain
``UPDATE`` passes, ``FOR UPDATE`` + ``UPDATE`` fails, ``FOR NO KEY
UPDATE`` + ``UPDATE`` passes.

What the soft lock cannot do: for PostgreSQL the key of a row is every
plain column of a unique index that is not partial (deferrable or not), the
primary key's and, say, ``UNIQUE (project_id, name)`` of
``cloud.instance``. An ``UPDATE`` that changes one of those columns is a
change of the key whatever lock came before, or none: the older
transaction fails all the same (measured on 8-nov-2026: ``FOR NO KEY
UPDATE`` + another column passes, ``FOR NO KEY UPDATE`` + ``project_id``
fails, no lock + ``project_id`` fails). There the lock decides nothing,
and ``FOR UPDATE`` is as good: a layer's hand-over of a ready instance,
which moves it to its owner's project, is such a write.

A strong lock that is really needed carries the comment
``# lock: key-update needed — <why>`` on the line before its statement
or on one of the statement's own lines (the header lines, for an ``if``,
a ``with`` or another statement with a body). Docstrings and other bare
strings are not code and are not read; tests are not scanned.

What the check does not see, as it reads string constants and call
names only: SQL built at run time (``"FOR" + " UPDATE"``,
``"FOR %s" % "UPDATE"``, an f-string's formatted parts) and a lock
reached by name (``getattr(records, "lock_for_update")()``). And what it
sees wrongly: ``FOR UPDATE`` inside a text for the user or for the log
that is not SQL, which the marker lets through with its reason.

This module is importable, not a test: each repository runs
:func:`check` over its own addons from a test of its own.
"""
import ast
import io
import re
import tokenize
from pathlib import Path

RULE = (
    "lock FOR NO KEY UPDATE (lock_for_update(allow_referencing=True)); "
    "FOR UPDATE only to delete the row or change a column of a unique "
    "index, marked "
    "'# lock: key-update needed — <why>'"
)

#: The comment that allows a strong lock, with its reason.
MARKER = re.compile(r"#\s*lock:\s*key-update needed\s*(?:—|-+)\s*\S")

#: ``FOR UPDATE`` in SQL. ``FOR NO KEY UPDATE`` does not contain it.
STRONG_SQL = re.compile(r"\bFOR\s+UPDATE\b", re.IGNORECASE)

#: The ORM's row locks, strong unless ``allow_referencing=True``.
LOCK_CALLS = frozenset({"lock_for_update", "try_lock_for_update"})


def _marked_lines(source):
    """Return the numbers of the lines that carry the marker comment."""
    lines = set()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT and MARKER.search(tok.string):
            lines.add(tok.start[0])
    return lines


def _soft_call(call):
    """Return True if the lock call *call* passes ``allow_referencing=True``."""
    return any(
        kw.arg == "allow_referencing"
        and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in call.keywords
    )


#: The fields of a statement that hold nested statements, not its header.
_BLOCKS = ("body", "orelse", "handlers", "finalbody", "cases")


def _statement_lines(stmt):
    """Return the lines a marker may sit on to cover the statement *stmt*.

    The line before it and its own lines; for a statement with a body
    (``if``, ``with``, ``def``, …) only its header, so a marker inside
    the body does not cover the header's lock.

    :param ast.stmt stmt: the statement
    :rtype: range
    """
    blocks = [getattr(stmt, f, None) for f in _BLOCKS]
    if not any(isinstance(b, list) for b in blocks):
        return range(stmt.lineno - 1, stmt.end_lineno + 1)
    nested = {id(n) for b in blocks if isinstance(b, list) for n in b}
    header = [
        n.end_lineno for n in ast.iter_child_nodes(stmt)
        if id(n) not in nested and getattr(n, "end_lineno", None)
    ]
    return range(stmt.lineno - 1, max(header, default=stmt.lineno) + 1)


class _LockVisitor(ast.NodeVisitor):
    """Collect the strong locks of one module, with where they are."""

    def __init__(self, marked):
        """Start with nothing found, at module level.

        :param set marked: the lines that carry the marker comment
        """
        self.marked = marked
        self.scope = []
        self.statements = []
        self.found = []

    def visit(self, node):
        """Visit *node*, keeping the statement it belongs to at hand.

        :param ast.AST node: the node to visit
        :return: what its visitor returns
        """
        if not isinstance(node, ast.stmt):
            return super().visit(node)
        self.statements.append(node)
        try:
            return super().visit(node)
        finally:
            self.statements.pop()

    def _add(self, node, line, what):
        """Record a strong lock unless the marker covers *node* or its statement."""
        lines = set(range(node.lineno - 1, node.end_lineno + 1))
        if self.statements:
            lines.update(_statement_lines(self.statements[-1]))
        if self.marked & lines:
            return
        self.found.append((line, ".".join(self.scope) or "<module>", what))

    def _visit_scope(self, node):
        """Visit a class or function body under its name."""
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _visit_scope

    def visit_Expr(self, node):
        """Skip docstrings and other bare strings: they run nothing."""
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return
        self.generic_visit(node)

    def visit_Constant(self, node):
        """Flag a string holding ``FOR UPDATE``."""
        if isinstance(node.value, str):
            match = STRONG_SQL.search(node.value)
            if match:
                line = node.lineno + node.value[:match.start()].count("\n")
                self._add(node, line, "SELECT … FOR UPDATE")

    def visit_Call(self, node):
        """Flag ``lock_for_update()``/``try_lock_for_update()`` left strong."""
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        else:
            name = getattr(func, "id", None)
        if name in LOCK_CALLS and not _soft_call(node):
            self._add(node, node.lineno, f"{name}() without allow_referencing=True")
        self.generic_visit(node)


def strong_locks(source, filename="<source>"):
    """Return the strong locks in *source* that no marker allows.

    :param str source: Python source
    :param str filename: for the syntax error, if any
    :return: ``[(line, "Class.method", what)]``, in source order
    """
    visitor = _LockVisitor(_marked_lines(source))
    visitor.visit(ast.parse(source, filename=filename))
    return sorted(visitor.found)


def check_sources(sources, temporary=None):
    """Return the rule violations in *sources*, one readable line each.

    :param dict sources: ``{filename: source}`` to check
    :param dict temporary: exceptions while another branch fixes them,
        ``{(filename, "Class.method"): reason}``, *filename* matched as
        a suffix of the checked file's path. One that excuses nothing
        any more is reported too, so it is removed when its fix lands.
    :return: a sorted list of ``"file:line Class.method: message"``
    """
    temporary = dict(temporary or {})
    used = set()
    violations = []
    for filename, source in sources.items():
        for line, scope, what in strong_locks(source, filename):
            key = next(
                (k for k in temporary
                 if k[1] == scope and Path(filename).as_posix().endswith(k[0])),
                None,
            )
            if key:
                used.add(key)
                continue
            violations.append(f"{filename}:{line} {scope}: {what} — {RULE}")
    violations.extend(
        f"{path} {scope}: the temporary exception ({reason}) excuses "
        "nothing any more — remove it"
        for (path, scope), reason in temporary.items()
        if (path, scope) not in used
    )
    return sorted(violations)


def check(paths, temporary=None):
    """Return the rule violations in the Python files *paths*.

    :param paths: files to check
    :param dict temporary: see :func:`check_sources`
    :return: see :func:`check_sources`
    """
    return check_sources(
        {str(p): Path(p).read_text(encoding="utf-8") for p in paths}, temporary,
    )


def addon_files(repo_dir):
    """Return the Python files of every addon in *repo_dir*, tests excluded.

    An addon is a directory with a ``__manifest__.py``; its ``tests``
    packages are left out, its migrations are not.

    :param repo_dir: the repository's root
    :return: sorted paths
    """
    return sorted(
        path
        for manifest in Path(repo_dir).glob("*/__manifest__.py")
        for path in manifest.parent.rglob("*.py")
        if "tests" not in path.relative_to(manifest.parent).parts
    )
