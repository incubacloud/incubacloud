import { describe, expect, test } from "@odoo/hoot";
import { mergeById, mergeMapById } from "@incubacloud/utils/merge_by_id";

/**
 * merge_by_id — refresh server-owned collections without discarding the
 * objects already rendered.
 *
 * The contract under test:
 *   - rows present in both keep their identity and get changed keys only
 *   - membership and order follow the server
 *   - keys the server dropped are removed, not left behind as ghosts
 */

describe("mergeById — arrays", () => {
    test("keeps object identity for rows present in both", () => {
        const current = [{ id: 1, name: "a" }, { id: 2, name: "b" }];
        const first = current[0];
        const out = mergeById(current, [{ id: 1, name: "a" }, { id: 2, name: "b" }]);
        expect(out[0]).toBe(first);
    });

    test("writes through the changed fields", () => {
        const current = [{ id: 1, disk: 30 }];
        const out = mergeById(current, [{ id: 1, disk: 84 }]);
        expect(out[0].disk).toBe(84);
        expect(out[0]).toBe(current[0]);
    });

    test("appends rows the server added", () => {
        const out = mergeById([{ id: 1 }], [{ id: 1 }, { id: 2 }]);
        expect(out.length).toBe(2);
        expect(out[1].id).toBe(2);
    });

    test("drops rows the server no longer returns", () => {
        const out = mergeById([{ id: 1 }, { id: 2 }], [{ id: 2 }]);
        expect(out.length).toBe(1);
        expect(out[0].id).toBe(2);
    });

    test("follows server order", () => {
        const out = mergeById([{ id: 1 }, { id: 2 }], [{ id: 2 }, { id: 1 }]);
        expect(out.map((r) => r.id)).toEqual([2, 1]);
    });

    test("removes keys the incoming row no longer carries", () => {
        const out = mergeById([{ id: 1, stale: "x" }], [{ id: 1 }]);
        expect("stale" in out[0]).toBe(false);
    });

    test("tolerates empty and missing collections", () => {
        expect(mergeById(null, [{ id: 1 }])[0].id).toBe(1);
        expect(mergeById([{ id: 1 }], null)).toEqual([]);
    });
});

describe("mergeMapById — id-keyed maps", () => {
    test("mutates in place so holders keep their reference", () => {
        const target = { 10: { id: 10, state: "deploying" } };
        const out = mergeMapById(target, { 10: { id: 10, state: "deployed" } });
        expect(out).toBe(target);
        expect(target[10].state).toBe("deployed");
    });

    test("preserves identity of surviving entries", () => {
        const target = { 10: { id: 10, n: 1 } };
        const before = target[10];
        mergeMapById(target, { 10: { id: 10, n: 2 } });
        expect(target[10]).toBe(before);
    });

    test("adds new entries", () => {
        const target = { 10: { id: 10 } };
        mergeMapById(target, { 10: { id: 10 }, 11: { id: 11 } });
        expect(target[11].id).toBe(11);
    });

    test("deletes entries the server dropped", () => {
        const target = { 10: { id: 10 }, 11: { id: 11 } };
        mergeMapById(target, { 11: { id: 11 } });
        expect(target[10]).toBe(undefined);
    });
});
