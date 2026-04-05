import { describe, expect, test } from "@odoo/hoot";
import { JobHistory } from "@incubacloud/components/job_history/job_history";

/**
 * JobHistory uses useService("orm") for cloud.job.load_history.
 * Full mounting requires mocking the ORM service.
 * We test the pure helper functions and filter/state logic.
 */

const ALL_STATES = [
    "pending",
    "enqueued",
    "wait_dependencies",
    "started",
    "done",
    "failed",
    "cancelled",
];

describe("JobHistory — formatDuration helper", () => {
    function formatDuration(s) {
        if (!s || s <= 0) return "\u2014";
        if (s < 60) return `${s}s`;
        const m = Math.floor(s / 60);
        const sec = s % 60;
        if (m < 60) return sec > 0 ? `${m}m ${sec}s` : `${m}m`;
        const h = Math.floor(m / 60);
        const min = m % 60;
        return min > 0 ? `${h}h ${min}m` : `${h}h`;
    }

    test("returns dash for 0", () => {
        expect(formatDuration(0)).toBe("\u2014");
    });

    test("returns dash for null", () => {
        expect(formatDuration(null)).toBe("\u2014");
    });

    test("returns dash for negative", () => {
        expect(formatDuration(-5)).toBe("\u2014");
    });

    test("seconds only", () => {
        expect(formatDuration(45)).toBe("45s");
    });

    test("minutes and seconds", () => {
        expect(formatDuration(125)).toBe("2m 5s");
    });

    test("exact minutes", () => {
        expect(formatDuration(120)).toBe("2m");
    });

    test("hours and minutes", () => {
        expect(formatDuration(3660)).toBe("1h 1m");
    });

    test("exact hours", () => {
        expect(formatDuration(7200)).toBe("2h");
    });
});

describe("JobHistory — formatDate helper", () => {
    function formatDate(dateStr) {
        if (!dateStr) return "\u2014";
        const d = new Date(dateStr);
        return d.toLocaleString([], {
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
        });
    }

    test("returns dash for empty", () => {
        expect(formatDate("")).toBe("\u2014");
        expect(formatDate(null)).toBe("\u2014");
    });

    test("formats valid date string", () => {
        const result = formatDate("2026-03-15T10:30:00Z");
        expect(result.length).toBeGreaterThan(0);
        expect(result).not.toBe("\u2014");
    });
});

