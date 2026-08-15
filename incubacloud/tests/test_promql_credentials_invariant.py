"""Structural guard: every PromQL query must carry both credential halves.

``promql_query`` builds its auth as
``auth=(user, token) if (token and user) else None``. Handing it only
one half is therefore not a partial credential — it is *no* credential,
and the central answers 401. The caller catches that, logs a warning,
and returns, so the feature simply stops working with nothing going red.

Every caller unpacks ``user, token = settings._metrics_auth()``, which
makes forwarding only ``token=`` an easy and invisible slip: the name
``user`` is right there, bound and unused, and nothing complains. It
happened in the instance-liveness cron, where it went unnoticed because
its only symptom was a warning among many.

A behavioural test would have to be written once per call site, which is
exactly what did not happen — the two healthy callers had no test
pinning this either. So this one is structural: it reads the source and
refuses any ``promql_query`` call that does not pass both keywords.
"""
import ast
import pathlib

from odoo.tests.common import BaseCase

_MODELS_DIR = pathlib.Path(__file__).resolve().parent.parent / "models"

#: The function whose calls are audited, and the keywords it needs.
_TARGET = "promql_query"
_REQUIRED = frozenset({"token", "user"})


def _query_calls(tree):
    """Yield every ``promql_query(...)`` call node in *tree*.

    Matches both the bare name and an attribute access, so a call
    written as ``module.promql_query(...)`` is covered too.

    :param tree: parsed module AST.
    :return: iterator of ``ast.Call`` nodes.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else None
        )
        if name == _TARGET:
            yield node


class TestPromqlCallsCarryCredentials(BaseCase):
    """No call site may query the metrics central anonymously."""

    def test_every_call_passes_user_and_token(self):
        offenders = []
        for path in sorted(_MODELS_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for call in _query_calls(tree):
                # The definition itself is not a call; ``**kwargs``
                # forwarding is accepted as deliberate delegation.
                if any(kw.arg is None for kw in call.keywords):
                    continue
                passed = {kw.arg for kw in call.keywords}
                missing = _REQUIRED - passed
                if missing:
                    offenders.append(
                        f"{path.name}:{call.lineno} misses "
                        f"{', '.join(sorted(missing))}"
                    )
        self.assertFalse(
            offenders,
            "promql_query called without a full credential — the central "
            "will answer 401 and the caller will log a warning and carry "
            "on with no data:\n  " + "\n  ".join(offenders),
        )

    def test_the_guard_can_see_a_missing_keyword(self):
        """The detector must fail the very slip it exists for."""
        tree = ast.parse("promql_query(base, expression, token=token)\n")
        call = next(_query_calls(tree))
        passed = {kw.arg for kw in call.keywords}
        self.assertEqual(_REQUIRED - passed, {"user"})
