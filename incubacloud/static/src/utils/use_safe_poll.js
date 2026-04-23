/**
 * useSafePoll — recursive setTimeout that self-cancels on unmount.
 *
 * Problem it solves:
 *   Polling patterns built on `setTimeout(poll, delay)` where `poll`
 *   calls itself keep running after OWL destroys the component —
 *   the closure still holds `this`, and the next tick tries to
 *   write into `this.state` on a detached record, producing silent
 *   "Cannot set properties of null" errors and a growing set of
 *   zombie timers that hit the server for no user benefit.
 *
 *   The right fix is to tie every scheduled callback to the
 *   component lifecycle: register an onWillUnmount that flips an
 *   `alive` flag and clears the pending timer. This helper packages
 *   that in a single call so new polls can't forget it.
 *
 * Usage:
 *   setup() {
 *       this.safePoll = useSafePoll();
 *       ...
 *   }
 *
 *   async _pollSomething() {
 *       const tick = async () => {
 *           const res = await rpc("/cloud/...");
 *           if (!this.safePoll.alive) return;   // belt & braces
 *           if (!done) this.safePoll.schedule(tick, 2000);
 *       };
 *       this.safePoll.schedule(tick, 1500);
 *   }
 */
import { onWillUnmount } from "@odoo/owl";

export function useSafePoll() {
    // Closure state — `alive` flips to false on unmount so anything
    // that gets past the clearTimeout race (e.g. a fn already queued
    // in the microtask loop) can short-circuit via safePoll.alive.
    const state = { alive: true };
    let pending = null;

    function schedule(fn, delay) {
        if (!state.alive) return null;
        pending = setTimeout(async () => {
            pending = null;
            if (!state.alive) return;
            await fn();
        }, delay);
        return pending;
    }

    onWillUnmount(() => {
        state.alive = false;
        if (pending !== null) {
            clearTimeout(pending);
            pending = null;
        }
    });

    return {
        schedule,
        // Consumers can short-circuit mid-tick by reading this.
        get alive() {
            return state.alive;
        },
    };
}
