import { Component, useState, useRef, useEffect, useExternalListener } from "@odoo/owl";

/**
 * Dropdown menu opened by a button («More ▾»).
 *
 * The default slot holds the entries, usually ``<button>`` elements: each
 * one keeps its own ``t-on-click`` handler, and a click on any of them closes
 * the menu afterwards. Entries rendered conditionally by the caller simply
 * do not appear; with no entry at all the menu renders nothing.
 *
 * Keyboard: Enter, Space or Arrow Down on the trigger open it and focus
 * the first entry; Arrow Up/Down, Home and End move between entries; Esc
 * closes it and returns the focus to the trigger; Tab leaves it and
 * closes it. A click outside also closes it.
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
        // On opening, the entries get their ARIA role and the first
        // one takes the focus.
        useEffect((open) => {
            if (!open) return;
            const items = this.items;
            items.forEach((el) => el.setAttribute("role", "menuitem"));
            items[0]?.focus();
        }, () => [this.state.open]);
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
            this.close();
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
        if (ev.target.closest("button, a")) this.close();
    }
}
