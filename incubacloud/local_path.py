"""The ``PATH`` the panel runs its own programs with.

Odoo.sh hands Odoo's processes a ``PATH`` that holds ``~/.local/bin``, where
pip installs ``ansible-playbook``, with the tilde as it is. A shell expands
it; ``execvp``, ``shutil.which`` and ansible-runner do not, so a program
there was not found. Nothing on Odoo.sh's side can change that ``PATH``.
"""
import os


def expanded_path(path=None):
    """Return a ``PATH`` whose entries starting with ``~`` are expanded.

    The order is kept and nothing is removed, so an entry may repeat one
    already there, which is harmless.

    :param str path: the ``PATH`` to expand; this process's when None
    :return: the same entries, each through ``os.path.expanduser``
    :rtype: str
    """
    if path is None:
        path = os.environ.get("PATH", os.defpath)
    return os.pathsep.join(os.path.expanduser(entry) for entry in path.split(os.pathsep))
