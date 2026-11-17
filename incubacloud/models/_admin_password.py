"""The step that replaces Odoo's default password of an instance's admin.

Odoo's ``base`` gives its administrator the password ``admin``. Every flow
that puts a database under an instance's Odoo runs ``admin_password.sh``
before Odoo serves it: the deploy that creates the database, and each
restore that loads one. The script changes the password only when the
stored one still verifies ``admin``, to a random one that is never kept,
shown nor returned; whoever needs a password of their own signs in with
Connect and sets it in Odoo.

What the script answered is kept on the instance
(``admin_password_state``, and ``admin_password_checked_at``, when it last
answered), so the panel knows which instances are still to be looked at,
those with no state: the sweep (``cloud.instance.cron_check_admin_passwords``)
checks them while they run, and ``start_instance`` when it starts one.

The script marks each change in the instance's database, in the same
transaction (``incubacloud.admin_password_replaced_at``, a time and
nothing else), and is told when the panel last heard an answer
(``since``): a change whose answer was lost after its commit is answered
``changed`` again by the next run, which tells it then.
"""
import calendar
import re
import time

from odoo import fields

#: The label of the step, in every flow that runs it.
LABEL_ADMIN_PASSWORD = "Replace the default admin password"

#: The line ``admin_password.sh`` prints on success.
_ANSWER_RE = re.compile(r"^ADMIN_PASSWORD (changed|own|absent)$", re.MULTILINE)


def parse_admin_password_answer(stdout):
    """Return what ``admin_password.sh`` answered, or None.

    :param str stdout: the step's output
    :return: ``"changed"``, ``"own"``, ``"absent"``, or None when the
        output holds no answer
    :rtype: str | None
    """
    found = _ANSWER_RE.findall(stdout or "")
    return found[-1] if found else None


def record_admin_password_answer(inst, answer, job=None, announce=True):
    """Keep on *inst* what its check answered, and tell a change.

    :param inst: the ``cloud.instance`` checked
    :param answer: ``"changed"``, ``"own"`` or ``"absent"``; None leaves
        the instance to be checked again, keeping when it last answered
        (``admin_password_checked_at``, the script's ``since``)
    :param job: the ``cloud.job`` that checked it, if any
    :param bool announce: raise the instance's alert and write the audit
        row when the password was changed
    """
    if answer is None:
        inst.sudo().write({"admin_password_state": False})
        return
    inst.sudo().write({
        "admin_password_state": answer,
        "admin_password_checked_at": fields.Datetime.now(),
    })
    if answer == "changed" and announce:
        inst._announce_default_admin_password(job=job)


class AdminPasswordMixin:
    """Run and record the default-password check, for instance executors.

    Expects the executor API: ``_inst()``, ``_inst_dir()``, ``job``,
    ``run_script()`` and ``_sys()``.
    """

    #: Whether a change is told on the instance (its alert and audit row).
    #: Off for a flow whose instance is the platform's own until it is
    #: handed over, such as a layer's spare instances.
    _announces_admin_password = True

    def _admin_password_step(self, d, inst, mode, fatal=False, loads=False):
        """Return the step that replaces the admin's default password.

        :param str d: the instance's directory on its host
        :param inst: the instance
        :param str mode: ``"stopped"`` (Odoo does not run: a one-off
            container) or ``"running"`` (Odoo serves: its container, and
            Odoo restarted when the password changed)
        :param bool fatal: stop the job when the step fails
        :param bool loads: the job puts a database under the instance's
            Odoo (a deploy, a restore): only a change this job makes counts
            as ``changed``, and a mark the loaded database brought from
            elsewhere does not. Otherwise the database is the one the
            instance had, and a mark newer than its last answer is a change
            whose answer was lost
        :return: a ``get_commands`` step
        :rtype: tuple
        """
        command = self.run_script(
            "admin_password.sh",
            [d, inst.postgres_dbname or "prod", mode,
             self._admin_password_since(inst, loads)],
        )
        if fatal:
            return (LABEL_ADMIN_PASSWORD, command, {"stop_on_failure": True})
        return (LABEL_ADMIN_PASSWORD, command)

    @staticmethod
    def _admin_password_since(inst, loads):
        """Return the ``since`` the script is given, in epoch seconds.

        :param inst: the instance
        :param bool loads: see :meth:`_admin_password_step`
        :return: now for a job that loads a database; else when the
            instance last answered, 0 if it never did
        :rtype: int
        """
        if loads:
            return int(time.time())
        last = inst.admin_password_checked_at
        return calendar.timegm(last.timetuple()) if last else 0

    def _admin_password_answer(self, results):
        """Return what this job's check answered, or None.

        None when the step did not run, failed, or answered nothing.

        :param dict results: ``{label: {'stdout', 'exit_status'}}``
        :rtype: str | None
        """
        data = (results or {}).get(LABEL_ADMIN_PASSWORD)
        if not data or data.get("exit_status", 1) != 0:
            return None
        return parse_admin_password_answer(data.get("stdout"))

    def _record_admin_password(self, results, loaded=True, announce=True):
        """Keep on the instance what its check answered, and tell a change.

        :param dict results: the job's step results
        :param bool loaded: whether the job put another database under the
            instance's Odoo. Without an answer, such a job leaves the
            instance to be checked again (the sweep, or its next start);
            one that loaded nothing leaves the mark as it was.
        :param bool announce: raise the instance's alert and write the audit
            row when the password was changed; off for the deploy that
            creates the database, whose admin always starts with the
            default one
        :return: the answer, or None
        :rtype: str | None
        """
        inst = self._inst()
        if not inst:
            return None
        answer = self._admin_password_answer(results)
        if answer is None:
            if loaded and LABEL_ADMIN_PASSWORD in (results or {}):
                self._sys(
                    "⚠ The admin user's password could not be checked: it is "
                    "checked again once the instance runs."
                )
            if loaded:
                record_admin_password_answer(inst, None)
            return None
        record_admin_password_answer(
            inst, answer, self.job, announce and self._announces_admin_password,
        )
        return answer
