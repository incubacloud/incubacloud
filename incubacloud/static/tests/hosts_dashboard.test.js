import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { useEnv, useState } from "@odoo/owl";
import {
    allowTranslations,
    mountWithCleanup,
    patchWithCleanup,
} from "@web/../tests/web_test_helpers";
import { relayIcon } from "@incubacloud/utils/icons";
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

/**
 * Refresh split: the skeleton belongs to the first paint, never to a
 * background refresh. Driving the prototype methods against a plain
 * object keeps this free of the service/mount stack, matching the
 * style of the helper tests above.
 */
describe("HostsDashboard — silent vs. visible refresh", () => {
    function makeSelf({ fail = false } = {}) {
        const self = {
            state: {
                hosts: [{ id: 1 }], visible_hosts: [{ id: 1 }],
                loading: false, error: "",
            },
            seenLoading: [],
        };
        self._fetchHosts = async () => {
            self.seenLoading.push(self.state.loading);
            if (fail) throw new Error("boom");
        };
        return self;
    }

    test("_silentRefresh never raises the loading flag", async () => {
        const self = makeSelf();
        await HostsDashboard.prototype._silentRefresh.call(self);
        expect(self.seenLoading).toEqual([false]);
        expect(self.state.loading).toBe(false);
    });

    test("_silentRefresh swallows a failure and keeps the fleet", async () => {
        const self = makeSelf({ fail: true });
        await HostsDashboard.prototype._silentRefresh.call(self);
        expect(self.state.error).toBe("");
        expect(self.state.hosts.length).toBe(1);
    });

    test("loadHosts shows the skeleton while fetching", async () => {
        const self = makeSelf();
        await HostsDashboard.prototype.loadHosts.call(self);
        expect(self.seenLoading).toEqual([true]);
        expect(self.state.loading).toBe(false);
    });

    test("loadHosts surfaces a failure as an error", async () => {
        const self = makeSelf({ fail: true });
        await HostsDashboard.prototype.loadHosts.call(self);
        expect(self.state.error).toBe("boom");
        expect(self.state.loading).toBe(false);
    });
});

/**
 * Live CPU and RAM on a host's card. The real prototype methods, on an
 * object made from the prototype so they reach each other through
 * ``this`` without the service/mount stack.
 */
describe("HostsDashboard — live usage", () => {
    beforeEach(() => allowTranslations());
    const card = Object.create(HostsDashboard.prototype);
    // A panel that can turn metrics on, as core's.
    card.env = { features: {} };
    const live = {
        usage_state: "live", cpu_usage: 23.4, ram_usage: 62.5,
        cpu_cores: 4, ram_total_gb: 16,
        allocated_cpus: 6, allocated_ram_gb: 12,
    };

    test("a live reading is shown; a stale one or a missing figure is not", () => {
        expect(card.hasLiveUsage(live)).toBe(true);
        expect(card.hasLiveUsage({ ...live, usage_state: "stale" })).toBe(false);
        expect(card.hasLiveUsage({ ...live, cpu_usage: null })).toBe(false);
        expect(card.hasLiveUsage({ ...live, ram_usage: undefined })).toBe(false);
    });

    test("a live 0 % is still a reading", () => {
        expect(card.hasLiveUsage({ ...live, cpu_usage: 0 })).toBe(true);
    });

    test("the bar's percentage is whole and within 0-100", () => {
        expect(card.pct(23.4)).toBe(23);
        expect(card.pct(100.6)).toBe(100);
        expect(card.pct(-1)).toBe(0);
        expect(card.pct(null)).toBe(0);
    });

    test("the CPU line gives cores in use and the allocation beside", () => {
        expect(String(card.cpuExtra(live))).toBe("0.9 of 4 cores · allocated: 6 vCPU");
    });

    test("an allocation over the host's cores reads as it is", () => {
        expect(String(card.cpuExtra({ ...live, allocated_cpus: 11.25 })))
            .toBe("0.9 of 4 cores · allocated: 11.3 vCPU");
    });

    test("without instances the allocation is left out", () => {
        expect(String(card.cpuExtra({ ...live, allocated_cpus: 0 }))).toBe("0.9 of 4 cores");
        expect(String(card.ramExtra({ ...live, allocated_ram_gb: 0 }))).toBe("10 of 16 GB");
    });

    test("the RAM line gives memory in use and the allocation beside", () => {
        expect(String(card.ramExtra(live))).toBe("10 of 16 GB · allocated: 12 GB");
    });

    test("each reason for no reading is said in words", () => {
        expect(String(card.usageMissing({ usage_state: "off" })))
            .toBe("Metrics are off. Turn them on in Settings to see live usage.");
        expect(String(card.usageMissing({ usage_state: "no_agents" })))
            .toBe("The metrics agents are not installed on this host.");
        expect(String(card.usageMissing({ usage_state: "stale", usage_last_seen: "" })))
            .toBe("No reading yet.");
    });

    test("a failed installation of the agents is said as such", () => {
        expect(String(card.usageMissing({ usage_state: "agents_failed" })))
            .toBe("The installation of the metrics agents on this host failed.");
    });

    test("on a panel that cannot turn metrics on, off gives no instruction", () => {
        const managed = Object.create(HostsDashboard.prototype);
        managed.env = { features: { observability: { configure: false } } };
        expect(String(managed.usageMissing({ usage_state: "off" })))
            .toBe("Live usage is not available on this panel.");
        expect(managed.usageIcon({ usage_state: "off" })).toBe("lock");
        const core = Object.create(HostsDashboard.prototype);
        core.env = { features: { observability: { configure: true } } };
        expect(String(core.usageMissing({ usage_state: "off" })))
            .toBe("Metrics are off. Turn them on in Settings to see live usage.");
    });

    test("a stale reading says when the last one was", () => {
        const threeHoursAgo = new Date(Date.now() - 3 * 3600 * 1000).toISOString();
        expect(String(card.usageMissing({ usage_state: "stale", usage_last_seen: threeHoursAgo })))
            .toBe("No reading in the last 15 minutes (last: 3h ago).");
    });

    test("each reason has its icon", () => {
        expect(card.usageIcon({ usage_state: "off" })).toBe("settings");
        expect(card.usageIcon({ usage_state: "no_agents" })).toBe("download");
        expect(card.usageIcon({ usage_state: "agents_failed" })).toBe("alert");
        expect(card.usageIcon({ usage_state: "stale" })).toBe("clock");
    });

    test("without a reading the allocation still shows, when there is one", () => {
        expect(String(card.allocatedText(live))).toBe("Allocated: 6 vCPU · 12 GB");
        expect(card.allocatedText({ allocated_cpus: 0, allocated_ram_gb: 0 })).toBe("");
    });
});

