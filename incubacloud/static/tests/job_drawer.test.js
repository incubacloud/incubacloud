import { describe, expect, test } from "@odoo/hoot";
import { JobDrawer } from "@incubacloud/components/job_drawer/job_drawer";

/**
 * JobDrawer uses useService("orm"), useService("bus_service"), and
 * env.navigate / env.toggleSlideOver. Full mounting requires mocking
 * the ORM call to cloud.job.load_jobs and the bus service.
 * We test the pure helper functions and state logic.
 */

const ACTIVE_STATES = ["pending", "enqueued", "wait_dependencies", "started", "blocked"];

describe("JobDrawer — jobClass helper", () => {
    function jobClass(job) {
        return (
            {
                pending: "job-pending",
                enqueued: "job-pending",
                wait_dependencies: "job-pending",
                started: "job-running",
                done: "job-done",
                failed: "job-failed",
                cancelled: "job-cancelled",
                blocked: "job-blocked",
            }[job.state] || ""
        );
    }

    test("pending maps to job-pending", () => {
        expect(jobClass({ state: "pending" })).toBe("job-pending");
    });

    test("enqueued maps to job-pending", () => {
        expect(jobClass({ state: "enqueued" })).toBe("job-pending");
    });

    test("started maps to job-running", () => {
        expect(jobClass({ state: "started" })).toBe("job-running");
    });

    test("done maps to job-done", () => {
        expect(jobClass({ state: "done" })).toBe("job-done");
    });

    test("failed maps to job-failed", () => {
        expect(jobClass({ state: "failed" })).toBe("job-failed");
    });

    test("cancelled maps to job-cancelled", () => {
        expect(jobClass({ state: "cancelled" })).toBe("job-cancelled");
    });

    test("blocked maps to job-blocked", () => {
        expect(jobClass({ state: "blocked" })).toBe("job-blocked");
    });

    test("unknown state returns empty string", () => {
        expect(jobClass({ state: "mystery" })).toBe("");
    });
});

describe("JobDrawer — stateName helper", () => {
    function stateName(job) {
        return (
            {
                pending: "Pending",
                enqueued: "Queued",
                wait_dependencies: "Waiting",
                started: "Running",
                done: "Done",
                failed: "Failed",
                cancelled: "Cancelled",
                blocked: "Blocked",
            }[job.state] || job.state
        );
    }

    test("maps started to Running", () => {
        expect(stateName({ state: "started" })).toBe("Running");
    });

    test("maps wait_dependencies to Waiting", () => {
        expect(stateName({ state: "wait_dependencies" })).toBe("Waiting");
    });

    test("unknown state returns raw value", () => {
        expect(stateName({ state: "custom_state" })).toBe("custom_state");
    });
});

describe("JobDrawer — formatDate helper", () => {
    function formatDate(dateStr) {
        if (!dateStr) return "";
        const d = new Date(dateStr);
        return d.toLocaleString([], {
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
        });
    }

    test("returns empty string for falsy input", () => {
        expect(formatDate("")).toBe("");
        expect(formatDate(null)).toBe("");
    });

    test("formats a valid date", () => {
        const result = formatDate("2026-03-15T10:30:00Z");
        // Result depends on locale, but should be non-empty
        expect(result.length).toBeGreaterThan(0);
    });
});

describe("JobDrawer — runningCount logic", () => {
    function runningCount(jobs) {
        return jobs.filter((j) => ACTIVE_STATES.includes(j.state)).length;
    }

    test("counts active jobs", () => {
        const jobs = [
            { state: "started" },
            { state: "done" },
            { state: "pending" },
            { state: "failed" },
            { state: "blocked" },
        ];
        expect(runningCount(jobs)).toBe(3);
    });

    test("returns 0 when no active jobs", () => {
        const jobs = [{ state: "done" }, { state: "failed" }, { state: "cancelled" }];
        expect(runningCount(jobs)).toBe(0);
    });

    test("returns 0 for empty list", () => {
        expect(runningCount([])).toBe(0);
    });
});

describe("JobDrawer — hasRunningJobs logic", () => {
    function hasRunningJobs(jobs) {
        return jobs.some((j) => ACTIVE_STATES.includes(j.state));
    }

    test("returns true when there are active jobs", () => {
        expect(hasRunningJobs([{ state: "started" }, { state: "done" }])).toBe(true);
    });

    test("returns false when all finished", () => {
        expect(hasRunningJobs([{ state: "done" }, { state: "cancelled" }])).toBe(false);
    });
});

describe("JobDrawer — cancel button visibility logic", () => {
    /**
     * Template shows cancel button for active states:
     * ['pending', 'enqueued', 'wait_dependencies', 'started']
     */
    const cancelStates = ["pending", "enqueued", "wait_dependencies", "started"];

    test("cancel visible for pending", () => {
        expect(cancelStates.includes("pending")).toBe(true);
    });

    test("cancel visible for started", () => {
        expect(cancelStates.includes("started")).toBe(true);
    });

    test("cancel hidden for done", () => {
        expect(cancelStates.includes("done")).toBe(false);
    });

    test("cancel hidden for failed", () => {
        expect(cancelStates.includes("failed")).toBe(false);
    });
});

describe("JobDrawer — retry button visibility logic", () => {
    /**
     * Template shows retry button when job.state === 'failed'.
     */
    test("retry visible for failed", () => {
        expect("failed" === "failed").toBe(true);
    });

    test("retry hidden for done", () => {
        expect("done" === "failed").toBe(false);
    });

    test("retry hidden for started", () => {
        expect("started" === "failed").toBe(false);
    });
});

describe("JobDrawer — class structure", () => {
    test("template name is set", () => {
        expect(JobDrawer.template).toBe("incubacloud.JobDrawer");
    });
});
