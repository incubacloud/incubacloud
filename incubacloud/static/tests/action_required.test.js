import { describe, expect, test } from "@odoo/hoot";
import { allowTranslations } from "@web/../tests/web_test_helpers";
import { InstanceDetail } from "@incubacloud/components/instance_detail/instance_detail";
import { HostDetail } from "@incubacloud/components/host_detail/host_detail";
import { AlertHistory } from "@incubacloud/components/alert_history/alert_history";
import { holdReason } from "@incubacloud/utils/job_queue";

/**
 * "Action required" says why (core 1.0.212).
 *
 * The chip of a job that waits on an alert said only that something
 * was required. Hovered, focused or pressed it now says what — by the
 * code of the alert that keeps the job, and for a failure that holds
 * the queue by what failed — and leads to that alert.
 *
 * No mounting (see ic_confirm_dialog.test.js): the shipped methods run
 * on stand-ins that record what they reach for.
 */

const HELD_BY_DEPLOY = {
    id: 7, state: "queued", queue_held: true, action_alert_id: 41,
    action_alert_code: "job_failed", action_job_type_code: "deploy_instance",
};

describe("Action required — the reason", () => {
    test("a failed deploy holding the queue says so", () => {
        allowTranslations();
        expect(String(holdReason(HELD_BY_DEPLOY))).toBe(
            "Waiting: the previous deploy failed. Retry it or resolve its alert.",
        );
    });

    test("each failure that holds a queue is named, any other is generic", () => {
        allowTranslations();
        const failed = (type) => String(holdReason({
            action_alert_code: "job_failed", action_job_type_code: type,
        }));
        expect(failed("rebuild_instance")).toBe(
            "Waiting: the previous rebuild failed. Retry it or resolve its alert.",
        );
        expect(failed("restore_instance")).toBe(failed("backup_restore"));
        expect(failed("delete_instance")).toMatch(/previous delete failed/);
        expect(failed("move_cutover")).toBe(
            "Waiting: an earlier job failed. Retry it or resolve its alert.",
        );
    });

    test("a conflict says which, and any other alert reads the same", () => {
        allowTranslations();
        expect(String(holdReason({ action_alert_code: "pip_conflict" }))).toMatch(/Python dependencies/);
        expect(String(holdReason({ action_alert_code: "addon_conflict" }))).toMatch(/more than one repository/);
        expect(String(holdReason({ action_alert_code: "cert_expiring" }))).toBe(
            "An alert needs you first. Resolve it to go on.",
        );
        expect(String(holdReason({}))).toBe(String(holdReason({ action_alert_code: "x" })));
    });

    test("both job lists say it the same way", () => {
        allowTranslations();
        for (const proto of [InstanceDetail.prototype, HostDetail.prototype]) {
            expect(String(proto.holdReason(HELD_BY_DEPLOY))).toBe(String(holdReason(HELD_BY_DEPLOY)));
        }
    });
});

/**
 * A job list stand-in whose env records the alert it opens.
 *
 * @param {object} proto the component's prototype
 * @returns {object} the stand-in, with ``opened`` (alert ids)
 */
function jobList(proto) {
    const comp = Object.create(proto);
    comp.state = { holdOpen: null };
    comp.opened = [];
    comp.env = { openAlert: (id) => comp.opened.push(id) };
    return comp;
}

describe("Action required — opening it", () => {
    test("a press opens the reason, a second closes it", () => {
        for (const proto of [InstanceDetail.prototype, HostDetail.prototype]) {
            const comp = jobList(proto);
            comp.toggleHold(HELD_BY_DEPLOY);
            expect(comp.state.holdOpen).toBe(7);
            comp.toggleHold({ id: 8 });
            expect(comp.state.holdOpen).toBe(8);
            comp.toggleHold({ id: 8 });
            expect(comp.state.holdOpen).toBe(null);
        }
    });

    test("Escape closes it and gives the focus back to the chip", () => {
        for (const proto of [InstanceDetail.prototype, HostDetail.prototype]) {
            const comp = jobList(proto);
            comp.state.holdOpen = 7;
            const wrapper = document.createElement("span");
            const chip = document.createElement("button");
            chip.className = "ic-tl-alert-badge";
            wrapper.appendChild(chip);
            document.body.appendChild(wrapper);
            let stopped = false;
            comp.onHoldKeydown({
                key: "Escape", currentTarget: wrapper, stopPropagation: () => { stopped = true; },
            });
            expect(comp.state.holdOpen).toBe(null);
            expect(stopped).toBe(true);
            expect(document.activeElement).toBe(chip);
            wrapper.remove();
        }
    });

    test("another key, or Escape with nothing open, does nothing", () => {
        const comp = jobList(InstanceDetail.prototype);
        comp.state.holdOpen = 7;
        comp.onHoldKeydown({ key: "Enter", stopPropagation() { throw new Error("stopped"); } });
        expect(comp.state.holdOpen).toBe(7);
        comp.state.holdOpen = null;
        comp.onHoldKeydown({ key: "Escape", stopPropagation() { throw new Error("stopped"); } });
        expect(comp.state.holdOpen).toBe(null);
    });

    test("its link opens the alert that keeps the job", () => {
        for (const proto of [InstanceDetail.prototype, HostDetail.prototype]) {
            const comp = jobList(proto);
            comp.state.holdOpen = 7;
            comp.openHoldAlert(HELD_BY_DEPLOY);
            expect(comp.opened).toEqual([41]);
            expect(comp.state.holdOpen).toBe(null);
        }
    });
});

describe("Action required — the alert it leads to", () => {
    /**
     * An AlertHistory stand-in listing *ids*, asked to open *focus*.
     *
     * @param {number[]} ids the alerts on the page
     * @param {number|false} focus ``focusAlertId``
     * @returns {object}
     */
    function alerts(ids, focus) {
        const comp = Object.create(AlertHistory.prototype);
        comp.props = { focusAlertId: focus };
        comp.state = { alerts: ids.map((id) => ({ id })), expanded: {}, focusedId: null };
        return comp;
    }

    test("is marked and opened once the list is loaded", () => {
        const comp = alerts([40, 41], 41);
        comp._focusAlert();
        expect(comp.state.focusedId).toBe(41);
        expect(comp.state.expanded[41]).toBe(true);
    });

    test("only once: a reload keeps what the person did since", () => {
        const comp = alerts([41], 41);
        comp._focusAlert();
        comp.state.expanded[41] = false;
        comp._focusAlert();
        expect(comp.state.expanded[41]).toBe(false);
    });

    test("one the page does not list, or none asked for, changes nothing", () => {
        for (const comp of [alerts([40], 41), alerts([41], false)]) {
            comp._focusAlert();
            expect(comp.state.focusedId).toBe(null);
            expect(comp.state.expanded).toEqual({});
        }
    });
});
