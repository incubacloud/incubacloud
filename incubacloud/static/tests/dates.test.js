import { describe, expect, test } from "@odoo/hoot";
import { parseUTC } from "@incubacloud/utils/dates";

describe("parseUTC", () => {

    // ── null / empty ────────────────────────────────────────────────────

    test("null returns null", () => {
        expect(parseUTC(null)).toBe(null);
    });

    test("undefined returns null", () => {
        expect(parseUTC(undefined)).toBe(null);
    });

    test("empty string returns null", () => {
        expect(parseUTC("")).toBe(null);
    });

    test("whitespace-only string returns null", () => {
        expect(parseUTC("   ")).toBe(null);
    });

    // ── Odoo naive format ("YYYY-MM-DD HH:MM:SS") ────────────────────────

    test("naive Odoo datetime is parsed as UTC", () => {
        const d = parseUTC("2026-04-08 10:57:32");
        expect(d).not.toBe(null);
        expect(d.getUTCFullYear()).toBe(2026);
        expect(d.getUTCMonth()).toBe(3);   // April = 3 (0-indexed)
        expect(d.getUTCDate()).toBe(8);
        expect(d.getUTCHours()).toBe(10);
        expect(d.getUTCMinutes()).toBe(57);
        expect(d.getUTCSeconds()).toBe(32);
    });

    test("naive datetime returns a Date instance", () => {
        const d = parseUTC("2026-01-01 00:00:00");
        expect(d instanceof Date).toBe(true);
    });

    test("midnight naive datetime has correct UTC hours", () => {
        const d = parseUTC("2026-06-15 00:00:00");
        expect(d.getUTCHours()).toBe(0);
        expect(d.getUTCMinutes()).toBe(0);
    });

    test("end of day naive datetime has correct UTC hours", () => {
        const d = parseUTC("2026-12-31 23:59:59");
        expect(d.getUTCHours()).toBe(23);
        expect(d.getUTCMinutes()).toBe(59);
        expect(d.getUTCSeconds()).toBe(59);
    });

    // ── Already-UTC strings (ends with Z) ──────────────────────────────

    test("ISO string with Z suffix parsed as-is", () => {
        const d = parseUTC("2026-04-08T10:57:32Z");
        expect(d.getUTCHours()).toBe(10);
        expect(d.getUTCMinutes()).toBe(57);
    });

    test("ISO string with Z is a Date instance", () => {
        const d = parseUTC("2026-04-08T10:57:32Z");
        expect(d instanceof Date).toBe(true);
    });

    test("ISO string with milliseconds and Z is parsed correctly", () => {
        const d = parseUTC("2026-04-08T10:57:32.123Z");
        expect(d.getUTCMilliseconds()).toBe(123);
    });

    // ── Strings with explicit timezone offset ───────────────────────────

    test("ISO string with +HH:MM offset parsed as-is", () => {
        const d = parseUTC("2026-04-08T12:57:32+02:00");
        expect(d instanceof Date).toBe(true);
        // 12:57 +02:00 = 10:57 UTC
        expect(d.getUTCHours()).toBe(10);
        expect(d.getUTCMinutes()).toBe(57);
    });

    test("ISO string with -HH:MM offset parsed as-is", () => {
        const d = parseUTC("2026-04-08T07:57:32-03:00");
        // 07:57 -03:00 = 10:57 UTC
        expect(d.getUTCHours()).toBe(10);
    });

    // ── Numeric timestamp passthrough ───────────────────────────────────

    test("numeric timestamp string treated as naive Odoo date", () => {
        // The function does String(dateStr).trim() — a number-like string
        // that is not empty and doesn't end in Z will go through
        // the replace path.  The result may be Invalid Date but it
        // must not throw.
        expect(() => parseUTC("not-a-date")).not.toThrow();
    });

    // ── Whitespace handling ─────────────────────────────────────────────

    test("leading/trailing whitespace is trimmed before parsing", () => {
        const d = parseUTC("  2026-04-08 10:57:32  ");
        expect(d instanceof Date).toBe(true);
        expect(d.getUTCHours()).toBe(10);
    });

    // ── Two distinct naive datetimes produce different values ───────────

    test("two different naive strings produce different timestamps", () => {
        const d1 = parseUTC("2026-01-01 00:00:00");
        const d2 = parseUTC("2026-01-01 01:00:00");
        expect(d2.getTime()).toBe(d1.getTime() + 3600 * 1000);
    });
});
