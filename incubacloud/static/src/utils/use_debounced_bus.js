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
