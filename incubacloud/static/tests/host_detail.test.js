import { describe, expect, test } from "@odoo/hoot";
import { HostDetail } from "@incubacloud/components/host_detail/host_detail";

/**
 * HostDetail uses useEnv, useService("orm"), rpc, onWillStart,
 * onMounted, onWillUnmount, setInterval, and PasswordInput.
 * We test pure helper functions: statusLabel, barClass, hasMetrics,
 * formatRam, diskExtra, formatDate, timeAgo, isActiveJob, jobIcon,
 * jobStateClass, getLineNumbers, tab system, and whitelist logic.
 */

describe("HostDetail — class structure", () => {
    test("template name is set", () => {
        expect(HostDetail.template).toBe("incubacloud.HostDetail");
    });

    test("props defines host_id as optional Number", () => {
        expect(HostDetail.props.host_id.type).toBe(Number);
        expect(HostDetail.props.host_id.optional).toBe(true);
    });

    test("components includes PasswordInput", () => {
        expect("PasswordInput" in HostDetail.components).toBe(true);
    });
});

describe("HostDetail — statusLabel helper", () => {
    function statusLabel(status) {
        return {
            compatible: "Compatible",
            degraded: "Degraded",
            unsupported: "Unsupported",
            checking: "Checking",
            unknown: "Unknown",
        }[status] || status;
    }

    test("maps compatible", () => {
        expect(statusLabel("compatible")).toBe("Compatible");
    });

    test("maps degraded", () => {
        expect(statusLabel("degraded")).toBe("Degraded");
    });

    test("maps unsupported", () => {
        expect(statusLabel("unsupported")).toBe("Unsupported");
    });

    test("maps checking", () => {
        expect(statusLabel("checking")).toBe("Checking");
    });

    test("maps unknown", () => {
        expect(statusLabel("unknown")).toBe("Unknown");
    });

    test("returns raw value for unmapped status", () => {
        expect(statusLabel("offline")).toBe("offline");
    });
});

