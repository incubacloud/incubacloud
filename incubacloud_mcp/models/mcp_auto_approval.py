"""Auto-approval: one credential of one person runs without asking, until a
date or, if so granted, with no end date.

An Administrator grants a rule, in the panel, to one credential of one
user: an API key, an OAuth client, or (with the assistant) the panel's
assistant. While the rule is active, every call of that credential that
would wait for an approval runs at once, marked ``auto`` in the audit and
on its jobs. Nothing else changes: the role, the routes' checks, the
secrets and the limits stay.

A tool that charges money (``ToolSpec.charges``) runs without asking only
under a rule that includes charges (``include_charges``), a box of the
grant left unticked by default; under any other rule it still asks.

The rule is active when, at the very call (no cache, no cron):

1. its user and credential are the request's, exactly;
2. it is not revoked and, if it has one, ``expires_at`` is after the
   database's clock;
3. the person's role is not above the one they had when it was granted;
4. ``cloud.settings._mcp_auto_approval_allowed()`` says yes.

Rules are never deleted: revoked or expired, they stay as history. No
group may create, write or delete them; the logic lives in private
methods (``_grant``, ``_revoke``, ``_use``) that Odoo's RPC cannot reach.

Values, with their reason:

* the end date is optional, with no maximum (decided on 10-nov-2026): a
  rule without one lasts until it is revoked or its credential or person
  ends; how long an agent acts without asking is the decision of whoever
  grants it. The panel recommends 90 days (:data:`RECOMMENDED_DAYS`), the
  usual rotation period of a credential, and says in the notice when a
  rule has no end date;
* no period chosen beforehand (10-nov-2026): the shortcuts
  (:data:`PERIOD_DAYS`, "no end date") and a date are offered, none
  preselected, so whoever grants it picks one on purpose;
* a credential is recorded when it connects, at most once an hour, and
  forgotten 90 days after it was last seen (:data:`SEEN_EVERY`,
  :data:`SEEN_KEPT`): a short list to choose from, without a write per call.
"""

import logging
from datetime import datetime, time, timedelta

import psycopg2
import pytz

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.incubacloud.models._concurrency import read_committed_cursor
from odoo.addons.incubacloud.models.cloud_security_mixin import _GROUP_HIERARCHY

from ..protocol.tool_spec import NO_END_DATE
from .cloud_audit_log import agent_mark

_logger = logging.getLogger(__name__)

#: The shortcuts the panel offers, in days; "no end date" and a date too.
PERIOD_DAYS = (7, 30, 90)
#: The period the panel recommends: the usual rotation period of a credential.
RECOMMENDED_DAYS = 90
#: The version of the notice the Administrator accepts (§6.7). A new text
#: gets a new version, and the panel refuses a grant that accepted another.
#: ``.2``: the notice says whether the rule includes charges; ``.3``: it
#: says when the rule has no end date.
NOTICE_VERSION = "2026-11-10.3"
#: A credential is recorded again when last seen longer ago than this.
SEEN_EVERY = timedelta(hours=1)
#: A credential not seen for this long, and without a live rule, is forgotten.
SEEN_KEPT = timedelta(days=90)
#: Why a rule ended before its date.
REVOKE_REASONS = [
    ("manual", "Revoked"),
    ("superseded", "Replaced by a new rule"),
    ("user archived", "User archived"),
    ("credential deleted", "Credential deleted"),
]
#: The roles of the panel, lowest first, as ``role_at_grant`` keeps them.
ROLE_SELECTION = [
    ("user", "Stakeholder"),
    ("consultant", "Consultant"),
    ("project_manager", "Project Manager"),
    ("developer", "Developer"),
    ("manager", "Administrator"),
]
#: The context key that lets ``create`` and ``write`` through, set only by
#: this model's own methods, with superuser rights.
_INTERNAL = "incubacloud_mcp_auto_approval_internal"
#: What a revocation may write; everything else is fixed at the grant.
_REVOCATION_FIELDS = frozenset({"revoked_at", "revoked_by_id", "revoked_reason"})


