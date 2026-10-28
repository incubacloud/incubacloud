import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { click, waitFor } from "@odoo/hoot-dom";
import { animationFrame } from "@odoo/hoot-mock";
import { Component, markup, useState, xml } from "@odoo/owl";
import {
    allowTranslations,
    mountWithCleanup,
    patchWithCleanup,
} from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import { HostHardeningModal } from "@incubacloud/components/host_hardening_modal/host_hardening_modal";
import { relayIcon } from "@incubacloud/utils/icons";

/**
 * The hardening dialog's production gate (27-oct-2026).
 *
 * "Type <name> to confirm:" was two terms around the name, and "Type" is
 * also the noun of every type column: Spanish read "Tipo <name> para
 * confirmar:". The sentence is now one term with the name as its
 * placeholder, drawn as markup so the name keeps its code style.
 *
 * No mounting (see ic_confirm_dialog.test.js): the getter under test is
 * the shipped one, read on a stand-in holding the props it reads.
 */

/**
 * The gate's sentence for a host called *hostName*.
 *
 * @param {string} hostName
 * @returns {unknown} what the getter returns
 */
function prompt(hostName) {
    const comp = Object.create(HostHardeningModal.prototype);
    comp.props = { hostName };
    return comp.prodConfirmPrompt;
}

describe("HostHardeningModal production gate", () => {
    beforeEach(() => allowTranslations());

    test("asks in one sentence, the name drawn as code", () => {
        const sentence = prompt("saas-paid-202610082249");
        expect(sentence).toBeInstanceOf(markup("").constructor);
        expect(String(sentence)).toBe(
            'Type <code class="rl-mono">saas-paid-202610082249</code> to confirm:'
        );
    });

    test("a host name is shown, never read as markup", () => {
        expect(String(prompt("<img src=x onerror=alert(1)>"))).toBe(
            'Type <code class="rl-mono">&lt;img src=x onerror=alert(1)&gt;</code> to confirm:'
        );
    });
});

/**
 * Who can open SSH after the hardening (plan-lista-ssh-sin-relleno).
 *
 * The dialog says it in every mode, and the asker's own address enters
 * the host's list only by its checkbox: nothing is added by itself.
 * The getters and ``_apply`` are the shipped ones, on a stand-in; the
 * server is ``rpc`` answering what each test says.
 */

const PANEL = "95.217.1.126";
const PERSON = "81.40.12.7";

/**
 * A dialog stand-in whose preflight said *access* about SSH.
 *
 * @param {object} access the preflight's ``ssh_access``
 * @param {object} [options]
 * @param {boolean} [options.ticked=false] the box as the user left it
 * @param {Array} [options.applied] receives each ``onApplied`` call
 * @returns {object} the stand-in
 */
function dialog(access, { ticked = false, applied = [] } = {}) {
    const comp = Object.create(HostHardeningModal.prototype);
    comp.env = {};
    comp.props = {
        hostId: 7,
        hostName: "pool-1",
        onClose: () => {},
        onApplied: (...args) => applied.push(args),
    };
    comp.state = {
        loading: false,
        error: null,
        preflight: {
            ok: true,
            prod_running: 0,
            has_known_hosts_key: true,
            has_active_job: false,
            ssh_access: access,
        },
        prodConfirmInput: "",
        submitting: false,
        allowMyIp: ticked,
    };
    return comp;
}

/**
 * The preflight's ``ssh_access`` for a host in *state*.
 *
 * @param {string} state ``open``, ``edge``, ``panel_only`` or ``list``
 * @param {object} [fields] what else it says
 * @returns {object}
 */
function access(state, fields = {}) {
    return {
        state,
        panel_ip: PANEL,
        entries: [],
        person: "",
        person_ip: "",
        person_family: "",
        host_family: "",
        ...fields,
    };
}

