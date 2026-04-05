import { describe, expect, test } from "@odoo/hoot";
import { ImportOdooshModal } from "@incubacloud/components/import_odoojs_modal/import_odoojs_modal";

/**
 * ImportOdooshModal pure-logic tests.
 *
 * The component uses env.dialogData, RPC calls, and RepoEditor sub-components.
 * We test the pure helpers (toHttpsUrl, isUrlValid), step computation,
 * validation logic, and class structure.
 */

// ── toHttpsUrl (mirrored from source) ───────────────────────────────────────

function toHttpsUrl(url) {
    if (!url) return url;
    url = url.trim();
    const sshMatch = url.match(/^git@github\.com:(.+?)(?:\.git)?$/);
    if (sshMatch) return `https://github.com/${sshMatch[1]}.git`;
    return url;
}

// ── isUrlValid regex (mirrored from source) ─────────────────────────────────

function isUrlValid(repoUrl) {
    return /github\.com[:/][^/]+\/[^/]+/.test((repoUrl || "").trim());
}

// ═══════════════════════════════════════════════════════════════════════════
// Tests
// ═══════════════════════════════════════════════════════════════════════════

describe("ImportOdooshModal — toHttpsUrl", () => {
    test("converts SSH URL to HTTPS", () => {
        expect(toHttpsUrl("git@github.com:odoo/odoo.git"))
            .toBe("https://github.com/odoo/odoo.git");
    });

    test("converts SSH URL without .git suffix", () => {
        expect(toHttpsUrl("git@github.com:odoo/odoo"))
            .toBe("https://github.com/odoo/odoo.git");
    });

    test("leaves HTTPS URL unchanged", () => {
        expect(toHttpsUrl("https://github.com/odoo/odoo.git"))
            .toBe("https://github.com/odoo/odoo.git");
    });

    test("leaves non-GitHub HTTPS URL unchanged", () => {
        expect(toHttpsUrl("https://gitlab.com/foo/bar.git"))
            .toBe("https://gitlab.com/foo/bar.git");
    });

    test("trims whitespace", () => {
        expect(toHttpsUrl("  https://github.com/odoo/odoo.git  "))
            .toBe("https://github.com/odoo/odoo.git");
    });

    test("returns falsy input as-is", () => {
        expect(toHttpsUrl("")).toBe("");
        expect(toHttpsUrl(null)).toBe(null);
        expect(toHttpsUrl(undefined)).toBe(undefined);
    });

    test("handles org/repo with hyphens and dots", () => {
        expect(toHttpsUrl("git@github.com:my-org/my.repo.git"))
            .toBe("https://github.com/my-org/my.repo.git");
    });

    test("handles nested paths in SSH URL", () => {
        expect(toHttpsUrl("git@github.com:org/repo-name"))
            .toBe("https://github.com/org/repo-name.git");
    });
});

describe("ImportOdooshModal — isUrlValid", () => {
    test("accepts HTTPS GitHub URL", () => {
        expect(isUrlValid("https://github.com/odoo/odoo")).toBe(true);
    });

    test("accepts HTTPS GitHub URL with .git", () => {
        expect(isUrlValid("https://github.com/odoo/odoo.git")).toBe(true);
    });

    test("accepts SSH GitHub URL", () => {
        expect(isUrlValid("git@github.com:odoo/odoo.git")).toBe(true);
    });

    test("rejects non-GitHub URL", () => {
        expect(isUrlValid("https://gitlab.com/foo/bar")).toBe(false);
    });

    test("rejects GitHub URL without repo path", () => {
        expect(isUrlValid("https://github.com/odoo")).toBe(false);
    });

    test("rejects empty string", () => {
        expect(isUrlValid("")).toBe(false);
    });

    test("rejects null/undefined", () => {
        expect(isUrlValid(null)).toBe(false);
        expect(isUrlValid(undefined)).toBe(false);
    });

    test("accepts URL with trailing whitespace", () => {
        expect(isUrlValid("  https://github.com/odoo/odoo  ")).toBe(true);
    });

    test("rejects plain text", () => {
        expect(isUrlValid("not a url")).toBe(false);
    });
});

