"""The agents' credential: write-only, apart from the panel's read one.

Until 1.0.230 every server of an account wrote with the account's own
credential, the one the panel reads with: whoever was root on a server
could read the security logs and the metrics of every other server of the
account through ``/r/`` and ``/lr/``. Each account now has a second
vmauth user, ``<account>-agent``, that reaches ``/w/`` and ``/lw/`` alone
and is forced to the same account label; the agents write with it and the
panel keeps the account's.

Handed out **by state, never by version** (plan #84, A2):

* the agents use it only while it is *in force*: its credential, user and
  password, on the list last put on the central
  (``metrics_credentials_deployed``), or handed to this panel by a layer
  that only hands it over once it is (``_metrics_credentials_injected``);
  otherwise they keep the account's. By the credential, not by the
  user's name: a token minted again while the name is still on the list
  is not in force until a sync puts it there;
* the moment it comes into force, every server's agents are applied again
  (``_reapply_observability_everywhere``), so none stays on the account's
  credential without anyone seeing it;
* the read credential is rotated only once every server of the account
  writes with the agents' user (``cloud.host.metrics_write_user``). The new
  one is first added to the list beside the old one (vmauth admits two
  passwords for one user: measured against v1.102.0 on 14-nov-2026), the
  panel moves to it once the list carries it, and the next sync removes
  the old one. From then on the account's user only reads, with a cap on
  the requests it has in flight.
"""
import hashlib
import logging

from odoo import api, fields, models

from .encrypted_char import EncryptedChar
from .password_utils import generate_password

_logger = logging.getLogger(__name__)

#: What the agents' vmauth user adds to the account's name. Accounts are
#: ``acct_`` and hexadecimal digits, so the suffix cannot collide with one.
AGENT_USER_SUFFIX = "-agent"

#: Requests the account's read user may have in flight once its credential
#: no longer writes (B3 of the review of 13-nov-2026): vmauth answers the
#: next one 429 at once. Provisional until step M (M-d) measures the
#: board's and the cron's LogQL; a board runs up to 16 queries on 4
#: threads, the cron 3 at a time.
READ_MAX_CONCURRENT = 8

#: Job states in which an account sync is still to end.
_SYNC_PENDING_STATES = ("queued", "pending", "enqueued", "wait_dependencies", "started")


def agent_user(account):
    """Return the vmauth user the agents of *account* write as.

    :param str account: the metrics account, e.g. ``acct_9f2c1ab4d0e7``.
    :rtype: str
    """
    return f"{account}{AGENT_USER_SUFFIX}"


def credential_digest(user, password):
    """Return a short digest of one vmauth credential.

    What the panel records of the credentials a run put on the central
    (``metrics_credentials_deployed``), so that "this password is in
    force" can be asked without keeping the password twice.

    :param str user: the vmauth user.
    :param str password: its password.
    :return: 16 hexadecimal digits of SHA-256.
    :rtype: str
    """
    return hashlib.sha256(f"{user}:{password}".encode()).hexdigest()[:16]


