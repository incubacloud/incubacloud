import { describe, expect, test } from "@odoo/hoot";

/**
 * InstanceDetail pure-logic tests.
 *
 * The component requires useService("orm"), useService("bus_service"), useEnv(),
 * and makes RPC calls on mount. We extract and test every helper, constant, and
 * getter as standalone functions — no component mounting needed.
 */

// ── Constants (mirrored from source) ────────────────────────────────────────

const ODOO_VERSIONS = [
    "7.0", "8.0", "9.0", "10.0", "11.0", "12.0", "13.0",
    "14.0", "15.0", "16.0", "17.0", "18.0", "19.0",
];
const PG_VERSIONS = ["14", "15", "16", "17", "18"];

// ── Helpers (mirrored from source) ──────────────────────────────────────────

function timeAgo(dateStr) {
    if (!dateStr) return "";
    const now = new Date();
    const d = new Date(dateStr);
    const secs = Math.floor((now - d) / 1000);
    if (secs < 60) return "just now";
    const mins = Math.floor(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
}

function formatDate(dateStr) {
    if (!dateStr) return "";
    const d = new Date(dateStr);
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function isActiveJob(state) {
    return ["pending", "enqueued", "started", "wait_dependencies"].includes(state);
}

function jobIcon(code) {
    const map = {
        deploy_instance: "fa-rocket",
        rebuild_instance: "fa-wrench",
        restart_instance: "fa-refresh",
        stop_instance: "fa-stop",
        start_instance: "fa-play",
        export_instance: "fa-download",
        delete_instance: "fa-trash-o",
        restore_instance: "fa-database",
    };
    return map[code] || "fa-cog";
}

function jobStateClass(state) {
    return {
        done: "jst-done", failed: "jst-failed", cancelled: "jst-cancelled",
        started: "jst-running", pending: "jst-pending", enqueued: "jst-pending",
        blocked: "jst-blocked",
    }[state] || "jst-pending";
}

function envLabel(env) {
    return { runbot: "Runbot", staging: "Staging", production: "Production" }[env] || env;
}

function statusLabel(inst) {
    if (!inst) return "";
    if (inst.status === "provisioning") return "Building...";
    if (!inst.deployed) return "Not deployed";
    if (inst.running) return "Running";
    if (inst.status === "error") return "Error";
    return "Stopped";
}

function statusClass(inst) {
    if (!inst) return "";
    if (inst.status === "provisioning") return "st-provisioning";
    if (!inst.deployed) return "st-pending";
    if (inst.running) return "st-running";
    if (inst.status === "error") return "st-error";
    return "st-stopped";
}

// ── Pip requirements helpers (mirrored from source) ─────────────────────────

function _parseReqLine(line) {
    const stripped = line.split('#')[0].trim();
    if (!stripped) return null;
    const m = stripped.match(/^([A-Za-z0-9_.-]+)/);
    if (!m) return null;
    const name = m[1].toLowerCase();
    const markerM = stripped.match(/;(.+)/);
    const marker = markerM ? markerM[1].trim().toLowerCase() : '';
    return { name, raw: stripped, key: marker ? `${name}|${marker}` : name };
}

function _formatSource(url, branch) {
    const m = (url || '').match(/github\.com[:/](.+)/);
    const name = m ? m[1].replace(/\.git$/, '') : (url || '').replace(/\/$/, '');
    return branch ? `${name} (${branch})` : name;
}

function _escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function _buildPkgMap(text) {
    const pkgMap = {};
    const re = /<<<<<<< (?<eSrc>[^\n]*)\n(?<eLine>[^\n]*)\n=======\n(?<iLine>[^\n]*)\n>>>>>>> (?<iSrc>[^\n]*)/g;
    let m;
    while ((m = re.exec(text || '')) !== null) {
        const parsed = _parseReqLine(m.groups.eLine.trim());
        if (parsed) {
            pkgMap[parsed.key] = {
                type: 'conflict', spec: parsed.raw, name: parsed.name,
                existingSrc: m.groups.eSrc.trim(), block: m[0],
            };
        }
    }
    const cleanOnly = (text || '').replace(
        /<<<<<<< [^\n]*\n[^\n]*\n=======\n[^\n]*\n>>>>>>> [^\n]*/g, ''
    );
    for (const line of cleanOnly.split('\n')) {
        const parsed = _parseReqLine(line);
        if (parsed && !(parsed.key in pkgMap)) {
            pkgMap[parsed.key] = { type: 'clean', spec: parsed.raw, name: parsed.name, block: null };
        }
    }
    return pkgMap;
}

function mergeRequirements(existingText, incomingText, repoUrl, repoBranch) {
    const sourceLabel = _formatSource(repoUrl, repoBranch);
    const pkgMap = _buildPkgMap(existingText);
    let newText = existingText || '';
    const added = [];
    const conflicts = [];

    for (const line of (incomingText || '').split('\n')) {
        const parsed = _parseReqLine(line);
        if (!parsed) continue;
        const { name, raw, key } = parsed;

        if (!(key in pkgMap)) {
            added.push(raw);
            pkgMap[key] = { type: 'clean', spec: raw, name, block: null };
        } else if (pkgMap[key].spec === raw) {
            // exact duplicate — skip
        } else if (pkgMap[key].type === 'conflict') {
            const oldBlock = pkgMap[key].block;
            const existingSrc = pkgMap[key].existingSrc;
            const existingSpec = pkgMap[key].spec;
            const newBlock = `<<<<<<< ${existingSrc}\n${existingSpec}\n=======\n${raw}\n>>>>>>> ${sourceLabel}`;
            newText = newText.replace(oldBlock, newBlock);
            pkgMap[key].block = newBlock;
            conflicts.push({ name, existing: existingSpec, existingSource: existingSrc, incoming: raw, incomingSource: sourceLabel });
        } else {
            const existingSpec = pkgMap[key].spec;
            const newBlock = `<<<<<<< existing\n${existingSpec}\n=======\n${raw}\n>>>>>>> ${sourceLabel}`;
            newText = newText.replace(
                new RegExp('^' + _escapeRegex(existingSpec) + '$', 'm'), newBlock
            );
            pkgMap[key] = { type: 'conflict', spec: existingSpec, name, existingSrc: 'existing', block: newBlock };
            conflicts.push({ name, existing: existingSpec, existingSource: 'existing', incoming: raw, incomingSource: sourceLabel });
        }
    }

    if (added.length) {
        const base = newText.trimEnd();
        newText = base + (base ? '\n' : '') + added.join('\n');
    }

    return { content: newText, conflicts };
}

function applyPipResolutions(text, choices) {
    return (text || '').replace(
        /<<<<<<< [^\n]*\n([^\n]*)\n=======\n[^\n]*\n>>>>>>> [^\n]*/g,
        (block, existingLine) => {
            const parsed = _parseReqLine(existingLine.trim());
            return (parsed && parsed.name in choices) ? choices[parsed.name] : block;
        }
    );
}

// ═══════════════════════════════════════════════════════════════════════════
// Tests
// ═══════════════════════════════════════════════════════════════════════════

describe("InstanceDetail — ODOO_VERSIONS constant", () => {
    test("is an array of strings", () => {
        expect(Array.isArray(ODOO_VERSIONS)).toBe(true);
        for (const v of ODOO_VERSIONS) {
            expect(typeof v).toBe("string");
        }
    });

    test("contains common versions", () => {
        expect(ODOO_VERSIONS).toInclude("14.0");
        expect(ODOO_VERSIONS).toInclude("17.0");
        expect(ODOO_VERSIONS).toInclude("19.0");
    });

    test("starts at 7.0 and ends at 19.0", () => {
        expect(ODOO_VERSIONS[0]).toBe("7.0");
        expect(ODOO_VERSIONS[ODOO_VERSIONS.length - 1]).toBe("19.0");
    });

    test("has 13 entries", () => {
        expect(ODOO_VERSIONS.length).toBe(13);
    });
});

describe("InstanceDetail — PG_VERSIONS constant", () => {
    test("is an array of strings", () => {
        for (const v of PG_VERSIONS) {
            expect(typeof v).toBe("string");
        }
    });

    test("contains versions 14 through 18", () => {
        expect(PG_VERSIONS).toEqual(["14", "15", "16", "17", "18"]);
    });
});

describe("InstanceDetail — timeAgo", () => {
    test("returns empty string for falsy input", () => {
        expect(timeAgo(null)).toBe("");
        expect(timeAgo("")).toBe("");
        expect(timeAgo(undefined)).toBe("");
    });

    test("returns 'just now' for a date within the last minute", () => {
        const now = new Date();
        expect(timeAgo(now.toISOString())).toBe("just now");
    });

    test("returns minutes ago", () => {
        const d = new Date(Date.now() - 5 * 60 * 1000);
        expect(timeAgo(d.toISOString())).toBe("5m ago");
    });

    test("returns hours ago", () => {
        const d = new Date(Date.now() - 3 * 60 * 60 * 1000);
        expect(timeAgo(d.toISOString())).toBe("3h ago");
    });

    test("returns days ago", () => {
        const d = new Date(Date.now() - 2 * 24 * 60 * 60 * 1000);
        expect(timeAgo(d.toISOString())).toBe("2d ago");
    });
});

describe("InstanceDetail — formatDate", () => {
    test("returns empty string for falsy input", () => {
        expect(formatDate(null)).toBe("");
        expect(formatDate("")).toBe("");
    });

    test("formats a known date correctly", () => {
        // formatDate uses local time via getHours/getMinutes — pass a
        // string and verify structure, not exact hour (timezone-dependent).
        const result = formatDate("2025-01-05T09:07:00Z");
        expect(result).toMatch(/^2025-01-05 \d{2}:\d{2}$/);
    });

    test("pads single-digit months and days", () => {
        const result = formatDate("2024-03-03T04:05:00Z");
        expect(result).toMatch(/^2024-03-03 \d{2}:\d{2}$/);
    });
});

describe("InstanceDetail — isActiveJob", () => {
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

    test("blocked is not active", () => {
        expect(isActiveJob("blocked")).toBe(false);
    });
});

describe("InstanceDetail — jobIcon mapping", () => {
    test("deploy_instance maps to fa-rocket", () => {
        expect(jobIcon("deploy_instance")).toBe("fa-rocket");
    });

    test("rebuild_instance maps to fa-wrench", () => {
        expect(jobIcon("rebuild_instance")).toBe("fa-wrench");
    });

    test("restart_instance maps to fa-refresh", () => {
        expect(jobIcon("restart_instance")).toBe("fa-refresh");
    });

    test("stop_instance maps to fa-stop", () => {
        expect(jobIcon("stop_instance")).toBe("fa-stop");
    });

    test("start_instance maps to fa-play", () => {
        expect(jobIcon("start_instance")).toBe("fa-play");
    });

    test("export_instance maps to fa-download", () => {
        expect(jobIcon("export_instance")).toBe("fa-download");
    });

    test("delete_instance maps to fa-trash-o", () => {
        expect(jobIcon("delete_instance")).toBe("fa-trash-o");
    });

    test("restore_instance maps to fa-database", () => {
        expect(jobIcon("restore_instance")).toBe("fa-database");
    });

    test("unknown code falls back to fa-cog", () => {
        expect(jobIcon("something_else")).toBe("fa-cog");
        expect(jobIcon("")).toBe("fa-cog");
    });
});

describe("InstanceDetail — jobStateClass mapping", () => {
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
        expect(jobStateClass("mystery")).toBe("jst-pending");
    });
});

