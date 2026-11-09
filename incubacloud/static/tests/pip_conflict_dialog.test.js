import { afterEach, beforeEach, describe, expect, test } from "@odoo/hoot";
import { click, waitFor } from "@odoo/hoot-dom";
import { animationFrame } from "@odoo/hoot-mock";
import { Component, useSubEnv, xml } from "@odoo/owl";
import {
    allowTranslations,
    mountWithCleanup,
    patchWithCleanup,
} from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import { AlertHistory } from "@incubacloud/components/alert_history/alert_history";
import { focusPipLines, pipFocusRange } from "@incubacloud/utils/pip_focus";
import { bareNameRelation, replaceReqLine } from "@incubacloud/utils/pip_specs";

/**
 * The pip conflict dialog of the alerts panel (9-nov-2026).
 *
 * Its "Edit Manually" button carried a class no stylesheet defines, so it
 * drew as the browser's own button beside the panel's Cancel and Apply.
 * Clicked, it navigated to the instance's Overview under the alerts
 * panel, which stayed open over it: nothing seemed to happen. It now
 * closes the dialog and the panel and opens the Dependencies tab of the
 * instance, or of the project for a project's alert, with the package
 * selected.
 */

// By its words, as the person finds it: the class it carries is under test.
const EDIT = ".ah-resolve-actions button:contains(Edit Manually)";

const CONFLICT = {
    name: "requests",
    existing: "requests==2.34.2",
    existing_source: "existing",
    incoming: "requests==2.30",
    incoming_source: "oca/queue (18.0)",
};

/**
 * One active pip_conflict alert as ``/cloud/get_alert_history`` lists it.
 *
 * @param {object} target ``instance_id`` and ``project_id``
 * @returns {object}
 */
function pipAlert(target) {
    return {
        id: 15,
        code: "pip_conflict",
        level: "warning",
        state: "active",
        message: "Pip dependency conflicts: requests",
        create_date: "2026-11-09 10:22:29",
        last_raised_at: "2026-11-09 10:22:29",
        host: "18 / ic",
        target: "18 / ic",
        job_id: false,
        resolution: false,
        resolved_at: false,
        resolved_by_job_id: false,
        resolved_by_job_name: false,
        conflict_data: [CONFLICT],
        payload: null,
        ...target,
    };
}

/**
 * Mount the alerts panel listing *alert*, in a panel env that records moves.
 *
 * @param {object} alert the one alert listed
 * @returns {Promise<{navigated: Array, closed: Array}>}
 */
async function mountPanel(alert) {
    const seen = { navigated: [], closed: [], order: [] };
    patchWithCleanup(rpc, {
        _rpc: (url) =>
            Promise.resolve(
                url === "/cloud/get_alert_history"
                    ? { total: 1, alerts: [alert] }
                    : false
            ),
    });
    class Panel extends Component {
        static template = xml`<AlertHistory embedded="true"/>`;
        static components = { AlertHistory };
        static props = {};
        setup() {
            useSubEnv({
                // Async, as the panel's: it may wait on a guard.
                navigate: async (route, params) => {
                    await Promise.resolve();
                    seen.navigated.push([route, params]);
                    seen.order.push("navigate");
                },
                closeSlideOver: () => {
                    seen.closed.push(true);
                    seen.order.push("close");
                },
                refreshAlertCount: () => {},
                icon: () => "",
            });
        }
    }
    await mountWithCleanup(Panel, { noMainContainer: true });
    await click(".ah-btn-resolve");
    await animationFrame();
    await waitFor(EDIT);
    return seen;
}

describe("Pip conflict dialog — Edit Manually", () => {
    beforeEach(() => allowTranslations());

    test("is drawn as the panel's secondary button", async () => {
        await mountPanel(pipAlert({ instance_id: 6, project_id: 5 }));
        expect(EDIT).toHaveClass("ic-confirm-btn-cancel");
        expect(EDIT).not.toHaveClass("ic-btn-secondary");
    });

    test("opens the instance's dependencies on the package and closes the panel", async () => {
        const seen = await mountPanel(pipAlert({ instance_id: 6, project_id: 5 }));
        await click(EDIT);
        await animationFrame();
        expect(seen.navigated).toEqual([
            ["instance_detail", {
                instance_id: 6, project_id: 5, pip_focus: { name: "requests" },
            }],
        ]);
        expect(seen.closed).toEqual([true]);
        expect(EDIT).toHaveCount(0);
    });

    test("closes the panel once the page has moved, not before", async () => {
        const seen = await mountPanel(pipAlert({ instance_id: 6, project_id: 5 }));
        await click(EDIT);
        await animationFrame();
        expect(seen.order).toEqual(["navigate", "close"]);
    });

    test("a project's conflict opens the project's dependencies", async () => {
        const seen = await mountPanel(pipAlert({ instance_id: false, project_id: 5 }));
        await click(EDIT);
        await animationFrame();
        expect(seen.navigated).toEqual([
            ["project_detail", { project_id: 5, pip_focus: { name: "requests" } }],
        ]);
    });
});

