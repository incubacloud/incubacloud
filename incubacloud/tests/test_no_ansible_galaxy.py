"""Nothing in the addons installs Ansible roles from Galaxy.

ansible-core 2.17.14 carries advisory PYSEC-2026-3458, which affects
``ansible-galaxy role install``. The 2.17 series has no fix and 2.18+
needs Python >= 3.11, while Odoo 18's image ships Python 3.10, so this
branch keeps 2.17.14 and CI ignores that one advisory. The exception is
safe only while the panel never runs that command: this test fails if
``ansible-galaxy`` shows up in the code, scripts or playbooks of the
addons (``ansible-galaxy collection ...`` is a different command and is
allowed). The reasoning and the condition for lifting the exception are
in the "Known advisory: ansible-core" section of docs/architecture.md.
"""
import pathlib
import re

from odoo.tests.common import BaseCase

_REPO = pathlib.Path(__file__).resolve().parents[2]
_THIS = pathlib.Path(__file__).resolve()
_FORBIDDEN = re.compile(r"ansible-galaxy(?!\s+collection\b)")
_SKIP_DIRS = {"node_modules", "__pycache__", ".git", "i18n"}
_SKIP_SUFFIXES = {".md", ".po", ".pot", ".png", ".jpg", ".svg", ".woff", ".woff2", ".ttf", ".pyc"}


class TestNoAnsibleGalaxy(BaseCase):

    def _offenders(self):
        """Files of the addons that name ``ansible-galaxy`` other than for collections."""
        found = []
        for addon in sorted(_REPO.glob("incubacloud*")):
            for path in addon.rglob("*"):
                if (
                    not path.is_file()
                    or path.resolve() == _THIS
                    or path.suffix in _SKIP_SUFFIXES
                    or _SKIP_DIRS & set(path.relative_to(_REPO).parts)
                ):
                    continue
                try:
                    text = path.read_text()
                except (UnicodeDecodeError, OSError):
                    continue
                if _FORBIDDEN.search(text):
                    found.append(str(path.relative_to(_REPO)))
        return found

    def test_the_scan_sees_the_playbooks(self):
        """The check below must have something to scan."""
        playbooks = list((_REPO / "incubacloud" / "ansible" / "playbooks").glob("*.yml"))
        self.assertTrue(playbooks)

    def test_no_ansible_galaxy_role_install(self):
        offenders = self._offenders()
        self.assertFalse(
            offenders,
            "ansible-galaxy must not be used here (ansible-core 2.17.14 has the "
            "unfixed advisory PYSEC-2026-3458 in `ansible-galaxy role install`, "
            "ignored in CI only because the panel never runs it). See 'Known "
            "advisory: ansible-core' in docs/architecture.md before changing "
            "this. Found in: %s" % ", ".join(offenders),
        )
