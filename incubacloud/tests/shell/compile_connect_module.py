"""Compile what a deploy ships of incubacloud_connect, with this Python.

doodba compiles every addon of the image with the series' own Python
(its ``500-compile``): 2.7 on 7.0 to 10.0, 3.5 on 11.0 and 12.0. One
syntax error and the image of the series does not build. CI runs this
with each of them, in their official images (``python:2.7-slim``,
``python:3.5-slim``), on the module as a deploy ships it: without its
tests nor bytecode (``_ic_connect_for_instances``). Written for Python
2.7 and later, so the oldest of them can run it (QA of the series,
7-nov-2026).

Usage: ``compile_connect_module.py <incubacloud_connect directory>``;
exits 1 naming every file that does not compile.
"""
import os
import sys

#: What ``_ic_connect_for_instances`` leaves out of the copy.
LEFT_OUT = ("tests", "__pycache__")


def module_files(root):
    """Return the ``.py`` files a deploy ships of the module at *root*.

    :param str root: the module's directory.
    :return: their paths, sorted.
    :rtype: list
    """
    found = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in LEFT_OUT]
        found += [
            os.path.join(folder, name) for name in files if name.endswith(".py")
        ]
    return sorted(found)


def syntax_error(path):
    """Return why *path* does not compile here, or None when it does.

    Compiled in memory, as ``py_compile`` would but without writing the
    bytecode, so the module can be mounted read-only. No ``__future__``
    of this script reaches the file (``dont_inherit``).

    :param str path: a Python file.
    :rtype: str
    """
    with open(path, "rb") as handle:
        try:
            compile(handle.read(), path, "exec", 0, True)
        except SyntaxError as error:
            return path + ":" + str(error.lineno) + ": " + error.msg
    return None


def main(root):
    """Compile every shipped file of the module at *root*; 1 if any fails.

    :param str root: the module's directory.
    :rtype: int
    """
    files = module_files(root)
    if not files:
        print("no Python file under " + root)
        return 1
    errors = [e for e in (syntax_error(path) for path in files) if e]
    for error in errors:
        print(error)
    print(
        "Python " + sys.version.split()[0] + ": "
        + str(len(files) - len(errors)) + " of " + str(len(files))
        + " file(s) compile"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
