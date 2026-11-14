import { describe, expect, test } from "@odoo/hoot";
import { animationFrame } from "@odoo/hoot-mock";
import { Component, onWillStart, useState, xml } from "@odoo/owl";
import { mountWithCleanup } from "@web/../tests/web_test_helpers";
import { useSettingsSave } from "@incubacloud/components/settings_form/use_settings_save";

/**
 * useSettingsSave: a Settings block joins the save bar once it loaded and
 * while mounted, and leaves on unmount only what it joined. Mounted for
 * real, under an env that records what Settings is told.
 */

/** The blocks mounted, by name, to reach their hook. */
let blocks = {};

/** A block that loads (or fails to) in onWillStart, then joins under its key. */
class Block extends Component {
    static template = xml`<div class="block" t-esc="props.name"/>`;
    static props = { name: String, saveKey: String, loads: Boolean };

    /** Load as the props say, and join through the hook. */
    setup() {
        this.loaded = false;
        this.state = useState({ form: { value: 1 } });
        onWillStart(() => {
            this.loaded = this.props.loads;
        });
        this.settingsSave = useSettingsSave({
            key: () => this.props.saveKey,
            save: async () => ({ ok: true }),
            state: this.state,
            writeOnly: ["secret"],
            ready: () => this.loaded,
        });
        blocks[this.props.name] = this;
    }
}

/** A page of blocks, which a test can take down one by one. */
class Page extends Component {
    static template = xml`
        <div>
            <t t-foreach="state.blocks" t-as="b" t-key="b.name">
                <Block name="b.name" saveKey="b.saveKey" loads="b.loads"/>
            </t>
        </div>`;
    static components = { Block };
    static props = { blocks: Array };

    /** Keep the blocks in a state, to drop them later. */
    setup() {
        this.state = useState({ blocks: [...this.props.blocks] });
    }

    /**
     * Take a block down.
     *
     * @param {string} name the block's name
     */
    drop(name) {
        this.state.blocks = this.state.blocks.filter((b) => b.name !== name);
    }
}

/**
 * Mount a page of blocks under an env that records Settings' calls.
 *
 * @param {object[]} list the blocks: ``{name, saveKey, loads}``
 * @returns {Promise<{page: Page, calls: Array[], tabs: Map}>} the page,
 *     the calls in order and what Settings holds by key
 */
async function mountPage(list) {
    blocks = {};
    const calls = [];
    const tabs = new Map();
    const env = {
        registerSettingsSave: (key, save, state, writeOnly) => {
            calls.push(["register", key, writeOnly]);
            tabs.set(key, state);
        },
        unregisterSettingsSave: (key) => {
            calls.push(["unregister", key]);
            tabs.delete(key);
        },
    };
    const page = await mountWithCleanup(Page, {
        props: { blocks: list }, env, noMainContainer: true,
    });
    return { page, calls, tabs };
}

describe("useSettingsSave", () => {
    test("a block that loaded joins once mounted", async () => {
        const { calls, tabs } = await mountPage([{ name: "a", saveKey: "A", loads: true }]);
        expect(calls).toEqual([["register", "A", ["secret"]]]);
        expect(tabs.get("A")).toBe(blocks.a.state);
    });

    test("one that did not load joins only once register finds it ready", async () => {
        const { calls } = await mountPage([{ name: "a", saveKey: "A", loads: false }]);
        expect(calls).toEqual([]);
        blocks.a.settingsSave.register();
        expect(calls).toEqual([]);
        blocks.a.loaded = true;
        blocks.a.settingsSave.register();
        expect(calls).toEqual([["register", "A", ["secret"]]]);
    });

    test("unmounted, it leaves under the key it joined", async () => {
        const { page, calls, tabs } = await mountPage([{ name: "a", saveKey: "A", loads: true }]);
        page.drop("a");
        await animationFrame();
        expect(calls).toEqual([["register", "A", ["secret"]], ["unregister", "A"]]);
        expect(tabs.has("A")).toBe(false);
    });

    test("one that never joined leaves another block's key alone", async () => {
        const { page, calls, tabs } = await mountPage([
            { name: "a", saveKey: "P", loads: true },
            { name: "b", saveKey: "P", loads: false },
        ]);
        expect(calls).toEqual([["register", "P", ["secret"]]]);
        page.drop("b");
        await animationFrame();
        expect(calls).toEqual([["register", "P", ["secret"]]]);
        expect(tabs.get("P")).toBe(blocks.a.state);
        page.drop("a");
        await animationFrame();
        expect(calls).toEqual([["register", "P", ["secret"]], ["unregister", "P"]]);
    });

    test("once unmounted, register does nothing", async () => {
        const { page, calls } = await mountPage([{ name: "a", saveKey: "A", loads: false }]);
        const block = blocks.a;
        page.drop("a");
        await animationFrame();
        block.loaded = true;
        block.settingsSave.register();
        expect(calls).toEqual([]);
    });
});