/**
 * Have the server answer the apply with *answer*; return what it was sent.
 *
 * @param {object} answer what ``/cloud/apply_host_hardening`` answers
 * @returns {Array} the params of every apply request, as they arrive
 */
function answeringApply(answer) {
    const sent = [];
    patchWithCleanup(rpc, {
        _rpc(url, params) {
            if (url === "/cloud/apply_host_hardening") {
                sent.push(params);
                return Promise.resolve(answer);
            }
            // ``announceJob`` asks how the job started.
            return Promise.resolve({ ok: true, job_id: 9, state: "pending" });
        },
    });
    return sent;
}

describe("HostHardeningModal says who can open SSH", () => {
    beforeEach(() => allowTranslations());

    test("only the panel: the box offers the asker's address", () => {
        const comp = dialog(access("panel_only", { person: "offer", person_ip: PERSON }));
        expect(comp.sshAccessLevel).toBe("warning");
        expect(String(comp.sshAccessText)).toBe(
            `Only this panel's server (${PANEL}) will be able to open SSH on this server. ` +
                "Any other address, yours included, will be refused."
        );
        expect(comp.showAllowMyIp).toBe(true);
        expect(String(comp.allowMyIpLabel)).toBe(`Also allow my IP (${PERSON})`);
    });

    test("unticked, the apply sends no address", async () => {
        const sent = answeringApply({ ok: true, job_ids: [9] });
        const applied = [];
        const comp = dialog(access("panel_only", { person: "offer", person_ip: PERSON }), {
            applied,
        });
        await comp.applyHardeningOnly();
        expect(sent).toEqual([{ host_id: 7, chain_full_setup: false }]);
        expect(applied).toEqual([[[9], undefined]]);
    });

    test("ticked, the apply sends the address shown and hands back the list", async () => {
        const list = `45.10.20.0/24, ${PERSON}`;
        const sent = answeringApply({ ok: true, job_ids: [9, 10], allowed_ssh_ips: list });
        const applied = [];
        const comp = dialog(
            access("list", { entries: ["45.10.20.0/24"], person: "offer", person_ip: PERSON }),
            { ticked: true, applied }
        );
        await comp.applyHardeningAndFullSetup();
        expect(sent).toEqual([{ host_id: 7, chain_full_setup: true, allow_ip: PERSON }]);
        expect(applied).toEqual([[[9, 10], list]]);
    });

    test("open to any address: a warning, and no box", async () => {
        const sent = answeringApply({ ok: true, job_ids: [9] });
        const comp = dialog(access("open", { panel_ip: "" }), { ticked: true });
        expect(comp.sshAccessLevel).toBe("warning");
        expect(String(comp.sshAccessText)).toBe(
            "SSH on this server will stay open to any address. Only key logins are " +
                "accepted, and fail2ban bans for an hour any address that fails three " +
                "times within ten minutes."
        );
        expect(comp.showAllowMyIp).toBe(false);
        expect(comp.sshPersonNote).toBe("");
        await comp.applyHardeningOnly();
        expect(sent).toEqual([{ host_id: 7, chain_full_setup: false }]);
    });

    test("closed by IncubaCloud's edge: said so, and no box", () => {
        const comp = dialog(access("edge", { panel_ip: "37.27.0.82" }));
        expect(comp.sshAccessLevel).toBe("info");
        expect(String(comp.sshAccessText)).toBe(
            "Only IncubaCloud and this panel's server can open SSH on this server: " +
                "a server IncubaCloud provides admits no other address."
        );
        expect(comp.showAllowMyIp).toBe(false);
    });

    test("an asker already allowed is told, and not counted as refused", () => {
        const comp = dialog(access("panel_only", { person: "allowed", person_ip: PANEL }));
        expect(String(comp.sshAccessText)).toBe(
            `Only this panel's server (${PANEL}) will be able to open SSH on this server. ` +
                "Any other address will be refused."
        );
        expect(comp.showAllowMyIp).toBe(false);
        expect(String(comp.sshPersonNote)).toBe(`Your IP (${PANEL}) is already allowed.`);
    });

    test("the list is named, with the panel or without it", () => {
        const withPanel = dialog(
            access("list", { entries: ["45.10.20.0/24", "foo"], person: "unknown" })
        );
        expect(withPanel.sshAccessLevel).toBe("info");
        expect(String(withPanel.sshAccessText)).toBe(
            `Only this panel's server (${PANEL}) and these addresses will be able to ` +
                "open SSH on this server: 45.10.20.0/24, foo."
        );
        expect(String(withPanel.sshPersonNote)).toBe(
            "This panel cannot tell your public address, so it cannot offer to allow it."
        );
        const corePanel = dialog(access("list", { panel_ip: "", entries: ["45.10.20.0/24"] }));
        expect(String(corePanel.sshAccessText)).toBe(
            "Only this panel's server and these addresses will be able to open SSH on " +
                "this server: 45.10.20.0/24."
        );
    });

    test("an address of the other family is explained, not offered", () => {
        const comp = dialog(
            access("panel_only", {
                person: "family",
                person_ip: "2a01:4f8:c0c:1234::1",
                person_family: "IPv6",
                host_family: "IPv4",
            })
        );
        expect(comp.showAllowMyIp).toBe(false);
        expect(String(comp.sshPersonNote)).toBe(
            "This panel sees you over IPv6 (2a01:4f8:c0c:1234::1), but SSH to this " +
                "server goes over IPv4, so allowing that address would not let you in."
        );
    });
});

