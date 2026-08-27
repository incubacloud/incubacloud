/**
 * Tests for the written confirmation on "Delete completely".
 *
 * Deleting an instance now takes its backups with it, so there is no
 * way back from that button and it asks for the instance name first.
 *
 * The name, not a fixed word like DELETE: the expensive mistake here is
 * almost never "I did not mean to delete", it is "I deleted the wrong
 * one" — two similarly named instances, the wrong tab. Typing the name
 * catches that; a fixed word catches nothing.
 *
 * And the match is exact — no trimming, no case folding. A barrier that
 * forgives is a barrier you clear on autopilot, which is the same
 * reflex the whole thing exists to interrupt.
 *
 * Why no mounting: ``removeConfirmed`` is a getter over
 * ``state.removeModal`` and nothing else, so the contract can be
 * asserted against a plain object without dragging in the bus/mail mock
 * stack (same reasoning as ic_confirm_dialog.test.js).
 */
import { describe, expect, test } from "@odoo/hoot";

import { InstanceDetail } from "@incubacloud/components/instance_detail/instance_detail";

/** Read the getter against a hand-made state, without mounting. */
function confirmedWith(typed, name = "prod-eu") {
    const stub = { state: { removeModal: { instance: { name }, typed } } };
    return Object.getOwnPropertyDescriptor(
        InstanceDetail.prototype, "removeConfirmed",
    ).get.call(stub);
}

describe("remove modal — written confirmation", () => {
    test("the exact name unlocks the button", () => {
        expect(confirmedWith("prod-eu")).toBe(true);
    });

    test("an empty box keeps it locked", () => {
        expect(confirmedWith("")).toBe(false);
    });

    test("a prefix of the name is not enough", () => {
        expect(confirmedWith("prod")).toBe(false);
    });

    test("a different case does not pass", () => {
        // Case folding here would let "PROD-EU" through for an
        // instance called "prod-eu" — and, worse, would blur two
        // instances whose names differ only in case.
        expect(confirmedWith("PROD-EU")).toBe(false);
    });

    test("surrounding whitespace does not pass", () => {
        // Trimming looks helpful and quietly widens what counts as a
        // match; a stray paste should be re-read, not accepted.
        expect(confirmedWith(" prod-eu ")).toBe(false);
    });

    test("another instance's name does not pass", () => {
        // The mistake this whole barrier is built for.
        expect(confirmedWith("prod-us")).toBe(false);
    });

    test("no open modal means nothing is confirmed", () => {
        const stub = { state: { removeModal: null } };
        const confirmed = Object.getOwnPropertyDescriptor(
            InstanceDetail.prototype, "removeConfirmed",
        ).get.call(stub);
        expect(confirmed).toBe(false);
    });
});
