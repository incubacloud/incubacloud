/**
 * useDialogA11y — add a11y behavior to bespoke modals that can't adopt
 * <IcModal> wholesale (because they have structured header/body/footer
 * markup of their own, e.g. create_project_modal, import_odoojs_modal,
 * remote_file_browser).
 *
 * What it adds:
 *   - Esc key handler that invokes ``onClose``.
 *   - Focus trap between the first/last focusable descendant of the
 *     ref'd dialog box.
 *   - Initial focus on mount (first ``[autofocus]`` or first
 *     focusable).
 *   - Focus restoration to ``document.activeElement`` captured at
 *     setup() time.
 *
 * What it does NOT do:
 *   - Set ``role="dialog"`` / ``aria-modal`` — those are declarative
 *     and belong in the template next to the markup. Consumers should
 *     add them inline on the box element.
 *
 * Usage:
 *   setup() {
 *       this.boxRef = useRef("box");
 *       useDialogA11y(this.boxRef, () => this.close());
 *   }
 *
 *   <div class="ic-modal" t-ref="box" role="dialog" aria-modal="true">
 *       ...
 *   </div>
 */
import { onMounted, onWillUnmount } from "@odoo/owl";

const FOCUSABLE_SEL = [
    "a[href]",
    "button:not([disabled])",
    "input:not([disabled]):not([type=hidden])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    "[tabindex]:not([tabindex='-1'])",
].join(",");

export function useDialogA11y(boxRef, onClose) {
    const previouslyFocused = document.activeElement;

    const onKeyDown = (ev) => {
        if (ev.key === "Escape") {
            ev.stopPropagation();
            onClose();
            return;
        }
        if (ev.key === "Tab") {
            const box = boxRef.el;
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
        document.addEventListener("keydown", onKeyDown, true);
        const box = boxRef.el;
        if (!box) return;
        const auto = box.querySelector("[autofocus]");
        const first = auto || box.querySelector(FOCUSABLE_SEL);
        first?.focus();
    });

    onWillUnmount(() => {
        document.removeEventListener("keydown", onKeyDown, true);
        if (previouslyFocused && typeof previouslyFocused.focus === "function") {
            Promise.resolve().then(() => {
                try { previouslyFocused.focus(); } catch (_) { /* stale ref */ }
            });
        }
    });
}