describe("InstanceDetail — envLabel mapping", () => {
    test("runbot maps to Runbot", () => {
        expect(envLabel("runbot")).toBe("Runbot");
    });

    test("staging maps to Staging", () => {
        expect(envLabel("staging")).toBe("Staging");
    });

    test("production maps to Production", () => {
        expect(envLabel("production")).toBe("Production");
    });

    test("unknown env returns the raw string", () => {
        expect(envLabel("dev")).toBe("dev");
        expect(envLabel("custom")).toBe("custom");
    });
});

describe("InstanceDetail — statusLabel getter logic", () => {
    test("returns empty string when inst is null", () => {
        expect(statusLabel(null)).toBe("");
    });

    test("returns 'Building...' when status is provisioning", () => {
        expect(statusLabel({ status: "provisioning", deployed: false, running: false })).toBe("Building...");
    });

    test("returns 'Not deployed' when deployed is false", () => {
        expect(statusLabel({ status: "ok", deployed: false, running: false })).toBe("Not deployed");
    });

    test("returns 'Running' when running is true", () => {
        expect(statusLabel({ status: "ok", deployed: true, running: true })).toBe("Running");
    });

    test("returns 'Error' when status is error", () => {
        expect(statusLabel({ status: "error", deployed: true, running: false })).toBe("Error");
    });

    test("returns 'Stopped' as fallback for deployed, non-running instance", () => {
        expect(statusLabel({ status: "ok", deployed: true, running: false })).toBe("Stopped");
    });
});

