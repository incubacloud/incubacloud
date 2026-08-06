import { describe, expect, test } from "@odoo/hoot";
import { advanceTime } from "@odoo/hoot-mock";
import { createProjectStore } from "@incubacloud/store/project_store";

/**
 * project_store — single source of truth for project data on the SPA.
 *
 * The store owns the cloud_jobs bus subscription, the project cache,
 * and coalesces concurrent refresh triggers into one RPC. Tests cover:
 *   - load() updates state and serves concurrent callers off one Promise
 *   - bus events drive a debounced refresh that collapses bursts
 *   - in-flight events queue a single trailing fire
 *   - getNextInstance respects production > staging priority
 *   - updateInstance / removeInstance / invalidate keep state coherent
 *
 * No real RPC or bus is involved: the factory accepts ``rpcFn`` and
 * ``busService`` as dependency injection, so the tests run pure.
 */

function makeBus() {
    let handler = null;
    return {
        subscribe(channel, fn) {
            if (channel === "cloud_jobs") handler = fn;
        },
        start() {},
        emit(payload) { handler?.(payload); },
    };
}

function makeRpc(responses) {
    let calls = 0;
    return {
        callCount: () => calls,
        rpcFn: async (_route, _params) => {
            const r = responses[Math.min(calls, responses.length - 1)];
            calls += 1;
            // Yield the microtask so awaiters see the increment.
            await Promise.resolve();
            return r;
        },
    };
}

/**
 * Build a fresh ``/cloud/get_project_full`` payload.
 *
 * A factory rather than a shared constant: the store hands its
 * ``instances`` map straight to callers, so ``updateInstance`` /
 * ``removeInstance`` and the in-place merge all write through to
 * whatever object they were given. A module-level literal would be
 * mutated by one test and read as damaged by every test after it.
 *
 * @param {object} [overrides] top-level keys to replace wholesale.
 * @returns {object} a payload no other test holds a reference to.
 */
function sample(overrides = {}) {
    return {
        project: { id: 7, name: "Acme", odoo_version: "19.0" },
        instances: {
            10: { id: 10, name: "prod", environment: "production" },
            11: { id: 11, name: "stg", environment: "staging" },
        },
        hosts: [],
        backup_backends: [],
        ...overrides,
    };
}

describe("project_store — load and reactive state", () => {
    test("load() populates state.data and clears loading", async () => {
        const { rpcFn } = makeRpc([sample()]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn, debounceMs: 5,
        });
        await store.load(7);
        expect(store.state.projectId).toBe(7);
        expect(store.state.data.project.name).toBe("Acme");
        expect(store.state.loading).toBe(false);
    });

    test("concurrent load() for same project shares one RPC", async () => {
        const counter = makeRpc([sample(), sample()]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn: counter.rpcFn, debounceMs: 5,
        });
        const p1 = store.load(7);
        const p2 = store.load(7);
        await Promise.all([p1, p2]);
        expect(counter.callCount()).toBe(1);
    });

    test("invalidate() clears state and stops bus refresh", async () => {
        const counter = makeRpc([sample(), sample(), sample()]);
        const bus = makeBus();
        const store = createProjectStore({
            busService: bus, rpcFn: counter.rpcFn, debounceMs: 5,
        });
        await store.load(7);
        store.invalidate();
        expect(store.state.projectId).toBe(null);
        expect(store.state.data).toBe(null);
        bus.emit({ id: 99 });
        await advanceTime(20);
        // No projectId → debounced refresh is a no-op.
        expect(counter.callCount()).toBe(1);
    });
});

describe("project_store — bus debouncing", () => {
    test("burst of bus events collapses to one RPC after the window", async () => {
        const counter = makeRpc([sample(), sample()]);
        const bus = makeBus();
        const store = createProjectStore({
            busService: bus, rpcFn: counter.rpcFn, debounceMs: 10,
        });
        await store.load(7);                 // call 1
        for (let i = 0; i < 20; i++) bus.emit({ id: i });
        await advanceTime(15);
        expect(counter.callCount()).toBe(2); // call 2 after debounce
    });

    test("event during inflight load queues exactly one trailing refresh", async () => {
        let firstResolve;
        const responses = [sample(), sample(), sample()];
        let calls = 0;
        const rpcFn = async () => {
            const r = responses[Math.min(calls, responses.length - 1)];
            calls += 1;
            // First call hangs until we release it; the test fires
            // bus events while it is in flight.
            if (calls === 1) {
                await new Promise((res) => { firstResolve = res; });
            }
            return r;
        };
        const bus = makeBus();
        const store = createProjectStore({
            busService: bus, rpcFn, debounceMs: 5,
        });
        const inflight = store.load(7);
        // While load #1 is hanging, fire several bus events. Only one
        // trailing refresh should be scheduled (not one per event).
        for (let i = 0; i < 10; i++) bus.emit({ id: i });
        firstResolve();
        await inflight;
        await advanceTime(15);
        expect(calls).toBe(2);
    });
});

