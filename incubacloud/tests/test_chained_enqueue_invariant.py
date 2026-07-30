"""Structural guard: a job chained from a callback must bypass the guard.

This bug has now been written three times, in three different files, by
three different pieces of work — and every time it was invisible: the
``UserError`` is raised, caught by the caller's own ``except``, logged as
a warning, and the feature simply never happens. Nothing goes red.

The mechanism is always identical. ``cloud.job.enqueue`` refuses to queue
a job while another *visible* job is active on the same target. When an
executor chains a follow-up from ``on_success``, the parent job is still
``started`` on that very target, so the guard matches the parent itself
and blocks its own descendant.

A behavioural test would have to be written once per call site, which is
exactly what did not happen. So this one is structural: it reads the
source of every executor and refuses any ``enqueue()`` written inside a
callback, unless it passes ``bypass_running_check``.
"""
import ast
import pathlib
import tempfile

from odoo.tests.common import BaseCase

#: Methods that run while the current job is still ``started``.
#: Anything called *from* these is covered too — see ``_callback_methods``.
_ENTRY_CALLBACKS = frozenset({"on_success", "on_failure", "after_commands"})

_MODELS_DIR = pathlib.Path(__file__).resolve().parent.parent / "models"


def _callback_methods(tree):
    """Return the methods that can run inside a still-active job.

    Starts from the executor callbacks and follows ``self.x()`` calls
    transitively, so a chain hidden one helper deep is still covered.

    :param tree: parsed module AST.
    :return: set of method names reachable from a callback.
    """
    defs = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    reachable = {name for name in defs if name in _ENTRY_CALLBACKS}
    frontier = set(reachable)
    while frontier:
        current = frontier.pop()
        for call in ast.walk(defs[current]):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            called = (
                func.attr
                if isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
                else None
            )
            if called and called in defs and called not in reachable:
                reachable.add(called)
                frontier.add(called)
    return reachable


def _offending_enqueues(path):
    """Find enqueue() calls in callback scope missing the bypass flag.

    :param path: module to inspect.
    :return: list of ``(method, lineno)`` for each violation.
    """
    tree = ast.parse(path.read_text())
    reachable = _callback_methods(tree)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in reachable:
            continue
        for call in ast.walk(node):
            is_enqueue = (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "enqueue"
            )
            if not is_enqueue:
                continue
            passes_flag = any(
                kw.arg == "bypass_running_check" for kw in call.keywords
            )
            if not passes_flag:
                bad.append((node.name, call.lineno))
    return bad


class TestChainedEnqueueInvariant(BaseCase):

    def test_no_callback_enqueues_without_bypassing_the_guard(self):
        """Every chained enqueue must opt out of the active-job guard."""
        violations = []
        for path in sorted(_MODELS_DIR.glob("*.py")):
            for method, lineno in _offending_enqueues(path):
                violations.append(f"{path.name}:{lineno} in {method}()")
        self.assertFalse(
            violations,
            "these enqueue() calls run while the parent job is still "
            "'started' on the same target, so the active-job guard will "
            "reject them and the chained job will never run:\n  "
            + "\n  ".join(violations),
        )

    def test_the_detector_actually_detects(self):
        """A canary: the scan must flag a violation it is shown.

        Without this, a broken walker would report 'no violations' and
        the guard above would pass while checking nothing.
        """
        source = (
            "class E:\n"
            "    def on_success(self, r):\n"
            "        self._chain()\n"
            "    def _chain(self):\n"
            "        self.env['cloud.job'].enqueue(1, 2, 'x')\n"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            canary = pathlib.Path(tmpdir) / "canary.py"
            canary.write_text(source)
            found = _offending_enqueues(canary)
        self.assertEqual(
            [name for name, _line in found], ["_chain"],
            "the scan missed an enqueue chained one helper deep",
        )
