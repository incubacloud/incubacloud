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

const SAMPLE = {
    project: { id: 7, name: "Acme", odoo_version: "19.0" },
    instances: {
        10: { id: 10, name: "prod", environment: "production" },
        11: { id: 11, name: "stg",  environment: "staging" },
    },
    hosts: [],
    backup_backends: [],
};

describe("project_store — load and reactive state", () => {
    test("load() populates state.data and clears loading", async () => {
        const { rpcFn } = makeRpc([SAMPLE]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn, debounceMs: 5,
        });
        await store.load(7);
        expect(store.state.projectId).toBe(7);
        expect(store.state.data.project.name).toBe("Acme");
        expect(store.state.loading).toBe(false);
    });

    test("concurrent load() for same project shares one RPC", async () => {
        const counter = makeRpc([SAMPLE, SAMPLE]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn: counter.rpcFn, debounceMs: 5,
        });
        const p1 = store.load(7);
        const p2 = store.load(7);
        await Promise.all([p1, p2]);
        expect(counter.callCount()).toBe(1);
    });

    test("invalidate() clears state and stops bus refresh", async () => {
        const counter = makeRpc([SAMPLE, SAMPLE, SAMPLE]);
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
        const counter = makeRpc([SAMPLE, SAMPLE]);
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
        const responses = [SAMPLE, SAMPLE, SAMPLE];
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
        const { rpcFn } = makeRpc([SAMPLE]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn, debounceMs: 5,
        });
        await store.load(7);
        const next = store.getNextInstance(null);
        expect(next.id).toBe(10);
    });

    test("getNextInstance excludes the given id", async () => {
        const { rpcFn } = makeRpc([SAMPLE]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn, debounceMs: 5,
        });
        await store.load(7);
        const next = store.getNextInstance(10);
        expect(next.id).toBe(11);
    });

    test("getNextInstance returns null when nothing matches", async () => {
        const onlyExcluded = {
            ...SAMPLE,
            instances: { 10: SAMPLE.instances[10] },
        };
        const { rpcFn } = makeRpc([onlyExcluded]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn, debounceMs: 5,
        });
        await store.load(7);
        expect(store.getNextInstance(10)).toBe(null);
    });

    test("updateInstance patches in place", async () => {
        const { rpcFn } = makeRpc([SAMPLE]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn, debounceMs: 5,
        });
        await store.load(7);
        store.updateInstance(10, { id: 10, name: "renamed" });
        expect(store.state.data.instances[10].name).toBe("renamed");
    });

    test("removeInstance drops the row", async () => {
        const { rpcFn } = makeRpc([SAMPLE]);
        const store = createProjectStore({
            busService: makeBus(), rpcFn, debounceMs: 5,
        });
        await store.load(7);
        store.removeInstance(10);
        expect(store.state.data.instances[10]).toBe(undefined);
    });
});
