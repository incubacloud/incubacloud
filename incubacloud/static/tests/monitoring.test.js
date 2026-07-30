import { describe, expect, test } from "@odoo/hoot";
import { Component } from "@odoo/owl";
import { Monitoring } from "@incubacloud/components/monitoring/monitoring";

/**
 * The embed URL is the part worth pinning: it is built by string
 * concatenation from an operator-supplied base URL, so a trailing slash
 * or a renamed dashboard silently produces a blank iframe rather than an
 * error anyone would notice.
 */
describe("Monitoring — class structure", () => {
    test("is a Component with the right template and no props", () => {
        expect(Monitoring.prototype).toBeInstanceOf(Component);
        expect(Monitoring.template).toBe("incubacloud.Monitoring");
        expect(Monitoring.props).toEqual({});
    });

    test("declares the dashboards provisioned by the central playbook", () => {
        // The uids must match ansible/files/dashboards/*.json — a
        // mismatch renders an empty Grafana page with no error.
        const uids = Monitoring.DASHBOARDS.map((d) => d.uid);
        expect(uids).toEqual(["ic-fleet", "ic-host", "ic-instance"]);
        for (const dash of Monitoring.DASHBOARDS) {
            expect(dash.slug).not.toBe("");
            expect(dash.label).not.toBe("");
        }
    });
});

describe("Monitoring — embed URL", () => {
    const build = (baseUrl, uid = "ic-host") => {
        const cmp = Object.create(Monitoring.prototype);
        cmp.state = { baseUrl, current: uid };
        return cmp.embedUrl(uid);
    };

    test("points at the dashboard and strips Grafana's chrome", () => {
        const url = build("https://grafana.example.com");
        expect(url).toInclude("/d/ic-host/incubacloud-host");
        // kiosk: the panel supplies navigation; without it the embed
        // shows a second, conflicting menu bar.
        expect(url).toInclude("kiosk");
    });

    test("tolerates a trailing slash in the configured base URL", () => {
        // Operators paste URLs with and without it; a double slash breaks
        // the route on some reverse proxies.
        expect(build("https://grafana.example.com/")).not.toInclude(".com//d/");
    });

    test("is empty when no base URL is configured", () => {
        // Guards the template: an empty src renders the page's own URL
        // inside the iframe, which looks like a broken recursive panel.
        expect(build("")).toBe("");
    });

    test("is empty for an unknown dashboard", () => {
        const cmp = Object.create(Monitoring.prototype);
        cmp.state = { baseUrl: "https://g.example.com", current: "nope" };
        expect(cmp.embedUrl("nope")).toBe("");
    });
});
