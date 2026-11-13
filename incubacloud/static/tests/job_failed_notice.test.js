import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { allowTranslations } from "@web/../tests/web_test_helpers";
import { Chrome } from "@incubacloud/app/app";

/**
 * A failed job's notice: what it says, where its "View" leads and how
 * Escape closes it. The browser walk of a failed job keeps only the
 * notice on a real panel; its words and its button are tested here.
 * The server's half, what failed, on what and why, is ``/cloud/get_job_brief``
 * (``tests/test_job_brief.py``); that the same text is not stacked twice
 * and that an error keeps its action is the toast service's
 * (``toast.test.js``).
 *
 * No mounting (see ic_confirm_dialog.test.js): the shipped methods run
 * on a stand-in of the panel's chrome.
 */

/**
 * A stand-in of the panel's chrome, with nothing open, that counts the
 * notices Escape dismisses.
 *
 * @returns {object} the stand-in, with ``dismissed``
 */
function chrome() {
    const cmp = Object.create(Chrome.prototype);
    cmp.state = { sidebarOpen: false, slideOver: null };
    cmp.dismissed = 0;
    cmp._dismissLatestToast = () => {
        cmp.dismissed += 1;
    };
    return cmp;
}

/**
 * An Escape keydown on the page itself, as a key with no focus sends it.
 *
 * @returns {object} what ``onGlobalKeydown`` reads of the event
 */
function escape() {
    return { key: "Escape", defaultPrevented: false, target: document.body };
}

describe("A failed job's notice", () => {
    beforeEach(() => {
        allowTranslations();
    });

    test("it says what failed, on what and why", () => {
        const text = chrome()._jobFailedMessage({
            name: "Check host", target_name: "qa-host", error: "connection refused",
        });
        expect(String(text)).toBe("Check host on qa-host failed: connection refused");
    });

    test("without a reason it says what and on what, without a target only what", () => {
        const cmp = chrome();
        expect(String(cmp._jobFailedMessage({ name: "Check host", target_name: "qa-host" })))
            .toBe("Check host on qa-host failed");
        expect(String(cmp._jobFailedMessage({ name: "Check host" }))).toBe("Check host failed");
    });

    test("View opens the job history of the host when the job has no instance", () => {
        const cmp = chrome();
        cmp._openJobsOf({ host_id: 4, instance_id: false });
        expect(cmp.state.slideOver).toEqual({
            panel: "jobs", instanceId: false, hostId: 4, expanded: false,
        });
    });

    test("View opens the job history of the instance when the job has one", () => {
        const cmp = chrome();
        cmp._openJobsOf({ host_id: 4, instance_id: 9 });
        expect(cmp.state.slideOver).toEqual({
            panel: "jobs", instanceId: 9, hostId: false, expanded: false,
        });
    });

    test("Escape with nothing open closes the latest notice", () => {
        const cmp = chrome();
        cmp.onGlobalKeydown(escape());
        expect(cmp.dismissed).toBe(1);
    });

    test("Escape with the job history open leaves the notices to it", () => {
        const cmp = chrome();
        cmp._openJobsOf({ host_id: 4 });
        cmp.onGlobalKeydown(escape());
        expect(cmp.dismissed).toBe(0);
    });
});
