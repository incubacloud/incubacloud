"""Which instances still have their admin on Odoo's default password.

An instance's database is born with ``admin``/``admin`` (Odoo's ``base``).
From 1.0.234 every flow that puts a database under an instance's Odoo
replaces that password with a random one before Odoo serves it
(``_admin_password``); the instances that were already running get it
from a job of their own (``check_admin_password``), queued once by the
update and again by the sweep below until each one has answered. A
stopped instance is checked when it starts (``start_instance``), is
restarted (``restart_instance``) or is rebuilt.

The state says where each instance stands: none, still to be checked;
``changed``, it had the default and has a random one; ``own``, it has a
password somebody set, which is never touched; ``absent``, its database
has no admin user. ``admin_password_checked_at`` is when it last answered,
kept when the state is emptied: the script counts only the changes made
after it (see ``_admin_password``). The state is emptied wherever the
database under the instance's Odoo may be another without an answer: a
restore that loaded one and could not check it, and the rollback of a
move, which starts the origin's database again.
"""
import logging
from datetime import timedelta

from odoo import _, api, fields, models

from .res_users_ext import as_platform

_logger = logging.getLogger(__name__)

#: The job that checks a running instance.
CHECK_JOB_TYPE = "check_admin_password"
#: The alert raised on an instance whose admin had the default password.
ALERT_CODE = "admin_password_replaced"
#: How long the sweep leaves an instance whose check failed, in hours:
#: a host that is down would otherwise fail a job every hour.
FAILED_CHECK_PAUSE_H = 6


class CloudInstanceAdminPassword(models.Model):
    _inherit = "cloud.instance"

    admin_password_state = fields.Selection(
        [
            ("changed", "Random password set"),
            ("own", "Its own password"),
            ("absent", "No admin user"),
        ],
        string="Admin user's password",
        copy=False,
        readonly=True,
        help="What the last check of the instance's Odoo admin user found: "
             "Odoo's default password, which was replaced with a random "
             "one; a password of its own, left as it is; or no admin "
             "user. Empty while the instance is still to be checked.",
    )
    admin_password_checked_at = fields.Datetime(
        string="Admin password checked",
        copy=False,
        readonly=True,
        help="When the instance's admin user was last checked for Odoo's "
             "default password. Kept while the instance is to be checked "
             "again.",
    )

    def _announce_default_admin_password(self, job=None):
        """Tell the instance's people that its admin got a random password.

        A normal alert on the instance and an audit row, both with the
        same text: what was found, what was done, and how to set a
        password of one's own.

        :param job: the ``cloud.job`` that changed it, if any
        """
        self.ensure_one()
        message = _(
            "The admin user still had Odoo's default password; it now has "
            "a random one. Sign in with Connect to set your own."
        )
        self.env["cloud.alert"].sudo().raise_alert(
            ALERT_CODE, message, instance=self, job=job,
        )
        self.env["cloud.audit.log"].sudo().create({
            "action": _("Admin password replaced"),
            "details": message,
            "job_id": job.id if job else False,
            **self._audit_target_vals(),
        })

    def _admin_password_check_domain(self):
        """Extra filter of the instances the sweep checks.

        The extension point for a layer whose instances get their admin's
        password some other way; core adds nothing.

        :return: a search domain, ANDed into the sweep's own
        """
        return []

    @api.model
    def _queue_admin_password_checks(self, limit=200, instances=None):
        """Queue a check of every running instance still to be checked.

        Deployed and running, with no check of its own queued or running,
        and none of its checks failed in the last ``FAILED_CHECK_PAUSE_H``
        hours. Other jobs waiting on the instance do not hold it back: the
        check waits its turn behind them, and passes a queue held by a
        failure (``cloud.job._get_hold_passing_job_types``). A stopped one
        is checked when it starts; one that fails is taken again later.
        The jobs are the platform's and show in the instance's jobs like
        any other. Each is queued in a savepoint of its own: one that
        cannot be queued leaves the others' and the caller's transaction
        as they were.

        :param int limit: most checks queued in one call
        :param instances: only these ``cloud.instance``, if given
        :return: how many were queued
        :rtype: int
        """
        Job = self.env["cloud.job"].sudo()
        busy = Job.search([
            ("instance_id", "!=", False),
            ("job_type_id.code", "=", CHECK_JOB_TYPE),
            ("state", "in", Job._active_states),
        ]).mapped("instance_id")
        paused = Job.search([
            ("instance_id", "!=", False),
            ("job_type_id.code", "=", CHECK_JOB_TYPE),
            ("state", "=", "failed"),
            ("create_date", ">=", fields.Datetime.now()
             - timedelta(hours=FAILED_CHECK_PAUSE_H)),
        ]).mapped("instance_id")
        instances = self.sudo().search(
            [
                ("deployed", "=", True),
                ("running", "=", True),
                ("host_id", "!=", False),
                ("admin_password_state", "=", False),
                ("id", "not in", (busy | paused).ids),
            ]
            + ([("id", "in", instances.ids)] if instances is not None else [])
            + self._periodic_maintenance_domain()
            + self._admin_password_check_domain(),
            limit=limit,
        )
        queued = 0
        for inst in instances:
            try:
                with self.env.cr.savepoint():
                    as_platform(self.env["cloud.job"]).sudo().enqueue(
                        inst.host_id.id, inst.id, CHECK_JOB_TYPE,
                    )
                queued += 1
            except Exception as exc:
                # One instance that cannot be queued must not cost the
                # others their check: the next sweep takes it again.
                _logger.warning(
                    "Could not queue %s for %s: %s",
                    CHECK_JOB_TYPE, inst.name, exc,
                )
        return queued

    def _enqueue_rollback_jobs(self, outcome=None, postponed=False):
        """Leave the instance to be checked again when a move rolls back.

        The move's restore may have checked, and changed, the database it
        loaded on the target; the rollback starts the origin's database
        again, which may still have Odoo's default password. Emptied
        before the start is queued, so that start checks it once it runs.
        A postponed move never stopped its source and keeps its state.

        :param outcome: see ``cloud.instance._enqueue_rollback_jobs``
        :param bool postponed: see ``cloud.instance._enqueue_rollback_jobs``
        :return: what the parent returns
        """
        if not postponed:
            self.sudo().write({"admin_password_state": False})
        return super()._enqueue_rollback_jobs(outcome=outcome, postponed=postponed)

    # Private: ``ir.cron`` runs it as the platform; nothing calls it
    # through ``call_kw``.
    @api.private
    @api.model
    def cron_check_admin_passwords(self):
        """Queue the checks of the running instances still to be checked.

        Hourly, and it finds nothing once every instance has answered:
        what a deploy or a restore loads is checked by its own step
        («Replace the default admin password»).

        :return: how many checks were queued
        :rtype: int
        """
        return self._queue_admin_password_checks()
