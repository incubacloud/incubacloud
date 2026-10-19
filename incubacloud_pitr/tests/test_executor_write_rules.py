"""Continuous recovery's executors follow core's write rule.

The rule, the reasons and the checker live in core
(``incubacloud.tests.executor_write_rules``); core's model files are read
too, because these executors extend core's.
"""
from pathlib import Path

from odoo.tests.common import BaseCase

import odoo.addons.incubacloud as core_addon
from odoo.addons.incubacloud.tests.executor_write_rules import (
    check,
    executors_in,
    model_files,
)

_ADDON = Path(__file__).resolve().parent.parent
_CORE = Path(core_addon.__file__).resolve().parent
#: Methods only the terminal hooks call, which write on their cursor.
_HOOK_HELPERS = {
    "_record_pitr_bases": "records the recovery window a run read",
    "_record_cluster": "records the cluster archiving now",
    "_record_archiving": "records what switching on did",
    "_record": "records what a base copy and its housekeeping did",
    "_restored": "records a restore and queues its full base copy",
    "_follow_on": "hands a running restore to the next job",
    "_pitr_alert": "raises a continuous recovery alert on the instance",
    "_pitr_resolve": "closes continuous recovery alerts on the instance",
}


class TestPitrExecutorsFollowTheWriteRule(BaseCase):

    def test_there_are_executors_to_check(self):
        """The check found the executors, so its silence means something."""
        self.assertIn(
            "PitrRestoreExecutor",
            executors_in(model_files(_ADDON), model_files(_CORE)),
        )

    def test_no_executor_writes_in_the_jobs_transaction(self):
        """No executor of continuous recovery breaks the rule."""
        violations = check(
            model_files(_ADDON), model_files(_CORE), helpers=_HOOK_HELPERS,
        )
        self.assertEqual(violations, [], "\n" + "\n".join(violations))