def credential_kind(credential):
    """Return the kind of a credential key: ``api_key``, ``oauth`` or ``''``.

    :param str credential: ``Principal.client`` (``API key #<id>`` or
        ``client_id <id>``)
    :rtype: str
    """
    if credential.startswith("API key #"):
        return "api_key"
    if credential.startswith("client_id "):
        return "oauth"
    return ""


def role_of(user):
    """Return the short name of *user*'s highest panel role, or ``''``.

    :param user: a ``res.users`` record
    :rtype: str
    """
    for group in reversed(_GROUP_HIERARCHY):
        if user.has_group(f"incubacloud.{group}"):
            return group.removeprefix("group_cloud_")
    return ""


def role_rank(role):
    """Return the place of the short role name *role* in the hierarchy, or -1."""
    names = [group.removeprefix("group_cloud_") for group in _GROUP_HIERARCHY]
    return names.index(role) if role in names else -1


class McpAutoApproval(models.Model):
    """A rule: this credential of this person runs without asking until a date."""

    _name = "incubacloud.mcp.auto.approval"
    _description = "Auto-approval of an agent's credential"
    _order = "id desc"

    user_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="restrict", readonly=True
    )
    kind = fields.Selection(
        [("api_key", "API key"), ("oauth", "OAuth client")],
        required=True,
        readonly=True,
    )
    credential = fields.Char(
        required=True,
        readonly=True,
        help="The credential, as an approval binds it: 'API key #<id>' or "
        "'client_id <id>'.",
    )
    credential_label = fields.Char(readonly=True, help="How the audit names it.")
    role_at_grant = fields.Selection(ROLE_SELECTION, required=True, readonly=True)
    expires_at = fields.Datetime(
        readonly=True, help="When the rule ends; empty, it has no end date."
    )
    include_charges = fields.Boolean(
        readonly=True,
        default=False,
        help="The tools that charge money run without asking too; without "
        "it they still ask.",
    )
    granted_by_id = fields.Many2one("res.users", readonly=True, ondelete="set null")
    granted_ip = fields.Char(readonly=True)
    notice_version = fields.Char(readonly=True)
    revoked_at = fields.Datetime(readonly=True)
    revoked_by_id = fields.Many2one("res.users", readonly=True, ondelete="set null")
    revoked_reason = fields.Selection(REVOKE_REASONS, readonly=True)
    use_count = fields.Integer(readonly=True, default=0)
    last_used_at = fields.Datetime(readonly=True)

    _live_uniq = models.UniqueIndex(
        "(user_id, credential) WHERE revoked_at IS NULL",
        "A credential has one live auto-approval rule at most.",
    )

    # ── No one writes a rule but this model ───────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        """Only :meth:`_grant` creates a rule."""
        if not (self.env.su and self.env.context.get(_INTERNAL)):
            raise AccessError(
                self.env._("Auto-approval rules are granted in Settings, Agents.")
            )
        return super().create(vals_list)

    def write(self, vals):
        """Only a revocation changes a rule, and only once.

        Its person, credential and date are fixed when it is granted;
        :meth:`_use` counts its calls in SQL.
        """
        if not (self.env.su and self.env.context.get(_INTERNAL)):
            raise AccessError(self.env._("An auto-approval rule cannot be changed."))
        if set(vals) - _REVOCATION_FIELDS:
            raise AccessError(self.env._("An auto-approval rule cannot be changed."))
        return super().write(vals)

    def unlink(self):
        """Rules are history: never deleted."""
        raise AccessError(self.env._("Auto-approval rules are kept as history."))

    # ── Whether a rule approves a call ────────────────────────────────────

    @api.model
    def _live_for(self, uid, credential):
        """Return the live (not revoked) rule of *uid* and *credential*, or empty.

        :param int uid: the request's user
        :param str credential: the request's credential key
        """
        if not uid or not credential:
            return self.sudo().browse()
        return self.sudo().search(
            [
                ("user_id", "=", uid),
                ("credential", "=", credential),
                ("revoked_at", "=", False),
            ],
            limit=1,
        )

    def _state(self):
        """Return this rule's state: ``active``, ``expired``, ``revoked``,
        ``paused_role`` (the person's role went up) or ``paused_platform``
        (the panel does not allow rules now).

        :rtype: str
        """
        self.ensure_one()
        rule = self.sudo()
        if rule.revoked_at:
            return "revoked"
        if rule.expires_at and rule.expires_at <= fields.Datetime.now():
            return "expired"
        if not self.env["cloud.settings"].sudo()._mcp_auto_approval_allowed():
            return "paused_platform"
        if role_rank(role_of(rule.user_id)) > role_rank(rule.role_at_grant):
            return "paused_role"
        return "active"

    @api.model
    def _active_for(self, uid, credential):
        """Return the rule that approves the calls of *uid* with *credential*
        now, or an empty recordset (see the module's docstring).
        """
        rule = self._live_for(uid, credential)
        if rule and rule._state() == "active":
            return rule
        return self.sudo().browse()

    def _until(self):
        """Return until when this rule lets its credential act without
        asking, as the listing and the instructions say it: its end
        (``YYYY-MM-DD HH:MM:SS``, UTC), :data:`NO_END_DATE` for a rule
        without one, or ``''`` for no rule.

        :rtype: str
        """
        if not self:
            return ""
        self.ensure_one()
        expires_at = self.sudo().expires_at
        return fields.Datetime.to_string(expires_at) if expires_at else NO_END_DATE

    def _covers(self, spec):
        """Return whether this rule approves the tool of *spec*: every tool,
        but one that charges money only when the rule includes charges.

        :param spec: the tool's :class:`ToolSpec`
        :rtype: bool
        """
        self.ensure_one()
        return not spec.charges or self.sudo().include_charges

    def _use(self):
        """Count one call this rule approves; return whether it still approves.

        One statement on a cursor of its own, at READ COMMITTED, committed
        before the action runs: it checks the revocation and the date
        against the database's clock (``clock_timestamp()``, the time of
        the statement, not of the transaction) as it counts, a rule without
        an end date passing that check, and an agent's parallel
        calls neither wait for each other during a long action nor make the
        request retry.

        :rtype: bool
        """
        self.ensure_one()
        try:
            with read_committed_cursor(self.env.registry) as cr:
                cr.execute(
                    "UPDATE incubacloud_mcp_auto_approval "
                    "SET use_count = use_count + 1, "
                    "last_used_at = (clock_timestamp() at time zone 'UTC') "
                    "WHERE id = %s AND revoked_at IS NULL "
                    "AND (expires_at IS NULL "
                    "OR expires_at > (clock_timestamp() at time zone 'UTC')) "
                    "RETURNING id",
                    (self.id,),
                )
                used = bool(cr.fetchone())
                cr.commit()
        except psycopg2.Error:
            _logger.warning(
                "[mcp] auto-approval rule %s could not be counted; the call asks",
                self.id,
                exc_info=True,
            )
            used = False
        self.invalidate_recordset(["use_count", "last_used_at"])
        return used

    # ── Granting and revoking ─────────────────────────────────────────────

    @api.model
    def _check_person(self):
        """Refuse a request that comes through an agent or the assistant.

        :raises AccessError: when the request carries an agent's mark
        """
        if agent_mark(self.env):
            raise AccessError(
                self.env._(
                    "Auto-approval rules are managed by a person in the panel, "
                    "never through an agent."
                )
            )

    @api.model
    def _candidates_for(self, user):
        """Return the credentials of *user* a rule may be granted to.

        Those that connected to ``/mcp`` (``incubacloud.mcp.credential``),
        but an API key that no longer exists: it authenticates no more, and
        another key may have its name; a module adds its own (the assistant).

        :param user: a ``res.users`` record
        :return: ``[{credential, kind, label, client_name, last_seen}]``
        :rtype: list[dict]
        """
        seen = (
            self.env["incubacloud.mcp.credential"]
            .sudo()
            .search([("user_id", "=", user.id)], order="last_seen desc")
        )
        existing = self._existing_api_keys()
        return [
            {
                "credential": row.client,
                "kind": credential_kind(row.client),
                "label": row.label or row.client,
                "client_name": row.client_name or "",
                "last_seen": fields.Datetime.to_string(row.last_seen),
            }
            for row in seen
            if credential_kind(row.client)
            and (credential_kind(row.client) != "api_key" or row.client in existing)
        ]

    @api.model
    def _existing_api_keys(self):
        """Return the credential keys of the API keys that exist now.

        :return: ``{"API key #<id>", ...}``
        :rtype: set[str]
        """
        self.env.cr.execute("SELECT id FROM res_users_apikeys")
        return {f"API key #{row[0]}" for row in self.env.cr.fetchall()}

    @api.model
    def _grantable_users(self):
        """Return the users a rule may be granted to: internal, active, with a role.

        :rtype: res.users recordset
        """
        return (
            self.env["res.users"]
            .sudo()
            .search([("share", "=", False), ("id", "!=", SUPERUSER_ID)])
            .filtered(
                lambda user: role_of(user) and not (user.login or "").startswith("__")
            )
        )

    @api.model
    def _candidates(self):
        """Return, for Settings, each user and the credentials they may be granted.

        Only users with at least one credential.

        :rtype: list[dict]
        """
        result = []
        for user in self._grantable_users().sorted("name"):
            credentials = self._candidates_for(user)
            if credentials:
                result.append(
                    {
                        "id": user.id,
                        "name": user.name,
                        "login": user.login,
                        "role": role_of(user),
                        "credentials": credentials,
                    }
                )
        return result

    @api.model
    def _expiry(self, days=None, expires_on=None, no_expiry=False):
        """Return when a rule granted now with this period ends (UTC, naive),
        or None for a rule with no end date.

        :param int days: a number of days, 1 or more
        :param str expires_on: a date (``YYYY-MM-DD``) after today: the rule
            ends at the end of that day, in the Administrator's time zone
        :param bool no_expiry: the rule has no end date (only ``True``
            counts), and neither *days* nor *expires_on* is given
        :raises UserError: for no period, more than one, or one out of bounds
        """
        now = fields.Datetime.now()
        chosen = [
            value for value in (days, expires_on) if value not in (None, False, "")
        ]
        if no_expiry is True:
            if chosen:
                raise UserError(self.env._("Choose a period or no end date, not both."))
            return None
        if days not in (None, False, ""):
            if isinstance(days, bool) or not isinstance(days, int) or days < 1:
                raise UserError(self.env._("The rule lasts 1 day or more."))
            try:
                return now + timedelta(days=days)
            except OverflowError:
                raise UserError(self.env._("Choose a nearer end.")) from None
        if not expires_on:
            raise UserError(self.env._("Choose how long the rule lasts."))
        try:
            day = fields.Date.to_date(expires_on)
        except (TypeError, ValueError):
            day = None
        tz = pytz.timezone(self.env.user.tz or "UTC")
        today = now.replace(tzinfo=pytz.utc).astimezone(tz).date()
        if not day or day <= today:
            raise UserError(self.env._("The rule ends on a day after today."))
        end = tz.localize(datetime.combine(day, time(23, 59, 59)))
        return end.astimezone(pytz.utc).replace(tzinfo=None)

    @api.model
    def _grant(
        self,
        user_id,
        credential,
        days=None,
        expires_on=None,
        acknowledged=False,
        notice_version="",
        ip="",
        include_charges=False,
        no_expiry=False,
    ):
        """Grant a rule to *credential* of *user_id*, as the current user.

        The current user is an Administrator in a request of their own
        (no agent's), who accepted the current notice. A live rule of the
        same credential is replaced (``superseded``). Leaves its audit row.

        :param int user_id: the person whose agent stops asking
        :param str credential: one of :meth:`_candidates_for`'s keys
        :param int days: the period, see :meth:`_expiry`
        :param str expires_on: or the last day, see :meth:`_expiry`
        :param bool acknowledged: the notice's box was ticked
        :param str notice_version: the notice's version the panel showed
        :param str ip: the Administrator's address
        :param bool include_charges: the tools that charge money run
            without asking too (only ``True`` counts)
        :param bool no_expiry: the rule has no end date, see :meth:`_expiry`
        :return: the rule
        :raises AccessError: through an agent, or below Administrator
        :raises UserError: for anything else that does not hold
        """
        self._check_person()
        self.env["cloud.security.mixin"]._check_can_manage_settings()
        if not self.env["cloud.settings"].sudo()._mcp_auto_approval_allowed():
            raise UserError(self.env._("This panel does not allow auto-approval."))
        if acknowledged is not True or notice_version != NOTICE_VERSION:
            raise UserError(
                self.env._(
                    "Read the notice again and confirm it: it changed, or it was "
                    "not confirmed."
                )
            )
        valid_id = isinstance(user_id, int) and not isinstance(user_id, bool)
        user = self.env["res.users"].sudo().browse(user_id if valid_id else [])
        if not user.exists() or user not in self._grantable_users():
            raise UserError(self.env._("Choose an active user with a role."))
        chosen = next(
            (
                item
                for item in self._candidates_for(user)
                if item["credential"] == credential
            ),
            None,
        )
        if not chosen:
            raise UserError(
                self.env._(
                    "Choose one of this user's credentials that connected to the panel."
                )
            )
        expires_at = self._expiry(days=days, expires_on=expires_on, no_expiry=no_expiry)
        Rule = self.sudo().with_context(**{_INTERNAL: True})
        Rule._live_for(user.id, credential)._revoke("superseded")
        rule = Rule.create(
            {
                "user_id": user.id,
                "kind": chosen["kind"],
                "credential": credential,
                "credential_label": chosen["label"],
                "role_at_grant": role_of(user),
                "expires_at": expires_at,
                "granted_by_id": self.env.uid,
                "granted_ip": ip or "",
                "notice_version": notice_version,
                "include_charges": include_charges is True,
            }
        )
        if expires_at:
            details = self.env._(
                "%(person)s · %(credential)s · until %(date)s UTC",
                person=user.name,
                credential=rule.credential_label,
                date=fields.Datetime.to_string(expires_at),
            )
        else:
            details = self.env._(
                "%(person)s · %(credential)s · no end date",
                person=user.name,
                credential=rule.credential_label,
            )
        if rule.include_charges:
            details += " · " + self.env._("charges included")
        rule._audit("Auto-approval rule granted", details)
        _logger.info(
            "[mcp] auto-approval rule %s granted to uid=%s via=%s until %s "
            "charges=%s by uid=%s",
            rule.id,
            user.id,
            credential,
            expires_at or "no end date",
            rule.include_charges,
            self.env.uid,
        )
        return rule

    def _revoke(self, reason, by=None):
        """End these rules now, for *reason*; those already ended stay as they are.

        :param str reason: one of :data:`REVOKE_REASONS`
        :param by: the user who revokes, if a person does
        :return: the rules this ended
        """
        live = self.sudo().filtered(lambda rule: not rule.revoked_at)
        if not live:
            return live
        live.with_context(**{_INTERNAL: True}).write(
            {
                "revoked_at": fields.Datetime.now(),
                "revoked_by_id": by.id if by else False,
                "revoked_reason": reason,
            }
        )
        # Written now: a rule granted next in the same transaction needs the
        # unique index to see this one ended.
        live.flush_recordset(["revoked_at", "revoked_by_id", "revoked_reason"])
        label = dict(REVOKE_REASONS)[reason]
        for rule in live:
            rule._audit(
                "Auto-approval rule revoked",
                self.env._(
                    "%(person)s · %(credential)s · %(reason)s",
                    person=rule.user_id.name,
                    credential=rule.credential_label or rule.credential,
                    reason=label,
                ),
            )
            _logger.info("[mcp] auto-approval rule %s revoked: %s", rule.id, reason)
        return live

    def _revoke_by_person(self):
        """Revoke this rule as the current user: an Administrator any rule,
        anyone else their own.

        :raises AccessError: through an agent, or for another's rule below
            Administrator
        """
        self.ensure_one()
        self._check_person()
        rule = self.sudo()
        if rule.user_id.id != self.env.uid:
            self.env["cloud.security.mixin"]._check_can_manage_settings()
        return rule._revoke("manual", by=self.env.user)

    def _audit(self, action, details):
        """Leave the audit row of this rule's grant or revocation."""
        self.ensure_one()
        self.env["cloud.audit.log"].sudo().create(
            {
                "action": action,
                "details": details[:2000],
                "approval_rule_id": self.id,
            }
        )

    # ── What the panel shows ──────────────────────────────────────────────

    def _format(self):
        """Return what Settings and Connect an agent show of each rule.

        :rtype: list[dict]
        """
        # The labels in the reader's language.
        roles = dict(self._fields["role_at_grant"]._description_selection(self.env))
        reasons = dict(self._fields["revoked_reason"]._description_selection(self.env))
        return [
            {
                "id": rule.id,
                "user_id": rule.user_id.id,
                "user_name": rule.user_id.name or "",
                "role_at_grant": roles.get(rule.role_at_grant, ""),
                "kind": rule.kind,
                "credential": rule.credential,
                "credential_label": rule.credential_label or rule.credential,
                "granted_by": rule.granted_by_id.name or "",
                "granted_at": fields.Datetime.to_string(rule.create_date),
                "expires_at": fields.Datetime.to_string(rule.expires_at)
                if rule.expires_at
                else "",
                "last_used_at": fields.Datetime.to_string(rule.last_used_at)
                if rule.last_used_at
                else "",
                "use_count": rule.use_count,
                "include_charges": rule.include_charges,
                "state": rule._state(),
                "revoked_at": fields.Datetime.to_string(rule.revoked_at)
                if rule.revoked_at
                else "",
                "revoked_by": rule.revoked_by_id.name or "",
                "revoked_reason": reasons.get(rule.revoked_reason, ""),
            }
            for rule in self.sudo()
        ]

    def _activity(self, offset=0, limit=20):
        """Return a page of the audit rows of this rule: what it approved, its
        grant and its revocation, newest first.

        :rtype: dict
        """
        self.ensure_one()
        Log = self.env["cloud.audit.log"].sudo()
        domain = [("approval_rule_id", "=", self.id)]
        rows = Log.search(domain, offset=offset, limit=limit, order="id desc")
        entries = rows._format()
        names = {
            row.id: {
                "instance_name": row.instance_id.name or "",
                "host_name": row.host_id.name or "",
                "project_name": row.project_id.name or "",
            }
            for row in rows
        }
        for entry in entries:
            entry.update(names.get(entry["id"], {}))
        return {"entries": entries, "total": Log.search_count(domain)}

    # ── Housekeeping ──────────────────────────────────────────────────────

    @api.autovacuum
    def _gc_deleted_credentials(self):
        """Mark ``credential deleted`` the live rules whose API key is gone.

        A deleted or expired key authenticates no more, and Odoo never
        reuses its id: the rule already approves nothing; this records why.
        """
        live = self.sudo().search(
            [("revoked_at", "=", False), ("kind", "=", "api_key")]
        )
        if not live:
            return
        existing = self._existing_api_keys()
        live.filtered(lambda rule: rule.credential not in existing)._revoke(
            "credential deleted"
        )


