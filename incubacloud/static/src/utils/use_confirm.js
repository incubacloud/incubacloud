/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";

/**
 * Open the SPA's own confirmation dialog and resolve with the answer.
 *
 * Five components carried a byte-for-byte copy of this promise/dialog
 * dance. One implementation means a fix (or an a11y improvement) lands
 * everywhere at once instead of in whichever copy someone remembered.
 *
 * The dialog is driven through component state rather than a service on
 * purpose: this is the SPA's own ``IcConfirmDialog``, never Odoo's
 * ``ConfirmationDialog``, whose chrome does not match the panel.
 *
 * @param {object} state Reactive component state holding ``confirmDialog``
 * @param {object} opts
 * @param {string} opts.title
 * @param {string} opts.message
 * @param {string} [opts.confirmLabel]
 * @param {boolean} [opts.isDanger] Style the action as destructive
 * @returns {Promise<boolean>} True when confirmed, false when dismissed
 */
export function confirmVia(
    state,
    { title, message, confirmLabel = _t("Confirm"), isDanger = false },
) {
    return new Promise((resolve) => {
        state.confirmDialog = {
            title,
            message,
            confirmLabel,
            isDanger,
            onConfirm: () => {
                state.confirmDialog = null;
                resolve(true);
            },
            onCancel: () => {
                state.confirmDialog = null;
                resolve(false);
            },
        };
    });
}
