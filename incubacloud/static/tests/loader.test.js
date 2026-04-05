import { describe, expect, test } from "@odoo/hoot";
import { Loader } from "@incubacloud/components/loader/loader";

describe("Loader", () => {
    test("template name is set", () => {
        expect(Loader.template).toBe("incubacloud.Loader");
    });

    test("props defines loader as Object", () => {
        expect(Loader.props.loader.type).toBe(Object);
    });

    test("loader shape requires isShown Boolean", () => {
        expect(Loader.props.loader.shape.isShown).toBe(Boolean);
    });

    test("setup method exists (uses useEffect)", () => {
        expect(typeof Loader.prototype.setup).toBe("function");
    });
});
