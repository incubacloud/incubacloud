import { Component, useState } from "@odoo/owl";

export class ToastContainer extends Component {
    static template = "incubacloud.ToastContainer";
    static props = {};

    setup() {
        this.toasts = useState(this.env.toasts || []);
    }

    dismiss(id) {
        this.env.dismissToast?.(id);
    }
}