/**
 * The real dialog, mounted as the host page mounts it: behind a ``t-if``
 * that a close turns off and the next opening turns back on, so each
 * opening is a new dialog. "Also allow my IP" must be unticked every
 * time: a tick left over from an opening the user cancelled would add
 * an address nobody chose to.
 */
describe("HostHardeningModal, mounted", () => {
    beforeEach(() => allowTranslations());

    test("the box is unticked on opening, and again on reopening", async () => {
        patchWithCleanup(rpc, {
            _rpc(url) {
                if (url !== "/cloud/preflight_host_hardening") {
                    return Promise.reject(new Error(`unexpected request to ${url}`));
                }
                return Promise.resolve({
                    ok: true,
                    host_id: 7,
                    host_name: "pool-1",
                    running_instances: 0,
                    prod_running: 0,
                    full_setup_done: true,
                    has_hardened_before: true,
                    has_known_hosts_key: true,
                    connecting_user: "incubacloud",
                    has_active_job: false,
                    active_job_queued: false,
                    ssh_access: access("panel_only", { person: "offer", person_ip: PERSON }),
                });
            },
        });

        class HostPage extends Component {
            static template = xml`
                <HostHardeningModal t-if="state.open" hostId="7" hostName="'pool-1'"
                                    onClose.bind="close"/>`;
            static components = { HostHardeningModal };
            static props = {};

            setup() {
                this.state = useState({ open: true });
            }

            /** Close the dialog, as the host page does. */
            close() {
                this.state.open = false;
            }
        }

        const hostPage = await mountWithCleanup(HostPage, {
            env: { icon: relayIcon },
            noMainContainer: true,
        });
        await waitFor("input.ic-hm-allow-ip-input");
        expect(".ic-hm-allow-ip").toHaveText(`Also allow my IP (${PERSON})`);
        expect("input.ic-hm-allow-ip-input").not.toBeChecked();

        await click("input.ic-hm-allow-ip-input");
        await animationFrame();
        expect("input.ic-hm-allow-ip-input").toBeChecked();

        await click(".ic-modal__footer button:first-child");
        await animationFrame();
        expect(".ic-modal").toHaveCount(0);

        hostPage.state.open = true;
        await waitFor("input.ic-hm-allow-ip-input");
        expect("input.ic-hm-allow-ip-input").not.toBeChecked();
    });
});
