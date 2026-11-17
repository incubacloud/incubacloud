"""Replace the default password of a running instance's admin user.

For the instances that already ran before 1.0.234, whose admin may still
have Odoo's default password: queued once by the update and then by the
hourly sweep (``cloud.instance.cron_check_admin_passwords``) until the
instance has answered. The step runs in the instance's Odoo container and
restarts Odoo when it changed the password, so the sessions opened with
the old one end; a password somebody set is left as it is. One that no
longer runs when its turn comes, or was checked meanwhile, is skipped: the
job ends done and says so.
"""
from ._admin_password import AdminPasswordMixin
from .abstract_executor import AbstractSSHExecutor


class AdminPasswordCheckExecutor(AdminPasswordMixin, AbstractSSHExecutor):
    """Check a running instance's admin password, and replace the default."""

    _job_type = "check_admin_password"

    #: Why this run checks nothing, set by ``pre_run_checks``; None to check.
    _skip = None

    def _inst(self):
        """Return the instance checked."""
        return self.job.instance_id

    def pre_run_checks(self):
        """Skip an instance that does not run any more, or was checked.

        A stopped instance is checked when it starts (``start_instance``)
        and keeps its mark meanwhile; one a deploy, a restore or a start
        checked while this job waited needs nothing more. Neither is a
        failure: the job ends done, without an alert, and its log says
        why.

        Only the restart hold of the base checks applies
        (``_defer_while_restarting``): the check deploys nothing, so the
        pip conflicts that refuse a deploy, one of the holds it is queued
        past, do not refuse it.
        """
        inst = self._inst()
        if not inst or not (inst.deployed and inst.running):
            self._skip = (
                "Not checked: the instance does not run. It is checked "
                "when it starts."
            )
        elif inst.admin_password_state:
            self._skip = (
                "Not checked: the instance was checked since this job was "
                "queued."
            )
        self._defer_while_restarting()

    def get_commands(self):
        """Return the single step, in the instance's Odoo container.

        No step when the run is skipped (``pre_run_checks``).
        """
        if self._skip:
            return []
        inst = self._inst()
        return [
            self._admin_password_step(self._inst_dir(inst), inst, "running"),
        ]

    async def on_success(self, results):
        """Keep what the check found; tell a change on the instance.

        ``changed`` comes only once Odoo was restarted after the change
        (``admin_password.sh``): a restart that failed fails the step,
        and the instance stays to be checked.
        """
        if self._skip:
            self._sys(f"✓ {self._skip}")
            return
        answer = self._record_admin_password(results)
        if answer == "changed":
            self._sys(
                "✓ The admin user had Odoo's default password: it has a "
                "random one now, and Odoo was restarted."
            )
        elif answer == "own":
            self._sys("✓ The admin user has a password of its own: left as it is.")
        elif answer == "absent":
            self._sys("✓ The instance's database has no admin user.")
        else:
            self._sys(
                "⚠ The check gave no answer: the instance is checked again "
                "later."
            )

    async def on_failure(self, results, errors):
        """Say why; the instance stays to be checked, by a later sweep."""
        for err in errors:
            self._sys(f"✗ {err}")
        self._sys(
            "The admin user's password was not checked: the instance is "
            "checked again later."
        )