class McpCredential(models.Model):
    """A credential that connected to ``/mcp``: what a rule may be granted to.

    The panel keeps no list of the OAuth clients that connect, only their
    tokens; this is that list, for the Administrator to choose from.
    """

    _name = "incubacloud.mcp.credential"
    _description = "Agent credential seen on the MCP endpoint"
    _order = "last_seen desc"

    user_id = fields.Many2one(
        "res.users", required=True, index=True, ondelete="cascade", readonly=True
    )
    client = fields.Char(
        required=True,
        readonly=True,
        help="The credential, as an approval binds it: 'API key #<id>' or "
        "'client_id <id>'.",
    )
    label = fields.Char(readonly=True, help="How the audit names it.")
    client_name = fields.Char(
        readonly=True, help="The name the client declares (not verified)."
    )
    first_seen = fields.Datetime(readonly=True)
    last_seen = fields.Datetime(readonly=True, index=True)

    _user_client_uniq = models.UniqueIndex(
        "(user_id, client)", "A credential is recorded once per user."
    )

    @api.model
    def _seen(self, principal, client_name=""):
        """Record that *principal*'s credential connected, at most once an hour.

        One indexed read per request; the write, on a cursor of its own at
        READ COMMITTED, at most once an hour per credential. A failure is
        logged and does not stop the request.

        :param principal: the request's :class:`Principal`
        :param str client_name: the name the client declares
        """
        if not principal.uid or not credential_kind(principal.client or ""):
            return
        self.env.cr.execute(
            "SELECT last_seen FROM incubacloud_mcp_credential "
            "WHERE user_id = %s AND client = %s",
            (principal.uid, principal.client),
        )
        row = self.env.cr.fetchone()
        now = fields.Datetime.now()
        if row and row[0] and now - row[0] < SEEN_EVERY:
            return
        try:
            with read_committed_cursor(self.env.registry) as cr:
                cr.execute(
                    "INSERT INTO incubacloud_mcp_credential "
                    "(user_id, client, label, client_name, first_seen, last_seen, "
                    "create_uid, write_uid, create_date, write_date) "
                    "VALUES (%(uid)s, %(client)s, %(label)s, %(name)s, %(now)s, "
                    "%(now)s, %(su)s, %(su)s, %(now)s, %(now)s) "
                    "ON CONFLICT (user_id, client) DO UPDATE SET "
                    "label = EXCLUDED.label, client_name = EXCLUDED.client_name, "
                    "last_seen = EXCLUDED.last_seen, write_date = EXCLUDED.write_date",
                    {
                        "uid": principal.uid,
                        "client": principal.client,
                        "label": principal.credential,
                        "name": client_name or "",
                        "now": now,
                        "su": SUPERUSER_ID,
                    },
                )
                cr.commit()
        except psycopg2.Error:
            _logger.warning(
                "[mcp] the credential of uid=%s could not be recorded",
                principal.uid,
                exc_info=True,
            )

    @api.autovacuum
    def _gc_unseen(self):
        """Forget the credentials not seen for :data:`SEEN_KEPT` and without a
        live rule."""
        old = self.sudo().search(
            [("last_seen", "<", fields.Datetime.now() - SEEN_KEPT)]
        )
        if not old:
            return
        live = (
            self.env["incubacloud.mcp.auto.approval"]
            .sudo()
            .search(
                [("revoked_at", "=", False), ("credential", "in", old.mapped("client"))]
            )
        )
        kept = {(rule.user_id.id, rule.credential) for rule in live}
        old.filtered(lambda row: (row.user_id.id, row.client) not in kept).unlink()
