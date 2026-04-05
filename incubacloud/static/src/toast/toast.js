/**
 * Toast notification service.
 *
 * Provides a global notification system exposed via ``useSubEnv`` so every
 * OWL component can call ``this.env.toast.success(msg)`` etc.
 *
 * Usage in app.js:
 *   import { createToastService } from "../toast/toast";
 *   const { toastApi, toasts, dismissToast } = createToastService();
 *   useSubEnv({ toast: toastApi, toasts, dismissToast });
 *
 * The ``toasts`` reactive array is consumed by ``ToastContainer`` to render
 * active notifications.
 */
import { reactive } from "@odoo/owl";

const DURATIONS = {
    success: 4000,
    error:   0,       // persistent — manual dismiss only
    warning: 6000,
    info:    4000,
};

const ICONS = {
    success: "fa-check-circle",
    error:   "fa-exclamation-triangle",
    warning: "fa-exclamation-circle",
    info:    "fa-info-circle",
};

let _nextId = 1;

export function createToastService() {
    const toasts = reactive([]);
    const _timers = new Map();

    function _add(type, message, duration) {
        const id = _nextId++;
        toasts.push({ id, type, message, icon: ICONS[type] || ICONS.info });
        if (duration > 0) {
            _timers.set(id, setTimeout(() => _dismiss(id), duration));
        }
        return id;
    }

    function _dismiss(id) {
        const toast = toasts.find((t) => t.id === id);
        if (!toast || toast.dismissing) return;
        const timer = _timers.get(id);
        if (timer) {
            clearTimeout(timer);
            _timers.delete(id);
        }
        // Trigger exit animation, then remove after it completes
        toast.dismissing = true;
        setTimeout(() => {
            const idx = toasts.findIndex((t) => t.id === id);
            if (idx !== -1) toasts.splice(idx, 1);
        }, 200);
    }

    const toastApi = {
        success: (msg) => _add("success", msg, DURATIONS.success),
        error:   (msg) => _add("error",   msg, DURATIONS.error),
        warning: (msg) => _add("warning", msg, DURATIONS.warning),
        info:    (msg) => _add("info",    msg, DURATIONS.info),
    };

    return { toastApi, toasts, dismissToast: _dismiss };
}

// Re-export constants for tests
export { DURATIONS, ICONS };