describe("InstanceDetail — statusClass getter logic", () => {
    test("returns empty string when inst is null", () => {
        expect(statusClass(null)).toBe("");
    });

    test("returns st-provisioning when status is provisioning", () => {
        expect(statusClass({ status: "provisioning", deployed: false, running: false })).toBe("st-provisioning");
    });

    test("returns st-pending when not deployed", () => {
        expect(statusClass({ status: "ok", deployed: false, running: false })).toBe("st-pending");
    });

    test("returns st-running when running", () => {
        expect(statusClass({ status: "ok", deployed: true, running: true })).toBe("st-running");
    });

    test("returns st-error when status is error", () => {
        expect(statusClass({ status: "error", deployed: true, running: false })).toBe("st-error");
    });

    test("returns st-stopped as fallback", () => {
        expect(statusClass({ status: "ok", deployed: true, running: false })).toBe("st-stopped");
    });
});

describe("InstanceDetail — hasUnsavedChanges concept", () => {
    test("detects changes via JSON.stringify comparison", () => {
        const original = { name: "test", host_id: 1 };
        const savedForm = JSON.stringify(original);

        // No change
        expect(JSON.stringify(original) !== savedForm).toBe(false);

        // With change
        const modified = { ...original, name: "changed" };
        expect(JSON.stringify(modified) !== savedForm).toBe(true);
    });

    test("null savedForm means no unsaved changes detected", () => {
        const savedForm = null;
        const form = { name: "test" };
        // The getter: this._savedForm && JSON.stringify(this.state.form) !== this._savedForm
        const result = savedForm && JSON.stringify(form) !== savedForm;
        expect(result).toBe(null); // falsy — no unsaved changes
    });
});