/**
 * What the template paints for each host: the bars with a live reading,
 * «No data» and its reason without one. The real class and template,
 * with the setup that reaches for services replaced by one that only
 * holds the state.
 */
describe("HostsDashboard — what a card paints", () => {
    beforeEach(() => allowTranslations());

    const base = {
        status: "compatible", ip_address: "10.0.0.1", region: "", tags: [],
        cpu_cores: 4, ram_total_gb: 16, disk: 40,
        allocated_cpus: 6, allocated_ram_gb: 12,
    };
    const live = {
        ...base, id: 1, name: "live-host", usage_state: "live",
        cpu_usage: 23.4, ram_usage: 62.5, usage_last_seen: "2026-11-12T10:00:00",
    };
    const stale = {
        ...base, id: 2, name: "stale-host", usage_state: "stale",
        cpu_usage: null, ram_usage: null, usage_last_seen: "",
    };

    /**
     * Mount the dashboard with these hosts.
     *
     * @param {Object[]} hosts
     * @param {Object} [features] the env's features
     */
    async function mountWith(hosts, features = {}) {
        patchWithCleanup(HostsDashboard.prototype, {
            setup() {
                this.env = useEnv();
                this.state = useState({
                    hosts, visible_hosts: hosts, truncated: false,
                    total: hosts.length, limit: 200, loading: false, error: "",
                });
            },
        });
        await mountWithCleanup(HostsDashboard, {
            env: { icon: relayIcon, features, navigate() {} },
            noMainContainer: true,
        });
    }

    /**
     * The card of the host called *name*.
     *
     * @param {string} name
     * @returns {string} a selector
     */
    const cardOf = (name) =>
        `.ic-host-card:has(.rl-rname:contains("${name}"))`;

    test("a live host paints the CPU and RAM bars and no «No data»", async () => {
        await mountWith([live]);
        const card = cardOf("live-host");
        expect(`${card} .ic-mbar-cpu .rl-mbar-fill`).toHaveCount(1);
        expect(`${card} .ic-mbar-ram .rl-mbar-fill`).toHaveCount(1);
        expect(`${card} .ic-mbar-cpu .rl-mbar-fill`).toHaveAttribute("style", "width:23%");
        expect(`${card} .ic-usage-none`).toHaveCount(0);
    });

    test("a host without a recent reading paints «No data» and no bar", async () => {
        await mountWith([stale]);
        const card = cardOf("stale-host");
        expect(`${card} .ic-usage-none`).toHaveCount(1);
        expect(`${card} .ic-usage-none`).toHaveText(/No data/);
        expect(`${card} .ic-usage-reason`).toHaveText("No reading yet.");
        expect(`${card} .ic-mbar-cpu`).toHaveCount(0);
        expect(`${card} .ic-mbar-ram`).toHaveCount(0);
    });

    test("each card paints its own: one with bars beside one without", async () => {
        await mountWith([live, stale]);
        expect(".ic-mbar-cpu").toHaveCount(1);
        expect(".ic-usage-none").toHaveCount(1);
        expect(`${cardOf("live-host")} .ic-usage-none`).toHaveCount(0);
        expect(`${cardOf("stale-host")} .ic-mbar-cpu`).toHaveCount(0);
    });

    test("a managed panel gives no instruction for off", async () => {
        const off = { ...stale, usage_state: "off" };
        await mountWith([off], { observability: { configure: false } });
        expect(".ic-usage-reason").toHaveText("Live usage is not available on this panel.");
    });
});
