/** @odoo-module **/

import { onMounted, onWillUnmount, status, useComponent, useEnv } from "@odoo/owl";

/**
 * Join Settings' save bar with a block's form, once it loaded and while mounted.
 *
 * The block joins when it mounts, if ``ready()`` already holds, and leaves
 * when it unmounts. A block mounted before its form is ready (a load that
 * failed and shows Retry) joins later through ``register``. Only while
 * mounted: a retry can answer after Settings took the block down, and that
 * block must not stand in for the one now on screen. On unmount it leaves
 * only if it joined, under the key it joined with: another block sharing
 * the key, or one that never joined (read-only, or still unloaded), does
 * not take down a registration that is not its own.
 *
 * @param {object} opts
 * @param {string|function(): string} opts.key the key the block saves under
 * @param {function(): Promise<object>} opts.save sends the block's changes
 * @param {object} opts.state the block's state; its ``form`` is what the bar compares
 * @param {string[]|function(): string[]} [opts.writeOnly] fields the server
 *     never shows back; a function is read each time the block joins
 * @param {function(): boolean} opts.ready whether the form holds what the server has
 *     (and the block has something to save)
 * @returns {{register: function(): void}} ``register`` joins again: after a reload, or
 *     to take what is on screen as saved
 */
export function useSettingsSave({ key, save, state, writeOnly = [], ready }) {
    const component = useComponent();
    const env = useEnv();
    // The key this block joined under, or null while it has not joined.
    let joined = null;

    const register = () => {
        if (ready() && status(component) === "mounted") {
            joined = typeof key === "function" ? key() : key;
            const fields = typeof writeOnly === "function" ? writeOnly() : writeOnly;
            env.registerSettingsSave?.(joined, save, state, fields);
        }
    };

    onMounted(register);
    onWillUnmount(() => {
        if (joined !== null) {
            env.unregisterSettingsSave?.(joined);
            joined = null;
        }
    });
    return { register };
}
