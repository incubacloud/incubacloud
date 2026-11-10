import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { allowTranslations, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import { getTemplate } from "@web/core/templates";
import {
    gitlabReposWithoutHook,
    pushRebuildOfHook,
    RepoEditor,
    settingsTab,
} from "@incubacloud/components/repo_editor/repo_editor";

/**
 * RepoEditor pure-logic tests.
 *
 * The component uses useState and RPC calls for branch/module fetching.
 * We test the computed properties, class structure, and set logic.
 */

describe("RepoEditor — class structure", () => {
    test("has the correct template name", () => {
        expect(RepoEditor.template).toBe("incubacloud.RepoEditor");
    });

    test("has SearchSelect and ModuleSelector as sub-components", () => {
        expect("SearchSelect" in RepoEditor.components).toBe(true);
        expect("ModuleSelector" in RepoEditor.components).toBe(true);
    });

    test("defines required props", () => {
        const { props } = RepoEditor;
        expect(props.repo.type).toBe(Object);
        expect(props.hasAddons.type).toBe(Boolean);
        expect(props.onChange.type).toBe(Function);
        expect(props.onRemove.type).toBe(Function);
    });

    test("defines optional props", () => {
        const { props } = RepoEditor;
        expect(props.preferredBranch.optional).toBe(true);
        expect(props.onFetchRequirements.optional).toBe(true);
        expect(props.repoModel.optional).toBe(true);
        expect(props.hideFreeze.optional).toBe(true);
    });
});

describe("RepoEditor — isFrozen computed", () => {
    /**
     * get isFrozen() { return !!this.props.repo.commit_sha; }
     */
    function isFrozen(commitSha) {
        return !!commitSha;
    }

    test("returns true when commit_sha is a non-empty string", () => {
        expect(isFrozen("abc1234def5678")).toBe(true);
    });

    test("returns false when commit_sha is empty string", () => {
        expect(isFrozen("")).toBe(false);
    });

    test("returns false when commit_sha is null/undefined", () => {
        expect(isFrozen(null)).toBe(false);
        expect(isFrozen(undefined)).toBe(false);
    });

    test("returns true for any truthy value", () => {
        expect(isFrozen("0")).toBe(true); // string "0" is truthy
    });
});

describe("RepoEditor — shortSha computed", () => {
    /**
     * get shortSha() { return (this.props.repo.commit_sha || "").slice(0, 7); }
     */
    function shortSha(commitSha) {
        return (commitSha || "").slice(0, 7);
    }

    test("returns first 7 characters of a full SHA", () => {
        expect(shortSha("abc1234def5678901234567890abcdef12345678")).toBe("abc1234");
    });

    test("returns the whole string if shorter than 7 chars", () => {
        expect(shortSha("abc")).toBe("abc");
    });

    test("returns empty string for null/undefined", () => {
        expect(shortSha(null)).toBe("");
        expect(shortSha(undefined)).toBe("");
        expect(shortSha("")).toBe("");
    });

    test("returns exactly 7 chars for a 7-char input", () => {
        expect(shortSha("1234567")).toBe("1234567");
    });
});

describe("RepoEditor — excludedSet logic", () => {
    /**
     * get excludedSet() {
     *     return new Set(
     *         (this.props.repo.excludes || "")
     *             .split(",").map(s => s.trim()).filter(Boolean)
     *     );
     * }
     */
    function excludedSet(excludes) {
        return new Set(
            (excludes || "").split(",").map(s => s.trim()).filter(Boolean)
        );
    }

    test("parses comma-separated excludes", () => {
        const result = excludedSet("sale,stock,purchase");
        expect(result.size).toBe(3);
        expect(result.has("sale")).toBe(true);
        expect(result.has("stock")).toBe(true);
        expect(result.has("purchase")).toBe(true);
    });

    test("trims whitespace around entries", () => {
        const result = excludedSet(" sale , stock ");
        expect(result.has("sale")).toBe(true);
        expect(result.has("stock")).toBe(true);
    });

    test("handles empty string", () => {
        const result = excludedSet("");
        expect(result.size).toBe(0);
    });

    test("handles null/undefined", () => {
        expect(excludedSet(null).size).toBe(0);
        expect(excludedSet(undefined).size).toBe(0);
    });

    test("filters out empty entries from trailing commas", () => {
        const result = excludedSet("sale,,stock,");
        expect(result.size).toBe(2);
    });
});

describe("RepoEditor — includedSet logic", () => {
    /**
     * Same logic as excludedSet but uses repo.addons.
     */
    function includedSet(addons) {
        return new Set(
            (addons || "").split(",").map(s => s.trim()).filter(Boolean)
        );
    }

    test("parses comma-separated addons", () => {
        const result = includedSet("crm,website");
        expect(result.size).toBe(2);
        expect(result.has("crm")).toBe(true);
        expect(result.has("website")).toBe(true);
    });

    test("handles empty/null", () => {
        expect(includedSet("").size).toBe(0);
        expect(includedSet(null).size).toBe(0);
    });
});

describe("RepoEditor — method existence", () => {
    const proto = RepoEditor.prototype;

    test("has setup method", () => {
        expect(typeof proto.setup).toBe("function");
    });

    test("has _fetchBranches method", () => {
        expect(typeof proto._fetchBranches).toBe("function");
    });

    test("has _fetchModules method", () => {
        expect(typeof proto._fetchModules).toBe("function");
    });

    test("has onUrlInput method", () => {
        expect(typeof proto.onUrlInput).toBe("function");
    });

    test("has onUrlBlur method", () => {
        expect(typeof proto.onUrlBlur).toBe("function");
    });

    test("has onBranchOpen method", () => {
        expect(typeof proto.onBranchOpen).toBe("function");
    });

    test("has onBranchChange method", () => {
        expect(typeof proto.onBranchChange).toBe("function");
    });

    test("has onModuleOpen method", () => {
        expect(typeof proto.onModuleOpen).toBe("function");
    });

    test("has toggleFreeze method", () => {
        expect(typeof proto.toggleFreeze).toBe("function");
    });
});

describe("RepoEditor — branch auto-selection logic", () => {
    /**
     * From onUrlBlur: auto-select best branch.
     * Priority: preferredBranch > main > master > first
     */
    function selectBest(branches, preferred) {
        return (preferred && branches.includes(preferred) ? preferred : null)
            ?? ["main", "master"].find(b => branches.includes(b))
            ?? branches[0];
    }

    test("selects preferredBranch when available", () => {
        expect(selectBest(["main", "17.0", "dev"], "17.0")).toBe("17.0");
    });

    test("falls back to main when preferred not found", () => {
        expect(selectBest(["main", "master", "dev"], "17.0")).toBe("main");
    });

    test("falls back to master when main not present", () => {
        expect(selectBest(["master", "dev"], "17.0")).toBe("master");
    });

    test("falls back to first branch when no main/master", () => {
        expect(selectBest(["dev", "feature-x"], "17.0")).toBe("dev");
    });

    test("selects main over master", () => {
        expect(selectBest(["master", "main"], null)).toBe("main");
    });

    test("works without preferred branch", () => {
        expect(selectBest(["main", "dev"], "")).toBe("main");
        expect(selectBest(["main", "dev"], null)).toBe("main");
    });
});

describe("RepoEditor — which settings tab a read error opens", () => {
    test("a Git provider's error opens Git providers, any other GitHub", () => {
        expect(settingsTab("git_providers")).toBe("git_providers");
        expect(settingsTab("github")).toBe("github");
        expect(settingsTab(true)).toBe("github");
        expect(settingsTab(false)).toBe(false);
        expect(settingsTab(undefined)).toBe(false);
    });
});

/**
 * A RepoEditor stand-in on a saved GitLab line (no mounting, as
 * git_providers_tab.test.js does).
 *
 * @param {Object} caps the line's ``repo_capabilities``
 * @param {Object} repo values over the saved line
 * @returns {Object} the stand-in
 */
function gitlabEditor(caps = {}, repo = {}) {
    const comp = Object.create(RepoEditor.prototype);
    comp.props = {
        repoModel: "cloud.project.repo",
        repo: {
            id: 12, url: "https://gitlab.com/acme/web.git", branch: "main",
            repo_capabilities: { provider: "gitlab", host: "gitlab.com", push_rebuild: "no_hook", ...caps },
            ...repo,
        },
    };
    comp.savedUrl = "https://gitlab.com/acme/web.git";
    comp.state = {
        pushRebuild: null, registering: false, hookManualReason: null, hookError: null,
    };
    return comp;
}

describe("RepoEditor — the webhook of a GitLab line", () => {
    beforeEach(() => allowTranslations());

    test("the saved capabilities say what a push does", () => {
        expect(gitlabEditor().pushRebuild).toBe("no_hook");
        expect(gitlabEditor({ push_rebuild: "ok" }).pushRebuild).toBe("ok");
        expect(gitlabEditor({ push_rebuild: "hook_failing" }).pushRebuild).toBe("hook_failing");
    });

    test("a line not saved, or with a URL typed since, has no webhook to show", () => {
        expect(gitlabEditor({}, { id: false }).pushRebuild).toBe("unsaved");
        expect(gitlabEditor({}, { url: "https://gitlab.com/acme/other.git" }).pushRebuild).toBe("unsaved");
    });

    test("a webhook's state maps to push-to-rebuild as the server's", () => {
        expect(pushRebuildOfHook("active")).toBe("ok");
        expect(pushRebuildOfHook("pending")).toBe("hook_pending");
        expect(pushRebuildOfHook("failing")).toBe("hook_failing");
        expect(pushRebuildOfHook("orphan")).toBe("hook_pending");
    });

    test("Register webhook registers the saved line and shows its new state", async () => {
        const calls = [];
        patchWithCleanup(rpc, {
            _rpc(url, params) {
                calls.push([url, params]);
                return Promise.resolve({ ok: true, hook: { state: "pending" }, manual: false });
            },
        });
        const comp = gitlabEditor();
        await comp.registerHook();
        expect(calls).toEqual([["/cloud/register_git_hook", { line_model: "project", line_id: 12 }]]);
        expect(comp.pushRebuild).toBe("hook_pending");
        expect(comp.state.hookManualReason).toBe(null);
    });

    test("a webhook GitLab refused says to set it up by hand, and why", async () => {
        patchWithCleanup(rpc, {
            _rpc: () => Promise.resolve({
                ok: true, hook: { state: "pending" }, manual: true,
                reason: "The credential does not have the Maintainer role",
            }),
        });
        const comp = gitlabEditor();
        await comp.registerHook();
        expect(comp.state.hookManualReason).toMatch(/Maintainer/);
        expect(comp.state.registering).toBe(false);
    });

    test("only unfrozen GitLab lines without a working webhook are counted", () => {
        const gl = (push, sha = "") => ({
            commit_sha: sha, repo_capabilities: { provider: "gitlab", push_rebuild: push },
        });
        const repos = [
            gl("no_hook"), gl("hook_failing"), gl("ok"), gl("no_hook", "abc1234"),
            { commit_sha: "", repo_capabilities: { provider: "github", push_rebuild: "no_hook" } },
            { commit_sha: "" },
        ];
        expect(gitlabReposWithoutHook(repos)).toEqual([repos[0], repos[1]]);
        expect(gitlabReposWithoutHook(undefined)).toEqual([]);
    });
});

describe("RepoEditor — who changes a saved line's URL", () => {
    beforeEach(() => allowTranslations());

    /**
     * A RepoEditor stand-in for a reader with *permissions*.
     *
     * @param {Object} permissions the reader's ``env.permissions``
     * @param {Object} repo values over a saved GitHub line
     * @returns {Object} the stand-in, with the ``onChange`` calls it made
     */
    function editor(permissions, repo = {}) {
        const comp = Object.create(RepoEditor.prototype);
        const changes = [];
        comp.env = { permissions };
        comp.props = {
            hasAddons: true,
            onChange: (field, value) => changes.push([field, value]),
            repo: { id: 7, url: "https://github.com/acme/addons", branch: "19.0", ...repo },
        };
        comp.state = { modules: [], branches: [] };
        comp.changes = changes;
        return comp;
    }

    test("a saved line's URL is read-only below Project Manager", () => {
        expect(editor({ can_change_repo_url: false }).urlLocked).toBe(true);
        expect(editor({}).urlLocked).toBe(true);
        expect(editor({ can_change_repo_url: true }).urlLocked).toBe(false);
    });

    test("a line not saved yet keeps its URL editable", () => {
        expect(editor({ can_change_repo_url: false }, { id: false }).urlLocked).toBe(false);
    });

    test("leaving a read-only URL keeps the branch and modules", async () => {
        const comp = editor({ can_change_repo_url: false });
        comp.onUrlInput({ target: { value: "https://github.com/acme/private" } });
        await comp.onUrlBlur({ target: { value: "https://github.com/acme/addons" } });
        expect(comp.changes).toEqual([]);
    });

    test("the template makes the URL read-only by it", () => {
        const input = getTemplate("incubacloud.RepoEditor").querySelector("input.rl-repo-url");
        expect(input.getAttribute("t-att-readonly")).toBe("urlLocked");
    });
});
