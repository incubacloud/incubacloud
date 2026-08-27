import { describe, expect, test } from "@odoo/hoot";
import {
    copyStateBadgeClass,
    copyStateLabel,
    formatBytes,
    isRevivable,
} from "@incubacloud/utils/archive_copy";

describe("archive_copy — formatBytes", () => {
    test("zero and null render as a dash, not as 0 B", () => {
        expect(formatBytes(0)).toBe("—");
        expect(formatBytes(null)).toBe("—");
        expect(formatBytes(undefined)).toBe("—");
    });

    test("bytes stay whole, larger units get one decimal", () => {
        expect(formatBytes(512)).toBe("512 B");
        expect(formatBytes(1536)).toBe("1.5 KB");
        expect(formatBytes(1024 * 1024 * 3)).toBe("3.0 MB");
    });

    test("stops at TB instead of running off the unit table", () => {
        expect(formatBytes(1024 ** 5)).toBe("1024.0 TB");
    });
});

describe("archive_copy — copyStateLabel", () => {
    test("maps the three real states", () => {
        expect(copyStateLabel("present").toString()).toBe("Present");
        expect(copyStateLabel("missing").toString()).toBe("Missing");
        expect(copyStateLabel("unreachable").toString()).toBe("Unreachable");
    });

    test("an unset state is 'not checked yet', never 'missing'", () => {
        // Reporting a copy the cron has not reached yet as missing would
        // send an operator chasing data loss that did not happen.
        expect(copyStateLabel("").toString()).toBe("Not checked yet");
        expect(copyStateLabel(undefined).toString()).toBe("Not checked yet");
    });
});

describe("archive_copy — copyStateBadgeClass", () => {
    test("missing is an error and unreachable only a warning", () => {
        expect(copyStateBadgeClass("present")).toBe("ok");
        expect(copyStateBadgeClass("missing")).toBe("failed");
        expect(copyStateBadgeClass("unreachable")).toBe("warn");
    });

    test("unknown states fall back to the muted bucket", () => {
        expect(copyStateBadgeClass("")).toBe("draft");
        expect(copyStateBadgeClass("something_new")).toBe("draft");
    });
});

describe("archive_copy — isRevivable", () => {
    test("no frozen copy means nothing to restore", () => {
        expect(isRevivable({ has_copy: false, copy_state: "present" })).toBe(false);
    });

    test("a copy the last check found gone is not offered", () => {
        expect(isRevivable({ has_copy: true, copy_state: "missing" })).toBe(false);
    });

    test("present and unreachable are both offered", () => {
        // Unreachable is a storage hiccup, not proof of loss, and revive
        // re-probes live before deploying anything.
        expect(isRevivable({ has_copy: true, copy_state: "present" })).toBe(true);
        expect(isRevivable({ has_copy: true, copy_state: "unreachable" })).toBe(true);
    });

    test("an unchecked copy is still offered", () => {
        expect(isRevivable({ has_copy: true, copy_state: "" })).toBe(true);
    });

    test("a missing row object never throws", () => {
        expect(isRevivable(null)).toBe(false);
        expect(isRevivable(undefined)).toBe(false);
    });
});