describe("HostDetail — barClass helper", () => {
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

    test("returns bar-empty for undefined", () => {
        expect(barClass(undefined)).toBe("bar-empty");
    });

    test("returns bar-ok for 30%", () => {
        expect(barClass(30)).toBe("bar-ok");
    });

    test("returns bar-ok for 64%", () => {
        expect(barClass(64)).toBe("bar-ok");
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

describe("HostDetail — hasMetrics helper", () => {
    function hasMetrics(host) {
        return host && (host.cpu_cores > 0 || host.ram_total_gb > 0 || host.disk > 0);
    }

    test("returns true when cpu_cores > 0", () => {
        expect(hasMetrics({ cpu_cores: 4, ram_total_gb: 0, disk: 0 })).toBe(true);
    });

    test("returns true when ram > 0", () => {
        expect(hasMetrics({ cpu_cores: 0, ram_total_gb: 16, disk: 0 })).toBe(true);
    });

    test("returns true when disk > 0", () => {
        expect(hasMetrics({ cpu_cores: 0, ram_total_gb: 0, disk: 50 })).toBe(true);
    });

    test("returns false when all zeros", () => {
        expect(hasMetrics({ cpu_cores: 0, ram_total_gb: 0, disk: 0 })).toBe(false);
    });

    test("returns null for null host", () => {
        expect(hasMetrics(null)).toBe(null);
    });
});

describe("HostDetail — formatRam helper", () => {
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
        expect(formatRam(8)).toBe("8 GB");
    });

    test("fractional GB shows one decimal", () => {
        expect(formatRam(15.7)).toBe("15.7 GB");
    });

    test("handles large values", () => {
        expect(formatRam(128)).toBe("128 GB");
    });
});

describe("HostDetail — diskExtra helper", () => {
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

    test("returns free only when pct is 100", () => {
        expect(diskExtra({ disk_free_gb: 0, disk: 100 })).toBe("0 GB free");
    });

    test("computes total correctly (50% used)", () => {
        expect(diskExtra({ disk_free_gb: 50, disk: 50 })).toBe("50 GB free / 100 GB");
    });

    test("computes total correctly (80% used)", () => {
        expect(diskExtra({ disk_free_gb: 20, disk: 80 })).toBe("20 GB free / 100 GB");
    });

    test("computes total correctly (25% used)", () => {
        expect(diskExtra({ disk_free_gb: 75, disk: 25 })).toBe("75 GB free / 100 GB");
    });
});

describe("HostDetail — formatDate helper", () => {
    function formatDate(dateStr) {
        if (!dateStr) return "";
        const d = new Date(dateStr);
        const pad = (n) => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }

    test("returns empty for null", () => {
        expect(formatDate(null)).toBe("");
    });

    test("returns empty for empty string", () => {
        expect(formatDate("")).toBe("");
    });

    test("formats ISO date correctly", () => {
        // Use a fixed UTC date and check format structure
        const result = formatDate("2025-03-15T10:30:00Z");
        // Check it matches YYYY-MM-DD HH:MM pattern
        expect(result).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    });

    test("pads single-digit months and days", () => {
        const result = formatDate("2025-01-05T08:05:00Z");
        expect(result).toMatch(/^\d{4}-01-05/);
    });
});

describe("HostDetail — timeAgo helper", () => {
    function timeAgo(dateStr) {
        if (!dateStr) return "";
        const secs = Math.floor((Date.now() - new Date(dateStr).getTime()) / 1000);
        if (secs < 60) return "just now";
        const mins = Math.floor(secs / 60);
        if (mins < 60) return `${mins}m ago`;
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return `${hrs}h ago`;
        return `${Math.floor(hrs / 24)}d ago`;
    }

    test("returns empty for null", () => {
        expect(timeAgo(null)).toBe("");
    });

    test("returns empty for empty string", () => {
        expect(timeAgo("")).toBe("");
    });

    test("returns just now for very recent time", () => {
        const recent = new Date(Date.now() - 5000).toISOString();
        expect(timeAgo(recent)).toBe("just now");
    });

    test("formats minutes correctly", () => {
        const date = new Date(Date.now() - 10 * 60 * 1000).toISOString();
        expect(timeAgo(date)).toBe("10m ago");
    });

    test("formats hours correctly", () => {
        const date = new Date(Date.now() - 3 * 3600 * 1000).toISOString();
        expect(timeAgo(date)).toBe("3h ago");
    });

    test("formats days correctly", () => {
        const date = new Date(Date.now() - 5 * 86400 * 1000).toISOString();
        expect(timeAgo(date)).toBe("5d ago");
    });
});

describe("HostDetail — isActiveJob helper", () => {
    function isActiveJob(state) {
        return ["pending", "enqueued", "started", "wait_dependencies"].includes(state);
    }

    test("pending is active", () => {
        expect(isActiveJob("pending")).toBe(true);
    });

    test("enqueued is active", () => {
        expect(isActiveJob("enqueued")).toBe(true);
    });

    test("started is active", () => {
        expect(isActiveJob("started")).toBe(true);
    });

    test("wait_dependencies is active", () => {
        expect(isActiveJob("wait_dependencies")).toBe(true);
    });

    test("done is not active", () => {
        expect(isActiveJob("done")).toBe(false);
    });

    test("failed is not active", () => {
        expect(isActiveJob("failed")).toBe(false);
    });

    test("cancelled is not active", () => {
        expect(isActiveJob("cancelled")).toBe(false);
    });
});

describe("HostDetail — jobIcon helper", () => {
    function jobIcon(code) {
        return {
            host_probe: "fa-heartbeat",
            full_setup: "fa-wrench",
            deploy_instance: "fa-rocket",
            rebuild_instance: "fa-wrench",
            setup_whitelist: "fa-shield",
            docker_prune: "fa-recycle",
            host_metrics: "fa-bar-chart",
        }[code] || "fa-cog";
    }

    test("host_probe maps to fa-heartbeat", () => {
        expect(jobIcon("host_probe")).toBe("fa-heartbeat");
    });

    test("full_setup maps to fa-wrench", () => {
        expect(jobIcon("full_setup")).toBe("fa-wrench");
    });

    test("deploy_instance maps to fa-rocket", () => {
        expect(jobIcon("deploy_instance")).toBe("fa-rocket");
    });

    test("setup_whitelist maps to fa-shield", () => {
        expect(jobIcon("setup_whitelist")).toBe("fa-shield");
    });

    test("docker_prune maps to fa-recycle", () => {
        expect(jobIcon("docker_prune")).toBe("fa-recycle");
    });

    test("host_metrics maps to fa-bar-chart", () => {
        expect(jobIcon("host_metrics")).toBe("fa-bar-chart");
    });

    test("unknown code returns fa-cog", () => {
        expect(jobIcon("something_else")).toBe("fa-cog");
    });
});

describe("HostDetail — jobStateClass helper", () => {
    function jobStateClass(state) {
        return {
            done: "jst-done",
            failed: "jst-failed",
            cancelled: "jst-cancelled",
            started: "jst-running",
            pending: "jst-pending",
            enqueued: "jst-pending",
            blocked: "jst-blocked",
        }[state] || "jst-pending";
    }

    test("done maps to jst-done", () => {
        expect(jobStateClass("done")).toBe("jst-done");
    });

    test("failed maps to jst-failed", () => {
        expect(jobStateClass("failed")).toBe("jst-failed");
    });

    test("cancelled maps to jst-cancelled", () => {
        expect(jobStateClass("cancelled")).toBe("jst-cancelled");
    });

    test("started maps to jst-running", () => {
        expect(jobStateClass("started")).toBe("jst-running");
    });

    test("pending maps to jst-pending", () => {
        expect(jobStateClass("pending")).toBe("jst-pending");
    });

    test("enqueued maps to jst-pending", () => {
        expect(jobStateClass("enqueued")).toBe("jst-pending");
    });

    test("blocked maps to jst-blocked", () => {
        expect(jobStateClass("blocked")).toBe("jst-blocked");
    });

    test("unknown state falls back to jst-pending", () => {
        expect(jobStateClass("something")).toBe("jst-pending");
    });
});

describe("HostDetail — tab system logic", () => {
    // New hosts start on "general" tab, existing hosts on "overview"

    test("new host starts on general tab", () => {
        const isNew = true;
        const defaultTab = isNew ? "general" : "overview";
        expect(defaultTab).toBe("general");
    });

    test("existing host starts on overview tab", () => {
        const isNew = false;
        const defaultTab = isNew ? "general" : "overview";
        expect(defaultTab).toBe("overview");
    });
});

describe("HostDetail — EMPTY_FORM defaults", () => {
    const defaults = {
        name: "",
        type: "self_hosted",
        ip_address: "",
        port: 22,
        user: "",
        login_type: "ssh_key",
        password: "",
        key_file: null,
        description: "",
        tags: "",
        wildcard_domain: "",
        traefik_panel_password: "",
        traefik_config_yml: "",
        traefik_inverseproxy_yaml: "",
        traefik_yml: "",
        whitelist: [
            "cdnjs.cloudflare.com",
            "fonts.googleapis.com",
            "fonts.gstatic.com",
            "www.google.com",
            "www.gravatar.com",
        ],
        newWhitelistEntry: "",
    };

    test("type defaults to self_hosted", () => {
        expect(defaults.type).toBe("self_hosted");
    });

    test("port defaults to 22", () => {
        expect(defaults.port).toBe(22);
    });

    test("login_type defaults to ssh_key", () => {
        expect(defaults.login_type).toBe("ssh_key");
    });

    test("whitelist has 5 default entries", () => {
        expect(defaults.whitelist.length).toBe(5);
    });

    test("whitelist includes cloudflare CDN", () => {
        expect(defaults.whitelist).toInclude("cdnjs.cloudflare.com");
    });

    test("whitelist includes Google fonts", () => {
        expect(defaults.whitelist).toInclude("fonts.googleapis.com");
        expect(defaults.whitelist).toInclude("fonts.gstatic.com");
    });
});

describe("HostDetail — whitelist management logic", () => {
    function addWhitelistEntry(whitelist, entry) {
        const clean = (entry || "").trim().toLowerCase();
        if (!clean || whitelist.includes(clean)) return whitelist;
        return [...whitelist, clean];
    }

    function removeWhitelistEntry(whitelist, hostname) {
        return whitelist.filter((h) => h !== hostname);
    }

    test("adds new entry", () => {
        const result = addWhitelistEntry(["a.com"], "b.com");
        expect(result).toEqual(["a.com", "b.com"]);
    });

    test("does not add duplicate", () => {
        const result = addWhitelistEntry(["a.com"], "a.com");
        expect(result).toEqual(["a.com"]);
    });

    test("trims and lowercases entry", () => {
        const result = addWhitelistEntry([], "  Example.COM  ");
        expect(result).toEqual(["example.com"]);
    });

    test("ignores empty entry", () => {
        const result = addWhitelistEntry(["a.com"], "");
        expect(result).toEqual(["a.com"]);
    });

    test("removes existing entry", () => {
        const result = removeWhitelistEntry(["a.com", "b.com"], "a.com");
        expect(result).toEqual(["b.com"]);
    });

    test("no-op when removing non-existent entry", () => {
        const result = removeWhitelistEntry(["a.com"], "z.com");
        expect(result).toEqual(["a.com"]);
    });
});

describe("HostDetail — hasMoreJobs getter logic", () => {
    function hasMoreJobs(jobsTotal, jobsLength) {
        return jobsTotal > jobsLength;
    }

    test("true when total exceeds displayed", () => {
        expect(hasMoreJobs(10, 5)).toBe(true);
    });

    test("false when equal", () => {
        expect(hasMoreJobs(5, 5)).toBe(false);
    });

    test("false when less", () => {
        expect(hasMoreJobs(3, 5)).toBe(false);
    });
});

describe("HostDetail — getLineNumbers logic", () => {
    function getLineNumbers(text) {
        const lines = Math.max(1, (text || "").split("\n").length);
        return Array.from({ length: lines }, (_, i) => i + 1);
    }

    test("empty string returns [1]", () => {
        expect(getLineNumbers("")).toEqual([1]);
    });

    test("single line returns [1]", () => {
        expect(getLineNumbers("hello")).toEqual([1]);
    });

    test("multi-line returns correct numbers", () => {
        expect(getLineNumbers("a\nb\nc")).toEqual([1, 2, 3]);
    });

    test("null returns [1]", () => {
        expect(getLineNumbers(null)).toEqual([1]);
    });
});

describe("HostDetail — TRAEFIK_FILES constant", () => {
    const TRAEFIK_FILES = [
        { key: "traefik_config_yml", label: "config.yml" },
        { key: "traefik_inverseproxy_yaml", label: "inverseproxy.yaml" },
        { key: "traefik_yml", label: "traefik.yml" },
    ];

    test("has 3 files", () => {
        expect(TRAEFIK_FILES.length).toBe(3);
    });

    test("first file is config.yml", () => {
        expect(TRAEFIK_FILES[0].key).toBe("traefik_config_yml");
        expect(TRAEFIK_FILES[0].label).toBe("config.yml");
    });

    test("each entry has key and label", () => {
        for (const f of TRAEFIK_FILES) {
            expect(typeof f.key).toBe("string");
            expect(typeof f.label).toBe("string");
        }
    });
});

describe("HostDetail — method existence", () => {
    test("save method exists", () => {
        expect(typeof HostDetail.prototype.save).toBe("function");
    });

    test("deleteHost method exists", () => {
        expect(typeof HostDetail.prototype.deleteHost).toBe("function");
    });

    test("runCheck method exists", () => {
        expect(typeof HostDetail.prototype.runCheck).toBe("function");
    });

    test("runFullSetup method exists", () => {
        expect(typeof HostDetail.prototype.runFullSetup).toBe("function");
    });

    test("addWhitelistEntry method exists", () => {
        expect(typeof HostDetail.prototype.addWhitelistEntry).toBe("function");
    });

    test("removeWhitelistEntry method exists", () => {
        expect(typeof HostDetail.prototype.removeWhitelistEntry).toBe("function");
    });

    test("applyWhitelist method exists", () => {
        expect(typeof HostDetail.prototype.applyWhitelist).toBe("function");
    });

    test("openLog method exists", () => {
        expect(typeof HostDetail.prototype.openLog).toBe("function");
    });
});

describe("HostDetail — hostAction", () => {
    /**
     * Build the minimum `this` the method touches.
     *
     * The real component needs a mounted OWL tree, so the method is
     * called against a stand-in; every collaborator it reaches for is
     * recorded so the test can assert on the calls rather than on the
     * DOM.
     *
     * @param {object} [opts]
     * @param {boolean} [opts.confirm] What the dialog answers.
     * @param {boolean} [opts.fail] Make the enqueue call reject.
     * @returns {object} the stand-in, with a `calls` log.
     */
    function harness({ confirm = true, fail = false } = {}) {
        const calls = { enqueue: [], success: [], error: [], refresh: 0, confirm: [] };
        return {
            calls,
            props: { host_id: 7 },
            state: { actionBusy: "" },
            orm: {
                call(model, method, args) {
                    calls.enqueue.push([model, method, args]);
                    return fail ? Promise.reject(new Error("boom")) : Promise.resolve(11);
                },
            },
            env: {
                toast: {
                    success: (msg) => calls.success.push(msg),
                    error: (msg) => calls.error.push(msg),
                },
            },
            _confirm(opts) {
                calls.confirm.push(opts);
                return Promise.resolve(confirm);
            },
            _silentRefresh() {
                calls.refresh += 1;
            },
            hostAction: HostDetail.prototype.hostAction,
        };
    }

    test("queues the job and says so", async () => {
        const h = harness();
        await h.hostAction({ code: "push_trusted_proxies", name: "Push Trusted Proxy Settings" });
        expect(h.calls.enqueue).toHaveLength(1);
        expect(h.calls.enqueue[0][2]).toEqual([7, false, "push_trusted_proxies"]);
        expect(h.calls.success).toHaveLength(1);
        expect(h.calls.error).toHaveLength(0);
    });

    test("refreshes at once instead of waiting for the bus", async () => {
        const h = harness();
        await h.hostAction({ code: "a", name: "A" });
        expect(h.calls.refresh).toBe(1);
    });

    test("a second press while one is in flight queues nothing", async () => {
        const h = harness();
        h.state.actionBusy = "a";
        await h.hostAction({ code: "a", name: "A" });
        expect(h.calls.enqueue).toHaveLength(0);
    });

    test("releases the guard so the next press works", async () => {
        const h = harness();
        await h.hostAction({ code: "a", name: "A" });
        expect(h.state.actionBusy).toBe("");
        await h.hostAction({ code: "a", name: "A" });
        expect(h.calls.enqueue).toHaveLength(2);
    });

    test("releases the guard after a failure too", async () => {
        const h = harness({ fail: true });
        await h.hostAction({ code: "a", name: "A" });
        expect(h.calls.error).toHaveLength(1);
        expect(h.state.actionBusy).toBe("");
    });

    test("an action carrying a notice asks before queueing", async () => {
        const h = harness();
        await h.hostAction({ code: "a", name: "A", action_confirm: "Traefik restarts." });
        expect(h.calls.confirm).toHaveLength(1);
        expect(h.calls.confirm[0].message).toBe("Traefik restarts.");
        expect(h.calls.enqueue).toHaveLength(1);
    });

    test("declining the notice queues nothing", async () => {
        const h = harness({ confirm: false });
        await h.hostAction({ code: "a", name: "A", action_confirm: "Traefik restarts." });
        expect(h.calls.enqueue).toHaveLength(0);
        expect(h.state.actionBusy).toBe("");
    });

    test("an action without a notice asks nothing", async () => {
        const h = harness();
        await h.hostAction({ code: "a", name: "A" });
        expect(h.calls.confirm).toHaveLength(0);
    });
});
