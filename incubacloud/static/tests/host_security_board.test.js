import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { animationFrame } from "@odoo/hoot-mock";
import {
    allowTranslations,
    mountWithCleanup,
    patchWithCleanup,
    preloadBundle,
} from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import { HostSecurityBoard } from "@incubacloud/components/metrics/host_security_board";
import { HostDetail } from "@incubacloud/components/host_detail/host_detail";
import { relayIcon } from "@incubacloud/utils/icons";

/**
 * A server's Security view (plan #84, C6): the Performance | Security
 * control of its Metrics tab, the states that read nothing, and the
 * tables drawn only from an answer that carries them.
 */

/**
 * A view stand-in holding the answer *board*, on the shipped methods.
 *
 * @param {object} board the route's answer
 * @param {object} [state]
 * @returns {object}
 */
function viewWith(board, state = {}) {
    const comp = Object.create(HostSecurityBoard.prototype);
    comp.props = { hostId: 3 };
    comp.state = { board, range: "1h", zoom: null, instanceId: 0, ...state };
    comp.env = { permissions: {} };
    return comp;
}

/**
 * Answer every read with *answers[url]* and count them.
 *
 * @param {object} answers by route
 * @returns {{asked: object, params: object}}
 */
function answer(answers) {
    const asked = {};
    const params = {};
    patchWithCleanup(rpc, {
        _rpc(url, sent) {
            asked[url] = (asked[url] || 0) + 1;
            params[url] = sent;
            return Promise.resolve(answers[url]);
        },
    });
    return { asked, params };
}

const ENV = {
    icon: relayIcon,
    permissions: { can_manage_hosts: true, can_view_metrics: true },
    features: { observability: { configure: true } },
    navigate: () => {},
};

/** A ready answer, the figures alone: what a Developer gets. */
function figuresOnly() {
    return {
        ok: true, state: "ready", start: 0, end: 3600, step: 30, timing: {},
        detail: false, behind_cdn: false, attribution: null,
        panels: {
            ssh_failed: { series: [{ name: "", role: "failed", points: [[0, 2], [30, 3]] }] },
            http: { rows: [
                { id: "9", name: "prod", rps: 3.2, r4xx: 0.1, r5xx: 0, weblogin: 41, unidentified: false },
                { id: "0", name: "", weblogin: 7, unidentified: true },
            ] },
            now: { value: { ssh_failed_10m: 3, collector_up: 1, lines_15m: 30 } },
        },
        diagnosis: { current: true, tone: "ok", findings: [{ code: "calm", tone: "ok", text: "Calm." }] },
        defense: { ssh_allowlist: 2, hardened: true, hardening_current: true, enabled: true, key_known: true, write_user: "agent" },
        weblogin: { instances: [{ id: 9, name: "prod", attempts: 41 }], unidentified: 7, bans: 1, unbans: 0,
                    distinct: 3, sampled: 5, cut: false, sample: 5000, window_hours: 24 },
    };
}

