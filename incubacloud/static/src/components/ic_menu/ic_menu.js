import { Component, useState, useRef, useEffect, useExternalListener } from "@odoo/owl";

/**
 * Dropdown menu opened by a button («More ▾»).
 *
 * The default slot holds the entries, usually ``<button>`` elements: each
 * one keeps its own ``t-on-click`` handler, and a click on any of them closes
 * the menu afterwards. Entries rendered conditionally by the caller simply
 * do not appear; with no entry at all the trigger is still drawn and opens
 * an empty list, so the caller decides whether to render the menu.
 *
 * Keyboard: Enter, Space or Arrow Down on the trigger open it and focus
 * the first entry; Arrow Up/Down, Home and End move between entries; Esc
 * closes it and returns the focus to the trigger; Tab leaves it and
 * closes it. A click outside also closes it.
 *
 * The list opens on the side of the trigger that has room for it, and
 * never past the viewport (``IcMenu.place``).
 */
export class IcMenu extends Component {
    static template = "incubacloud.IcMenu";
    static props = {
        /** Text of the trigger button. */
        label: { type: String },
        /** Optional extra classes for the trigger button. */
        triggerClass: { type: String, optional: true },
        slots: { type: Object, optional: true },
    };

    /**
     * Index of the entry that a navigation key lands on.
     *
     * @param {number} count how many entries the menu has.
     * @param {number} current index of the focused entry, -1 when none.
     * @param {string} key the ``KeyboardEvent.key``.
     * @returns {number} the index to focus, or -1 if the key does not
     *   navigate (or there is nothing to focus).
     */
    static nextIndex(count, current, key) {
        if (!count) return -1;
        switch (key) {
            case "ArrowDown": return current < 0 ? 0 : (current + 1) % count;
            case "ArrowUp": return current <= 0 ? count - 1 : current - 1;
            case "Home": return 0;
            case "End": return count - 1;
            default: return -1;
        }
    }

    /**
     * Where the list goes, in viewport pixels, for a trigger at a given
     * place.
     *
     * It starts at the trigger's left edge and grows to the right; when
     * that would pass the viewport, it ends at the trigger's right edge
     * and grows to the left; when neither fits, it is pushed back inside.
     * It is never wider than the viewport less its margins.
     *
     * @param {Object} geometry
     * @param {number} geometry.triggerLeft left edge of the trigger.
     * @param {number} geometry.triggerRight right edge of the trigger.
     * @param {number} geometry.width the list's own width.
     * @param {number} geometry.viewport the viewport's width.
     * @param {number} [geometry.margin=16] room kept on each side.
     * @returns {{left: number, maxWidth: number}} the list's left edge
     *   and its widest.
     */
    static place({ triggerLeft, triggerRight, width, viewport, margin = 16 }) {
        const maxWidth = Math.max(0, viewport - 2 * margin);
        const w = Math.min(width, maxWidth);
        let left = triggerLeft;
        if (left + w > viewport - margin) {
            left = triggerRight - w;
        }
        left = Math.min(Math.max(left, margin), viewport - margin - w);
        return { left, maxWidth };
    }

    /**
     * Set up the open state, the refs, the close on a pointer press outside,
     * the placement and the focus of the first entry when the menu opens,
     * and the placement again when the window is resized while open.
     */
    setup() {
        this.state = useState({ open: false });
        this.root = useRef("root");
        this.trigger = useRef("trigger");
        this.list = useRef("list");
        useExternalListener(window, "pointerdown", (ev) => {
            if (this.state.open && !this.root.el?.contains(ev.target)) {
                this.state.open = false;
            }
        }, { capture: true });
        useExternalListener(window, "resize", () => {
            if (this.state.open) this.position();
        });
        // On opening, the list is placed, the entries get their ARIA role
        // and the first one takes the focus.
        useEffect((open) => {
            if (!open) return;
            this.position();
            const items = this.items;
            items.forEach((el) => el.setAttribute("role", "menuitem"));
            items[0]?.focus();
        }, () => [this.state.open]);
    }

    /**
     * Place the open list beside its trigger, inside the viewport
     * (``IcMenu.place``). The list is positioned against the menu's root,
     * so the left edge computed in the viewport is made relative to it.
     */
    position() {
        const list = this.list.el;
        const trigger = this.trigger.el;
        const root = this.root.el;
        if (!list || !trigger || !root) return;
        // Measured at its natural width, before any earlier placement.
        list.style.left = "0px";
        list.style.maxWidth = "";
        const t = trigger.getBoundingClientRect();
        const { left, maxWidth } = IcMenu.place({
            triggerLeft: t.left,
            triggerRight: t.right,
            width: list.getBoundingClientRect().width,
            viewport: document.documentElement.clientWidth,
        });
        list.style.maxWidth = `${maxWidth}px`;
        list.style.left = `${left - root.getBoundingClientRect().left}px`;
    }

    /** @returns {HTMLElement[]} the entries that can take the focus. */
    get items() {
        const el = this.list.el;
        return el ? [...el.querySelectorAll("button:not([disabled]), a[href]")] : [];
    }

    /** Open the menu when closed, close it otherwise. */
    toggle() {
        this.state.open = !this.state.open;
    }

    /**
     * Close the menu.
     *
     * @param {boolean} [refocus=false] give the focus back to the trigger.
     */
    close(refocus = false) {
        this.state.open = false;
        if (refocus) this.trigger.el?.focus();
    }

    /**
     * Keys on the trigger: Arrow Down opens the menu.
     *
     * @param {KeyboardEvent} ev
     */
    onTriggerKeydown(ev) {
        if (ev.key === "ArrowDown" && !this.state.open) {
            ev.preventDefault();
            this.state.open = true;
        }
    }

    /**
     * Keys inside the menu: Esc closes, Tab leaves, arrows navigate.
     *
     * @param {KeyboardEvent} ev
     */
    onKeydown(ev) {
        if (!this.state.open) return;
        if (ev.key === "Escape") {
            ev.stopPropagation();
            this.close(true);
            return;
        }
        if (ev.key === "Tab") {
            // The list is hidden on the next render: without taking the
            // focus back now, Tab lands on an entry that then disappears.
            ev.preventDefault();
            this.close(true);
            return;
        }
        const items = this.items;
        const index = IcMenu.nextIndex(
            items.length, items.indexOf(document.activeElement), ev.key);
        if (index >= 0) {
            ev.preventDefault();
            items[index].focus();
        }
    }

    /**
     * A click on an entry (not on the gaps between them) closes the menu.
     *
     * @param {MouseEvent} ev
     */
    onListClick(ev) {
        if (ev.target.closest("button, a")) {
            // A keyboard activation keeps the focus on the entry, which is
            // about to be hidden: give it back to the trigger.
            this.close(this.list.el.contains(document.activeElement));
        }
    }
}
