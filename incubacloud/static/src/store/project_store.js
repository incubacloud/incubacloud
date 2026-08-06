/**
 * project_store — single source of truth for project-level data on the
 * SPA side.
 *
 * Why this exists:
 *   Before, every component that needed project state (sidebar, instance
 *   detail, project detail) wired its own ``cloud_jobs`` bus subscriber,
 *   ran its own ``/cloud/get_project_full`` RPC, and asked siblings to
 *   refresh via env-level callbacks (``refreshSidebar``, ``getProjectCache``,
 *   ``updateCacheInstance``…). A single bus event therefore produced two
 *   or three concurrent RPCs to the same endpoint, plus the cross-component
 *   manual refresh path bypassed each component's debounce. Adding a new
 *   panel multiplied the problem.
 *
 * What it does:
 *   - Owns the only ``cloud_jobs`` bus subscription that drives project
 *     data (other components still subscribe for instance-scoped or
 *     host-scoped concerns; those don't fan out here).
 *   - Holds one reactive ``state`` that any number of components consume
 *     via ``useState(store.state)``. Re-renders are automatic.
 *   - Coalesces a burst of bus events into a single RPC: 300 ms debounce,
 *     last-wins, in-flight guard, trailing fire if events arrive during
 *     the inflight load.
 *   - Exposes small write helpers (``updateInstance`` / ``removeInstance``)
 *     so optimistic UI patches stay in one place — no component reaches
 *     into another's cache.
 *   - Exposes ``getNextInstance(excludeId)`` for navigation logic that
 *     used to require a registered sidebar callback.
 *
 * How callers use it:
 *   const store = this.env.projectStore;
 *   this.state  = useState(store.state);    // reactive subscription
 *   onWillStart(() => store.load(this.props.projectId));
 *
 * Trade-offs:
 *   - The 300 ms window is invisible to humans and collapses chunk-flush
 *     storms (~50/s) into ~3/s. Set lower if a specific UI needs faster
 *     updates than that.
 *   - Last-wins on the projectId argument: switching projects mid-flight
 *     (rare; would need a user double-click) discards the older request.
 *     Acceptable — the user only cares about the project they're on now.
 */
import { reactive } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { mergeMapById } from "../utils/merge_by_id";

const REFRESH_DEBOUNCE_MS = 300;

/**
 * @param {object}   deps
 * @param {object}   deps.busService — Odoo bus service.
 * @param {Function} [deps.rpcFn]    — RPC function. Defaults to the
 *     framework's ``rpc``; tests inject a stub. Production code does
 *     NOT pass this argument.
 * @param {number}   [deps.debounceMs] — Override the 300 ms refresh
 *     debounce window. Tests use a tiny value to keep timers tight.
 */
export function createProjectStore({
    busService,
    rpcFn = rpc,
    debounceMs = REFRESH_DEBOUNCE_MS,
}) {
    const state = reactive({
        projectId: null,
        data: null,            // { project, instances: {}, hosts, backup_backends }
        loading: false,
        error: null,
    });

    let _refreshTimer = null;
    let _inflight = null;
    let _trailing = false;

    async function _runLoad(projectId) {
        state.loading = true;
        state.projectId = projectId;
        try {
            const fresh = await rpcFn("/cloud/get_project_full", {
                project_id: projectId,
            });
            // Merge the instance map in place rather than swapping the
            // whole payload: the sidebar and the detail view render one
            // row per instance, and replacing every object made all of
            // them re-render on any refresh. Only applies when we are
            // refreshing the same project — switching projects is a
            // clean slate.
            if (state.data?.instances && fresh?.instances
                && state.projectId === projectId) {
                mergeMapById(state.data.instances, fresh.instances);
                fresh.instances = state.data.instances;
            }
            state.data = fresh;
            state.error = null;
        } catch (e) {
            state.error = e;
            console.warn("[project_store] load failed:", e);
        } finally {
            state.loading = false;
        }
    }

    /**
     * Explicit (non-debounced) load. Use from ``onWillStart``,
     * ``onWillUpdateProps``, and right after any write that must be
     * visible before the next user action (e.g. before navigating
     * after a delete).
     *
     * Returns a Promise that resolves once ``state.data`` is fresh.
     * Concurrent calls for the same projectId share the inflight Promise.
     */
    function load(projectId) {
        if (_inflight && state.projectId === projectId) {
            return _inflight;
        }
        _inflight = _runLoad(projectId).finally(() => {
            _inflight = null;
            if (_trailing) {
                _trailing = false;
                scheduleRefresh();
            }
        });
        return _inflight;
    }

    /**
     * Bus-driven debounced refresh. Coalesces a storm of cloud_jobs
     * events into one RPC. Trailing-fires once if more events arrived
     * while a load was in flight, so the UI never settles on stale.
     */
    function scheduleRefresh() {
        if (!state.projectId) return;
        if (_inflight) {
            _trailing = true;
            return;
        }
        if (_refreshTimer) return;
        _refreshTimer = setTimeout(() => {
            _refreshTimer = null;
            if (state.projectId) load(state.projectId);
        }, debounceMs);
    }

    /**
     * Drop all cached data and stop reacting to bus events. Call when
     * navigating away from the project context so the store doesn't
     * keep refreshing in the background.
     */
    function invalidate() {
        if (_refreshTimer) {
            clearTimeout(_refreshTimer);
            _refreshTimer = null;
        }
        _trailing = false;
        state.projectId = null;
        state.data = null;
        state.error = null;
    }

    /** Patch one instance in-place (optimistic UI after a write). */
    function updateInstance(instanceId, instData) {
        if (state.data?.instances) {
            state.data.instances[instanceId] = instData;
        }
    }

    /** Drop one instance after the server confirmed deletion. */
    function removeInstance(instanceId) {
        if (state.data?.instances) {
            delete state.data.instances[instanceId];
        }
    }

    /**
     * Pick the most ‘important’ instance other than ``excludeId``.
     * Production wins over staging; ties broken by insertion order.
     * Used for post-delete and onboarding navigation.
     */
    function getNextInstance(excludeId = null) {
        const groups = { production: [], staging: [] };
        for (const inst of Object.values(state.data?.instances || {})) {
            const env = inst.environment || "staging";
            if (groups[env]) groups[env].push(inst);
        }
        for (const env of ["production", "staging"]) {
            for (const inst of groups[env]) {
                if (inst.id !== excludeId) return inst;
            }
        }
        return null;
    }

        // Only jobs belonging to the project currently loaded. This store
    // lives for the whole SPA session and never unsubscribes, so
    // without the filter every job anywhere in the fleet cost one
    // ``/cloud/get_project_full`` — the single largest source of
    // pointless refreshes in the app.
    //
    // A job with no ``project_id`` is a host-level job: it has no
    // project to compare against, and it can still change the host
    // rows this endpoint returns, so it always passes. Filtering on
    // ``instance_id`` against the cached instances would be wrong
    // here — an instance created by another user is not in the cache
    // yet, and we would never learn about it.
    busService.subscribe("cloud_jobs", (payload) => {
        const projectId = payload?.project_id;
        if (projectId != null && projectId !== state.projectId) return;
        scheduleRefresh();
    });
    busService.start();

    return {
        state,
        load,
        invalidate,
        updateInstance,
        removeInstance,
        getNextInstance,
        refresh: scheduleRefresh,
    };
}