describe("HostSecurityBoard", () => {
    beforeEach(() => allowTranslations());

    test("reads its server's view in one route, the filter beside it", () => {
        const comp = viewWith(null);
        expect(comp.route).toBe("/cloud/host/security_board");
        expect(comp.routeParams()).toEqual({ host_id: 3 });
        comp.state.instanceId = 9;
        expect(comp.routeParams()).toEqual({ host_id: 3, instance_id: 9 });
    });

    test("the server's Metrics tab offers Performance and Security", () => {
        expect("HostSecurityBoard" in HostDetail.components).toBe(true);
        expect("HostBoard" in HostDetail.components).toBe(true);
    });

    test("an answer without tables has none, whatever the screen allows", () => {
        const comp = viewWith(figuresOnly());
        expect(comp.hasTables).toBe(false);
        expect(comp.table("weblogin_sources").rows).toEqual([]);
        expect(comp.httpRows.map((r) => r.name)).toEqual(["prod"]);
    });

    test("whose a sign-in looks reads in words, and the bad ones are marked", () => {
        const comp = viewWith(null);
        expect(comp.attributionText("panel")).toBe("the panel");
        expect(comp.attributionText("foreign_key")).toBe("another key");
        expect(comp.attributionBad("foreign_key")).toBe(true);
        expect(comp.attributionBad("panel")).toBe(false);
        expect(comp.instancesText([{ id: 0, name: null, attempts: 2 }])).toBe("unidentified (2)");
        expect(comp.blockedText(null)).toBe("—");
        expect(comp.countryText("local")).toBe("local");
    });

    test("a sign-in with a known key reads with its name, from elsewhere marked", () => {
        const comp = viewWith(null);
        expect(comp.attributionText("known_key", "Ana's laptop")).toBe("known key: Ana's laptop");
        expect(comp.attributionText("known_key_elsewhere", "CI")).toMatch(/known key «CI» from an address/);
        expect(comp.attributionBad("known_key")).toBe(false);
        expect(comp.attributionBad("known_key_elsewhere")).toBe(true);
        expect(comp.keyText("known")).toBe("a known key");
    });

    test("the known keys split into the active ones and the removed ones", () => {
        const comp = viewWith({ known_keys: { can_edit: false, rows: [
            { id: 1, name: "Laptop", active: true, only_from: false, added_by: "Ana", added_at: null, last_used_at: null },
            { id: 2, name: "Old", active: false, only_from: true, removed_by: "Bea", removed_at: null },
        ] } });
        expect(comp.activeKeys.map((k) => k.id)).toEqual([1]);
        expect(comp.removedKeys.map((k) => k.id)).toEqual([2]);
        expect(comp.keyOnlyFromText(comp.activeKeys[0])).toBe("any address");
        expect(comp.keyOnlyFromText(comp.removedKeys[0])).toBe("some addresses only");
        expect(comp.keyAddedText(comp.activeKeys[0])).toMatch(/Added by Ana .* never used/);
        expect(comp.keyRemovedText(comp.removedKeys[0])).toMatch(/Removed by Bea/);
        expect(comp.removedKeysTitle).toBe("Removed keys (1)");
        comp.state.board.known_keys.removed_count = 25;
        expect(comp.removedKeysTitle).toBe("Removed keys (25)");
    });

    test("a cut sample says it counts the last lines", () => {
        const comp = viewWith({ tables: { ssh_sources: { rows: [], cut: true, sample: 5000 } } });
        expect(comp.sampleText("ssh_sources")).toMatch(/5.?000/);
        expect(viewWith({ tables: { ssh_sources: { rows: [], cut: false } } }).sampleText("ssh_sources")).toBe("");
    });
});

