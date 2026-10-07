import { Component, useState } from "@odoo/owl";

export class ToastContainer extends Component {
    static template = "incubacloud.ToastContainer";
    static props = {};

    setup() {
        this.toasts = useState(this.env.toasts || []);
    }

    /**
     * Close one toast.
     *
     * @param {number} id the toast to dismiss
     */
    dismiss(id) {
        this.env.dismissToast?.(id);
    }

    /**
     * Run a toast's action and close it: the action is how the user
     * deals with what the toast reported.
     *
     * @param {{id: number, action: {onClick: Function}}} toast
     */
    runAction(toast) {
        this.dismiss(toast.id);
        toast.action?.onClick?.();
    }
}
