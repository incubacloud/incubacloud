import { describe, expect, test } from "@odoo/hoot";
import { refreshesOn } from "@incubacloud/utils/use_debounced_bus";

/**
 * refreshesOn — which ``cloud_jobs`` events a page refetches on.
 *
 * A running job publishes ``progress`` every half second it wrote
 * output. Only a progress event carrying a system message changes
 * what a list renders; the rest is the storm every page used to
 * answer with a full refetch.
 */
describe("refreshesOn", () => {
    test("a created job refreshes", () => {
        expect(refreshesOn({ id: 1, kind: "created" })).toBe(true);
    });

    test("a job that ended refreshes", () => {
        expect(refreshesOn({ id: 1, kind: "state", state: "done" })).toBe(true);
    });

    test("output alone does not refresh", () => {
        expect(refreshesOn({ id: 1, kind: "progress", system: false })).toBe(false);
    });

    test("a system message does", () => {
        expect(refreshesOn({ id: 1, kind: "progress", system: true })).toBe(true);
    });

    test("progress without the flag is taken as output", () => {
        expect(refreshesOn({ id: 1, kind: "progress" })).toBe(false);
    });

    test("an event without a kind refreshes, as before", () => {
        expect(refreshesOn({ id: 1, host_id: 3 })).toBe(true);
        expect(refreshesOn(undefined)).toBe(true);
    });
});
