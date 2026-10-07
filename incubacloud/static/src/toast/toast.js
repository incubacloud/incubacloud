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
 *
 * ``error`` and ``warning`` take an optional second argument, an action
 * ``{label, onClick}`` rendered as a button inside the toast. A message
 * identical to one already on screen is not stacked again, and at most
 * ``MAX_TOASTS`` stay visible: the oldest makes room for the newest.
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

// Errors persist, so a poll failing in a loop would otherwise pile up
// toasts until they cover the page. Every one of them is also in the
// alerts bell when it matters.
const MAX_TOASTS = 5;

let _nextId = 1;

export function createToastService() {
    const toasts = reactive([]);
    const _timers = new Map();

    /**
     * Show a toast, unless the same one is already on screen.
     *
     * @param {string} type success | error | warning | info
     * @param {string} message text shown to the user
     * @param {number} duration milliseconds before it leaves; 0 keeps it
     * @param {{label: string, onClick: Function}} [action] optional button
     * @returns {number} the id of the toast shown, or of the identical one
     *     already visible
     */
    function _add(type, message, duration, action) {
        const twin = toasts.find(
            (t) => !t.dismissing && t.type === type && t.message === message,
        );
        if (twin) return twin.id;
        const id = _nextId++;
        toasts.push({ id, type, message, icon: ICONS[type] || ICONS.info, action });
        if (duration > 0) {
            _timers.set(id, setTimeout(() => _dismiss(id), duration));
        }
        const visible = toasts.filter((t) => !t.dismissing);
        if (visible.length > MAX_TOASTS) _dismiss(visible[0].id);
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

    /**
     * Dismiss the most recent toast still on screen (Escape).
     *
     * @returns {boolean} whether there was one to dismiss
     */
    function _dismissLatest() {
        const latest = toasts.findLast((t) => !t.dismissing);
        if (!latest) return false;
        _dismiss(latest.id);
        return true;
    }

    const toastApi = {
        success: (msg) => _add("success", msg, DURATIONS.success),
        error:   (msg, action) => _add("error",   msg, DURATIONS.error, action),
        warning: (msg, action) => _add("warning", msg, DURATIONS.warning, action),
        info:    (msg) => _add("info",    msg, DURATIONS.info),
    };

    return {
        toastApi,
        toasts,
        dismissToast: _dismiss,
        dismissLatestToast: _dismissLatest,
    };
}

// Re-export constants for tests
export { DURATIONS, ICONS, MAX_TOASTS };
