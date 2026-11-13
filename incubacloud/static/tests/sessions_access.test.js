import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { allowTranslations, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import { SessionsAccess } from "@incubacloud/components/sessions_access/sessions_access";

/**
 * Sessions and access (#48, phase 2).
 *
 * No mounting (see ic_confirm_dialog.test.js): the methods under test
 * are the shipped ones, on a stand-in holding the state they read; the
 * server is ``rpc`` answering what each test says.
 */

/**
 * A page stand-in holding *data* as the server sent it.
 *
 * @param {object} [data]
 * @returns {object}
 */
function page(data = null) {
    const comp = Object.create(SessionsAccess.prototype);
    const toasts = [];
    comp.env = {
        toast: {
            error: (text) => toasts.push(["error", String(text)]),
            success: (text) => toasts.push(["success", String(text)]),
        },
    };
    comp.state = { loading: false, error: false, data, busy: false };
    comp.toasts = toasts;
    comp.left = [];
    comp._leave = (url) => comp.left.push(url);
    return comp;
}

/**
 * Have the server answer *url* with *answer*; return what it was sent.
 *
 * @param {string} url
 * @param {object} answer
 * @returns {Array} the params of each call to *url*
 */
function answering(url, answer) {
    const sent = [];
    patchWithCleanup(rpc, {
        _rpc(called, params) {
            if (called === url) {
                sent.push(params);
                return Promise.resolve(answer);
            }
            return Promise.resolve({ ok: false });
        },
    });
    return sent;
}

const SESSIONS = [
    { current: false, active: true, browsers: ["Firefox"], platforms: ["Linux"], countries: [] },
    { current: true, active: true, browsers: ["Chrome"], platforms: ["Macos"], countries: ["Spain"] },
];

describe("SessionsAccess", () => {
    beforeEach(() => allowTranslations());

    test("a close says why, in the words of the page", () => {
        expect(String(SessionsAccess.reasonLabel("country"))).toBe("It was used from another country");
        expect(String(SessionsAccess.reasonLabel("browser"))).toBe("It was used from another browser");
        expect(String(SessionsAccess.reasonLabel("inactivity"))).toBe("Inactivity");
    });

    test("the current session is apart from the others", () => {
        const comp = page({ sessions: SESSIONS });
        expect(comp.currentSession.browsers).toEqual(["Chrome"]);
        expect(comp.otherSessions.map((s) => s.browsers[0])).toEqual(["Firefox"]);
    });

    test("a session names its browsers on its systems", () => {
        const comp = page();
        expect(String(comp.device({ browsers: ["Chrome"], platforms: ["Linux"] }))).toBe("Chrome on Linux");
        expect(String(comp.device({ browsers: ["Chrome"], platforms: [] }))).toBe("Chrome");
        expect(String(comp.device({ browsers: [], platforms: [] }))).toBe("Unknown device");
    });

    test("Sign out everywhere asks nothing and leaves for the sign-in", async () => {
        const sent = answering("/cloud/sessions/sign_out_everywhere", { ok: true, redirect: "/web/login" });
        const comp = page({ sessions: SESSIONS });
        await comp.signOutEverywhere();
        expect(sent).toEqual([{}]);
        expect(comp.left).toEqual(["/web/login"]);
    });

    test("a refused Sign out everywhere stays and says so", async () => {
        answering("/cloud/sessions/sign_out_everywhere", { ok: false });
        const comp = page({ sessions: SESSIONS });
        await comp.signOutEverywhere();
        expect(comp.left).toEqual([]);
        expect(comp.toasts).toEqual([["error", "Could not sign out everywhere."]]);
        expect(comp.state.busy).toBe(false);
    });

    test("Revoke sends the way in and shows the page afresh", async () => {
        const fresh = { ok: true, sessions: [], closes: [], doors: [], links: [] };
        const sent = answering("/cloud/sessions/revoke", fresh);
        const comp = page({ sessions: SESSIONS, doors: [{ kind: "api_key", id: 4 }] });
        await comp.revoke({ kind: "api_key", id: 4 });
        expect(sent).toEqual([{ kind: "api_key", door_id: 4 }]);
        expect(comp.state.data).toBe(fresh);
        expect(comp.toasts).toEqual([["success", "Revoked."]]);
    });

    test("revoking a Git token says to revoke it at the Git server too", async () => {
        const fresh = { ok: true, sessions: [], closes: [], doors: [], links: [] };
        answering("/cloud/sessions/revoke", fresh);
        const comp = page({ sessions: SESSIONS, doors: [{ kind: "git_token", id: 7 }] });
        await comp.revoke({ kind: "git_token", id: 7 });
        expect(comp.toasts).toEqual([
            ["success", "Revoked here. Revoke the token in GitLab or GitHub too."],
        ]);
    });

    test("a refused Revoke keeps the page as it was", async () => {
        answering("/cloud/sessions/revoke", { ok: false });
        const data = { sessions: SESSIONS, doors: [{ kind: "api_key", id: 4 }] };
        const comp = page(data);
        await comp.revoke({ kind: "api_key", id: 4 });
        expect(comp.state.data).toBe(data);
        expect(comp.toasts).toEqual([["error", "Could not revoke it."]]);
    });
});
