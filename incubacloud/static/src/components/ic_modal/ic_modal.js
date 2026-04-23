/**
 * IcModal — accessible wrapper for structured modals (non-confirm).
 *
 * Why this exists:
 *   The SPA ships several bespoke dialogs (clone-to-staging, restore,
 *   shell, sync, pip conflicts, addon conflicts, notification detail,
 *   etc.) that duplicate the same overlay+box markup and never declare
 *   ``role="dialog"`` / ``aria-modal`` / focus trap / Esc handling.
 *   Instead of patching 10+ templates with the same five attributes and
 *   five hook calls, consumers wrap their body in <IcModal> and get
 *   those for free.
 *
 * Props:
 *   - ``onClose``       : function, fired on Esc, overlay click or the
 *                         close affordance the consumer wires up.
 *   - ``title``         : string (optional). When provided, exposed via
 *                         ``aria-labelledby`` so screen readers announce
 *                         the dialog. Consumers still render their own
 *                         visible heading — pass the same string.
 *   - ``boxClass``      : string (optional). Override the default
 *                         ``.ic-confirm-box`` when the consumer needs a
 *                         wider layout (e.g. ``ic-sync-box``,
 *                         ``ic-restore-box``).
 *   - ``closeOnOverlay``: boolean (default true). Set false when an
 *                         in-flight async action must not be cancelled
 *                         by a stray backdrop click.
 *
 * Slot:
 *   Default slot renders inside the dialog box — no structural
 *   constraints, so the existing markup for each modal survives
 *   verbatim.
 *
 * Accessibility:
 *   - ``role="dialog"`` + ``aria-modal="true"`` on the box.
 *   - ``aria-labelledby`` wired to an internal id; consumers that pass
 *     ``title`` get it announced. Otherwise the dialog is still modal
 *     but announced generically.
 *   - Esc fires ``onClose`` (capture-phase, so it wins over any
 *     underlying page handler).
 *   - Focus trap: Tab/Shift-Tab cycle inside the dialog only.
 *   - On mount we focus the first focusable element (usually an input
 *     or primary button). On unmount we restore focus to whatever had
 *     it before opening.
 */
import {
    Component,
    onMounted,
    onWillUnmount,
    useRef,
} from "@odoo/owl";

let _titleIdSeq = 0;

const FOCUSABLE_SEL = [
    "a[href]",
    "button:not([disabled])",
    "input:not([disabled]):not([type=hidden])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    "[tabindex]:not([tabindex='-1'])",
].join(",");

export class IcModal extends Component {
    static template = "incubacloud.IcModal";
    static props = {
        onClose: { type: Function },
        title: { type: String, optional: true },
        boxClass: { type: String, optional: true },
        closeOnOverlay: { type: Boolean, optional: true },
        slots: { type: Object, optional: true },
    };

    setup() {
        this.boxRef = useRef("box");
        this.titleId = `ic-modal-title-${++_titleIdSeq}`;
        this._previouslyFocused = document.activeElement;

        this._onKeyDown = (ev) => {
            if (ev.key === "Escape") {
                ev.stopPropagation();
                this.props.onClose();
                return;
            }
            if (ev.key === "Tab") {
                const box = this.boxRef.el;
                if (!box) return;
                const focusables = [...box.querySelectorAll(FOCUSABLE_SEL)]
                    .filter((el) => el.offsetParent !== null);
                if (!focusables.length) {
                    ev.preventDefault();
                    return;
                }
                const first = focusables[0];
                const last = focusables[focusables.length - 1];
                const active = document.activeElement;
                if (ev.shiftKey && (active === first || !box.contains(active))) {
                    ev.preventDefault();
                    last.focus();
                } else if (!ev.shiftKey && (active === last || !box.contains(active))) {
                    ev.preventDefault();
                    first.focus();
                }
            }
        };

        onMounted(() => {
            document.addEventListener("keydown", this._onKeyDown, true);
            const box = this.boxRef.el;
            if (!box) return;
            // Prefer an explicit autofocus element, else the first
            // focusable descendant — gives screen-reader users a
            // deterministic landing spot.
            const auto = box.querySelector("[autofocus]");
            const first = auto || box.querySelector(FOCUSABLE_SEL);
            first?.focus();
        });

        onWillUnmount(() => {
            document.removeEventListener("keydown", this._onKeyDown, true);
            const target = this._previouslyFocused;
            if (target && typeof target.focus === "function") {
                Promise.resolve().then(() => {
                    try { target.focus(); } catch (_) { /* stale ref */ }
                });
            }
        });
    }

    onOverlayClick() {
        if (this.props.closeOnOverlay === false) return;
        this.props.onClose();
    }

    get resolvedBoxClass() {
        return this.props.boxClass || "ic-confirm-box";
    }
}
