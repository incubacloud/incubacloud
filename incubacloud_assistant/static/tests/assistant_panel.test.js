import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { allowTranslations, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import { AssistantPanel } from "@incubacloud_assistant/panel/assistant_panel";

/**
 * The panel's calls (Stop, Retry): the answer of the server never replaces a
 * state the bus delivered while the call was on its way.
 *
 * No mounting (see assistant_settings_tab.test.js): the shipped methods on a
 * stand-in holding the state they read; the server is ``rpc``.
 */

/**
 * A panel stand-in with the conversation 4 open, working.
 *
 * @returns {object}
 */
function panel() {
    const comp = Object.create(AssistantPanel.prototype);
    comp.stateEvents = 0;
    comp.props = { onChange: () => {} };
    comp.state = {
        view: "chat",
        error: "",
        stopAsked: false,
        messages: [],
        progress: "",
        conversation: { id: 4, state: "working", stopping: false },
    };
    return comp;
}

describe("AssistantPanel calls", () => {
    beforeEach(() => allowTranslations());

    test("the answer of Stop is the new state when nothing came meanwhile", async () => {
        const comp = panel();
        patchWithCleanup(rpc, {
            _rpc: () => Promise.resolve({ conversation: { id: 4, state: "working", stopping: true } }),
        });
        await comp.stop();
        expect(comp.state.conversation.stopping).toBe(true);
        expect(comp.state.stopAsked).toBe(false);
    });

    test("a stopped event that comes before the answer of Stop is not overwritten", async () => {
        const comp = panel();
        patchWithCleanup(rpc, {
            _rpc() {
                // The action ended in milliseconds: the bus is faster than the reply.
                comp.onEvent({ conversation_id: 4, kind: "state", state: "stopped" });
                return Promise.resolve({ conversation: { id: 4, state: "working", stopping: true } });
            },
        });
        await comp.stop();
        expect(comp.state.conversation.state).toBe("stopped");
        expect(comp.state.conversation.stopping).toBe(false);
    });

    test("an event of another conversation does not hide the answer", async () => {
        const comp = panel();
        patchWithCleanup(rpc, {
            _rpc() {
                comp.onEvent({ conversation_id: 9, kind: "state", state: "stopped" });
                return Promise.resolve({ conversation: { id: 4, state: "working", stopping: true } });
            },
        });
        await comp.stop();
        expect(comp.state.conversation.stopping).toBe(true);
    });
});
