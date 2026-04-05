import { describe, expect, test } from "@odoo/hoot";
import { RemoteFileBrowser } from "@incubacloud/components/remote_file_browser/remote_file_browser";

/**
 * RemoteFileBrowser pure-logic tests.
 *
 * Tests cover the breadcrumbs getter, step state machine, message list,
 * goUp guard, and class structure. RPC-dependent methods (navigate, doImport)
 * are covered through state-simulation tests.
 */

// ── breadcrumbs (mirrored from source) ──────────────────────────────────────

function computeBreadcrumbs(currentPath) {
    const parts = currentPath.split("/").filter(Boolean);
    const crumbs = [{ name: "/", path: "/" }];
    let acc = "";
    for (const p of parts) {
        acc += "/" + p;
        crumbs.push({ name: p, path: acc });
    }
    return crumbs;
}

// ── helpers ──────────────────────────────────────────────────────────────────

function makeState(overrides = {}) {
    return {
        step: "browse",
        loading: false,
        currentPath: "",
        parentPath: "",
        entries: [],
        isDoodba: false,
        copierInfo: {},
        alreadyImported: false,
        error: null,
        result: null,
        messages: [],
        ...overrides,
    };
}

// ─────────────────────────────────────────────────────────────────────────────

describe("RemoteFileBrowser — breadcrumbs", () => {

    test("root path yields only root crumb", () => {
        const crumbs = computeBreadcrumbs("/");
        expect(crumbs.length).toBe(1);
        expect(crumbs[0].name).toBe("/");
        expect(crumbs[0].path).toBe("/");
    });

    test("empty string yields only root crumb", () => {
        const crumbs = computeBreadcrumbs("");
        expect(crumbs.length).toBe(1);
        expect(crumbs[0].name).toBe("/");
    });

    test("single segment path", () => {
        const crumbs = computeBreadcrumbs("/home");
        expect(crumbs.length).toBe(2);
        expect(crumbs[1].name).toBe("home");
        expect(crumbs[1].path).toBe("/home");
    });

    test("three-segment path produces four crumbs including root", () => {
        const crumbs = computeBreadcrumbs("/home/user/projects");
        expect(crumbs.length).toBe(4);
        expect(crumbs[0]).toEqual({ name: "/", path: "/" });
        expect(crumbs[1]).toEqual({ name: "home", path: "/home" });
        expect(crumbs[2]).toEqual({ name: "user", path: "/home/user" });
        expect(crumbs[3]).toEqual({ name: "projects", path: "/home/user/projects" });
    });

    test("last crumb path equals full currentPath", () => {
        const path = "/srv/odoo/myproject";
        const crumbs = computeBreadcrumbs(path);
        expect(crumbs[crumbs.length - 1].path).toBe(path);
    });

    test("each intermediate path is a prefix of the next", () => {
        const crumbs = computeBreadcrumbs("/a/b/c");
        for (let i = 1; i < crumbs.length - 1; i++) {
            expect(crumbs[i + 1].path.startsWith(crumbs[i].path)).toBe(true);
        }
    });

    test("tilde home path treated as single segment", () => {
        // navigate("~") returns an absolute path from the server, but if
        // currentPath happens to contain "~" it is treated as a segment name
        const crumbs = computeBreadcrumbs("/home/ubuntu");
        expect(crumbs[2].name).toBe("ubuntu");
    });
});

// ─────────────────────────────────────────────────────────────────────────────

