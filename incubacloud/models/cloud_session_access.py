"""Sessions and access: what a user sees of their own sessions, and is told.

Three things, around the session guard (``cloud_session_guard``):

- **The history** (``cloud.session.seen``): the countries and browser
  families each user's sessions have been used from. The guard writes it
  on the requests it already judges (a new trace), so no request pays
  for it otherwise.
- **The notices** (``cloud.session.notice``): an email, without any
  question, when an account is used from a country or a browser family
  its history does not have, and when somebody opens a new way in that
  outlives a session (an API key, a Git token; the layers add their
  own). At most one a day per user and reason. An account with no
  history yet only gets it written: neither its first sign-in nor the
  first one after this is deployed sends anything.
- **The page** ("Sessions and access", ``/cloud/sessions``): the user's
  sessions, the last closes with their reason, the countries and
  browsers seen, the ways in they hold, each with Revoke, and "Sign out
  everywhere". That button asks for no password and no second factor
  (the platform owner's decision, 11-nov-2026): it moves the user's
  ``backchannel_logout_counter``, part of the session token, so every
  cookie of the user stops matching at once. Agents and API keys are
  left alone: they are revoked one by one, so nobody breaks their CI by
  signing out.

Core alone sends no notice unless switched on
(``incubacloud.session_guard.notices``): it may be installed on
somebody's ERP, where emailing every user is not ours to decide. The
values, and why: ``docs/runbooks/RB-33-session-guard.md``.
"""
import logging
from datetime import timedelta
from urllib.parse import urlsplit

from markupsafe import Markup

from odoo import api, fields, models
from odoo.http import get_session_max_inactivity, request

from ..mail_layout import wrap_in_mail_layout
from .cloud_session_guard import (
    COUNTRY_DIRECT,
    COUNTRY_TOR,
    PARAM_PREFIX,
    SESSION_IDENTIFIER_LENGTH,
    known_browser,
    known_country,
)

_logger = logging.getLogger(__name__)

#: The QWeb template of every notice.
NOTICE_TEMPLATE = "incubacloud.mail_session_notice"
#: How many hours one notice per user and reason covers. A rate-limit
#: bucket keeps it, and the buckets are swept a day after they start, so
#: a longer value would not hold: it is capped here. RB-33 (13-nov-2026).
NOTICE_WINDOW_HOURS = 24
#: How many sessions, closes and values the page lists.
PAGE_SESSIONS = 20
PAGE_CLOSES = 10
#: The API keys of this scope are Odoo's "trusted devices" of two-factor
#: authentication, not a way in of their own.
TRUSTED_DEVICE_SCOPE = "browser"