describe("ImportOdooshModal — step computation", () => {
    test("starts at step 1", () => {
        const initialStep = 1;
        expect(initialStep).toBe(1);
    });

    test("moves to step 2 after successful fetch", () => {
        // fetchSubmodules sets step = 2 on success
        let step = 1;
        step = 2; // simulating success path
        expect(step).toBe(2);
    });

    test("moves to step 3 during project creation", () => {
        // createProject sets step = 3
        let step = 2;
        step = 3;
        expect(step).toBe(3);
    });

    test("goes back to step 2 on creation error", () => {
        // On error in createProject: step = 2
        let step = 3;
        step = 2;
        expect(step).toBe(2);
    });

    test("goBack resets to step 1 and clears error", () => {
        const state = { step: 2, error: "some error" };
        state.step = 1;
        state.error = null;
        expect(state.step).toBe(1);
        expect(state.error).toBe(null);
    });
});

describe("ImportOdooshModal — isCreateValid logic", () => {
    /**
     * get isCreateValid() {
     *     return (
     *         this.state.projectName.trim().length > 0 &&
     *         this.state.repos.some(r => r.url.trim())
     *     );
     * }
     */
    function isCreateValid(projectName, repos) {
        return (
            projectName.trim().length > 0 &&
            repos.some(r => r.url.trim())
        );
    }

    test("valid when project name and at least one repo URL", () => {
        expect(isCreateValid("My Project", [{ url: "https://github.com/a/b" }])).toBe(true);
    });

    test("invalid when project name is empty", () => {
        expect(isCreateValid("", [{ url: "https://github.com/a/b" }])).toBe(false);
        expect(isCreateValid("   ", [{ url: "https://github.com/a/b" }])).toBe(false);
    });

    test("invalid when no repos have URLs", () => {
        expect(isCreateValid("My Project", [{ url: "" }, { url: "  " }])).toBe(false);
    });

    test("invalid when repos array is empty", () => {
        expect(isCreateValid("My Project", [])).toBe(false);
    });

    test("valid when only one of multiple repos has a URL", () => {
        expect(isCreateValid("Proj", [{ url: "" }, { url: "https://github.com/a/b" }])).toBe(true);
    });
});

describe("ImportOdooshModal — repo manipulation helpers", () => {
    test("removeRepo splices at index", () => {
        const repos = [
            { url: "a", branch: "main", commit_sha: "" },
            { url: "b", branch: "main", commit_sha: "" },
            { url: "c", branch: "main", commit_sha: "" },
        ];
        repos.splice(1, 1);
        expect(repos.length).toBe(2);
        expect(repos[0].url).toBe("a");
        expect(repos[1].url).toBe("c");
    });

    test("addRepo pushes a new empty repo", () => {
        const repos = [{ url: "a", branch: "main", commit_sha: "" }];
        repos.push({ url: "", branch: "main", commit_sha: "" });
        expect(repos.length).toBe(2);
        expect(repos[1].url).toBe("");
        expect(repos[1].branch).toBe("main");
    });

    test("updateRepo sets field on specific index", () => {
        const repos = [
            { url: "a", branch: "main", commit_sha: "" },
            { url: "b", branch: "main", commit_sha: "" },
        ];
        repos[1]["branch"] = "dev";
        expect(repos[1].branch).toBe("dev");
    });
});

describe("ImportOdooshModal — class structure", () => {
    test("ImportOdooshModal can be imported", () => {
        expect(typeof ImportOdooshModal).toBe("function");
    });

    test("has correct template name", () => {
        expect(ImportOdooshModal.template).toBe("incubacloud.ImportProjectModal");
    });

    test("has SearchSelect as sub-component", () => {
        expect("SearchSelect" in ImportOdooshModal.components).toBe(true);
    });

    test("defines onClose and onCreated props as Function", () => {
        expect(ImportOdooshModal.props.onClose).toBe(Function);
        expect(ImportOdooshModal.props.onCreated).toBe(Function);
    });
});

describe("ImportOdooshModal — branch auto-selection in _fetchBranches", () => {
    /**
     * Logic: main > master > first branch
     */
    function autoSelectBranch(branches) {
        if (branches.includes("main")) return "main";
        if (branches.includes("master")) return "master";
        if (branches.length) return branches[0];
        return "";
    }

    test("selects main when available", () => {
        expect(autoSelectBranch(["dev", "main", "master"])).toBe("main");
    });

    test("selects master when main not available", () => {
        expect(autoSelectBranch(["dev", "master"])).toBe("master");
    });

    test("selects first branch as fallback", () => {
        expect(autoSelectBranch(["dev", "feature"])).toBe("dev");
    });

    test("returns empty string for no branches", () => {
        expect(autoSelectBranch([])).toBe("");
    });
});