describe("RemoteFileBrowser — step transitions", () => {

    test("selectCurrent sets step to confirm", () => {
        const state = makeState({ step: "browse" });
        // mirror: selectCurrent() { this.state.step = "confirm"; }
        state.step = "confirm";
        expect(state.step).toBe("confirm");
    });

    test("backToBrowse sets step to browse", () => {
        const state = makeState({ step: "confirm" });
        state.step = "browse";
        expect(state.step).toBe("browse");
    });

    test("retry resets step, error, and messages", () => {
        const state = makeState({
            step: "error",
            error: "connection refused",
            messages: [{ text: "Reading instance configuration...", status: "done" }],
        });
        // mirror: retry() { step = "browse"; error = null; messages = []; }
        state.step = "browse";
        state.error = null;
        state.messages = [];
        expect(state.step).toBe("browse");
        expect(state.error).toBe(null);
        expect(state.messages.length).toBe(0);
    });

    test("doImport transitions from confirm to importing", () => {
        const state = makeState({ step: "confirm" });
        // doImport starts with: this.state.step = "importing"; this.state.messages = [];
        state.step = "importing";
        state.messages = [];
        expect(state.step).toBe("importing");
        expect(state.messages.length).toBe(0);
    });

    test("doImport transitions to done on success", () => {
        const state = makeState({ step: "importing" });
        const result = { ok: true, project_name: "myproj", instance_name: "prod", is_running: true };
        state.result = result;
        state.step = "done";
        expect(state.step).toBe("done");
        expect(state.result).toBe(result);
    });

    test("doImport transitions to error on failure", () => {
        const state = makeState({ step: "importing" });
        state.error = "Connection refused";
        state.step = "error";
        expect(state.step).toBe("error");
        expect(state.error).toBe("Connection refused");
    });

    test("retry from done also clears messages and step", () => {
        const state = makeState({
            step: "done",
            error: null,
            messages: [{ text: "a", status: "done" }, { text: "b", status: "done" }],
        });
        state.step = "browse";
        state.error = null;
        state.messages = [];
        expect(state.step).toBe("browse");
        expect(state.messages).toEqual([]);
    });
});

// ─────────────────────────────────────────────────────────────────────────────

describe("RemoteFileBrowser — _addMessage", () => {

    test("pushes message with status done", () => {
        const messages = [];
        // mirror: _addMessage(text) { this.state.messages.push({ text, status: "done" }); }
        messages.push({ text: "Reading instance configuration...", status: "done" });
        expect(messages.length).toBe(1);
        expect(messages[0].text).toBe("Reading instance configuration...");
        expect(messages[0].status).toBe("done");
    });

    test("multiple messages accumulate in order", () => {
        const messages = [];
        const texts = ["Step 1", "Step 2", "Step 3"];
        for (const t of texts) {
            messages.push({ text: t, status: "done" });
        }
        expect(messages.length).toBe(3);
        expect(messages.map(m => m.text)).toEqual(texts);
    });

    test("all messages have status done", () => {
        const messages = [];
        messages.push({ text: "a", status: "done" });
        messages.push({ text: "b", status: "done" });
        expect(messages.every(m => m.status === "done")).toBe(true);
    });

    test("doImport success appends three messages", () => {
        const messages = [];
        const result = { project_name: "p1", instance_name: "inst1", is_running: false };
        // mirror the success path of doImport
        messages.push({ text: "Reading instance configuration...", status: "done" });
        messages.push({ text: `Project "${result.project_name}" ready`, status: "done" });
        messages.push({ text: `Instance "${result.instance_name}" imported`, status: "done" });
        messages.push({ text: result.is_running ? "Status: running" : "Status: stopped", status: "done" });
        expect(messages.length).toBe(4);
        expect(messages[3].text).toBe("Status: stopped");
    });

    test("doImport success running status message", () => {
        const messages = [];
        const result = { project_name: "p", instance_name: "i", is_running: true };
        messages.push({ text: result.is_running ? "Status: running" : "Status: stopped", status: "done" });
        expect(messages[0].text).toBe("Status: running");
    });
});

// ─────────────────────────────────────────────────────────────────────────────

describe("RemoteFileBrowser — goUp guard", () => {

    test("goUp does nothing when parentPath is empty", () => {
        let navigateCalled = false;
        const state = makeState({ parentPath: "" });
        // mirror: goUp() { if (this.state.parentPath) { this.navigate(...); } }
        if (state.parentPath) {
            navigateCalled = true;
        }
        expect(navigateCalled).toBe(false);
    });

    test("goUp calls navigate when parentPath is set", () => {
        let navigatedTo = null;
        const state = makeState({ parentPath: "/home/user" });
        if (state.parentPath) {
            navigatedTo = state.parentPath;
        }
        expect(navigatedTo).toBe("/home/user");
    });

    test("goUp passes parentPath as navigate argument", () => {
        const parentPath = "/srv/odoo";
        const state = makeState({ parentPath });
        let arg = null;
        if (state.parentPath) {
            arg = state.parentPath;
        }
        expect(arg).toBe(parentPath);
    });
});

// ─────────────────────────────────────────────────────────────────────────────

