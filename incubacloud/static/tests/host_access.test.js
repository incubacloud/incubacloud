import { describe, expect, test } from "@odoo/hoot";
import { HostAccess } from "@incubacloud/components/host_access/host_access";

/**
 * A server's Access tab: what it says of each sign-in, without mounting it
 * (the component reads ``/cloud/host/access`` on start).
 */
describe("HostAccess", () => {
    const tab = Object.create(HostAccess.prototype);

    test("has its template and takes the server's id", () => {
        expect(HostAccess.template).toBe("incubacloud.HostAccess");
        expect(HostAccess.props.hostId).toBe(Number);
        expect(HostAccess.props.onOpenSecurity.optional).toBe(true);
    });

    test("only the panel's own sign-ins are not marked", () => {
        expect(tab.attributionBad("panel")).toBe(false);
        for (const code of ["panel_elsewhere", "foreign_key", "password", "other_user"]) {
            expect(tab.attributionBad(code)).toBe(true);
        }
    });

    test("an attribution without words is said as it came, none as a dash", () => {
        expect(tab.attributionText("something")).toBe("something");
        expect(tab.attributionText("")).toBe("—");
    });

    test("a sign-in without a date reads as a dash", () => {
        expect(tab.fmtAt("")).toBe("—");
    });
});