// ── Pip requirements helpers ────────────────────────────────────────────────

describe("InstanceDetail — _parseReqLine", () => {
    test("parses simple package name", () => {
        const r = _parseReqLine("requests==2.28.0");
        expect(r.name).toBe("requests");
        expect(r.raw).toBe("requests==2.28.0");
        expect(r.key).toBe("requests");
    });

    test("ignores comments", () => {
        const r = _parseReqLine("flask>=2.0  # web framework");
        expect(r.name).toBe("flask");
        expect(r.raw).toBe("flask>=2.0");
    });

    test("returns null for blank lines", () => {
        expect(_parseReqLine("")).toBe(null);
        expect(_parseReqLine("   ")).toBe(null);
        expect(_parseReqLine("# just a comment")).toBe(null);
    });

    test("handles environment markers", () => {
        const r = _parseReqLine("pywin32>=300; sys_platform == 'win32'");
        expect(r.name).toBe("pywin32");
        expect(r.key).toBe("pywin32|sys_platform == 'win32'");
    });

    test("normalises name to lowercase", () => {
        const r = _parseReqLine("PyYAML>=6.0");
        expect(r.name).toBe("pyyaml");
    });
});

describe("InstanceDetail — _formatSource", () => {
    test("extracts GitHub org/repo from HTTPS URL", () => {
        expect(_formatSource("https://github.com/odoo/odoo.git", "17.0"))
            .toBe("odoo/odoo (17.0)");
    });

    test("extracts GitHub org/repo from SSH URL", () => {
        expect(_formatSource("git@github.com:odoo/enterprise.git", "main"))
            .toBe("odoo/enterprise (main)");
    });

    test("handles URL without branch", () => {
        expect(_formatSource("https://github.com/odoo/odoo.git", ""))
            .toBe("odoo/odoo");
    });

    test("handles non-GitHub URL", () => {
        expect(_formatSource("https://gitlab.com/foo/bar/", "dev"))
            .toBe("https://gitlab.com/foo/bar (dev)");
    });
});