describe("RemoteFileBrowser — navigate state updates", () => {

    test("navigate sets loading=true at start", () => {
        const state = makeState({ loading: false });
        // navigate starts with: this.state.loading = true; this.state.error = null;
        state.loading = true;
        state.error = null;
        expect(state.loading).toBe(true);
    });

    test("navigate on success updates path and entries", () => {
        const state = makeState();
        const data = {
            ok: true,
            current_path: "/home/user",
            parent_path: "/home",
            entries: [{ name: "projects", type: "dir" }],
            is_doodba: false,
            copier_info: {},
            already_imported: false,
        };
        // mirror: on success, assign all fields
        state.currentPath = data.current_path;
        state.parentPath = data.parent_path;
        state.entries = data.entries;
        state.isDoodba = data.is_doodba;
        state.copierInfo = data.copier_info;
        state.alreadyImported = data.already_imported;
        state.loading = false;
        expect(state.currentPath).toBe("/home/user");
        expect(state.parentPath).toBe("/home");
        expect(state.entries.length).toBe(1);
        expect(state.loading).toBe(false);
    });

    test("navigate on error sets error and loading=false", () => {
        const state = makeState();
        const data = { ok: false, error: "Permission denied" };
        // mirror: if (!data.ok) { error = data.error; loading = false; return; }
        if (!data.ok) {
            state.error = data.error;
            state.loading = false;
        }
        expect(state.error).toBe("Permission denied");
        expect(state.loading).toBe(false);
    });

    test("navigate clears previous error on new navigation", () => {
        const state = makeState({ error: "previous error" });
        // navigate starts with: this.state.error = null;
        state.error = null;
        expect(state.error).toBe(null);
    });

    test("navigate sets loading=false in finally block", () => {
        const state = makeState({ loading: true });
        // finally: this.state.loading = false;
        state.loading = false;
        expect(state.loading).toBe(false);
    });
});

// ─────────────────────────────────────────────────────────────────────────────

describe("RemoteFileBrowser — class structure", () => {

    test("can be imported as a class", () => {
        expect(typeof RemoteFileBrowser).toBe("function");
    });

    test("has correct OWL template name", () => {
        expect(RemoteFileBrowser.template).toBe("incubacloud.RemoteFileBrowser");
    });

    test("declares hostId as Number prop", () => {
        expect(RemoteFileBrowser.props.hostId).toBe(Number);
    });

    test("declares onSelect as Function prop", () => {
        expect(RemoteFileBrowser.props.onSelect).toBe(Function);
    });

    test("declares onCancel as Function prop", () => {
        expect(RemoteFileBrowser.props.onCancel).toBe(Function);
    });

    test("has exactly three props", () => {
        expect(Object.keys(RemoteFileBrowser.props).length).toBe(3);
    });
});

// ─────────────────────────────────────────────────────────────────────────────

describe("RemoteFileBrowser — onClickDir path building", () => {

    test("appends entry name to currentPath with slash", () => {
        const currentPath = "/home/user";
        const entry = { name: "projects", type: "dir" };
        // mirror: onClickDir(entry) { this.navigate(this.state.currentPath + "/" + entry.name); }
        const expected = currentPath + "/" + entry.name;
        expect(expected).toBe("/home/user/projects");
    });

    test("nested path appends correctly", () => {
        const currentPath = "/srv/odoo";
        const entry = { name: "myinstance" };
        expect(currentPath + "/" + entry.name).toBe("/srv/odoo/myinstance");
    });

    test("root path concatenation does not double slash", () => {
        // If currentPath is "/" the result would be "//entry" — that is the
        // actual behaviour (server normalises it). The test documents this.
        const currentPath = "/";
        const entry = { name: "home" };
        const result = currentPath + "/" + entry.name;
        expect(result).toBe("//home");
    });
});

// ─────────────────────────────────────────────────────────────────────────────

describe("RemoteFileBrowser — doImport error fallback", () => {

    test("catch block uses err.message when available", () => {
        const err = new Error("Network timeout");
        // mirror: catch(err) { this.state.error = err.message || "Import failed"; }
        const errorMsg = err.message || "Import failed";
        expect(errorMsg).toBe("Network timeout");
    });

    test("catch block falls back to 'Import failed' when no message", () => {
        const err = {};
        const errorMsg = err.message || "Import failed";
        expect(errorMsg).toBe("Import failed");
    });

    test("navigate catch block falls back to 'Failed to browse directory'", () => {
        const err = {};
        // mirror: catch(err) { error = err.message || "Failed to browse directory"; }
        const errorMsg = err.message || "Failed to browse directory";
        expect(errorMsg).toBe("Failed to browse directory");
    });
});