describe("project_store — instance helpers", () => {
    test("getNextInstance prefers production over staging", async () => {
        const { rpcFn } = makeRpc([sample()]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn, debounceMs: 5,
        });
        await store.load(7);
        const next = store.getNextInstance(null);
        expect(next.id).toBe(10);
    });

    test("getNextInstance excludes the given id", async () => {
        const { rpcFn } = makeRpc([sample()]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn, debounceMs: 5,
        });
        await store.load(7);
        const next = store.getNextInstance(10);
        expect(next.id).toBe(11);
    });

    test("getNextInstance returns null when nothing matches", async () => {
        const { rpcFn } = makeRpc([
            sample({ instances: { 10: { id: 10, name: "prod", environment: "production" } } }),
        ]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn, debounceMs: 5,
        });
        await store.load(7);
        expect(store.getNextInstance(10)).toBe(null);
    });

    test("updateInstance patches in place", async () => {
        const { rpcFn } = makeRpc([sample()]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn, debounceMs: 5,
        });
        await store.load(7);
        store.updateInstance(10, { id: 10, name: "renamed" });
        expect(store.state.data.instances[10].name).toBe("renamed");
    });

    test("removeInstance drops the row", async () => {
        const { rpcFn } = makeRpc([sample()]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn, debounceMs: 5,
        });
        await store.load(7);
        store.removeInstance(10);
        expect(store.state.data.instances[10]).toBe(undefined);
    });
});

describe("project_store — bus scoping", () => {
    test("a job from another project does not trigger a refresh", async () => {
        const counter = makeRpc([sample(), sample()]);
        const bus = makeBus();
        const store = createProjectStore({
            busService: bus, rpcFn: counter.rpcFn, debounceMs: 10,
        });
        await store.load(7);                              // call 1
        bus.emit({ id: 1, project_id: 99, instance_id: 42 });
        await advanceTime(20);
        expect(counter.callCount()).toBe(1);              // no call 2
    });

    test("a job from this project triggers a refresh", async () => {
        const counter = makeRpc([sample(), sample()]);
        const bus = makeBus();
        const store = createProjectStore({
            busService: bus, rpcFn: counter.rpcFn, debounceMs: 10,
        });
        await store.load(7);
        bus.emit({ id: 1, project_id: 7, instance_id: 10 });
        await advanceTime(20);
        expect(counter.callCount()).toBe(2);
    });

    test("a host-level job (no project) still refreshes", async () => {
        // get_project_full carries host rows, so a job with no project
        // can still change what this view renders. Unknown means
        // refresh, never "not mine".
        const counter = makeRpc([sample(), sample()]);
        const bus = makeBus();
        const store = createProjectStore({
            busService: bus, rpcFn: counter.rpcFn, debounceMs: 10,
        });
        await store.load(7);
        bus.emit({ id: 1, project_id: null, host_id: 3, instance_id: null });
        await advanceTime(20);
        expect(counter.callCount()).toBe(2);
    });
});

describe("project_store — instance identity across refreshes", () => {
    test("refreshing the same project preserves instance object identity", async () => {
        const counter = makeRpc([
            sample(),
            sample(),
        ]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn: counter.rpcFn, debounceMs: 5,
        });
        await store.load(7);
        const before = store.state.data.instances[10];
        await store.load(7);
        expect(store.state.data.instances[10]).toBe(before);
    });

    test("an instance dropped server-side disappears from the map", async () => {
        const counter = makeRpc([
            sample(),
            sample({ instances: { 11: { id: 11, name: "stg", environment: "staging" } } }),
        ]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn: counter.rpcFn, debounceMs: 5,
        });
        await store.load(7);
        await store.load(7);
        expect(store.state.data.instances[10]).toBe(undefined);
        expect(store.state.data.instances[11].name).toBe("stg");
    });
});