class CloudSettings(models.Model):
    _inherit = "cloud.settings"

    metrics_agent_token = EncryptedChar(
        string="Agents' metrics credential",
        groups="base.group_system",
        copy=False,
        help="Password of this panel's agents' user on the central "
             "(<account>-agent), which may only write, under the account's "
             "label. The servers write with it once the central knows it; "
             "the panel reads with the account's credential. Stored "
             "encrypted; written to each server as root-only 0600.",
    )
    metrics_read_token_next = EncryptedChar(
        string="Next metrics read credential",
        groups="base.group_system",
        copy=False,
        help="The account's new read credential while it is rotated in: on "
             "the central beside the old one until the panel uses it.",
    )
    metrics_read_rotated = fields.Boolean(
        string="Metrics read credential rotated",
        readonly=True,
        copy=False,
        help="The account's credential was rotated once every server wrote "
             "with the agents' user: since then it only reads, with a cap on "
             "requests in flight, and no server holds it.",
    )
    metrics_credentials_deployed = fields.Text(
        string="Credentials on the central",
        readonly=True,
        copy=False,
        groups="base.group_system",
        help="A digest of each credential the last successful deployment or "
             "sync put on the central, one per line: what says a new "
             "credential is in force without keeping it twice.",
    )

    def write(self, vals):
        """Re-apply every server's agents when their credential changes in force.

        A layer that hands this panel its credentials hands over the
        agents' one only once the central knows it, so writing a new one
        is the moment the servers can use it (A2). Where this panel owns
        the central the token is minted before the central knows it, and
        the servers are re-applied when the list in force carries it
        (:meth:`_metrics_record_deployed`).
        """
        watched = "metrics_agent_token" in vals
        before = {}
        if watched:
            before = {rec.id: rec.sudo().metrics_agent_token or "" for rec in self}
        result = super().write(vals)
        if watched and any(
            (rec.sudo().metrics_agent_token or "") != before[rec.id] for rec in self
        ) and self._metrics_agent_in_force():
            self.env["cloud.host"]._reapply_observability_everywhere()
        return result

    @api.model
    def _metrics_credentials_injected(self):
        """Return whether this panel's metrics credentials come from a layer above.

        No in core: this panel mints its own and its central confirms
        them. A layer that hands a panel its credentials, and hands the
        agents' one only once its central knows it, says yes.

        :rtype: bool
        """
        return False

    def _ensure_metrics_agent_token(self):
        """Return the agents' credential, minting it once where this panel mints.

        :return: the plaintext token, or '' where it is handed down and
            not there yet.
        :rtype: str
        """
        settings = self._get_system()
        if settings.metrics_agent_token:
            return settings.metrics_agent_token
        if self._metrics_credentials_injected():
            return ""
        settings.sudo().write({"metrics_agent_token": generate_password(32)})
        return settings.metrics_agent_token

    @api.model
    def _metrics_agent_in_force(self):
        """Return whether the agents may write with their own user now.

        Where this panel mints its credentials, when the last list put on
        the central carries the agents' user with the token this panel
        holds (:meth:`_metrics_credential_in_force`): the predicate the
        installs and the reconciliation's revision pass both decide by.

        :rtype: bool
        """
        settings = self._get_system()
        if not (settings.metrics_agent_token and settings.metrics_account):
            return False
        if self._metrics_credentials_injected():
            return True
        return self._metrics_credential_in_force(
            agent_user(settings.metrics_account), settings.metrics_agent_token,
        )

    @api.model
    def _metrics_agent_auth(self):
        """Return ``(user, password, kind)`` the agents write with.

        The agents' user while it is in force, ``kind`` ``agent``; the
        account's credential otherwise, ``kind`` ``account`` (the way
        back, A2). ``('', '', 'account')`` without either.

        :rtype: tuple[str, str, str]
        """
        settings = self._get_system()
        if self._metrics_agent_in_force():
            return (
                agent_user(settings.metrics_account),
                settings.metrics_agent_token,
                "agent",
            )
        user, token = self._metrics_auth()
        return user, token, "account"

    def _metrics_account_extras(self, accounts):
        """Return what each account has on the central besides its credential.

        Core knows its own account. A layer that serves other panels adds
        theirs, as it adds their accounts to ``_desired_metrics_accounts``.

        :param accounts: the account names on the list.
        :return: ``{account: {"agent": token or '', "next": token or '',
            "rotated": bool}}`` for the accounts it knows.
        :rtype: dict
        """
        settings = self._get_system()
        own = settings.metrics_account
        if not own or own not in accounts:
            return {}
        return {
            own: {
                "agent": settings.metrics_agent_token or "",
                "next": settings.metrics_read_token_next or "",
                "rotated": bool(settings.metrics_read_rotated),
            },
        }

    def _vmauth_users(self):
        """Return every user the central's vmauth list is to carry, but the operator.

        Per account: its own user (reads, and writes until its credential
        is rotated, then capped), its next read credential while one is
        rotated in (reads only, capped), and its agents' user (writes
        only). Each is ``{"username", "password", "account", "read",
        "write", "capped"}``.

        :rtype: list[dict]
        """
        accounts = [
            (user, token) for user, token in self._desired_metrics_accounts()
            if user and token
        ]
        extras = self._metrics_account_extras({user for user, _token in accounts})
        users = []
        for account, token in accounts:
            extra = extras.get(account) or {}
            rotated = bool(extra.get("rotated"))
            users.append({
                "username": account, "password": token, "account": account,
                "read": True, "write": not rotated, "capped": rotated,
            })
            if extra.get("next"):
                users.append({
                    "username": account, "password": extra["next"],
                    "account": account, "read": True, "write": False,
                    "capped": True,
                })
            if extra.get("agent"):
                users.append({
                    "username": agent_user(account), "password": extra["agent"],
                    "account": account, "read": False, "write": True,
                    "capped": False,
                })
        return users

    @api.model
    def _metrics_deployed_digests(self, users):
        """Return the digests of *users*' credentials, sorted.

        :param users: as :meth:`_vmauth_users` returns them.
        :rtype: list[str]
        """
        return sorted({
            credential_digest(user["username"], user["password"]) for user in users
        })

    def _metrics_record_deployed(self, users):
        """Record the list a deployment or a sync put in force, and act on it.

        The users and the digests of their credentials. Then: when the
        agents' user just came into force, every server's agents are
        applied again (A2); when the read credential being rotated in is
        on the central, the panel moves to it, and the next sync removes
        the old one.

        :param users: as :meth:`_vmauth_users` returned them for the run.
        """
        settings = self._get_system()
        was_in_force = self._metrics_agent_in_force()
        settings.write({
            "metrics_accounts_deployed": "\n".join(
                sorted({user["username"] for user in users})
            ),
            "metrics_credentials_deployed": "\n".join(
                self._metrics_deployed_digests(users)
            ),
        })
        if self._metrics_agent_in_force() and not was_in_force:
            _logger.info(
                "[metrics] the agents' user is on the central: re-applying "
                "every server's agents",
            )
            self.env["cloud.host"]._reapply_observability_everywhere()
        self._metrics_promote_read_token()

    def _metrics_credential_in_force(self, user, password):
        """Return whether the last list put on the central carries this credential.

        :param str user: the vmauth user.
        :param str password: its password.
        :rtype: bool
        """
        settings = self._get_system()
        digests = (settings.metrics_credentials_deployed or "").split()
        return credential_digest(user, password) in digests

    def _metrics_promote_read_token(self):
        """Move the panel to its new read credential once the central carries it.

        :return: whether it moved.
        :rtype: bool
        """
        settings = self._get_system()
        nxt = settings.metrics_read_token_next
        account = settings.metrics_account
        if not (nxt and account and self._metrics_credential_in_force(account, nxt)):
            return False
        settings.write({
            "metrics_remote_write_token": nxt,
            "metrics_read_token_next": False,
            "metrics_read_rotated": True,
        })
        self.env["cloud.audit.log"].sudo().create({
            "action": "Metrics read credential rotated",
            "details": account,
        })
        _logger.info(
            "[metrics] the read credential of %s was rotated: it no longer "
            "writes", account,
        )
        return True

    @api.model
    def _metrics_rotation_due(self):
        """Return whether this panel's read credential is to be rotated now.

        Once, where this panel mints its credentials: when no server of
        the account writes with it any more (every active server whose
        agents were installed writes with the agents' user, A2) and the
        agents' user is in force.

        :rtype: bool
        """
        settings = self._get_system()
        if self._metrics_credentials_injected():
            return False
        if settings.metrics_read_rotated or settings.metrics_read_token_next:
            return False
        if not self._metrics_agent_in_force():
            return False
        return self.env["cloud.host"]._metrics_every_host_writes_as_agent()

    @api.model
    def _reconcile_metrics_credentials(self):
        """Converge the credentials of the central this panel owns.

        Run by the agents' reconciliation cron. Mints the agents' user
        where it is missing (a central deployed before 1.0.230), starts
        the read credential's rotation when it is due, and syncs the list
        while what the central carries differs from what it should.

        :return: the queued sync's id, or ``False``.
        """
        settings = self._get_system()
        if not settings.metrics_enabled or self._metrics_credentials_injected():
            return False
        host = self._live_central_host()
        if not (host and settings.metrics_account):
            return False
        self._ensure_metrics_agent_token()
        if self._metrics_rotation_due():
            settings.write({"metrics_read_token_next": generate_password(32)})
            _logger.info(
                "[metrics] every server writes with the agents' user: "
                "rotating the read credential of %s", settings.metrics_account,
            )
        desired = self._metrics_deployed_digests(self._vmauth_users())
        deployed = sorted((settings.metrics_credentials_deployed or "").split())
        if desired == deployed:
            return False
        Job = self.env["cloud.job"].sudo()
        if Job.search_count([
            ("job_type_id.code", "in", ("sync_metrics_accounts", "deploy_metrics_central")),
            ("state", "in", _SYNC_PENDING_STATES),
        ]):
            return False
        try:
            with self.env.cr.savepoint():
                return Job.enqueue(host.id, False, "sync_metrics_accounts")
        except Exception as exc:  # noqa: BLE001 — a cron pass never fails on it
            _logger.info(
                "[metrics] could not queue the account sync on %s: %s",
                host.name, exc,
            )
            return False
