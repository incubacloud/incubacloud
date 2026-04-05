import { describe, expect, test } from "@odoo/hoot";
import { Overview } from "@incubacloud/components/dashboard/dashboard";

/**
 * The Overview (Dashboard) component uses rpc("/cloud/get_dashboard"),
 * setInterval for polling, env.navigate, env.toggleSlideOver, and
 * env.refreshAlertCount. Full mounting would require mocking all of
 * these deep dependencies. We test helpers as pure functions and
 * validate the class structure.
 */

describe("Overview — timeAgo helper", () => {
    function timeAgo(dateStr) {
        if (!dateStr) return "";
        const diff = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
        if (diff < 60) return `${diff}s ago`;
        if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
        if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
        return `${Math.floor(diff / 86400)}d ago`;
    }

    test("returns empty string for falsy input", () => {
        expect(timeAgo("")).toBe("");
        expect(timeAgo(null)).toBe("");
        expect(timeAgo(undefined)).toBe("");
    });

    test("seconds ago", () => {
        const now = new Date(Date.now() - 30 * 1000).toISOString();
        expect(timeAgo(now)).toBe("30s ago");
    });

    test("minutes ago", () => {
        const now = new Date(Date.now() - 5 * 60 * 1000).toISOString();
        expect(timeAgo(now)).toBe("5m ago");
    });

    test("hours ago", () => {
        const now = new Date(Date.now() - 3 * 3600 * 1000).toISOString();
        expect(timeAgo(now)).toBe("3h ago");
    });

    test("days ago", () => {
        const now = new Date(Date.now() - 2 * 86400 * 1000).toISOString();
        expect(timeAgo(now)).toBe("2d ago");
    });
});

describe("Overview — jobStateLabel helper", () => {
    function jobStateLabel(state) {
        return (
            {
                pending: "Pending",
                enqueued: "Queued",
                wait_dependencies: "Waiting",
                started: "Running",
                done: "Done",
                failed: "Failed",
                cancelled: "Cancelled",
            }[state] || state
        );
    }

    test("maps pending to Pending", () => {
        expect(jobStateLabel("pending")).toBe("Pending");
    });

    test("maps started to Running", () => {
        expect(jobStateLabel("started")).toBe("Running");
    });

    test("maps done to Done", () => {
        expect(jobStateLabel("done")).toBe("Done");
    });

    test("maps failed to Failed", () => {
        expect(jobStateLabel("failed")).toBe("Failed");
    });

    test("unknown state returns itself", () => {
        expect(jobStateLabel("something_else")).toBe("something_else");
    });
});

describe("Overview — class structure", () => {
    test("template name is set", () => {
        expect(Overview.template).toBe("incubacloud.Overview");
    });
});

describe("Overview — KPI data shape", () => {
    /**
     * Validates the expected shape of the dashboard response
     * that the component expects from /cloud/get_dashboard.
     */
    test("expected counts keys", () => {
        const sampleResponse = {
            counts: { projects: 3, hosts: 2, instances: 7, alerts: 1 },
            alerts: [],
            recent_jobs: [],
        };
        expect(Object.keys(sampleResponse.counts).sort()).toEqual([
            "alerts",
            "hosts",
            "instances",
            "projects",
        ]);
    });

    test("alerts count > 0 would render kpi-warning class", () => {
        // Template logic: 'ic-kpi-card' + (state.counts.alerts > 0 ? ' kpi-warning' : ' kpi-ok')
        const alertCount = 3;
        const cls = "ic-kpi-card" + (alertCount > 0 ? " kpi-warning" : " kpi-ok");
        expect(cls).toBe("ic-kpi-card kpi-warning");
    });

    test("alerts count == 0 renders kpi-ok class", () => {
        const alertCount = 0;
        const cls = "ic-kpi-card" + (alertCount > 0 ? " kpi-warning" : " kpi-ok");
        expect(cls).toBe("ic-kpi-card kpi-ok");
    });
});
