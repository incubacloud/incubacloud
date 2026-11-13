"""Where the session guard hooks into Odoo's session check.

Odoo's ``check_session`` calls ``res.device.log._update_device`` on every
request with a signed-in session, after the session token matched and
before the request is served, and pays no attention to what it returns.
The guard runs first, while the session's trace still holds the last
activity it is judged on; a close raises ``SessionExpiredException``,
which Odoo turns into the same sign-out as an expired session — on an
HTTP route, a JSON route and the websocket alike. Odoo 18 calls the same
method from the same place.

Any other failure of the guard is logged and the request goes on: Odoo
turns whatever ``check_session`` raises into a 403 on every route, the
sign-in and the sign-out included (review of 12-nov-2026, A2).

On a route open to visitors (``auth='public'`` or ``'none'``) a close is
served as Odoo serves a session it no longer accepts there: signed out,
as a visitor, instead of sent to the sign-in (M1 of the same review).

Odoo keeps the trace of a user's sessions, with the address, country and
browser of each, for good: its own vacuum only drops the repeated rows of
one session, and deleting a user only empties the rows' user. The
privacy policy keeps them while the user's account exists (#74, 13-nov-
2026), so the daily vacuum deletes the rows of a user who is deleted or
archived.

Archiving a user deletes their stored sessions too, and so does the
vacuum before it deletes the rows that name them: Odoo's session token
includes ``active``, so a session still on disk is accepted again the
moment its user is reactivated, and once the rows are gone "Log out from
all devices" no longer sees it (review of #74, 13-nov-2026).
"""
import logging
import re
from contextlib import nullcontext

from odoo import api, http, models
from odoo.http import SessionExpiredException, root

from ..net import real_ip
from .cloud_session_guard import CLOSED_MARK, SESSION_IDENTIFIER_LENGTH, known_country

_logger = logging.getLogger(__name__)

#: The routes a closed session is served on as a visitor.
VISITOR_AUTHS = ("public", "none")
#: A session identifier as Odoo's store takes it; it refuses the whole
#: call on any other value.
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{%d}$" % SESSION_IDENTIFIER_LENGTH)


