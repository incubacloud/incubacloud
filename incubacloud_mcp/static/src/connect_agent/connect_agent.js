/** @odoo-module **/

import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { API_KEY_ENV, API_KEY_PLACEHOLDER, connectView } from "./connect_agent_format";

/** How long a Copy button says it copied. */
const COPIED_MS = 2000;

/** A command or an address the user copies: the text and its Copy button. */
export class CopyBlock extends Component {
    static template = "incubacloud_mcp.CopyBlock";
    static props = {
        text: String,
        inline: { type: Boolean, optional: true },
    };

    /** Nothing copied yet. */
    setup() {
        this.state = useState({ copied: false });
        onWillUnmount(() => clearTimeout(this._copiedTimer));
    }

    /** Copy the text; the button says so for a moment. */
    async copy() {
        try {
            await navigator.clipboard.writeText(this.props.text);
        } catch {
            this.env.toast?.error(_t("Could not copy. Select the text and copy it by hand."));
            return;
        }
        this.state.copied = true;
        clearTimeout(this._copiedTimer);
        this._copiedTimer = setTimeout(() => {
            this.state.copied = false;
        }, COPIED_MS);
    }
}

/** "Connect an agent": this panel's MCP URL and how each client uses it. */
export class ConnectAgent extends Component {
    static template = "incubacloud_mcp.ConnectAgent";
    static components = { CopyBlock };
    static props = {};

    /** Load what the page shows; Claude Code's instructions first. */
    setup() {
        this.apiKeyEnv = API_KEY_ENV;
        this.apiKeyPlaceholder = API_KEY_PLACEHOLDER;
        this.state = useState({ loading: true, config: null, tab: "claude_code" });
        onWillStart(() => this.load());
    }

    /** Read what to show from `/cloud/mcp/config`. */
    async load() {
        try {
            this.state.config = await rpc("/cloud/mcp/config", {});
        } catch {
            this.env.toast?.error(_t("Could not load how to connect an agent."));
        } finally {
            this.state.loading = false;
        }
    }

    /** Everything the template shows (see `connectView`). */
    get view() {
        return connectView(this.state.config || {});
    }

    /**
     * Show the instructions of one client.
     *
     * @param {string} tab the client's tab
     */
    setTab(tab) {
        this.state.tab = tab;
    }
}
