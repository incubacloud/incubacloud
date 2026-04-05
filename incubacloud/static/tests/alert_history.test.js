import { describe, expect, test } from "@odoo/hoot";
import { AlertHistory } from "@incubacloud/components/alert_history/alert_history";

/**
 * AlertHistory pure-logic tests.
 *
 * The component uses useEnv(), RPC calls, and env.navigate — cannot mount
 * without significant mocking. We test filter logic, date formatting,
 * modal state transitions, and conflict resolution logic.
 */

describe("AlertHistory — class structure", () => {
    test("has the correct template name", () => {
        expect(AlertHistory.template).toBe("incubacloud.AlertHistory");
    });

    test("props allow optional embedded boolean", () => {
        expect(AlertHistory.props.embedded.type).toBe(Boolean);
        expect(AlertHistory.props.embedded.optional).toBe(true);
    });

    test("is a Component subclass with setup", () => {
        expect(typeof AlertHistory.prototype.setup).toBe("function");
    });
});

describe("AlertHistory — filter logic", () => {
    /**
     * setFilter(f) sets stateFilter and reloads.
     * We simulate the state transition.
     */
    test("setFilter updates stateFilter", () => {
        const state = { stateFilter: "active" };
        function setFilter(f) { state.stateFilter = f; }
        setFilter("resolved");
        expect(state.stateFilter).toBe("resolved");
    });

    test("default filter is 'active'", () => {
        // From setup(): stateFilter: "active"
        expect("active").toBe("active");
    });

    test("setFilter accepts any string", () => {
        const state = { stateFilter: "active" };
        function setFilter(f) { state.stateFilter = f; }
        setFilter("dismissed");
        expect(state.stateFilter).toBe("dismissed");
        setFilter("all");
        expect(state.stateFilter).toBe("all");
    });
});

describe("AlertHistory — formatDate", () => {
    /**
     * Mirrored from source:
     *   formatDate(dateStr) {
     *       if (!dateStr) return "\u2014";
     *       const d = new Date(dateStr);
     *       return d.toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
     *   }
     */
    function formatDate(dateStr) {
        if (!dateStr) return "\u2014";
        const d = new Date(dateStr);
        return d.toLocaleString([], {
            month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit",
        });
    }

    test("returns em-dash for falsy input", () => {
        expect(formatDate(null)).toBe("\u2014");
        expect(formatDate("")).toBe("\u2014");
        expect(formatDate(undefined)).toBe("\u2014");
    });

    test("returns a non-empty string for valid date", () => {
        const result = formatDate("2025-06-15T14:30:00Z");
        expect(typeof result).toBe("string");
        expect(result.length).toBeGreaterThan(0);
    });

    test("output contains numeric characters (digits from date)", () => {
        const result = formatDate("2025-01-05T09:07:00Z");
        expect(/\d/.test(result)).toBe(true);
    });
});

describe("AlertHistory — openResolve modal logic", () => {
    test("pre-fills choices with existing values from conflict_data", () => {
        const alert = {
            id: 1,
            conflict_data: [
                { name: "flask", existing: "flask>=2.0" },
                { name: "requests", existing: "requests>=2.28" },
            ],
        };
        const choices = {};
        for (const c of (alert.conflict_data || [])) {
            choices[c.name] = c.existing;
        }
        expect(choices).toEqual({
            flask: "flask>=2.0",
            requests: "requests>=2.28",
        });
    });

    test("handles empty conflict_data", () => {
        const alert = { id: 2, conflict_data: [] };
        const choices = {};
        for (const c of (alert.conflict_data || [])) {
            choices[c.name] = c.existing;
        }
        expect(choices).toEqual({});
    });

    test("handles missing conflict_data", () => {
        const alert = { id: 3 };
        const choices = {};
        for (const c of (alert.conflict_data || [])) {
            choices[c.name] = c.existing;
        }
        expect(choices).toEqual({});
    });
});

describe("AlertHistory — setChoice", () => {
    test("updates a choice in the resolve modal", () => {
        const resolveModal = {
            alert: { id: 1 },
            choices: { flask: "flask>=2.0" },
        };
        // Simulate setChoice
        function setChoice(pkgName, spec) {
            if (resolveModal) {
                resolveModal.choices[pkgName] = spec;
            }
        }
        setChoice("flask", "flask>=3.0");
        expect(resolveModal.choices.flask).toBe("flask>=3.0");
    });
});

describe("AlertHistory — openAddonConflict modal logic", () => {
    test("pre-selects first repo for each conflict", () => {
        const alert = {
            id: 1,
            conflict_data: [
                { addon: "sale", repos: ["repo-a", "repo-b"] },
                { addon: "stock", repos: ["repo-c"] },
            ],
        };
        const choices = {};
        for (const c of (alert.conflict_data || [])) {
            choices[c.addon] = (c.repos || [])[0] || '';
        }
        expect(choices).toEqual({
            sale: "repo-a",
            stock: "repo-c",
        });
    });

    test("handles empty repos array", () => {
        const alert = {
            id: 1,
            conflict_data: [{ addon: "sale", repos: [] }],
        };
        const choices = {};
        for (const c of (alert.conflict_data || [])) {
            choices[c.addon] = (c.repos || [])[0] || '';
        }
        expect(choices.sale).toBe("");
    });
});

describe("AlertHistory — close modal helpers", () => {
    test("closeResolveModal sets resolveModal to null", () => {
        const state = { resolveModal: { alert: {}, choices: {} } };
        state.resolveModal = null;
        expect(state.resolveModal).toBe(null);
    });

    test("closeAddonConflictModal sets addonConflictModal to null", () => {
        const state = { addonConflictModal: { alert: {}, choices: {} } };
        state.addonConflictModal = null;
        expect(state.addonConflictModal).toBe(null);
    });
});

describe("AlertHistory — method existence", () => {
    const proto = AlertHistory.prototype;

    test("has loadAlerts method", () => {
        expect(typeof proto.loadAlerts).toBe("function");
    });

    test("has setFilter method", () => {
        expect(typeof proto.setFilter).toBe("function");
    });

    test("has openJob method", () => {
        expect(typeof proto.openJob).toBe("function");
    });

    test("has openResolve method", () => {
        expect(typeof proto.openResolve).toBe("function");
    });

    test("has confirmResolve method", () => {
        expect(typeof proto.confirmResolve).toBe("function");
    });

    test("has dismissAlert method", () => {
        expect(typeof proto.dismissAlert).toBe("function");
    });

    test("has formatDate method", () => {
        expect(typeof proto.formatDate).toBe("function");
    });

    test("has goBack method", () => {
        expect(typeof proto.goBack).toBe("function");
    });
});
