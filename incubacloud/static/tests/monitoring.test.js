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

describe("Monitoring — the way out of the frame", () => {
    /**
     * The embed cannot ask for a password: the identity provider serves
     * `frame-ancestors 'self'`, so the browser refuses to paint its
     * login page inside a frame on another subdomain. Measured against
     * production on 16-ago; before that we assumed the cookie was the
     * obstacle and it is not — same registrable domain, Lax travels.
     */
    const build = (baseUrl, uid = "ic-host") => {
        const cmp = Object.create(Monitoring.prototype);
        cmp.state = { baseUrl, current: uid };
        return cmp.externalUrl(uid);
    };

    test("keeps Grafana's chrome, which is where the sign-in lives", () => {
        // The whole point of the link: kiosk hides the very prompt the
        // user opens this tab to answer.
        expect(build("https://grafana.example.com")).not.toInclude("kiosk");
    });

    test("points at the same dashboard as the embed", () => {
        // A link that landed somewhere else would send the user to sign
        // in for a page they are not looking at.
        expect(build("https://grafana.example.com"))
            .toInclude("/d/ic-host/incubacloud-host");
    });

    test("carries the subject the embed was pinned to", () => {
        const cmp = Object.create(Monitoring.prototype);
        cmp.state = {
            baseUrl: "https://grafana.example.com",
            current: "ic-instance",
            instance: "acme-prod",
            instances: [{ name: "acme-prod", host: "h1" }],
        };
        const url = cmp.externalUrl("ic-instance");
        expect(url).toInclude("var-instance=acme-prod");
        expect(url).toInclude("var-host=h1");
    });

    test("is empty when there is nothing to point at", () => {
        // Guards the template: an href of "" reloads the panel itself,
        // which looks like the link is broken rather than absent.
        expect(build("")).toBe("");
    });
});

describe("Monitoring — who may configure it", () => {
    /**
     * The axis that decides whether "set it up in Settings" is advice or
     * a dead end. It comes from the boot config rather than the metrics
     * route because it is a property of the deployment, not a setting.
     */
    const withEnv = (features) => {
        const cmp = Object.create(Monitoring.prototype);
        cmp.env = { features };
        return cmp;
    };

    test("a panel that owns its settings may be told to open them", () => {
        expect(withEnv({ observability: { configure: true } }).canConfigure)
            .toBe(true);
    });

    test("a panel whose settings are injected may not", () => {
        // The tenant case: pointing them at a Settings tab their panel
        // hides is the dead end this replaces.
        expect(withEnv({ observability: { configure: false } }).canConfigure)
            .toBe(false);
    });

    test("defaults to configurable when the descriptor is absent", () => {
        // A boot config that failed to load must not silently downgrade
        // an operator's panel into the injected-settings variant.
        expect(withEnv(undefined).canConfigure).toBe(true);
        expect(withEnv({}).canConfigure).toBe(true);
    });
});
