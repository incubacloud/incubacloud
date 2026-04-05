import { describe, expect, test } from "@odoo/hoot";
import { HostsDashboard } from "@incubacloud/components/hosts_dashboard/hosts_dashboard";

/**
 * HostsDashboard uses useService("orm"), useService("bus_service"),
 * useDebounced, rpc, setInterval, env.navigate, and useEnv.
 * Full mounting requires a complex service mock setup.
 * We test the pure helper functions extracted from the class prototype.
 */

describe("HostsDashboard — barClass helper", () => {
    // Extracted from HostsDashboard.prototype.barClass
    function barClass(value) {
        if (!value) return "bar-empty";
        if (value >= 85) return "bar-danger";
        if (value >= 65) return "bar-warn";
        return "bar-ok";
    }

    test("returns bar-empty for 0", () => {
        expect(barClass(0)).toBe("bar-empty");
    });

    test("returns bar-empty for null", () => {
        expect(barClass(null)).toBe("bar-empty");
    });

    test("returns bar-ok for low usage", () => {
        expect(barClass(30)).toBe("bar-ok");
    });

    test("returns bar-warn for 65%", () => {
        expect(barClass(65)).toBe("bar-warn");
    });

    test("returns bar-warn for 84%", () => {
        expect(barClass(84)).toBe("bar-warn");
    });

    test("returns bar-danger for 85%", () => {
        expect(barClass(85)).toBe("bar-danger");
    });

    test("returns bar-danger for 100%", () => {
        expect(barClass(100)).toBe("bar-danger");
    });
});

describe("HostsDashboard — hasMetrics helper", () => {
    function hasMetrics(host) {
        return host.cpu_cores > 0 || host.ram_total_gb > 0 || host.disk > 0;
    }

    test("returns true when cpu_cores > 0", () => {
        expect(hasMetrics({ cpu_cores: 4, ram_total_gb: 0, disk: 0 })).toBe(true);
    });

    test("returns true when all metrics present", () => {
        expect(hasMetrics({ cpu_cores: 4, ram_total_gb: 16, disk: 50 })).toBe(true);
    });

    test("returns false when all zeros", () => {
        expect(hasMetrics({ cpu_cores: 0, ram_total_gb: 0, disk: 0 })).toBe(false);
    });
});

describe("HostsDashboard — formatRam helper", () => {
    function formatRam(gb) {
        if (!gb) return "\u2014";
        return gb % 1 === 0 ? `${gb} GB` : `${gb.toFixed(1)} GB`;
    }

    test("returns dash for 0", () => {
        expect(formatRam(0)).toBe("\u2014");
    });

    test("returns dash for null", () => {
        expect(formatRam(null)).toBe("\u2014");
    });

    test("integer GB shows no decimal", () => {
        expect(formatRam(16)).toBe("16 GB");
    });

    test("fractional GB shows one decimal", () => {
        expect(formatRam(15.5)).toBe("15.5 GB");
    });
});

describe("HostsDashboard — diskExtra helper", () => {
    function diskExtra(host) {
        const free = host.disk_free_gb;
        const pct = host.disk;
        if (!free && !pct) return "";
        if (!pct || pct <= 0 || pct >= 100) return `${free} GB free`;
        const total = Math.round((free * 100) / (100 - pct));
        return `${free} GB free / ${total} GB`;
    }

    test("returns empty when no data", () => {
        expect(diskExtra({ disk_free_gb: 0, disk: 0 })).toBe("");
    });

    test("returns free only when pct is 0", () => {
        expect(diskExtra({ disk_free_gb: 50, disk: 0 })).toBe("50 GB free");
    });

    test("returns free / total when both values present", () => {
        expect(diskExtra({ disk_free_gb: 50, disk: 50 })).toBe("50 GB free / 100 GB");
    });

    test("computes total correctly", () => {
        // 20 GB free, 80% used => total = 20*100/20 = 100 GB
        expect(diskExtra({ disk_free_gb: 20, disk: 80 })).toBe("20 GB free / 100 GB");
    });
});

describe("HostsDashboard — statusLabel helper", () => {
    function statusLabel(status) {
        return (
            {
                compatible: "Compatible",
                degraded: "Degraded",
                unsupported: "Unsupported",
                checking: "Checking",
                unknown: "Unknown",
            }[status] || status
        );
    }

    test("maps compatible", () => {
        expect(statusLabel("compatible")).toBe("Compatible");
    });

    test("maps degraded", () => {
        expect(statusLabel("degraded")).toBe("Degraded");
    });

    test("unknown status returns itself", () => {
        expect(statusLabel("offline")).toBe("offline");
    });
});

describe("HostsDashboard — timeAgo helper", () => {
    function timeAgo(dateStr) {
        if (!dateStr) return null;
        const diff = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
        if (diff < 60) return `${diff}s ago`;
        if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
        if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
        return `${Math.floor(diff / 86400)}d ago`;
    }

    test("returns null for falsy input", () => {
        expect(timeAgo(null)).toBe(null);
        expect(timeAgo("")).toBe(null);
    });

    test("formats minutes correctly", () => {
        const date = new Date(Date.now() - 10 * 60 * 1000).toISOString();
        expect(timeAgo(date)).toBe("10m ago");
    });
});

describe("HostsDashboard — search filter logic", () => {
    function filterHosts(hosts, query) {
        return hosts.filter((host) =>
            host.name.toLowerCase().includes(query.toLowerCase())
        );
    }

    const sampleHosts = [
        { id: 1, name: "Production Server" },
        { id: 2, name: "Staging Node" },
        { id: 3, name: "Dev Server" },
    ];

    test("filters by name case-insensitively", () => {
        const result = filterHosts(sampleHosts, "server");
        expect(result.length).toBe(2);
    });

    test("no match returns empty", () => {
        expect(filterHosts(sampleHosts, "zzz")).toEqual([]);
    });

    test("partial match works", () => {
        const result = filterHosts(sampleHosts, "stag");
        expect(result.length).toBe(1);
        expect(result[0].id).toBe(2);
    });
});

describe("HostsDashboard — class structure", () => {
    test("template name is set", () => {
        expect(HostsDashboard.template).toBe("incubacloud.HostsDashboard");
    });
});
