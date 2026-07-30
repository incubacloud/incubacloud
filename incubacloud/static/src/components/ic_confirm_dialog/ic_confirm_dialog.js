/**
 * IcConfirmDialog — accessible confirm dialog reused across the SPA.
 *
 * Why this exists:
 *   The same ~15-line markup block lived in 8+ templates (host_detail,
 *   settings, instance_detail, backup_backend_detail,
 *   project_sidebar, app_header, alert_history). Each copy shipped
 *   without role=dialog, aria-modal, focus trap, Esc handler or
 *   focus restoration — so screen readers couldn't announce the
 *   dialog and a Tab keypress still cycled through the underlying
 *   page.
 *
 * Contract:
 *   Props mirror the legacy ``state.confirmDialog`` shape so the
 *   existing ``_confirm({ title, message, confirmLabel, isDanger })``
 *   helper in every consumer still works unchanged.
 *
 *   - ``title``          : string, required.
 *   - ``message``        : string, required.
 *   - ``confirmLabel``   : string (defaults to "Confirm").
 *   - ``isDanger``       : boolean (red confirm button).
 *   - ``onConfirm``      : function, fired on OK click / Enter.
 *   - ``onCancel``       : function, fired on Cancel click / Esc /
 *                          overlay click.
 *
 * Accessibility:
 *   - ``role="dialog"`` + ``aria-modal="true"`` + ``aria-labelledby``
 *     pointing at the <h3>.
 *   - Initial focus on the confirm button (muscle-memory friendly —
 *     Enter confirms).
 *   - Tab/Shift-Tab cycle between Cancel and Confirm only (focus
 *     trap).
 *   - Esc triggers ``onCancel``.
 *   - On unmount, restores focus to whatever had it before the
 *     dialog opened.
 */
import {
    Component,
    onMounted,
    onWillUnmount,
    useRef,
    useState,
} from "@odoo/owl";

let _titleIdSeq = 0;

export class IcConfirmDialog extends Component {
    static template = "incubacloud.IcConfirmDialog";
    static props = {
        title: { type: String },
        message: { type: String },
        confirmLabel: { type: String, optional: true },
        isDanger: { type: Boolean, optional: true },
        onConfirm: { type: Function },
        onCancel: { type: Function },
    };

    setup() {
        this.confirmBtnRef = useRef("confirmBtn");
        this.cancelBtnRef = useRef("cancelBtn");
        this.titleId = `ic-confirm-title-${++_titleIdSeq}`;

        // Stash whatever was focused so we can restore it when the
        // dialog closes — a screen-reader user lands back where they
        // were, not on <body>.
        this._previouslyFocused = document.activeElement;

        this._onKeyDown = (ev) => {
            if (ev.key === "Escape") {
                ev.stopPropagation();
                this.props.onCancel();
                return;
            }
            if (ev.key === "Tab") {
                // Focus trap between the two buttons. If neither is
                // current (unlikely — we focus confirm on mount) the
                // next Tab lands on Cancel.
                const focusables = [
                    this.cancelBtnRef.el,
                    this.confirmBtnRef.el,
                ].filter(Boolean);
                if (focusables.length < 2) return;
                const [first, last] = [
                    focusables[0], focusables[focusables.length - 1],
                ];
                const active = document.activeElement;
                if (ev.shiftKey && active === first) {
                    ev.preventDefault();
                    last.focus();
                } else if (!ev.shiftKey && active === last) {
                    ev.preventDefault();
                    first.focus();
                }
            }
        };

        onMounted(() => {
            // Capture phase so we see Esc/Tab before any consumer
            // handler in the underlying page tries to swallow them.
            document.addEventListener("keydown", this._onKeyDown, true);
            this.confirmBtnRef.el?.focus();
        });

        onWillUnmount(() => {
            document.removeEventListener("keydown", this._onKeyDown, true);
            // Defer focus restoration a tick so the element that had
            // focus is still in the DOM (some callers re-render right
            // after onCancel/onConfirm).
            const target = this._previouslyFocused;
            if (target && typeof target.focus === "function") {
                Promise.resolve().then(() => {
                    try { target.focus(); } catch (_) { /* stale ref */ }
                });
            }
        });

        // useState is only here so OWL warns if a consumer mutates
        // props directly. No internal state otherwise.
        this.state = useState({});
    }

    get confirmButtonClass() {
        return (
            "ic-confirm-btn-ok" +
            (this.props.isDanger ? " danger" : "")
        );
    }
}