describe("InstanceDetail — _escapeRegex", () => {
    test("escapes special regex characters", () => {
        expect(_escapeRegex("a.b+c")).toBe("a\\.b\\+c");
        expect(_escapeRegex("foo[0]")).toBe("foo\\[0\\]");
    });

    test("leaves normal strings unchanged", () => {
        expect(_escapeRegex("requests")).toBe("requests");
    });
});

describe("InstanceDetail — mergeRequirements", () => {
    test("adds new packages to empty text", () => {
        const { content, conflicts } = mergeRequirements("", "flask>=2.0\nrequests", "https://github.com/a/b.git", "main");
        expect(content).toInclude("flask>=2.0");
        expect(content).toInclude("requests");
        expect(conflicts.length).toBe(0);
    });

    test("skips exact duplicates", () => {
        const { content, conflicts } = mergeRequirements("flask>=2.0", "flask>=2.0", "https://github.com/a/b.git", "main");
        expect(content).toBe("flask>=2.0");
        expect(conflicts.length).toBe(0);
    });

    test("creates conflict markers for version mismatch", () => {
        const { content, conflicts } = mergeRequirements(
            "flask>=2.0",
            "flask>=3.0",
            "https://github.com/a/b.git", "main"
        );
        expect(content).toInclude("<<<<<<<");
        expect(content).toInclude("flask>=2.0");
        expect(content).toInclude("flask>=3.0");
        expect(content).toInclude(">>>>>>>");
        expect(conflicts.length).toBe(1);
        expect(conflicts[0].name).toBe("flask");
    });

    test("appends genuinely new packages", () => {
        const { content, conflicts } = mergeRequirements(
            "flask>=2.0",
            "celery>=5.0",
            "https://github.com/a/b.git", "main"
        );
        expect(content).toInclude("flask>=2.0");
        expect(content).toInclude("celery>=5.0");
        expect(conflicts.length).toBe(0);
    });
});

describe("InstanceDetail — applyPipResolutions", () => {
    test("resolves conflict markers with chosen spec", () => {
        const text = "<<<<<<< existing\nflask>=2.0\n=======\nflask>=3.0\n>>>>>>> a/b (main)";
        const result = applyPipResolutions(text, { flask: "flask>=3.0" });
        expect(result).toBe("flask>=3.0");
    });

    test("leaves unmatched conflicts untouched", () => {
        const text = "<<<<<<< existing\nflask>=2.0\n=======\nflask>=3.0\n>>>>>>> a/b (main)";
        const result = applyPipResolutions(text, { celery: "celery>=5.0" });
        expect(result).toInclude("<<<<<<<");
    });

    test("handles empty text", () => {
        expect(applyPipResolutions("", {})).toBe("");
        expect(applyPipResolutions(null, {})).toBe("");
    });
});
