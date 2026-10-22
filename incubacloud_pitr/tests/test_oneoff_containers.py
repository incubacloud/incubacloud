"""Continuous recovery's scripts and executors follow core's one-off rule.

The rule, the reasons and the checker live in core
(``incubacloud.tests.oneoff_rules``). A recovery runs for hours in a
container of the ``db`` service: started by hand, it would be counted in
a sleeping tenant's Sablier group and stopped when the session expired.
"""
from pathlib import Path

from odoo.tests.common import BaseCase

import odoo.addons.incubacloud as core_addon
from odoo.addons.incubacloud.tests.oneoff_rules import RULE, violations

_ADDON = Path(__file__).resolve().parent.parent
_CORE = Path(core_addon.__file__).resolve().parent


class TestPitrOneOffContainers(BaseCase):

    def test_pitr_starts_and_reads_one_offs_through_the_library(self):
        """No raw ``docker compose run`` or running-services read."""
        found = violations(_ADDON, _CORE)
        self.assertFalse(found, f"{RULE}:\n" + "\n".join(found))
