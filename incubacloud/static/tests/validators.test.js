import { describe, expect, test } from "@odoo/hoot";
import { retention } from "@incubacloud/utils/validators";

describe("retention", () => {
    const check = retention("bad");

    test("takes what the server takes: a count from 1 and D, W, M or Y", () => {
        const values = ["1D", "30D", "2W", "3M", "12M", "1Y", "10Y", " 7D "];
        expect(values.filter((v) => check(v) !== null)).toEqual([]);
    });

    test("refuses what the server refuses", () => {
        const values = [
            "0D", "03M", "1m", "7d", "1H", "D", "3", "3 M", "-1M", "1.5M",
            "3M;", "3M; rm -rf /", "١M",
        ];
        expect(values.filter((v) => check(v) !== "bad")).toEqual([]);
    });

    test("leaves an empty value to required", () => {
        expect(["", "  ", null, undefined].map(check)).toEqual([null, null, null, null]);
    });

    test("names the weeks in its default message", () => {
        expect(retention()("1H")).toBe("Use N + D/W/M/Y (e.g. 30D, 2W, 3M, 1Y)");
    });
});
