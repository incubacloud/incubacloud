import { describe, expect, test } from "@odoo/hoot";
import {
    approvalNote,
    canRevoke,
    credentialText,
    grantPayload,
    grantReady,
    ownRuleActive,
    periodEnd,
    periodPayload,
    RULE_STATES,
} from "@incubacloud_mcp/auto_approval/auto_approval_format";

/**
 * auto_approval_format — what the screens of auto-approval show.
 *
 * The grant dialog's button, the period it sends, the states of a rule
 * and the marks the audit and the jobs show all come from these
 * functions.
 */

/** A grant form with everything chosen and the box ticked. */
function readyForm(overrides = {}) {
    return {
        userId: 7,
        credential: "API key #12",
        period: "30",
        date: "",
        acknowledged: true,
        busy: false,
        ...overrides,
    };
}

describe("Auto-approval — the grant dialog", () => {
    test("the button stays disabled until the box is ticked", () => {
        expect(grantReady(readyForm({ acknowledged: false }))).toBe(false);
        expect(grantReady(readyForm())).toBe(true);
    });

    test("nothing is chosen beforehand: an empty form cannot be confirmed", () => {
        const empty = {
            userId: null,
            credential: "",
            period: "",
            date: "",
            acknowledged: true,
        };
        expect(grantReady(empty)).toBe(false);
        expect(grantReady(readyForm({ userId: null }))).toBe(false);
        expect(grantReady(readyForm({ credential: "" }))).toBe(false);
        expect(grantReady(readyForm({ period: "" }))).toBe(false);
        expect(grantReady(readyForm({ period: "date", date: "" }))).toBe(false);
    });

    test("a box ticked by anything but true does not count", () => {
        expect(grantReady(readyForm({ acknowledged: "yes" }))).toBe(false);
    });

    test("a grant on its way cannot be sent again", () => {
        expect(grantReady(readyForm({ busy: true }))).toBe(false);
    });

    test("the period is sent as days or as the last day", () => {
        expect(periodPayload(readyForm({ period: "7" }))).toEqual({ days: 7 });
        expect(periodPayload(readyForm({ period: "date", date: "2026-12-01" }))).toEqual({
            expires_on: "2026-12-01",
        });
        expect(periodPayload(readyForm({ period: "" }))).toBe(null);
    });

    test("the request carries the notice's version and the box", () => {
        expect(grantPayload(readyForm({ period: "90" }), "2026-11-10")).toEqual({
            user_id: 7,
            credential: "API key #12",
            days: 90,
            acknowledged: true,
            notice_version: "2026-11-10",
        });
    });

    test("the notice says the day the rule ends", () => {
        const now = new Date("2026-11-10T12:00:00Z");
        expect(periodEnd(readyForm({ period: "1" }), now)).toBe("2026-11-11");
        expect(periodEnd(readyForm({ period: "date", date: "2026-11-20" }), now)).toBe(
            "2026-11-20"
        );
        expect(periodEnd(readyForm({ period: "" }), now)).toBe("");
    });
});

describe("Auto-approval — the rules", () => {
    test("only a rule that has not ended can be revoked", () => {
        expect(canRevoke({ state: "active" })).toBe(true);
        expect(canRevoke({ state: "paused_role" })).toBe(true);
        expect(canRevoke({ state: "paused_platform" })).toBe(true);
        expect(canRevoke({ state: "expired" })).toBe(false);
        expect(canRevoke({ state: "revoked" })).toBe(false);
    });

    test("the states the server names are the ones the screens know", () => {
        expect(RULE_STATES).toEqual([
            "active",
            "paused_role",
            "paused_platform",
            "expired",
            "revoked",
        ]);
    });

    test("a credential reads with the name its client declares", () => {
        expect(credentialText({ credential_label: "API key ci" })).toBe("API key ci");
        expect(credentialText({ label: "client_id x", client_name: "Claude Code" })).toBe(
            "client_id x (Claude Code)"
        );
    });

    test("Connect an agent says whether the rule acts now", () => {
        expect(ownRuleActive({ state: "active" })).toBe(true);
        expect(ownRuleActive({ state: "paused_role" })).toBe(false);
        expect(ownRuleActive({ state: "paused_platform" })).toBe(false);
    });
});

describe("Auto-approval — the audit's and the jobs' marks", () => {
    test("auto-approved names the rule and who granted it", () => {
        expect(
            approvalNote({ approval: "auto", approval_rule_id: 4, approval_granted_by: "Ada" })
        ).toEqual({ kind: "auto", rule: 4, by: "Ada" });
    });

    test("approved by the person, and nothing when no approval was needed", () => {
        expect(approvalNote({ approval: "person" })).toEqual({
            kind: "person",
            rule: null,
            by: "",
        });
        expect(approvalNote({ approval: "" })).toBe(null);
        expect(approvalNote({})).toBe(null);
    });
});
