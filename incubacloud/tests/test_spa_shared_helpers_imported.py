"""A shared helper called without importing it is a live ReferenceError.

The SPA's ``utils/`` modules exist because five components carried the
same code. Each extraction rewires the callers to the new helper, and
the step that gets forgotten is the ``import`` line: the component keeps
parsing, keeps rendering, and only throws when the user finally presses
the button that calls it. ``confirmVia`` shipped that way and broke ten
confirmation dialogs in production while every test stayed green.

Nothing in the Python suite loads the bundle, so the check is textual:
for every name a ``utils/`` module exports, any file that calls it must
name it in an import. That is what a JavaScript ``no-undef`` would say,
without needing a linter in the pipeline.
"""
import pathlib
import re

from odoo.tests.common import BaseCase

_SRC = pathlib.Path(__file__).resolve().parent.parent / "static" / "src"
_UTILS = _SRC / "utils"

_EXPORT_RE = re.compile(
    r"^export\s+(?:function|const|let)\s+(\w+)", re.MULTILINE
)
_IMPORT_RE = re.compile(r"import\s*\{([^}]*)\}\s*from")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
# ``(?<!:)`` keeps the ``//`` of a URL out of the comment stripper.
_LINE_COMMENT_RE = re.compile(r"(?<!:)//.*$", re.MULTILINE)


def _code_only(source):
    """Return ``source`` with comments blanked out.

    A helper named in prose ("see confirmVia()") is not a call site and
    must not be reported as one.

    :param source: JavaScript source.
    :return: the source minus its comments.
    """
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", source))


def _exported_helpers():
    """Map every name exported by ``utils/`` to the module exporting it.

    :return: dict of helper name -> defining path.
    """
    return {
        name: module
        for module in sorted(_UTILS.glob("*.js"))
        for name in _EXPORT_RE.findall(module.read_text())
    }


def _imported_names(source):
    """Collect the names a file pulls in through ``import {...}``.

    :param source: JavaScript source.
    :return: set of local names bound by named imports.
    """
    return {
        # ``a as b`` binds b, so the last word is the local name.
        part.strip().split()[-1]
        for block in _IMPORT_RE.findall(source)
        for part in block.split(",")
        if part.strip()
    }


def _uses(source, name):
    """Tell whether ``source`` calls ``name`` as a free identifier.

    ``this.name(`` and ``obj.name(`` are method calls that carry their
    own receiver and need no import.

    :param source: JavaScript source, comments already stripped.
    :param name: helper name to look for.
    :return: True when the file calls the bare name.
    """
    return bool(re.search(rf"(?<![\w.]){name}\s*\(", source))


def _declares(source, name):
    """Tell whether ``source`` defines ``name`` itself.

    A component free to shadow a helper name locally is not calling the
    shared one.

    :param source: JavaScript source, comments already stripped.
    :param name: helper name to look for.
    :return: True when the file declares the name.
    """
    return bool(
        re.search(rf"(?:function|const|let|var|class)\s+{name}\b", source)
    )


def _resolutions():
    """Yield ``(path, helper, imported)`` for every shared-helper call.

    :return: iterator over the call sites of ``utils/`` exports.
    """
    helpers = _exported_helpers()
    for path in sorted(_SRC.rglob("*.js")):
        source = _code_only(path.read_text())
        imported = _imported_names(source)
        for name, module in helpers.items():
            if path == module or not _uses(source, name):
                continue
            if _declares(source, name):
                continue
            yield path, name, name in imported


class TestSharedHelpersAreImported(BaseCase):

    def test_every_shared_helper_call_has_an_import(self):
        """No file may reach for a utils/ export it never imported."""
        offenders = [
            f"{path.relative_to(_SRC)}: calls {name}() without importing it"
            for path, name, imported in _resolutions()
            if not imported
        ]
        self.assertFalse(
            offenders,
            "shared helpers called as undefined globals:\n  "
            + "\n  ".join(offenders),
        )

    def test_the_scan_finds_the_calls_it_should(self):
        """Canary: a silent zero here would make the guard vacuous."""
        found = sum(1 for _ in _resolutions())
        self.assertGreaterEqual(
            found, 20, "the shared-helper scan stopped finding call sites",
        )
