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
