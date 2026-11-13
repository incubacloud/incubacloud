#!/usr/bin/env python3
"""Check that every entry of the translation files names its module.

Odoo loads a ``.po`` by reading each entry's ``#. module: <name>`` comment;
an entry without it has no module and ``odoo -u`` with the language loaded
fails with ``'NoneType' object has no attribute 'groups'``. Tools such as
polib append entries without that line, and no coverage check notices.

Usage: check_po_module_line.py DIR [DIR...]
Every ``<module>/i18n/*.po`` and ``*.pot`` under each DIR is checked against
the name of its module folder. Prints one line per offending entry and exits
1 if there is any, 0 otherwise.
"""

import re
import sys
from pathlib import Path

_MODULE = re.compile(r"^#\. module: (.+?)\s*$", re.M)


def entries_without_module(text, module):
    """Return the msgids of the entries of ``text`` lacking ``#. module: <module>``.

    The header entry (empty msgid followed directly by msgstr) and obsolete
    entries (``#~``) are skipped. ``text`` is the content of a ``.po``/``.pot``.
    """
    missing = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip("\n").split("\n")
        idx = next((i for i, ln in enumerate(lines) if ln.startswith("msgid ")), None)
        if idx is None:
            continue
        if lines[idx] == 'msgid ""' and (
            idx + 1 >= len(lines) or lines[idx + 1].startswith("msgstr")
        ):
            continue
        if module in _MODULE.findall(block):
            continue
        missing.append(lines[idx][:80])
    return missing


def check_tree(root):
    """Return one ``<file>: <problem>`` line per bad entry under ``root``."""
    problems = []
    for path in sorted(Path(root).glob("*/i18n/*.po*")):
        if path.suffix not in (".po", ".pot"):
            continue
        module = path.parent.parent.name
        for msgid in entries_without_module(path.read_text(encoding="utf-8"), module):
            problems.append(f"{path}: no '#. module: {module}' for {msgid}")
    return problems


def main(argv):
    """Check every directory of ``argv``; print the problems; return the exit code."""
    problems = [p for root in argv for p in check_tree(root)]
    sys.stdout.write("".join(f"{p}\n" for p in problems))
    if problems:
        sys.stderr.write(
            f"{len(problems)} translation entries without their module line\n"
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
