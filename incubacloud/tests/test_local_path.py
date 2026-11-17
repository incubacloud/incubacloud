"""The ``PATH`` the panel runs its own programs with.

Odoo.sh hands Odoo's processes a ``PATH`` holding ``~/.local/bin`` with
the tilde as it is. A shell expands it; ``execvp``, ``shutil.which`` and
ansible-runner do not, so a program pip installed there was not found.
"""
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from odoo.tests.common import BaseCase

from odoo.addons.incubacloud.local_path import expanded_path
from odoo.addons.incubacloud.models import cloud_host
from odoo.addons.incubacloud.net import git_reader


class TestExpandedPath(BaseCase):

    def test_a_tilde_entry_is_expanded_in_place(self):
        """Each ``~`` entry becomes the home, in its place; the rest stays."""
        with patch.dict(os.environ, {"HOME": "/home/odoo"}):
            self.assertEqual(
                expanded_path("~/.local/bin:/usr/bin:~:/bin"),
                "/home/odoo/.local/bin:/usr/bin:/home/odoo:/bin",
            )

    def test_a_path_without_tilde_is_unchanged(self):
        """A ``PATH`` with nothing to expand comes back as it was."""
        self.assertEqual(expanded_path("/usr/local/bin:/usr/bin"), "/usr/local/bin:/usr/bin")

    def test_by_default_it_reads_this_process_path(self):
        """Without an argument it expands this process's ``PATH``."""
        with patch.dict(os.environ, {"HOME": "/home/odoo", "PATH": "~/.local/bin:/usr/bin"}):
            self.assertEqual(expanded_path(), "/home/odoo/.local/bin:/usr/bin")


class TestProgramsFoundUnderTilde(BaseCase):
    """The local programs the panel looks up or runs see the expanded ``PATH``."""

    def setUp(self):
        """A home of its own with ``~/.local/bin``, and a ``PATH`` naming it with a tilde."""
        super().setUp()
        self.home = tempfile.mkdtemp(prefix="ic-path-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        Path(self.home, ".local", "bin").mkdir(parents=True)
        environ = patch.dict(os.environ, {"HOME": self.home, "PATH": "~/.local/bin:/nonexistent"})
        environ.start()
        self.addCleanup(environ.stop)

    def test_sshpass_in_the_local_bin_is_found(self):
        """Ansible logs in to a password host through sshpass: it must be seen there."""
        program = Path(self.home, ".local", "bin", "sshpass")
        program.write_text("#!/bin/sh\n")
        program.chmod(0o700)
        cloud_host._sshpass_available.cache_clear()
        self.addCleanup(cloud_host._sshpass_available.cache_clear)
        self.assertTrue(cloud_host._sshpass_available())

    def test_git_runs_with_the_expanded_path(self):
        """git and the helpers it starts are looked up in the expanded ``PATH``."""
        session = git_reader._Session(git_reader.GitReader(), None, None)
        session.home = self.home
        self.assertEqual(
            session._env()["PATH"], f"{self.home}/.local/bin:/nonexistent",
        )
