import { describe, expect, test } from "@odoo/hoot";

/**
 * Host auto-assign feature — pure logic tests.
 * Tests isValid behavior, dropdown labeling, and response parsing.
 */

// ── isValid logic (mirrored from instance_detail.js) ───────────────────────

function isValid(form, autoassignEnabled) {
    const nameOk = !!(form.name || "").trim();
    return autoassignEnabled ? nameOk : nameOk && !!form.host_id;
}

describe("Host auto-assign — isValid logic", () => {
    test("autoassign ON: valid with name only (no host_id)", () => {
        expect(isValid({ name: "my-inst", host_id: null }, true)).toBe(true);
    });

    test("autoassign ON: valid with name and host_id", () => {
        expect(isValid({ name: "my-inst", host_id: 42 }, true)).toBe(true);
    });

    test("autoassign OFF: invalid without host_id", () => {
        expect(isValid({ name: "my-inst", host_id: null }, false)).toBe(false);
    });

    test("autoassign OFF: valid with name and host_id", () => {
        expect(isValid({ name: "my-inst", host_id: 42 }, false)).toBe(true);
    });

    test("empty name always invalid", () => {
        expect(isValid({ name: "", host_id: 42 }, true)).toBe(false);
        expect(isValid({ name: "  ", host_id: 42 }, false)).toBe(false);
    });
});

// ── Dropdown option formatting ──────────────────────────────────────────────

function hostOptionLabel(host) {
    const cpu = host.available_cpus ?? "?";
    const ram = host.available_ram_gb ?? "?";
    return `${host.name} (${cpu} CPU / ${ram} GB free)`;
}

describe("Host auto-assign — dropdown options", () => {
    test("host options show available resources", () => {
        const h = { name: "Server-1", available_cpus: 4.25, available_ram_gb: 12.25 };
        expect(hostOptionLabel(h)).toBe("Server-1 (4.25 CPU / 12.25 GB free)");
    });

    test("missing availability shows question marks", () => {
        const h = { name: "Server-X" };
        expect(hostOptionLabel(h)).toBe("Server-X (? CPU / ? GB free)");
    });
});

// ── get_hosts response parsing ──────────────────────────────────────────────

function parseGetHostsResponse(data) {
    if (Array.isArray(data)) {
        // Legacy format: plain array
        return { hosts: data, autoassignEnabled: false };
    }
    return {
        hosts: data.hosts || [],
        autoassignEnabled: !!data.autoassign_enabled,
    };
}

describe("Host auto-assign — get_hosts response parsing", () => {
    test("parses new dict format", () => {
        const resp = {
            autoassign_enabled: true,
            hosts: [{ id: 1, name: "H1" }],
        };
        const parsed = parseGetHostsResponse(resp);
        expect(parsed.autoassignEnabled).toBe(true);
        expect(parsed.hosts).toHaveLength(1);
    });

    test("extracts autoassign_enabled false", () => {
        const resp = { autoassign_enabled: false, hosts: [] };
        const parsed = parseGetHostsResponse(resp);
        expect(parsed.autoassignEnabled).toBe(false);
    });

    test("fallback for legacy array format", () => {
        const resp = [{ id: 1 }, { id: 2 }];
        const parsed = parseGetHostsResponse(resp);
        expect(parsed.hosts).toHaveLength(2);
        expect(parsed.autoassignEnabled).toBe(false);
    });
});
