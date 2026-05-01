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
 *
 * Testing:
 *   Pass ``unmountHook`` to capture the cleanup callback and call it
 *   manually to simulate unmount, avoiding the cost of mounting a
 *   real component just to exercise the closure logic.
 *
 *       let cleanup;
 *       const sp = useSafePoll({ unmountHook: (fn) => { cleanup = fn; } });
 *       cleanup();   // simulate component destroy
 *       expect(sp.alive).toBe(false);
 */
import { onWillUnmount as _defaultUnmount } from "@odoo/owl";

export function useSafePoll({ unmountHook } = {}) {
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

    // Register cleanup. ``unmountHook`` is injectable so tests can
    // capture the callback and trigger it manually without spinning
    // up the Hoot mount/discuss/bus stack.
    const registerUnmount = unmountHook || _defaultUnmount;
    registerUnmount(() => {
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
