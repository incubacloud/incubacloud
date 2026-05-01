/**
 * Tests for useSafePoll — the unmount guard for recursive setTimeout
 * poll patterns. The contract under test:
 *
 *   1. `schedule(fn, delay)` runs `fn` after the delay.
 *   2. After the component unmounts, any still-pending `fn` must
 *      NOT run — if it did, the closure would try to write into a
 *      destroyed component's state.
 *   3. `.alive` flips to false on unmount so user code can short-
 *      circuit mid-tick (between an awaited RPC and the next state
 *      write).
 *
 * Why no Component / mountWithCleanup:
 *   ``mountWithCleanup`` spins up the bus/discuss/mail mock stack and
 *   fails with ``Cannot find a definition for model "discuss.channel"``
 *   for a test that doesn't need any of it. ``useSafePoll`` accepts
 *   an injectable ``unmountHook`` so we capture the cleanup callback
 *   directly and trigger it manually — purely synchronous, no OWL
 *   mounting, no mocks beyond a one-line capture function.
 */
import { describe, expect, test } from "@odoo/hoot";

import { useSafePoll } from "@incubacloud/utils/use_safe_poll";


/**
 * Build a useSafePoll instance with a captured unmount callback.
 * Returns ``{ poll, unmount }`` so each test can call ``unmount()``
 * to simulate the component being destroyed.
 */
function makePoll() {
    let captured = null;
    const poll = useSafePoll({
        unmountHook: (fn) => { captured = fn; },
    });
    if (!captured) {
        throw new Error("useSafePoll did not register an unmount hook");
    }
    return { poll, unmount: captured };
}


describe("useSafePoll", () => {

    test("schedule fires after the delay while mounted", async () => {
        const { poll } = makePoll();
        let fired = 0;
        poll.schedule(() => { fired += 1; }, 10);
        // Real setTimeout is fine for this — keep delay tiny so the
        // test still runs in a few ms.
        await new Promise(r => setTimeout(r, 30));
        expect(fired).toBe(1);
    });

    test("pending schedule is cancelled on unmount", async () => {
        const { poll, unmount } = makePoll();
        let fired = 0;
        poll.schedule(() => { fired += 1; }, 50);
        // Unmount BEFORE the timer fires — the would-be tick must
        // stay silent.
        unmount();
        await new Promise(r => setTimeout(r, 80));
        expect(fired).toBe(0);
        expect(poll.alive).toBe(false);
    });

    test("alive goes false on unmount so awaited ticks short-circuit", () => {
        const { poll, unmount } = makePoll();
        expect(poll.alive).toBe(true);
        unmount();
        expect(poll.alive).toBe(false);
    });

    test("schedule after unmount is a no-op", async () => {
        const { poll, unmount } = makePoll();
        let fired = 0;
        unmount();
        const handle = poll.schedule(() => { fired += 1; }, 10);
        // schedule returns null when alive is false — that's the
        // signal callers can use to detect a dropped schedule.
        expect(handle).toBe(null);
        await new Promise(r => setTimeout(r, 30));
        expect(fired).toBe(0);
    });

    test("alive starts true on a fresh poll", () => {
        const { poll } = makePoll();
        expect(poll.alive).toBe(true);
    });
});
