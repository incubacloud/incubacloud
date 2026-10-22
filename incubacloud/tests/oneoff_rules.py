"""The one-off container rule, checked over source code.

    Every one-off container of an instance's stack starts through
    ``ic_compose_run`` (scripts) or ``COMPOSE_RUN`` (executors), and
    every "which services are running" goes through
    ``ic_running_services``; never the raw ``docker compose`` command.

Why: a ``docker compose run`` container inherits its service's labels.
Traefik routes visitors to it, and Sablier counts it in a sleeping
tenant's group, so the customer gets the waiting page while it lives
and Sablier stops it when the session expires (QA, 2026-10-22). And
from compose 2.29 on, ``docker compose ps --services --status running``
names a one-off after its service, so a script asking whether ``odoo``
runs gets yes for an ``odoo shell``. ``lib/common.sh`` holds the only
correct spelling of both.

This module is importable, not a test: each repository with scripts or
executors runs :func:`violations` over its own files from a test of its
own, so the rule holds wherever a one-off can be started.
"""
import ast
import re
from pathlib import Path

RULE = (
    "start one-off containers with ic_compose_run (scripts) or "
    "COMPOSE_RUN (executors), and read running services with "
    "ic_running_services: see scripts/lib/common.sh"
)

#: The library that defines the helpers, the one place allowed to spell
#: the raw commands.
LIBRARY = Path("scripts") / "lib" / "common.sh"

_RAW_RUN = re.compile(r"\bdocker compose run\b")
_RUNNING_READ = re.compile(r"\bcompose ps\b[^\n]*--(services|status)\b")


def library_labels(core_dir):
    """Return the labels ``IC_ONEOFF_LABELS`` declares, as one string.

    :param Path core_dir: the ``incubacloud`` addon directory.
    :returns: e.g. ``"--label traefik.enable=false --label …"``.
    :rtype: str
    """
    text = (Path(core_dir) / LIBRARY).read_text()
    match = re.search(r"^IC_ONEOFF_LABELS=\((.*)\)$", text, re.M)
    if not match:
        raise AssertionError(f"no IC_ONEOFF_LABELS in {LIBRARY}")
    return match.group(1).strip()


def compose_run(core_dir):
    """Return the one correct spelling of ``docker compose run``.

    :param Path core_dir: the ``incubacloud`` addon directory.
    :rtype: str
    """
    return f"docker compose run {library_labels(core_dir)}"


def script_files(addon_dir):
    """Return every shell script under the addon's ``scripts/``.

    :param Path addon_dir: the addon directory.
    :rtype: list[Path]
    """
    return sorted((Path(addon_dir) / "scripts").rglob("*.sh"))


def python_files(addon_dir):
    """Return every Python file of the addon outside its tests.

    :param Path addon_dir: the addon directory.
    :rtype: list[Path]
    """
    addon_dir = Path(addon_dir)
    return sorted(
        path for path in addon_dir.rglob("*.py")
        if "tests" not in path.relative_to(addon_dir).parts
    )


def _shell_lines(text):
    """Yield ``(line number, code)`` of a script, comments left out.

    A command continued with a trailing backslash is joined into the
    line it starts on, so a flag on the next line still counts.
    """
    lines = text.split("\n")
    number = 0
    while number < len(lines):
        start = number
        code = lines[number]
        while code.endswith("\\") and number + 1 < len(lines):
            number += 1
            code = code[:-1] + " " + lines[number].strip()
        number += 1
        if code.lstrip().startswith("#"):
            continue
        yield start + 1, code


def _python_strings(text):
    """Yield ``(line number, literal text)`` of a module's strings.

    Docstrings are left out: they describe commands, they do not run
    them. An f-string counts by its literal parts, an expression in it
    standing for nothing.
    """
    tree = ast.parse(text)
    # Docstrings, and the literal parts of f-strings, which are read
    # through their f-string.
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            docstrings.update(id(part) for part in node.values)
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(
                first.value, ast.Constant,
            ):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            literal = "".join(
                part.value for part in node.values
                if isinstance(part, ast.Constant)
            )
            yield node.lineno, literal
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.lineno, node.value


def violations(addon_dir, core_dir, allowed=None):
    """Return where the addon breaks the one-off rule.

    :param Path addon_dir: the addon whose scripts and Python files are
        read.
    :param Path core_dir: the ``incubacloud`` addon, for the library's
        labels.
    :param dict allowed: ``{relative path: reason}`` of files left out on
        purpose, each with why.
    :returns: one ``"path:line: text"`` entry per offence.
    :rtype: list[str]
    """
    addon_dir = Path(addon_dir)
    allowed = allowed or {}
    correct_run = compose_run(core_dir)
    found = []
    for path in script_files(addon_dir):
        relative = path.relative_to(addon_dir)
        if relative == LIBRARY or str(relative) in allowed:
            continue
        for number, code in _shell_lines(path.read_text()):
            if _RAW_RUN.search(code) or _RUNNING_READ.search(code):
                found.append(f"{relative}:{number}: {code.strip()}")
    for path in python_files(addon_dir):
        relative = path.relative_to(addon_dir)
        if str(relative) in allowed:
            continue
        for number, literal in _python_strings(path.read_text()):
            raw_run = _RAW_RUN.search(literal) and correct_run not in literal
            if raw_run or _RUNNING_READ.search(literal):
                found.append(f"{relative}:{number}: {literal.strip()[:120]}")
    return found