class ResDeviceLog(models.Model):
    _inherit = "res.device.log"

    @api.model
    def _update_device(self, request):
        """Judge the session (``cloud.session.guard``), then trace it.

        A failure of the guard other than a close is logged, the
        request's transaction rolled back as the guard's own writes
        already do, and the request goes on: a fault of ours closes
        nobody and blocks nobody.

        :param request: the request or websocket request being
            authenticated
        :raises SessionExpiredException: when the guard closed the session
        """
        try:
            self.env["cloud.session.guard"]._session_guard_check(request)
        except SessionExpiredException:
            raise
        except Exception:  # noqa: BLE001 - never a 403 of ours
            _logger.exception(
                "[session-guard] the guard failed on a request of user %s; "
                "the request goes on", request.session.uid,
            )
            self.env.cr.rollback()
        before = self._ic_traces(request)
        result = super()._update_device(request)
        if self._ic_traces(request) != before:
            self._ic_fill_country(request)
        return result

    @api.model
    def _ic_traces(self, request):
        """Return the session's traces as they stand, to tell whether
        Odoo wrote a row (a new trace, or one refreshed after an hour).

        :param request: the request being authenticated
        :rtype: list
        """
        return [
            (trace.get("platform"), trace.get("browser"),
             trace.get("ip_address"), trace.get("last_activity"))
            for trace in request.session.get("_trace") or []
        ]

    @api.model
    def _ic_fill_country(self, request):
        """Give the row Odoo just wrote the country the CDN states.

        Odoo writes the country its GeoIP finds, and behind a CDN the
        image usually carries no GeoIP database: the row would name none,
        and "Sessions and access" shows the countries of each session.
        Only behind a CDN with its country header (``net/real_ip``), as
        the session guard reads it, and only on rows with no country:
        one source per database. By its English name, as Odoo's GeoIP
        writes it. On the cursor Odoo wrote with: a new one when the
        request's is read-only. A failure is logged; the request goes on.

        :param request: the request being authenticated
        """
        if not (real_ip.installed() and real_ip.country_header()):
            return
        try:
            code = self.env["cloud.session.guard"]._seen_country(request)
            if not known_country(code):
                return
            name = self.env["res.country"].sudo().with_context(lang="en_US").search(
                [("code", "=", code)], limit=1,
            ).name
            if not name:
                return
            if self.env.cr.readonly:
                self.env.cr.rollback()
                cursor = self.env.registry.cursor(readonly=False)
            else:
                cursor = nullcontext(self.env.cr)
            with cursor as cr:
                cr.execute("""
                    UPDATE res_device_log SET country = %s
                     WHERE session_identifier = %s AND ip_address = %s
                       AND country IS NULL
                """, (name, request.session.sid[:SESSION_IDENTIFIER_LENGTH],
                      request.httprequest.remote_addr))
        except Exception:  # noqa: BLE001 - never a 403 of ours
            _logger.exception(
                "[session-guard] could not write the country of a session of "
                "user %s", request.session.uid,
            )

    @api.model
    def _delete_stored_sessions(self, identifiers):
        """Delete the stored sessions named by *identifiers*, with the
        ones they were rotated from.

        A value that is not a session identifier is skipped: Odoo's store
        would refuse every other one with it.

        :param identifiers: iterable of ``session_identifier`` values
        :return: how many identifiers were deleted
        :rtype: int
        """
        identifiers = sorted({
            identifier for identifier in identifiers
            if identifier and IDENTIFIER_RE.match(identifier)
        })
        if identifiers:
            root.session_store.delete_from_identifiers(identifiers)
        return len(identifiers)

    @api.autovacuum
    def _gc_device_log_of_gone_users(self):
        """Delete the traces of the users who are gone, and the sessions
        they name.

        A deleted user's rows are left with no user (the column's foreign
        key sets it empty); an archived user cannot sign in, and their
        account is closed. The sessions go first: one still on disk would
        be accepted again if the user were reactivated, and with its rows
        gone nothing would show it. In SQL: ``res.device`` is a view over
        the same table, and the user's archived state is not read through
        the ORM.
        """
        self.env.cr.execute("""
            SELECT DISTINCT session_identifier FROM res_device_log
             WHERE user_id IS NULL
                OR user_id IN (SELECT id FROM res_users WHERE active IS NOT TRUE)
        """)
        self._delete_stored_sessions(row[0] for row in self.env.cr.fetchall())
        self.env.cr.execute("""
            DELETE FROM res_device_log
             WHERE user_id IS NULL
                OR user_id IN (SELECT id FROM res_users WHERE active IS NOT TRUE)
        """)
        _logger.info(
            "[session-guard] deleted %d device log rows of users who are gone",
            self.env.cr.rowcount,
        )


class ResUsers(models.Model):
    _inherit = "res.users"

    def write(self, vals):
        """Delete the stored sessions of the users archived.

        Odoo's session token includes ``active``: a session left on disk
        is accepted again if the user is reactivated, and once the daily
        vacuum deleted its trace "Log out from all devices" cannot see it.
        """
        result = super().write(vals)
        if "active" in vals and not vals["active"]:
            Log = self.env["res.device.log"].sudo()
            Log._delete_stored_sessions(Log.search_fetch(
                [("user_id", "in", self.ids)], ["session_identifier"],
            ).mapped("session_identifier"))
        return result


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _authenticate_explicit(cls, auth):
        """Serve a session the guard closed as a visitor, on a route open
        to visitors.

        Odoo does so with a session whose token no longer matches; a
        close raises instead, which on any HTTP route sends the browser
        to the sign-in, so the home or pricing page of somebody whose
        session closed would land them there. The session is already
        destroyed; it is signed out in place and the route's own
        authentication runs as for anybody unknown.

        :param str auth: the route's ``auth``
        :raises SessionExpiredException: on any other route
        """
        try:
            return super()._authenticate_explicit(auth)
        except SessionExpiredException as error:
            if auth not in VISITOR_AUTHS or not getattr(error, CLOSED_MARK, False):
                raise
        request = http.request
        request.session.logout(keep_db=True)
        request.env = api.Environment(
            request.env.cr, None, request.session.context,
        )
        return getattr(cls, f"_auth_method_{auth}")()
