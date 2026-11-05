import { _t } from "@web/core/l10n/translation";
import { user } from "@web/core/user";

/**
 * Parse a datetime string from the Odoo backend as UTC.
 *
 * Odoo serializes ``fields.Datetime`` as naive strings like
 * ``"2026-04-08 10:57:32"`` (no timezone indicator).  JavaScript's
 * ``new Date()`` interprets those as **local time**, which is wrong
 * because Odoo stores everything in UTC.
 *
 * This helper appends ``Z`` so the browser knows the value is UTC
 * and local getters (``getHours``, ``toLocaleString``, …) return
 * the correct time for the user's timezone.
 */
export function parseUTC(dateStr) {
    if (!dateStr) return null;
    const s = String(dateStr).trim();
    if (!s) return null;
    // Already ISO with T and/or Z → parse as-is
    if (s.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(s)) {
        return new Date(s);
    }
    // Odoo naive format: "YYYY-MM-DD HH:MM:SS" → add T and Z
    return new Date(s.replace(' ', 'T') + 'Z');
}

/**
 * The language dates are written in: the Odoo user's, not the browser's.
 *
 * ``toLocaleString([])`` follows the browser, so a user who reads the
 * panel in Spanish on an English browser got "Oct 18, 2026".
 *
 * @returns {string|undefined} a BCP 47 tag such as ``es-ES``; undefined
 *   (the browser's) when the user has none.
 */
export function userLocale() {
    return user.lang || undefined;
}

/**
 * How long ago something happened, short and in the reader's language:
 * "just now", "5m ago", "3h ago", "2d ago".
 *
 * @param {number} secs seconds elapsed.
 * @returns {string}
 */
export function agoText(secs) {
    if (secs < 60) return _t("just now");
    const mins = Math.floor(secs / 60);
    if (mins < 60) return _t("%sm ago", mins);
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return _t("%sh ago", hrs);
    return _t("%sd ago", Math.floor(hrs / 24));
}
