/**
 * useDebouncedBus — coalesce a burst of bus notifications into a
 * single callback invocation.
 *
 * Why this exists:
 *   A running job can fire dozens of ``cloud_jobs`` bus events per
 *   second via chunk flushes. Without a guard, every subscriber
 *   would call an RPC per event — for instance_detail that meant
 *   N × ``load_jobs`` + N × ``/cloud/get_instance`` on the same tab
 *   during a rebuild. On top of the RPC flood, out-of-order
 *   responses could overwrite fresh state with stale state.
 *
 * What it does:
 *   - Last-write-wins buffer: every ``trigger(arg)`` call replaces
 *     the pending argument. Only the most recent arg reaches the
 *     callback when the timer fires. (Chosen over a set-of-args
 *     strategy because every consumer we have calls a
 *     "refresh the whole recordset" action afterwards, so the arg
 *     is merely a trigger, not a payload to aggregate.)
 *   - In-flight guard: if the previous invocation is still awaiting
 *     its RPC, further triggers keep updating ``pendingArg`` but do
 *     not schedule a second run until the first finishes.
 *   - Trailing fire: if more events arrived while the callback was
 *     running, a follow-up is scheduled after the current one
 *     resolves, so the UI never ends up stale on the last event.
 *   - Unmount cleanup: ``onWillUnmount`` flips ``alive`` to false
 *     and clears any pending timer, so callbacks can't write into
 *     a destroyed component.
 *
 * Trade-offs:
 *   - 300 ms default delay is invisible to human perception and
 *     collapses the common ``docker compose build`` chunk storm from
 *     ~50/s to ~3/s.
 *   - Last-write-wins means that if two unrelated events arrive
 *     within the window and the first one matched a filter you
 *     care about, the callback only sees the second one. Safe for
 *     callers that ignore the arg and just refresh a whole view;
 *     watch out if you branch on the arg itself.
 *
 * Usage:
 *   const trigger = useDebouncedBus((id) => this.loadData());
 *   this._busService.subscribe("cloud_jobs", (payload) => trigger(payload.id));
 */
import { onWillUnmount } from "@odoo/owl";

/**
 * Whether a ``cloud_jobs`` event is one a page should refetch on.
 *
 * A running job publishes a ``progress`` event every half second it
 * wrote output, and every page used to answer each with a full
 * refetch: the host page reloaded itself and its job list twice a
 * second through a deploy it does not even list (QA, 20-oct-2026).
 * Output lines change nothing a page renders; a system message (a
 * step done, a warning) is the job's "last message" in every list,
 * so a ``progress`` event carrying one does count. ``created`` and
 * ``state`` always do. An event without ``kind`` (an older server)
 * counts too: refetching for nothing beats going stale.
 *
 * The live log page is not a caller: it wants every chunk.
 *
 * @param {{kind?: string, system?: boolean}} payload bus event
 * @returns {boolean}
 */
export function refreshesOn(payload) {
    if (payload?.kind !== "progress") return true;
    return payload.system === true;
}

export function useDebouncedBus(callback, { delay = 300 } = {}) {
    let timer = null;
    let pendingArg = null;
    let hasPending = false;
    let inflight = false;
    let alive = true;

    onWillUnmount(() => {
        alive = false;
        if (timer !== null) {
            clearTimeout(timer);
            timer = null;
        }
    });

    async function run() {
        timer = null;
        if (!alive || inflight) return;
        const arg = pendingArg;
        pendingArg = null;
        hasPending = false;
        inflight = true;
        try {
            await callback(arg);
        } catch (_e) {
            // Swallow — a failed callback must not break future
            // triggers. Caller is responsible for its own logging.
        } finally {
            inflight = false;
        }
        // A fresh event landed while we were running. Schedule one
        // more pass so the last trigger is always honored.
        if (alive && hasPending) schedule();
    }

    function schedule() {
        if (timer !== null) return;
        timer = setTimeout(run, delay);
    }

    return function trigger(arg) {
        if (!alive) return;
        pendingArg = arg;
        hasPending = true;
        if (!inflight) schedule();
    };
}
