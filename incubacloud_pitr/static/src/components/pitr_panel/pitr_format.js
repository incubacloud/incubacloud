/** What the continuous recovery card shows, as pure functions.
 *
 *  Kept apart from the component so the front end's tests can check
 *  the classes and the dates without mounting it. Dates come from the
 *  server as UTC strings and are shown in the browser's local time, in
 *  the panel's own format (``YYYY-MM-DD HH:MM``, as the backups table).
 */

/** The panel's ``rl-pill`` class per state. */
export const PILL_CLASSES = {
    off: "stopped",
    enabling: "building",
    ok: "running",
    lagging: "pending",
    failed: "failed",
};

/** "2026-10-18 22:10:29" (UTC) → Date. */
export function fromUtc(value) {
    return value ? new Date(value.replace(" ", "T") + "Z") : null;
}

const pad = (n) => String(n).padStart(2, "0");

/** Date → "YYYY-MM-DD HH:MM" in local time, the panel's own format. */
export function formatDate(date) {
    if (!date) return "";
    return (
        `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
        `${pad(date.getHours())}:${pad(date.getMinutes())}`
    );
}

/** Date → the "YYYY-MM-DDTHH:MM" a datetime-local input takes (local). */
export function toLocalInput(date) {
    return (
        `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
        `T${pad(date.getHours())}:${pad(date.getMinutes())}`
    );
}

/** How long ago *date* was, in the words the instance page uses. */
export function ago(date, now = Date.now()) {
    if (!date) return "";
    const secs = Math.max(0, Math.floor((now - date) / 1000));
    if (secs < 60) return "just now";
    const mins = Math.floor(secs / 60);
    if (mins < 60) return `${mins} min ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs} h ago`;
    return `${Math.floor(hrs / 24)} d ago`;
}
