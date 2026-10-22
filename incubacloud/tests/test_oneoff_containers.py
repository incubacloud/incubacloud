"""Core's scripts and executors follow the one-off container rule.

The rule, the reasons and the checker live in ``oneoff_rules``; pitr and
saas run the same check over their own files.
"""
import tempfile
from pathlib import Path

from odoo.tests.common import BaseCase

from ..models.abstract_executor import COMPOSE_RUN, NOT_ONEOFF
from .oneoff_rules import RULE, compose_run, violations

_ADDON = Path(__file__).resolve().parent.parent

#: Files left out of the check on purpose, each with why.
_ALLOWED = {
    "scripts/export_sanitize.sh": (
        "its compose run is text of the DEVELOPMENT.md handed to the "
        "developer, run on their machine against devel, not on a host"
    ),
}


class TestOneOffContainers(BaseCase):

    def test_core_starts_and_reads_one_offs_through_the_library(self):
        """No raw ``docker compose run`` or running-services read."""
        found = violations(_ADDON, _ADDON, _ALLOWED)
        self.assertFalse(found, f"{RULE}:\n" + "\n".join(found))

    def test_the_executors_label_one_offs_as_the_scripts_do(self):
        """``COMPOSE_RUN`` and ``IC_ONEOFF_LABELS`` cannot drift apart."""
        self.assertEqual(COMPOSE_RUN, compose_run(_ADDON))

    def test_scripts_and_executors_tell_one_offs_apart_alike(self):
        """``ic_running_services`` and ``NOT_ONEOFF`` read the same label
        the same way; one changed alone would leave the other behind."""
        library = (_ADDON / "scripts" / "lib" / "common.sh").read_text()
        self.assertIn(NOT_ONEOFF, library)

    def test_the_labels_keep_one_offs_out_of_traefik_and_sablier(self):
        """What the labels are for: Sablier only reads ``sablier.enable``
        and Traefik only ``traefik.enable``."""
        self.assertIn("--label traefik.enable=false", COMPOSE_RUN)
        self.assertIn("--label sablier.enable=false", COMPOSE_RUN)

    def test_the_check_sees_what_it_is_for(self):
        """Its silence means something: each offence it exists for is
        caught, and a comment, a docstring or the right spelling is not."""
        with tempfile.TemporaryDirectory() as tmp:
            addon = Path(tmp)
            (addon / "scripts" / "lib").mkdir(parents=True)
            (addon / "scripts" / "lib" / "common.sh").write_text(
                (_ADDON / "scripts" / "lib" / "common.sh").read_text(),
            )
            (addon / "scripts" / "op.sh").write_text(
                "# docker compose run in a comment is fine\n"
                "ic_compose_run --rm odoo odoo shell\n"
                "docker compose run --rm odoo odoo shell\n"
                "x=$(docker compose ps --services \\\n"
                "    --status running)\n"
            )
            (addon / "models").mkdir()
            (addon / "models" / "executor.py").write_text(
                'def steps(d):\n'
                '    """Runs ``docker compose run`` (a docstring)."""\n'
                '    good = f"cd {d} && ' + COMPOSE_RUN + ' --rm odoo"\n'
                '    bad = f"cd {d} && docker compose run --rm odoo"\n'
                '    read = "docker compose ps --status running --services"\n'
                '    return good, bad, read\n'
            )
            found = violations(addon, addon)
        self.assertEqual(
            [entry.split(": ")[0] for entry in found],
            [
                "scripts/op.sh:3",
                "scripts/op.sh:4",
                "models/executor.py:4",
                "models/executor.py:5",
            ],
        )
