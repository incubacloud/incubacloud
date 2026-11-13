import { Component, useState, onWillStart } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { parseUTC, userLocale } from "../../utils/dates";

/**
 * Sessions and access: the signed-in user's sessions, the last closes
 * with their reason, the countries and browsers seen, the ways in they
 * hold, and Sign out everywhere (``/cloud/sessions/*``).
 *
 * Nothing here asks for a password, a second factor or a confirmation
 * (the platform owner's decision, 11-nov-2026): Revoke revokes, and Sign
 * out everywhere signs out. Where the sessions are all closed from
 * another database, the server answers a link instead of the button
 * (``everywhere.mode === "link"``).
 */
export class SessionsAccess extends Component {
    static template = "incubacloud.SessionsAccess";
    static props = {};

    setup() {
        this.state = useState({
            loading: true,
            error: false,
            data: null,
            busy: false,
        });
        onWillStart(() => this.load());
    }

    /** Ask the server for the page's data. */
    async load() {
        this.state.loading = true;
        try {
            const data = await rpc("/cloud/sessions/get", {});
            this.state.data = data?.ok ? data : null;
            this.state.error = !data?.ok;
        } catch {
            this.state.error = true;
        } finally {
            this.state.loading = false;
        }
    }

    /** @returns {object|undefined} the session this page is open in */
    get currentSession() {
        return (this.state.data?.sessions || []).find((s) => s.current);
    }

    /** @returns {Array} every other session listed, the latest first */
    get otherSessions() {
        return (this.state.data?.sessions || []).filter((s) => !s.current);
    }

    /**
     * The reason of a close, as the user reads it.
     *
     * @param {string} reason ``country``, ``browser`` or ``inactivity``
     * @returns {string}
     */
    reasonLabel(reason) {
        return SessionsAccess.reasonLabel(reason);
    }

    /**
     * See :meth:`reasonLabel`; static so a test reads it without mounting.
     *
     * @param {string} reason
     * @returns {string}
     */
    static reasonLabel(reason) {
        if (reason === "country") return _t("It was used from another country");
        if (reason === "browser") return _t("It was used from another browser");
        if (reason === "inactivity") return _t("Inactivity");
        return reason || "";
    }

    /**
     * A date from the server, in the user's language and time zone.
     *
     * @param {string} value naive UTC, as Odoo sends it
     * @returns {string}
     */
    when(value) {
        const date = parseUTC(value);
        if (!date) return "";
        return date.toLocaleString(userLocale(), {
            year: "numeric", month: "short", day: "numeric",
            hour: "2-digit", minute: "2-digit",
        });
    }

    /**
     * A list of names on one line.
     *
     * @param {Array<string>} names
     * @returns {string}
     */
    joined(names) {
        return (names || []).filter(Boolean).join(", ");
    }

    /**
     * What one session ran in: its browsers on its systems.
     *
     * @param {object} session a row of ``sessions``
     * @returns {string}
     */
    device(session) {
        const browsers = this.joined(session.browsers);
        const platforms = this.joined(session.platforms);
        if (browsers && platforms) return _t("%(browsers)s on %(platforms)s", { browsers, platforms });
        return browsers || platforms || _t("Unknown device");
    }

    /** Sign out of every session, this one included, then leave. */
    async signOutEverywhere() {
        if (this.state.busy) return;
        this.state.busy = true;
        try {
            const res = await rpc("/cloud/sessions/sign_out_everywhere", {});
            if (res?.ok) {
                this._leave(res.redirect || "/web/login");
                return;
            }
            this.env.toast?.error(_t("Could not sign out everywhere."));
        } catch {
            this.env.toast?.error(_t("Could not sign out everywhere."));
        }
        this.state.busy = false;
    }

    /**
     * Send the browser to *url*: the session it had is gone.
     *
     * @param {string} url
     */
    _leave(url) {
        window.location.href = url;
    }

    /**
     * Revoke one way in and show the page afresh.
     *
     * @param {object} door a row of ``doors``
     */
    async revoke(door) {
        if (this.state.busy) return;
        this.state.busy = true;
        try {
            const res = await rpc("/cloud/sessions/revoke", { kind: door.kind, door_id: door.id });
            if (res?.ok) {
                this.state.data = res;
                this.env.toast?.success(_t("Revoked."));
            } else {
                this.env.toast?.error(_t("Could not revoke it."));
            }
        } catch {
            this.env.toast?.error(_t("Could not revoke it."));
        } finally {
            this.state.busy = false;
        }
    }
}