describe("HostSecurityBoard, mounted", () => {
    preloadBundle("web.chartjs_lib");
    beforeEach(() => allowTranslations());

    for (const [state, words] of [
        ["unavailable", "not available on this panel yet"],
        ["off", "does not send its security logs"],
    ]) {
        test(`the ${state} state says so and draws nothing else`, async () => {
            answer({ "/cloud/host/security_board": { ok: true, state, timing: {}, defense: {} } });
            await mountWithCleanup(HostSecurityBoard, { props: { hostId: 3 }, env: ENV, noMainContainer: true });
            await animationFrame();
            expect(`.ic-mb-stub[data-reason=${state}] h3`).toHaveText(new RegExp(words));
            expect(".ic-mb-grid").toHaveCount(0);
        });
    }

    test("observability off says it as the other boards do", async () => {
        answer({ "/cloud/host/security_board": { ok: false, reason: "disabled", error: "x" } });
        await mountWithCleanup(HostSecurityBoard, { props: { hostId: 3 }, env: ENV, noMainContainer: true });
        await animationFrame();
        expect(".ic-mb-stub[data-reason=disabled] h3").toHaveText("Observability is not enabled");
    });

    test("a Developer's answer draws the figures and no table nor lines", async () => {
        answer({ "/cloud/host/security_board": figuresOnly() });
        await mountWithCleanup(HostSecurityBoard, { props: { hostId: 3 }, env: ENV, noMainContainer: true });
        await animationFrame();
        expect(".ic-mb-finding[data-code=calm]").toHaveCount(1);
        expect("[data-defense=hardening]").toHaveText("Hardening: current");
        expect("[data-panel=http] tbody tr").toHaveCount(1);
        expect("[data-block=weblogin] [data-figure=distinct]").toHaveText(/3 addresses/);
        expect("[data-table]").toHaveCount(0);
        expect(".ic-sb-lines").toHaveCount(0);
        expect("#ic-sb-instance").toHaveCount(0);
    });

    test("a Developer reads the known keys without a button", async () => {
        const board = figuresOnly();
        board.known_keys = { can_edit: false, rows: [
            { id: 1, name: "Laptop", fingerprint: "SHA256:abc", key_type: "ED25519", active: true,
              only_from: true, added_by: "Ana", added_at: "2026-11-17T10:00:00Z", last_used_at: null },
        ] };
        answer({ "/cloud/host/security_board": board });
        await mountWithCleanup(HostSecurityBoard, { props: { hostId: 3 }, env: ENV, noMainContainer: true });
        await animationFrame();
        expect("[data-card=known_keys] [data-list=known_keys] tbody tr").toHaveCount(1);
        expect("[data-card=known_keys] [data-list=known_keys] tbody tr").toHaveText(/some addresses only/);
        expect("[data-action=add_key]").toHaveCount(0);
        expect("[data-action=remove_key]").toHaveCount(0);
        expect("[data-table]").toHaveCount(0);
    });

    test("a manager adds a key from the empty card", async () => {
        const board = figuresOnly();
        board.detail = true;
        board.known_keys = { can_edit: true, rows: [] };
        const added = { ok: true, known_keys: { can_edit: true, rows: [
            { id: 4, name: "Laptop", fingerprint: "SHA256:abc", key_type: "", active: true,
              only_from: true, allowed_networks: "192.0.2.15/32", added_by: "Ana",
              added_at: "2026-11-17T10:00:00Z", last_used_at: null },
        ] } };
        const { params } = answer({ "/cloud/host/security_board": board, "/cloud/host/known_key_add": added });
        const comp = await mountWithCleanup(HostSecurityBoard, { props: { hostId: 3 }, env: ENV, noMainContainer: true });
        await animationFrame();
        expect("[data-empty=known_keys]").toHaveText(/No known key/);
        comp.openKeyForm();
        await animationFrame();
        expect("[data-form=known_key]").toHaveCount(1);
        Object.assign(comp.state.keyForm, { name: "Laptop", key: "SHA256:abc", networks: "192.0.2.15" });
        await comp.addKey();
        await animationFrame();
        expect(params["/cloud/host/known_key_add"]).toEqual({
            host_id: 3, key: "SHA256:abc", name: "Laptop", networks: "192.0.2.15",
        });
        expect("[data-form=known_key]").toHaveCount(0);
        expect("[data-list=known_keys] tbody tr").toHaveText(/192\.0\.2\.15\/32/);
        expect("[data-action=remove_key]").toHaveCount(1);
    });

    test("a refused key leaves the field empty and says why", async () => {
        const board = figuresOnly();
        board.detail = true;
        board.known_keys = { can_edit: true, rows: [], removed_count: 0 };
        answer({
            "/cloud/host/security_board": board,
            "/cloud/host/known_key_add": { ok: false, error: "This is a private key." },
        });
        const comp = await mountWithCleanup(HostSecurityBoard, { props: { hostId: 3 }, env: ENV, noMainContainer: true });
        await animationFrame();
        comp.openKeyForm();
        Object.assign(comp.state.keyForm, { name: "Oops", key: "-----BEGIN OPENSSH PRIVATE KEY-----" });
        await comp.addKey();
        await animationFrame();
        expect(comp.state.keyForm.key).toBe("");
        expect(comp.state.keyForm.name).toBe("Oops");
        expect("#ic-sb-key-key").toHaveValue("");
        expect("[data-form=known_key] .ic-field-error").toHaveText("This is a private key.");
    });

    test("a manager's answer draws its tables, an empty one with its words", async () => {
        const board = figuresOnly();
        board.detail = true;
        board.attribution = "IP Geolocation by DB-IP (https://db-ip.com), CC BY 4.0";
        board.tables = {
            ssh_sources: { rows: [{ address: "192.0.2.4", country: "DE", attempts: 12, blocked: true, last: "2026-11-14T10:00:00+00:00" }],
                           users: [{ user: "root", attempts: 12 }], distinct_users: 1, cut: false },
            accepted: { rows: [{ at: "2026-11-14T10:00:00+00:00", user: "root", address: "192.0.2.9", country: "FR",
                                 key: "other", attribution: "foreign_key" }] },
            sudo: { rows: [] },
            changes: { rows: [] },
            weblogin_sources: { rows: [{ address: "198.51.100.7", country: "ES", attempts: 41,
                                         instances: [{ id: 9, name: "prod", attempts: 41 }], blocked: false }], cut: false },
            weblogin_bans: { rows: [] },
        };
        answer({ "/cloud/host/security_board": board });
        await mountWithCleanup(HostSecurityBoard, { props: { hostId: 3 }, env: ENV, noMainContainer: true });
        await animationFrame();
        expect("[data-table=ssh_sources] tbody tr").toHaveCount(1);
        expect("[data-table=accepted] tr.ic-mb-blocked").toHaveCount(1);
        expect("[data-table=weblogin_sources] tbody tr").toHaveText(/prod \(41\)/);
        expect("[data-empty=weblogin_bans]").toHaveText(/banned no address/);
        expect("#ic-sb-instance option").toHaveCount(2);
        expect(".ic-mb-meta").toHaveText(/DB-IP/);
    });
});