describe("Pip list focus", () => {
    const BLOCK =
        "<<<<<<< existing\nrequests==2.34.2\n=======\nrequests\n>>>>>>> oca/queue (18.0)";

    test("selects the whole conflict block of the package", () => {
        const text = `urllib3==2.8.0\n${BLOCK}\nPyYAML==6.0.3`;
        const range = pipFocusRange(text, "requests");
        expect(text.slice(range.start, range.end)).toBe(BLOCK);
    });

    test("selects the package's line in a list without markers", () => {
        const text = "urllib3==2.8.0\nrequests==2.34.2\nPyYAML==6.0.3";
        const range = pipFocusRange(text, "requests");
        expect(text.slice(range.start, range.end)).toBe("requests==2.34.2");
    });

    test("compares names as pip does", () => {
        const text = "Py_YAML==6.0.3";
        const range = pipFocusRange(text, "py-yaml");
        expect(text.slice(range.start, range.end)).toBe("Py_YAML==6.0.3");
    });

    test("a package the list does not name selects nothing", () => {
        expect(pipFocusRange("urllib3==2.8.0", "requests")).toBe(null);
        expect(pipFocusRange("", "requests")).toBe(null);
    });

    describe("in the editor", () => {
        let textarea;
        beforeEach(() => {
            textarea = document.createElement("textarea");
            document.body.appendChild(textarea);
        });
        afterEach(() => textarea.remove());

        test("focuses the editor with the lines selected", () => {
            textarea.value = "urllib3==2.8.0\nrequests==2.34.2";
            expect(focusPipLines(textarea, "requests")).toBe(true);
            expect(document.activeElement).toBe(textarea);
            expect(textarea.value.slice(textarea.selectionStart, textarea.selectionEnd))
                .toBe("requests==2.34.2");
        });

        test("still focuses the editor when the package is gone", () => {
            textarea.value = "urllib3==2.8.0";
            expect(focusPipLines(textarea, "requests")).toBe(false);
            expect(document.activeElement).toBe(textarea);
        });
    });
});

describe("Editors' merge — a bare name is no conflict", () => {
    test("a bare incoming name is met by the stored pin", () => {
        expect(bareNameRelation("requests==2.34.2", "requests")).toBe("existing");
    });

    test("a stored bare name gives way to the incoming pin", () => {
        expect(bareNameRelation("requests", "requests==2.34.2")).toBe("incoming");
    });

    test("two versions are left to the person", () => {
        expect(bareNameRelation("requests==2.34.2", "requests==2.30")).toBe(null);
        expect(bareNameRelation("requests>=2", "requests[socks]")).toBe(null);
    });

    test("a marker or a URL against a bare name is left to the person", () => {
        expect(bareNameRelation("requests==2.34.2; python_version>'3'", "requests")).toBe(null);
        expect(bareNameRelation("requests", "requests==2.34.2; python_version>'3'")).toBe(null);
        expect(bareNameRelation("pkg @ git+https://github.com/o/pkg.git", "pkg")).toBe(null);
        expect(bareNameRelation("pkg", "pkg @ git+https://github.com/o/pkg.git")).toBe(null);
    });

    test("only a name and its version clauses are settled", () => {
        expect(bareNameRelation("requests[socks]>=2.30, <3", "requests")).toBe("existing");
        expect(bareNameRelation("requests", "requests~=2.30")).toBe("incoming");
        expect(bareNameRelation("requests", "--hash=sha256:00")).toBe(null);
        expect(bareNameRelation("--hash=sha256:00", "requests")).toBe(null);
        expect(bareNameRelation("requests==2.34.2 \\", "requests")).toBe(null);
        expect(bareNameRelation("foo-", "foo-==1.0")).toBe(null);
        expect(bareNameRelation("foo-==1.0", "foo-")).toBe(null);
    });
});

describe("Editors' merge — the stored line is found as it is written", () => {
    const isRequests = (line) => line.split("#")[0].trim() === "requests";

    test("a line with a comment is replaced, its comment kept", () => {
        expect(replaceReqLine(
            "requests  # needed by X\nlibx==1.0", isRequests, "requests==2.34.2", true
        )).toEqual({ text: "requests==2.34.2  # needed by X\nlibx==1.0", replaced: true });
    });

    test("an indented line is replaced whole", () => {
        expect(replaceReqLine("  requests\nlibx==1.0", isRequests, "BLOCK")).toEqual({
            text: "BLOCK\nlibx==1.0", replaced: true,
        });
    });

    test("a replacement with $ is written as it is", () => {
        expect(replaceReqLine("requests", isRequests, "a$&b").text).toBe("a$&b");
    });

    test("no line found says so", () => {
        expect(replaceReqLine("libx==1.0", isRequests, "x")).toEqual({
            text: "libx==1.0", replaced: false,
        });
    });
});
