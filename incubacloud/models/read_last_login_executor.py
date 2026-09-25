"""Read the last login recorded inside a staging's own Odoo database.

The second half of the staging activity clock. The panel signal
(``last_touched_at``) only sees people who act on the instance *from the
panel*; somebody can work inside a staging for weeks without ever
opening it. Asking the staging who last logged in is the cheapest honest
way to see that, and the only one a scanner cannot fake — it takes a
valid credential to produce a row.

The one rule this file exists to hold: **a failed reading is not
evidence of disuse.** A stopped container, an unreachable database, a
host that times out — all of them leave ``last_login_seen_at`` exactly
as it was. The field only ever moves forward, and only on a real answer.
"""
import logging
import re
from datetime import datetime, timezone

from odoo import fields

from .abstract_executor import AbstractSSHExecutor

_logger = logging.getLogger(__name__)

#: The single line ``read_last_login.sh`` prints on success.
_ANSWER_RE = re.compile(r"^LAST_LOGIN (\S+)$", re.MULTILINE)
_LABEL = "Read last login"


class ReadLastLoginExecutor(AbstractSSHExecutor):
    """Ask a staging's database when somebody last logged into it."""

    _job_type = "read_last_login"

    def _inst(self):
        """Return the instance this job reads."""
        return self.job.instance_id

    def get_commands(self):
        """Return the single reading command.

        Credentials go through as script arguments rather than baked
        into the command text, which is what keeps them out of the job
        log and the command injection-free.
        """
        inst = self._inst()
        return [
            (
                _LABEL,
                self.run_script("read_last_login.sh", [
                    self._inst_dir(inst),
                    inst.postgres_dbname or "prod",
                    inst.postgres_username or "odoo",
                    inst.postgres_password or "",
                ]),
            ),
        ]

    async def on_success(self, results):
        """Move the login stamp forward, if the answer is newer.

        Three answers are possible and only one writes anything:

        * a timestamp newer than what we hold — recorded;
        * a timestamp older than what we hold — ignored. A staging
          restored from an older backup would otherwise hand back a
          past date and *shorten* its own remaining life;
        * ``none``, meaning the table is there and nobody has ever
          logged in — also nothing to record, and emphatically not a
          reason to blank the field.
        """
        inst = self._inst()
        stamp = self._parse_answer(results.get(_LABEL, {}).get("stdout", ""))
        if not stamp:
            self._sys("No login has ever been recorded in this instance.")
            return
        if inst.last_login_seen_at and stamp <= inst.last_login_seen_at:
            return
        inst.sudo().write({"last_login_seen_at": stamp})
        self._sys(f"Last login inside the instance: {stamp}.")

    async def on_failure(self, results, errors):
        """Leave the clock untouched and say why.

        Deliberately quiet: a staging that is stopped, or on a host
        having a bad minute, is the normal case rather than an incident,
        and an alert per instance per day would train everyone to ignore
        the alert panel. What matters is that nothing here writes to
        ``last_login_seen_at`` — the reading simply did not happen.
        """
        for err in errors:
            self._sys(f"✗ {err}")
        self._sys(
            "No reading taken. The activity clock is unchanged: not "
            "being able to ask is not the same as nobody having logged in."
        )

    @staticmethod
    def _parse_answer(stdout):
        """Return the datetime the script reported, or ``False``.

        :param str stdout: the command's raw output.
        :return: a naive UTC ``datetime``, or ``False`` for ``none`` and
            for anything that does not parse.
        """
        match = _ANSWER_RE.search(stdout or "")
        if not match:
            return False
        raw = match.group(1)
        if raw == "none":
            return False
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            _logger.warning("read_last_login: unparseable answer %r", raw)
            return False
        # Odoo stores naive UTC. A tenant whose column happens to carry a
        # timezone would otherwise produce an offset-aware value that
        # cannot be compared with anything already on the record.
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        # A clock skewed into the future would hand a staging an
        # indefinite reprieve, so the answer is capped at now.
        return min(parsed, fields.Datetime.now())
