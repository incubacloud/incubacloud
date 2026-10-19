"""Every provider alert core raises is declared in ``PROVIDER_ALERT_CODES``.

A provider alert is relayed away from the operator who sees it, to
whoever runs the service behind it. ``encrypted_value_unreadable`` was one
nobody had listed, and it reached customers' panels before anyone knew it
was a provider alert at all (18-oct-2026). This reads the source: every
call or dict that sets ``audience`` to ``'provider'`` — even conditionally
— names a code, and that code must be in the list.
"""
import ast
from pathlib import Path

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.models.cloud_alert import PROVIDER_ALERT_CODES

_MODULE_DIR = Path(__file__).resolve().parent.parent


def _mentions_provider(node):
    """Whether an expression can evaluate to the string ``'provider'``."""
    return any(
        isinstance(sub, ast.Constant) and sub.value == "provider"
        for sub in ast.walk(node)
    )


class _ConstantResolver:
    """Turn a code expression into a pattern, ``*`` for what varies."""

    def __init__(self, files):
        self._constants = {}
        for tree in files.values():
            for node in tree.body:
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    self._constants[node.targets[0].id] = node.value.value

    def resolve(self, node):
        """Return the code pattern *node* stands for, or None."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self._constants.get(node.id)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    parts.append(value.value)
                else:
                    parts.append(self.resolve(value.value) or "*")
            return "".join(parts)
        return None


def _provider_codes():
    """Return ``{pattern: 'file:line'}`` for every provider alert in core."""
    files = {
        path: ast.parse(path.read_text(), str(path))
        for path in _MODULE_DIR.rglob("*.py")
        if "tests" not in path.relative_to(_MODULE_DIR).parts
        and "migrations" not in path.relative_to(_MODULE_DIR).parts
    }
    resolver = _ConstantResolver(files)
    found = {}
    for path, tree in files.items():
        where = path.relative_to(_MODULE_DIR)
        for node in ast.walk(tree):
            code_node = None
            if isinstance(node, ast.Call):
                audience = next(
                    (kw.value for kw in node.keywords if kw.arg == "audience"),
                    None,
                )
                if audience is not None and _mentions_provider(audience):
                    code_node = node.args[0] if node.args else next(
                        (kw.value for kw in node.keywords if kw.arg == "code"),
                        None,
                    )
                    if code_node is None:
                        found[f"<unresolved call>@{where}:{node.lineno}"] = (
                            f"{where}:{node.lineno}"
                        )
                        continue
            elif isinstance(node, ast.Dict):
                keys = {
                    k.value: v for k, v in zip(node.keys, node.values)
                    if isinstance(k, ast.Constant)
                }
                if "audience" in keys and _mentions_provider(keys["audience"]):
                    code_node = keys.get("code")
            if code_node is None:
                continue
            pattern = resolver.resolve(code_node)
            key = pattern or f"<unresolved>@{where}:{node.lineno}"
            found[key] = f"{where}:{node.lineno}"
    return found


class TestProviderAlertInventory(BaseCase):
    """The declared list and the code agree, both ways."""

    def test_every_provider_alert_in_the_code_is_declared(self):
        found = _provider_codes()
        undeclared = {
            code: where for code, where in found.items()
            if code not in PROVIDER_ALERT_CODES
        }
        self.assertFalse(
            undeclared,
            "Provider alerts not in PROVIDER_ALERT_CODES (declare them, "
            "or make their code resolvable here): %s" % undeclared,
        )

    def test_every_declared_code_is_still_raised(self):
        stale = PROVIDER_ALERT_CODES - set(_provider_codes())
        self.assertFalse(
            stale, "Declared but no longer raised as provider alerts: %s" % stale,
        )

    def test_the_unreadable_secret_alert_is_found(self):
        """The one that started this list is seen through its f-string."""
        self.assertIn("encrypted_value_unreadable:*.*", _provider_codes())
