import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { allowTranslations } from "@web/../tests/web_test_helpers";
import {
    PILL_CLASSES,
    ago,
    formatDate,
    fromUtc,
    toLocalInput,
} from "@incubacloud_pitr/components/pitr_panel/pitr_format";
import { OBJECTIVE_LABELS, STATE_LABELS } from "@incubacloud_pitr/components/pitr_panel/pitr_panel";

/**
 * pitr_format — the words and dates of the continuous recovery card.
 */
describe("pitr_format — states", () => {
    // Translated strings, read as text with the catalogue loaded.
    beforeEach(() => allowTranslations());

    test("a healthy state says OK, not only that it archives", () => {
        expect(String(STATE_LABELS.ok)).toBe("OK · Archiving");
        expect(PILL_CLASSES.ok).toBe("running");
    });

    test("every state has a label and a pill class of the panel", () => {
        for (const state of ["off", "enabling", "ok", "lagging", "failed"]) {
            expect(String(STATE_LABELS[state]).length).toBeGreaterThan(0);
            expect(["stopped", "building", "running", "pending", "failed"]).toInclude(
                PILL_CLASSES[state]
            );
        }
    });

    test("the objectives are the server's four", () => {
        expect(Object.keys(OBJECTIVE_LABELS)).toEqual(["1", "5", "15", "60"]);
        expect(String(OBJECTIVE_LABELS[60])).toBe("1 hour");
    });
});

describe("pitr_format — dates", () => {
    test("a server time is UTC", () => {
        const d = fromUtc("2026-10-20 17:24:53");
        expect(d.toISOString()).toBe("2026-10-20T17:24:53.000Z");
        expect(fromUtc(false)).toBe(null);
    });

    test("a date is shown in the panel's format, local time", () => {
        const d = new Date(2026, 9, 20, 19, 24, 53);
        expect(formatDate(d)).toBe("2026-10-20 19:24");
        expect(formatDate(null)).toBe("");
    });

    test("a datetime-local input gets the local minute", () => {
        expect(toLocalInput(new Date(2026, 9, 21, 0, 5, 59))).toBe("2026-10-21T00:05");
    });

    test("how long ago, in the instance page's words", () => {
        const now = new Date(2026, 9, 21, 0, 46).getTime();
        const at = (minutes) => new Date(now - minutes * 60000);
        expect(ago(at(0.5), now)).toBe("just now");
        expect(ago(at(10), now)).toBe("10 min ago");
        expect(ago(at(130), now)).toBe("2 h ago");
        expect(ago(at(60 * 50), now)).toBe("2 d ago");
        expect(ago(null, now)).toBe("");
    });
});