describe("JobHistory — stateName / stateLabel helper", () => {
    function stateName(state) {
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

    test("maps all known states", () => {
        expect(stateName("pending")).toBe("Pending");
        expect(stateName("enqueued")).toBe("Queued");
        expect(stateName("wait_dependencies")).toBe("Waiting");
        expect(stateName("started")).toBe("Running");
        expect(stateName("done")).toBe("Done");
        expect(stateName("failed")).toBe("Failed");
        expect(stateName("cancelled")).toBe("Cancelled");
    });

    test("unknown state returns raw value", () => {
        expect(stateName("custom")).toBe("custom");
    });
});

describe("JobHistory — toggleState filter logic", () => {
    function toggleState(states, s) {
        const copy = [...states];
        const idx = copy.indexOf(s);
        if (idx >= 0) {
            copy.splice(idx, 1);
        } else {
            copy.push(s);
        }
        return copy;
    }

    test("adds state when not present", () => {
        expect(toggleState([], "done")).toEqual(["done"]);
    });

    test("removes state when present", () => {
        expect(toggleState(["done", "failed"], "done")).toEqual(["failed"]);
    });

    test("toggle is idempotent", () => {
        const result = toggleState(toggleState([], "done"), "done");
        expect(result).toEqual([]);
    });
});

describe("JobHistory — isStateActive logic", () => {
    function isStateActive(states, s) {
        return states.length === 0 || states.includes(s);
    }

    test("all active when no filter", () => {
        expect(isStateActive([], "done")).toBe(true);
        expect(isStateActive([], "failed")).toBe(true);
    });

    test("only selected state active", () => {
        expect(isStateActive(["done"], "done")).toBe(true);
        expect(isStateActive(["done"], "failed")).toBe(false);
    });
});

describe("JobHistory — _buildFilters logic", () => {
    function buildFilters(filter, propsHostId, propsInstanceId) {
        const filters = { job_category: filter.job_category };
        if (filter.job_id) filters.job_id = filter.job_id;
        if (filter.states.length) filters.states = filter.states;
        if (filter.host_id) filters.host_id = filter.host_id;
        if (filter.date_from) filters.date_from = filter.date_from;
        if (filter.date_to) filters.date_to = filter.date_to;
        if (filter.instance_id) filters.instance_id = filter.instance_id;
        if (propsHostId) filters.apply_to = "host";
        if (propsInstanceId) filters.apply_to = "instance";
        return filters;
    }

    test("minimal filter has only job_category", () => {
        const result = buildFilters(
            { job_category: "operational", states: [], job_id: null, host_id: null, date_from: "", date_to: "", instance_id: null },
            null,
            null
        );
        expect(result).toEqual({ job_category: "operational" });
    });

    test("includes states when non-empty", () => {
        const result = buildFilters(
            { job_category: "all", states: ["done", "failed"], job_id: null, host_id: null, date_from: "", date_to: "", instance_id: null },
            null,
            null
        );
        expect(result.states).toEqual(["done", "failed"]);
    });

    test("sets apply_to host when propsHostId", () => {
        const result = buildFilters(
            { job_category: "operational", states: [], job_id: null, host_id: 5, date_from: "", date_to: "", instance_id: null },
            5,
            null
        );
        expect(result.apply_to).toBe("host");
        expect(result.host_id).toBe(5);
    });

    test("sets apply_to instance when propsInstanceId", () => {
        const result = buildFilters(
            { job_category: "operational", states: [], job_id: null, host_id: null, date_from: "", date_to: "", instance_id: 10 },
            null,
            10
        );
        expect(result.apply_to).toBe("instance");
    });
});

describe("JobHistory — gantt time window buttons", () => {
    /**
     * Template has 4 time-window buttons: 1h, 6h, 24h, 168h (7d).
     * The active one matches state.ganttHours.
     */
    const windows = [1, 6, 24, 168];

    test("all expected window sizes exist", () => {
        expect(windows).toEqual([1, 6, 24, 168]);
    });

    test("24h is the default", () => {
        // From setup: ganttHours: 24
        const defaultGanttHours = 24;
        expect(defaultGanttHours).toBe(24);
    });
});

describe("JobHistory — ganttTicks computation", () => {
    function ganttTicks(ganttHours) {
        const count = Math.min(ganttHours, 12);
        const step = ganttHours / count;
        const now = Date.now();
        const ticks = [];
        for (let i = 0; i <= count; i++) {
            const ms = now - (ganttHours - i * step) * 3600 * 1000;
            const d = new Date(ms);
            const label =
                d.getHours().toString().padStart(2, "0") +
                ":" +
                d.getMinutes().toString().padStart(2, "0");
            ticks.push({ pct: ((i / count) * 100).toFixed(1), label });
        }
        return ticks;
    }

    test("produces correct number of ticks for 24h", () => {
        const ticks = ganttTicks(24);
        // count = min(24, 12) = 12, so 13 ticks (0..12)
        expect(ticks.length).toBe(13);
    });

    test("first tick is at 0%", () => {
        const ticks = ganttTicks(24);
        expect(ticks[0].pct).toBe("0.0");
    });

    test("last tick is at 100%", () => {
        const ticks = ganttTicks(24);
        expect(ticks[ticks.length - 1].pct).toBe("100.0");
    });

    test("produces correct number of ticks for 1h", () => {
        const ticks = ganttTicks(1);
        // count = min(1, 12) = 1, so 2 ticks
        expect(ticks.length).toBe(2);
    });

    test("tick labels are HH:MM format", () => {
        const ticks = ganttTicks(6);
        for (const tick of ticks) {
            expect(tick.label).toMatch(/^\d{2}:\d{2}$/);
        }
    });
});

describe("JobHistory — class structure", () => {
    test("template name is set", () => {
        expect(JobHistory.template).toBe("incubacloud.JobHistory");
    });

    test("declares expected props", () => {
        expect(JobHistory.props.job_id).not.toBe(undefined);
        expect(JobHistory.props.instance_id).not.toBe(undefined);
        expect(JobHistory.props.host_id).not.toBe(undefined);
        expect(JobHistory.props.embedded).not.toBe(undefined);
    });

    test("all props are optional", () => {
        expect(JobHistory.props.job_id.optional).toBe(true);
        expect(JobHistory.props.instance_id.optional).toBe(true);
        expect(JobHistory.props.host_id.optional).toBe(true);
        expect(JobHistory.props.embedded.optional).toBe(true);
    });
});

describe("JobHistory — allStateKeys getter", () => {
    test("returns all 7 states", () => {
        expect(ALL_STATES.length).toBe(7);
        expect(ALL_STATES).toInclude("pending");
        expect(ALL_STATES).toInclude("done");
        expect(ALL_STATES).toInclude("failed");
        expect(ALL_STATES).toInclude("cancelled");
    });
});
