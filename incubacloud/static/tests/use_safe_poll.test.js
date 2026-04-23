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
 */
import { Component, xml } from "@odoo/owl";
import { describe, expect, test } from "@odoo/hoot";
import { animationFrame, advanceTime } from "@odoo/hoot-mock";
import { mountWithCleanup } from "@web/../tests/web_test_helpers";

import { useSafePoll } from "@incubacloud/utils/use_safe_poll";


class Harness extends Component {
    static template = xml`<div t-att-data-alive="safePoll.alive"/>`;
    static props = {
        onTick: { type: Function, optional: true },
        delay: { type: Number, optional: true },
        autoStart: { type: Boolean, optional: true },
        stash: { type: Function, optional: true },
    };
    setup() {
        this.safePoll = useSafePoll();
        if (this.props.stash) this.props.stash(this.safePoll);
        if (this.props.autoStart !== false) {
            this.safePoll.schedule(
                () => this.props.onTick?.(),
                this.props.delay ?? 100,
            );
        }
    }
}


describe("useSafePoll", () => {

    test("schedule fires after the delay while mounted", async () => {
        let fired = 0;
        await mountWithCleanup(Harness, {
            props: { delay: 50, onTick: () => { fired += 1; } },
        });
        await advanceTime(50);
        await animationFrame();
        expect(fired).toBe(1);
    });

    test("pending schedule is cancelled on unmount", async () => {
        let fired = 0;
        let stashed;
        const comp = await mountWithCleanup(Harness, {
            props: {
                delay: 200,
                onTick: () => { fired += 1; },
                stash: (sp) => { stashed = sp; },
            },
        });
        // Unmount BEFORE the timer fires — the would-be tick must
        // stay silent.
        comp.__owl__.destroy();
        await advanceTime(500);
        await animationFrame();
        expect(fired).toBe(0);
        expect(stashed.alive).toBe(false);
    });

    test("alive goes false on unmount so awaited ticks short-circuit", async () => {
        let stashed;
        const comp = await mountWithCleanup(Harness, {
            props: {
                autoStart: false,
                stash: (sp) => { stashed = sp; },
            },
        });
        expect(stashed.alive).toBe(true);
        comp.__owl__.destroy();
        expect(stashed.alive).toBe(false);
    });

    test("schedule after unmount is a no-op", async () => {
        let stashed;
        let fired = 0;
        const comp = await mountWithCleanup(Harness, {
            props: {
                autoStart: false,
                stash: (sp) => { stashed = sp; },
            },
        });
        comp.__owl__.destroy();
        stashed.schedule(() => { fired += 1; }, 10);
        await advanceTime(50);
        await animationFrame();
        expect(fired).toBe(0);
    });
});
