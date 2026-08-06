/**
 * mergeById — refresh a collection of server records without throwing
 * away the objects the UI is already rendering.
 *
 * Why this exists:
 *   The refresh pattern across the SPA is ``state.rows = resp.rows``.
 *   That is correct but coarse: every row becomes a brand-new object,
 *   so OWL's reactivity sees the entire collection as changed and
 *   re-renders every consumer, even when a single field on a single
 *   row moved. The rendered DOM stays right — OWL diffs it — but the
 *   render work is wasted, and any object identity a caller was
 *   holding (an optimistic patch, a selection) is silently dropped.
 *
 *   Merging by id keeps the existing objects and writes only the keys
 *   that differ, so reactivity fires for exactly the rows that moved.
 *
 * What it does NOT do:
 *   No deep merge. Nested values are replaced wholesale by identity
 *   comparison, which is what we want for the flat, server-owned
 *   records these endpoints return (a host's ``tags`` array is
 *   authoritative, not something to reconcile element by element).
 *
 * Usage:
 *   state.hosts = mergeById(state.hosts, resp.hosts);
 *   mergeMapById(state.data.instances, resp.instances);
 */

/**
 * Merge an incoming array of records into an existing one, matching on
 * ``id`` and preserving object identity for rows present in both.
 *
 * Order and membership follow ``incoming`` — rows the server no longer
 * returns are dropped, new rows are appended in server order.
 *
 * @param {Array<object>} current  rows currently in state.
 * @param {Array<object>} incoming rows just fetched.
 * @returns {Array<object>} a new array whose elements are the preserved
 *   (mutated in place) objects for known ids and the incoming objects
 *   for new ones. Assign it back to state.
 */
export function mergeById(current, incoming) {
    const byId = new Map((current || []).map((r) => [r.id, r]));
    return (incoming || []).map((row) => {
        const existing = byId.get(row.id);
        if (!existing) return row;
        assignChanged(existing, row);
        return existing;
    });
}

/**
 * Same idea for an id-keyed object map, mutated in place so callers
 * holding the map keep their reference.
 *
 * @param {object} target   map currently in state, keyed by id.
 * @param {object} incoming map just fetched, keyed by id.
 * @returns {object} ``target``.
 */
export function mergeMapById(target, incoming) {
    if (!target || !incoming) return target;
    for (const key of Object.keys(target)) {
        // The server is authoritative on membership: a key it stopped
        // returning was deleted, and leaving it behind would render a
        // ghost row forever.
        if (!(key in incoming)) delete target[key];
    }
    for (const [key, row] of Object.entries(incoming)) {
        if (target[key]) {
            assignChanged(target[key], row);
        } else {
            target[key] = row;
        }
    }
    return target;
}

/**
 * Copy the keys of ``source`` onto ``target``, touching only those whose
 * value actually differs, and dropping keys the source no longer has.
 * Writing unchanged keys would defeat the whole point — every write to
 * a reactive object notifies its subscribers.
 *
 * @param {object} target object to mutate.
 * @param {object} source authoritative values.
 */
function assignChanged(target, source) {
    for (const key of Object.keys(target)) {
        if (!(key in source)) delete target[key];
    }
    for (const [key, value] of Object.entries(source)) {
        if (target[key] !== value) target[key] = value;
    }
}
