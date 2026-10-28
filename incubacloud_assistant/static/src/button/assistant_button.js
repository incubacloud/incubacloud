/** @odoo-module **/

import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { buttonView } from "../assistant_format";
import { AssistantPanel } from "../panel/assistant_panel";

/** The assistant's button in the top bar; it opens the side panel. */
export class AssistantButton extends Component {
    static template = "incubacloud_assistant.AssistantButton";
    static components = { AssistantPanel };
    static props = {};

    /** Read whether the assistant is on for the person; follow its state changes. */
    setup() {
        this.state = useState({ status: { available: false }, open: false });
        this.bus = useService("bus_service");
        this._onEvent = (payload) => {
            if (payload?.kind === "state") {
                this.refresh();
            }
        };
        // Asked once mounted, not before: the top bar never waits for it.
        onMounted(() => {
            this.refresh();
            this.bus.subscribe("cloud_assistant", this._onEvent);
        });
        onWillUnmount(() => this.bus.unsubscribe("cloud_assistant", this._onEvent));
    }

    /** Ask the server again whether the assistant is on, and what is unread. */
    async refresh() {
        try {
            this.state.status = await rpc("/cloud/assistant/status", {});
        } catch {
            // A passing failure keeps what was there: an open panel stays open.
        }
    }

    /** What the button shows. */
    get view() {
        return buttonView(this.state.status);
    }

    /** Open or close the side panel. */
    toggle() {
        this.state.open = !this.state.open;
    }
}
