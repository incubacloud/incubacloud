#!/usr/bin/env python3
"""Check that no module has a migration newer than its manifest version.

Odoo runs ``<module>/migrations/<version>/`` scripts only when
``installed < version <= manifest version`` (``odoo/modules/migration.py``).
A folder above the manifest version is never run: a silent failure, usually a
manifest bump that was forgotten.

Both version formats of Odoo are understood: with the series prefix
(``19.0.1.0.228``) and without it (``1.0.228``, to which Odoo prepends the
series). A version with more than two dots is taken as already prefixed.
Folders that Odoo ignores (``tests``, names not matching its version pattern)
and ``0.0.0`` (runs on any change) are skipped.

Usage: check_migration_versions.py [--series 19.0] DIR [DIR...]
Every ``<module>/__manifest__.py`` directly under each DIR is checked. Prints
one line per offending module and exits 1 if there is any, 0 otherwise.
"""

import ast
import re
import sys
from pathlib import Path

DEFAULT_SERIES = "19.0"

# The same shape Odoo accepts for a folder name (odoo/modules/migration.py).
_VERSION = re.compile(
    r"""^
        ((6\.1|[6-9]\.0|[1-9]\d+\.0|(7|8|9|10)\.saas~[1-9]\d*|saas~(1[1-9]|[2-9]\d+)\.[1-9])\.)?
        \d+\.\d+(\.\d+)?
    $""",
    re.VERBOSE | re.ASCII,
)


def full_version(version, series=DEFAULT_SERIES):
    """Return ``version`` as a tuple of ints, with the series prefixed if absent.

    Mirrors ``convert_version`` of Odoo: more than two dots means the prefix
    is already there. Non numeric parts (``saas~12``) are kept as strings.
    """
    if version.count(".") <= 2:
        version = f"{series}.{version}"
    return tuple(int(p) if p.isdigit() else p for p in version.split("."))


def manifest_version(path):
    """Return the ``version`` string of the manifest at ``path``, or None."""
    data = ast.literal_eval(path.read_text(encoding="utf-8"))
    return data.get("version")


def check_module(module_dir, series=DEFAULT_SERIES):
    """Return a problem line for ``module_dir`` if a migration exceeds its manifest."""
    manifest = module_dir / "__manifest__.py"
    migrations = module_dir / "migrations"
    if not manifest.is_file() or not migrations.is_dir():
        return None
    current = manifest_version(manifest)
    if not current:
        return None
    top = None
    for folder in migrations.iterdir():
        name = folder.name
        if not folder.is_dir() or name in ("tests", "0.0.0") or not _VERSION.match(name):
            continue
        try:
            if top is None or full_version(name, series) > full_version(top, series):
                top = name
        except TypeError:  # saas~ parts mixed with numbers: not comparable
            continue
    if top is None:
        return None
    try:
        above = full_version(top, series) > full_version(current, series)
    except TypeError:
        return None
    if above:
        return (
            f"{module_dir}: migrations/{top} is above the manifest version "
            f"{current}; Odoo will never run it"
        )
    return None


def check_tree(root, series=DEFAULT_SERIES):
    """Return the problem lines of every module directly under ``root``."""
    found = (check_module(m.parent, series) for m in sorted(Path(root).glob("*/__manifest__.py")))
    return [p for p in found if p]


def main(argv):
    """Check every directory of ``argv``; print the problems; return the exit code."""
    series = DEFAULT_SERIES
    if argv[:1] == ["--series"]:
        series, argv = argv[1], argv[2:]
    problems = [p for root in argv for p in check_tree(root, series)]
    sys.stdout.write("".join(f"{p}\n" for p in problems))
    if problems:
        sys.stderr.write(f"{len(problems)} modules with a migration above their version\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