class CloudSessionSeen(models.Model):
    """A country or browser family a user's sessions were used from.

    One row per user, kind and value, written in SQL by the session
    guard on a cursor of its own (``cloud.session.guard``). Kept while
    the user's account exists, as Odoo's own trace of the sessions is
    (#74): a deleted user's rows go with the user, an archived one's at
    the next daily vacuum.
    """

    _name = "cloud.session.seen"
    _description = "Country or browser a user's sessions were used from"
    _order = "last_seen desc, id desc"

    user_id = fields.Many2one(
        "res.users", required=True, ondelete="cascade", index=True,
    )
    kind = fields.Selection(
        [("country", "Country"), ("browser", "Browser")], required=True,
    )
    value = fields.Char(
        required=True,
        help="A country code, or a browser family as Odoo's parser names it.",
    )
    first_seen = fields.Datetime(required=True)
    last_seen = fields.Datetime(required=True)

    def init(self):
        """Create the index the upsert of a row conflicts on."""
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS cloud_session_seen_once
                ON cloud_session_seen (user_id, kind, value)
        """)

    @api.autovacuum
    def _gc_seen_of_archived_users(self):
        """Delete the history of the users who are archived (#74)."""
        self.env.cr.execute("""
            DELETE FROM cloud_session_seen
             WHERE user_id IN (SELECT id FROM res_users WHERE active IS NOT TRUE)
        """)


class CloudSessionNotice(models.AbstractModel):
    """The emails of "Sessions and access": who, how often, what they say."""

    _name = "cloud.session.notice"
    _description = "Session and access notices"

    # -- switches and places -------------------------------------------

    @api.model
    def _session_notices_default(self):
        """Return whether notices go out when no parameter says.

        ``False`` in core: see the module's docstring. The layers that
        run the platform return ``True``.

        :rtype: bool
        """
        return False

    @api.model
    def _session_notices_on(self):
        """Return whether notices go out: the parameter over the default.

        ``incubacloud.session_guard.notices``, ``on`` or ``off``; any
        other value counts as the default. Read from Odoo's cache.

        :rtype: bool
        """
        raw = (
            self.env["ir.config_parameter"].sudo().get_param(
                PARAM_PREFIX + "notices",
            ) or ""
        ).strip().lower()
        if raw in ("on", "off"):
            return raw == "on"
        return self._session_notices_default()

    @api.model
    def _session_notice_on_sign_in(self):
        """Return whether a new country or browser seen on a session's
        first request is told, or only one seen later in a session.

        ``True`` in core. A layer whose every sign-in comes through
        another database that already told it returns ``False``.

        :rtype: bool
        """
        return True

    @api.model
    def _session_notice_holders(self):
        """Return who holds the account, told of every new way in too.

        Nobody in core; the layers name their account's holder.

        :rtype: res.users
        """
        return self.env["res.users"]

    @api.model
    def _session_notice_place(self):
        """Return how the notices name this database: its address's host.

        :rtype: str
        """
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        return urlsplit(base).hostname or base

    @api.model
    def _session_notice_page_url(self):
        """Return the address of "Sessions and access" the notices link to.

        :rtype: str
        """
        base = (
            self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        ).rstrip("/")
        return f"{base}/cloud/sessions"

    # -- what a request shows ------------------------------------------

    @api.model
    def _session_notice_origin(self):
        """Return the country code, browser family and system of the
        request being served, as the session guard reads them; empty
        outside a request.

        :rtype: dict
        """
        if not request:
            return {}
        guard = self.env["cloud.session.guard"]
        try:
            country = guard._seen_country(request)
            agent = request.httprequest.user_agent
            return {
                "country": country if known_country(country) else "",
                "browser": guard._seen_browser(request),
                "platform": agent.platform or "",
            }
        except Exception:  # noqa: BLE001 - a notice without its origin still goes
            _logger.warning("[session-notice] could not read the request's origin",
                            exc_info=True)
            return {}

    # -- sending -------------------------------------------------------

    @api.model
    def _session_notify(self, users, reason, facts, origin=None):
        """Email *users* about *reason*, once a day each at most.

        Nothing goes out while notices are off. A user who is archived,
        or has no email address, is skipped. Each email is queued
        (``mail.mail``) and the mail queue woken, never sent while the
        request waits.

        :param users: the ``res.users`` to tell
        :param str reason: ``country``, ``browser``, ``agent`` (an agent
            or an API key), ``git_token`` or ``invited_user``
        :param dict facts: what the email names, see the template
            ``incubacloud.mail_session_notice``
        :param dict origin: the country, browser and system it came
            from; by default the request's (:meth:`_session_notice_origin`)
        :return: the users told
        :rtype: res.users
        """
        if not self._session_notices_on():
            return self.env["res.users"]
        if origin is None:
            origin = self._session_notice_origin()
        told = self.env["res.users"]
        for user in users.sudo().exists():
            if not user.active or not user.partner_id.email:
                continue
            if not self._session_notice_within_quota(user, reason):
                continue
            self._session_notice_queue(user, reason, facts, origin)
            told |= user
        if told:
            self.env.ref("mail.ir_cron_mail_scheduler_action").sudo()._trigger()
        return told

    @api.model
    def _session_notice_within_quota(self, user, reason):
        """Count a notice to *user* for *reason*; return whether it may go.

        One per :data:`NOTICE_WINDOW_HOURS` (``notice_window_hours``, at
        most that), on ``cloud.rate.limit``, counted even in tests.

        :rtype: bool
        """
        try:
            hours = int(self.env["ir.config_parameter"].sudo().get_param(
                PARAM_PREFIX + "notice_window_hours",
            ))
        except (TypeError, ValueError):
            hours = NOTICE_WINDOW_HOURS
        hours = min(hours, NOTICE_WINDOW_HOURS) if hours > 0 else NOTICE_WINDOW_HOURS
        return self.env["cloud.rate.limit"].sudo().with_context(
            force_rate_limit=True,
        ).hit(
            f"session_notice:{user.id}:{reason}",
            max_per_window=1, window_seconds=hours * 3600,
        )

    @api.model
    def _session_notice_queue(self, user, reason, facts, origin):
        """Render the notice for *user*, in their language, and queue it.

        :param user: the recipient
        :param str reason: see :meth:`_session_notify`
        :param dict facts: what the email names
        :param dict origin: country, browser and system it came from
        """
        lang = user.lang or user.partner_id.lang or "en_US"
        env = self.with_context(lang=lang).env
        country = ""
        if origin.get("country"):
            country = env["res.country"].sudo().search(
                [("code", "=", origin["country"])], limit=1,
            ).name or origin["country"]
        now = fields.Datetime.now()
        values = {
            # What each reason names, empty unless the facts say.
            "seen": "", "door_kind": "", "door": "", "owner": "", "actor": "",
            "panel": "", "invitee": "",
            "reason": reason,
            "name": user.name or user.login,
            "place": self._session_notice_place(),
            # In the recipient's time zone: the record's context names it,
            # the environment's user (the superuser here) would not.
            "when": fields.Datetime.context_timestamp(
                user.with_context(tz=user.tz or "UTC"), now,
            ).strftime("%Y-%m-%d %H:%M %Z").strip(),
            "country": country,
            "browser": (origin.get("browser") or "").capitalize()
            if known_browser(origin.get("browser")) else "",
            "platform": (origin.get("platform") or "").capitalize(),
            "page_url": self._session_notice_page_url(),
            **facts,
        }
        if reason in ("country", "browser") and not values["seen"]:
            values["seen"] = values["country"] if reason == "country" else values["browser"]
        notice = self.with_env(env)
        subject = notice._session_notice_subject(reason, values)
        _ = env._
        device = values["browser"]
        if device and values["platform"]:
            device = _("%(browser)s on %(platform)s", **values)
        body = env["ir.qweb"]._render(NOTICE_TEMPLATE, {
            "greeting": _("Hello %(name)s,", name=values["name"]),
            "lead": notice._session_notice_lead(reason, values),
            "open_page": Markup(_("Open %(page)s and sign out everywhere.")) % {
                "page": Markup('<a href="%s">%s</a>') % (
                    values["page_url"], _("Sessions and access"),
                ),
            },
            "when": values["when"],
            "country": values["country"],
            "device": device,
        }, minimal_qcontext=True)
        env["mail.mail"].sudo().create({
            "subject": subject,
            "body_html": wrap_in_mail_layout(env, body),
            "email_to": user.partner_id.email_formatted,
            "auto_delete": True,
        })

    @api.model
    def _session_notice_lead(self, reason, values):
        """Return the sentence that says what happened, in this
        environment's language, each name escaped and in bold.

        Whole sentences, so that a language puts the names where its
        grammar wants them.

        :param str reason: see :meth:`_session_notify`
        :param dict values: what the email names
        :rtype: markupsafe.Markup
        """
        _ = self.env._
        if reason == "country":
            text = _("Your account on %(place)s was used from %(seen)s, a "
                     "country it had not been used from before.")
        elif reason == "browser":
            text = _("Your account on %(place)s was used from %(seen)s, a "
                     "browser it had not been used from before.")
        elif reason == "agent" and values.get("door_kind") == "api_key":
            text = _("An API key named %(door)s was created on %(place)s for "
                     "%(owner)s.")
        elif reason == "agent":
            text = _("%(owner)s connected the agent %(door)s to %(panel)s: it "
                     "can use that panel on their behalf.")
        elif reason == "git_token":
            text = _("%(actor)s saved a Git token on %(place)s, for %(door)s.")
        elif reason == "invited_user" and values.get("actor"):
            text = _("%(actor)s invited %(invitee)s to %(panel)s.")
        elif reason == "invited_user":
            text = _("%(invitee)s was invited to %(panel)s.")
        else:
            text = _("There is a new way in to %(place)s.")
        bold = {
            key: Markup("<strong>%s</strong>") % (value or "")
            for key, value in values.items()
            if key in ("place", "seen", "door", "panel", "invitee")
        }
        plain = {key: value or "" for key, value in values.items() if key not in bold}
        return Markup(text) % _OneLine(plain | bold, fold=False)

    @api.model
    def _session_notice_subject(self, reason, values):
        """Return the subject of a notice, in this environment's language.

        :param str reason: see :meth:`_session_notify`
        :param dict values: what the email names
        :rtype: str
        """
        _ = self.env._
        if reason == "country":
            subject = _("Your account was used from a new country: %(seen)s")
        elif reason == "browser":
            subject = _("Your account was used from a new browser: %(seen)s")
        elif reason == "agent" and values.get("door_kind") == "api_key":
            subject = _("New API key on %(place)s: %(door)s")
        elif reason == "agent":
            subject = _("New agent connected to %(panel)s: %(door)s")
        elif reason == "git_token":
            subject = _("New Git token on %(place)s")
        elif reason == "invited_user":
            subject = _("%(invitee)s was invited to %(panel)s")
        else:
            subject = _("New access to %(place)s")
        # One line each: a name written by somebody would end the header
        # there. A value the reason does not name is empty.
        return subject % _OneLine(values)


class _OneLine(dict):
    """The values of a subject, each on one line, and empty when missing."""

    def __init__(self, values, fold=True):
        """Keep *values*, each with its whitespace folded to single spaces.

        :param dict values: what the email names
        :param bool fold: whether to fold them; ``False`` keeps each
            value as it is (markup stays markup)
        """
        super().__init__(
            (key, " ".join(str(value or "").split()) if fold else value)
            for key, value in values.items()
        )

    def __missing__(self, key):
        """Return an empty value for a name the subject has and the values not.

        :param str key: the missing name
        :rtype: str
        """
        return ""


class ResUsers(models.Model):
    _inherit = "res.users"

    backchannel_logout_counter = fields.Integer(
        copy=False,
        help="Part of the session token: moving it signs every session of "
             "the user out at once (Sign out everywhere; on a layer, a "
             "logout another database sends). Empty until first moved, so "
             "that adding it changed nobody's token.",
    )

    def _get_session_token_fields(self):
        """Add the counter to what the session token is made of.

        Odoo leaves an empty value out of the token, so the counter
        changes no session until it is first moved; and it clears its
        cache of tokens whenever one of these fields is written.
        """
        return super()._get_session_token_fields() | {"backchannel_logout_counter"}

    # -- Sign out everywhere -------------------------------------------

    def _ic_sign_out_everywhere(self):
        """Sign this user out of every session, without asking anything.

        Moves the counter (every cookie of the user stops matching), and
        deletes the stored sessions Odoo's trace names, marking them
        ended. Agents and API keys are not sessions and are left as they
        are. A layer adds the other databases (:meth:`_ic_signed_out_everywhere`).
        The caller signs the current request out.
        """
        self.ensure_one()
        sid = ""
        if request and request.session.uid == self.id:
            sid = request.session.sid
        user = self.sudo()
        user.backchannel_logout_counter = (user.backchannel_logout_counter or 0) + 1
        Log = self.env["res.device.log"].sudo()
        logs = Log.search([("user_id", "=", self.id), ("revoked", "=", False)])
        Log._delete_stored_sessions(logs.mapped("session_identifier"))
        logs.write({"revoked": True})
        _logger.info(
            "[sessions] user %s signed out everywhere (%d sessions)",
            self.id, len(set(logs.mapped("session_identifier"))),
        )
        self._ic_signed_out_everywhere(sid)

    def _ic_signed_out_everywhere(self, sid):
        """Run what Sign out everywhere does beyond this database. Nothing
        in core.

        :param str sid: the id of the session that asked, ``''`` when it
            was not the user's own request
        """

    # -- the page ------------------------------------------------------

    def _ic_sessions_and_access(self):
        """Return what "Sessions and access" shows this user.

        :return: ``sessions``, ``closes``, ``seen``, ``doors``,
            ``everywhere`` and ``links``; see each method
        :rtype: dict
        """
        self.ensure_one()
        return {
            "sessions": self._ic_session_rows(),
            "closes": self._ic_close_rows(),
            "seen": self._ic_seen_rows(),
            "doors": self._ic_doors(),
            "everywhere": self._ic_everywhere(),
            "links": self._ic_sessions_links(),
        }

    def _ic_country_names(self, codes=(), names=()):
        """Return the country of each code and English name, in the
        user's language.

        Odoo's trace names a country in English (GeoIP's), the guard by
        its code. One search for all of them.

        :param codes: country codes
        :param names: English country names
        :return: ``{code or name: name in the user's language}``; one not
            found is left out
        :rtype: dict
        """
        codes, names = set(filter(None, codes)), set(filter(None, names))
        if not codes and not names:
            return {}
        Country = self.env["res.country"].sudo().with_context(lang="en_US")
        found = Country.search(["|", ("code", "in", list(codes)), ("name", "in", list(names))])
        english = {country.id: country.name for country in found}
        local = found.with_context(lang=self.lang or "en_US")
        result = {}
        for country in local:
            if country.code in codes:
                result[country.code] = country.name
            if english[country.id] in names:
                result[english[country.id]] = country.name
        return result

    def _ic_session_rows(self):
        """Return this user's last sessions, from Odoo's trace of them.

        :return: one dict per session, the most recently used first:
            ``current``, ``active``, ``first`` and ``last`` (datetimes),
            ``browsers``, ``platforms`` and ``countries``
        :rtype: list
        """
        self.env.cr.execute("""
            SELECT session_identifier, min(first_activity), max(last_activity),
                   bool_or(revoked IS TRUE),
                   array_agg(DISTINCT browser) FILTER (WHERE browser IS NOT NULL),
                   array_agg(DISTINCT platform) FILTER (WHERE platform IS NOT NULL),
                   array_agg(DISTINCT country) FILTER (WHERE country IS NOT NULL)
              FROM res_device_log
             WHERE user_id = %s
             GROUP BY session_identifier
             ORDER BY max(last_activity) DESC NULLS LAST
             LIMIT %s
        """, (self.id, PAGE_SESSIONS))
        rows = self.env.cr.fetchall()
        current = ""
        if request and request.session.sid:
            current = request.session.sid[:SESSION_IDENTIFIER_LENGTH]
        idle = timedelta(seconds=get_session_max_inactivity(self.env))
        now = fields.Datetime.now()
        names = self._ic_country_names(
            names=[name for row in rows for name in (row[6] or [])],
        )
        return [{
            "current": identifier == current,
            "active": identifier == current or (
                not revoked and bool(last) and now - last < idle
            ),
            "first": first,
            "last": last,
            "browsers": sorted(browser.capitalize() for browser in browsers or []),
            "platforms": sorted(platform.capitalize() for platform in platforms or []),
            "countries": sorted(names.get(country, country) for country in countries or []),
        } for identifier, first, last, revoked, browsers, platforms, countries in rows]

    def _ic_close_rows(self):
        """Return the last closes of this user's sessions, with their reason.

        :return: ``when``, ``reason`` (``country``, ``browser`` or
            ``inactivity``), ``from`` and ``to`` (countries by name)
        :rtype: list
        """
        closes = self.env["cloud.session.closure"].sudo().search(
            [("user_id", "=", self.id), ("action", "=", "closed")],
            limit=PAGE_CLOSES,
        )
        names = self._ic_country_names(codes=[
            value for close in closes if close.signal == "country"
            for value in (close.pinned_value, close.seen_value)
        ])
        rows = []
        for close in closes:
            pinned, seen = close.pinned_value, close.seen_value
            if close.signal == "country":
                special = {COUNTRY_TOR: "Tor", COUNTRY_DIRECT: self.env._("unknown")}
                pinned = special.get(pinned) or names.get(pinned, pinned)
                seen = special.get(seen) or names.get(seen, seen)
            elif close.signal == "browser":
                pinned, seen = pinned.capitalize(), seen.capitalize()
            else:
                pinned = seen = ""
            rows.append({
                "when": close.create_date,
                "reason": close.signal,
                "from": pinned,
                "to": seen,
            })
        return rows

    def _ic_seen_rows(self):
        """Return the countries and browsers this user's sessions were
        used from (``cloud.session.seen``).

        :return: ``countries`` and ``browsers``, each a list of
            ``name``, ``first`` and ``last``, the most recent first
        :rtype: dict
        """
        seen = self.env["cloud.session.seen"].sudo().search([("user_id", "=", self.id)])
        names = self._ic_country_names(
            codes=seen.filtered(lambda row: row.kind == "country").mapped("value"),
        )
        result = {"countries": [], "browsers": []}
        for row in seen:
            if row.kind == "country":
                name = names.get(row.value, row.value)
                result["countries"].append(
                    {"name": name, "first": row.first_seen, "last": row.last_seen},
                )
            else:
                result["browsers"].append({
                    "name": row.value.capitalize(),
                    "first": row.first_seen, "last": row.last_seen,
                })
        return result

    def _ic_doors(self):
        """Return the ways in this user holds that outlive a session.

        Core lists the user's API keys (not two-factor's trusted devices)
        and, to a manager of the panel, the Git tokens it keeps. Layers
        add theirs.

        :return: one dict per way in: ``kind``, ``id``, ``kind_label``,
            ``name``, ``detail``, ``created``, ``expires``, ``last_used``
        :rtype: list
        """
        self.ensure_one()
        _ = self.env._
        doors = []
        keys = self.env["res.users.apikeys"].sudo().search([
            ("user_id", "=", self.id), ("scope", "!=", TRUSTED_DEVICE_SCOPE),
        ], order="create_date desc, id desc")
        for key in keys:
            doors.append({
                "kind": "api_key", "id": key.id, "kind_label": _("API key"),
                "name": key.name, "detail": "",
                "created": key.create_date, "expires": key.expiration_date,
                "last_used": False,
            })
        if self.has_group("incubacloud.group_cloud_manager"):
            credentials = self.env["cloud.git.credential"].sudo().search([])
            kinds = dict(credentials._fields["kind"]._description_selection(self.env))
            for cred in credentials:
                where = cred.provider_id.name or ""
                if cred.namespace:
                    where = f"{where} · {cred.namespace}" if where else cred.namespace
                doors.append({
                    "kind": "git_token", "id": cred.id, "kind_label": _("Git token"),
                    "name": where, "detail": kinds.get(cred.kind, ""),
                    "created": cred.create_date, "expires": cred.expires_on,
                    "last_used": False,
                })
        return doors

    def _ic_revoke_door(self, kind, door_id):
        """Revoke one of this user's ways in, without asking anything.

        :param str kind: a ``kind`` of :meth:`_ic_doors`
        :param int door_id: its ``id``
        :return: whether it was revoked; ``False`` when it is not one
            this user may revoke, or is gone
        :rtype: bool
        """
        self.ensure_one()
        if kind == "api_key":
            key = self.env["res.users.apikeys"].sudo().search(
                [("id", "=", door_id), ("user_id", "=", self.id)],
            )
            if not key:
                return False
            key.with_user(self)._remove()
            return True
        if kind == "git_token":
            if not self.has_group("incubacloud.group_cloud_manager"):
                return False
            credential = self.env["cloud.git.credential"].with_user(self).browse(
                door_id,
            ).exists()
            if not credential:
                return False
            credential.unlink()
            return True
        return False

    def _ic_everywhere(self):
        """Return how the page offers "Sign out everywhere".

        :return: ``{'mode': 'button'}``; a layer whose sessions are all
            closed from another database returns ``{'mode': 'link',
            'url': …}``
        :rtype: dict
        """
        return {"mode": "button"}

    def _ic_sessions_links(self):
        """Return the page's links to what is kept elsewhere. None in core.

        :return: ``label`` and ``url`` of each
        :rtype: list
        """
        return []


class ResUsersApikeys(models.Model):
    _inherit = "res.users.apikeys"

    def _generate(self, scope, name, expiration_date):
        """Tell the user, and the account's holder, of a new API key.

        Two-factor's trusted devices are API keys of their own scope and
        are not told. A notice that fails is logged; the key is made.
        """
        key = super()._generate(scope, name, expiration_date)
        if scope != TRUSTED_DEVICE_SCOPE:
            user = self.env.user
            Notice = self.env["cloud.session.notice"].sudo()
            try:
                Notice._session_notify(
                    user | Notice._session_notice_holders(), "agent",
                    {"door_kind": "api_key", "door": name, "owner": user.name},
                )
            except Exception:  # noqa: BLE001 - the key is made all the same
                _logger.exception("[session-notice] could not tell of a new API key")
        return key


class CloudGitCredential(models.Model):
    _inherit = "cloud.git.credential"

    @api.model_create_multi
    def create(self, vals_list):
        """Tell of the Git tokens saved."""
        credentials = super().create(vals_list)
        credentials.filtered(lambda cred: cred.sudo().token)._ic_tell_new_token()
        return credentials

    def write(self, vals):
        """Tell of a Git token replaced by another."""
        result = super().write(vals)
        if vals.get("token"):
            self._ic_tell_new_token()
        return result

    def _ic_tell_new_token(self):
        """Tell whoever saved these tokens, and the account's holder.

        A notice that fails is logged; the token is saved.
        """
        if not self:
            return
        Notice = self.env["cloud.session.notice"].sudo()
        user = self.env.user
        for credential in self.sudo():
            where = credential.provider_id.name or ""
            if credential.namespace:
                where = f"{where} · {credential.namespace}" if where else credential.namespace
            try:
                Notice._session_notify(
                    user | Notice._session_notice_holders(), "git_token",
                    {"door": where, "actor": user.name},
                )
            except Exception:  # noqa: BLE001 - the token is saved all the same
                _logger.exception("[session-notice] could not tell of a new Git token")
