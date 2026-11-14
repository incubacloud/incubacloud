import {
    Component,
    useEffect,
    useExternalListener,
    useRef,
    useState,
} from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

let nextId = 0;

/**
 * A record's tab bar, with the tabs that do not fit under «More ▾».
 *
 * The form standard keeps at most eight tabs in the bar; the rest go in
 * a list the «More» button opens. A tab is either in the bar or in the
 * list, never one or the other by the room left: the caller says which.
 * While the open tab is one of the list's, the button carries its name
 * and the bar's active look. A tab with unsaved changes, or a field to
 * fix, shows a dot, and so does «More» when any of its tabs does.
 *
 * The list is a sibling of the bar, not inside it: the bar scrolls on
 * its own (``overflow-x``) and would clip it. Its entries keep the class
 * ``rl-tab`` and the role ``tab``, as the bar's own. «More» is a tab of
 * the bar too, selected while the open tab is in the list, and names the
 * list in ``aria-controls`` while it is open.
 */
export class TabBar extends Component {
    static template = "incubacloud.TabBar";
    static props = {
        /** Tabs shown in the bar: ``[{key, label}]``. */
        tabs: { type: Array },
        /** Tabs under «More»: ``[{key, label}]``. */
        more: { type: Array, optional: true },
        /** Key of the open tab. */
        active: { type: String, optional: true },
        /** Keys of the tabs with unsaved changes or a field to fix. */
        marked: { type: Array, optional: true },
        /** Called with the key of the tab chosen. */
        onSelect: { type: Function },
    };
    static defaultProps = {
        more: [],
        marked: [],
    };

    /**
     * Where the open list goes, against the bar's wrapper.
     *
     * Under the button, from its left edge; pushed back to the left when
     * it would pass the wrapper or the viewport.
     *
     * @param {object} geometry in viewport pixels
     * @param {DOMRect|object} geometry.button the button's rectangle
     * @param {DOMRect|object} geometry.wrap the wrapper's rectangle
     * @param {number} geometry.width the list's own width
     * @param {number} geometry.viewport the viewport's width
     * @param {number} [geometry.margin=8] room kept to the viewport's edge
     * @param {number} [geometry.gap=4] room between the button and the list
     * @returns {{left: number, top: number}} the list's place in the wrapper
     */
    static place({ button, wrap, width, viewport, margin = 8, gap = 4 }) {
        const right = Math.min(wrap.width, viewport - wrap.left - margin);
        const left = Math.max(0, Math.min(button.left - wrap.left, right - width));
        return { left: Math.round(left), top: Math.round(button.bottom - wrap.top + gap) };
    }

    /**
     * Set up the open state, the list's id and the refs, the close on a
     * press outside or a resize, and the focus on an entry when the list
     * opens.
     */
    setup() {
        this.state = useState({ open: false, menuStyle: "" });
        // The list's id, which «More» names in aria-controls while it is open.
        this.menuId = `ic-tabbar-menu-${++nextId}`;
        this.root = useRef("root");
        this.moreButton = useRef("more");
        this.menu = useRef("menu");
        useExternalListener(window, "pointerdown", (ev) => {
            if (!this.state.open) {
                return;
            }
            if (this.moreButton.el?.contains(ev.target) || this.menu.el?.contains(ev.target)) {
                return;
            }
            this.state.open = false;
        }, { capture: true });
        useExternalListener(window, "resize", () => {
            if (this.state.open) {
                this.position();
            }
        });
        useEffect((open) => {
            if (!open) {
                return;
            }
            this.position();
            const items = this.items;
            (items.find((el) => el.classList.contains("active")) || items[0])?.focus();
        }, () => [this.state.open]);
    }

    /**
     * The tabs in the bar. A list of one would hide a single tab behind
     * a button that takes as much room: that tab goes in the bar.
     *
     * @returns {{key: string, label: string}[]}
     */
    get barTabs() {
        return this.props.more.length === 1
            ? [...this.props.tabs, ...this.props.more]
            : this.props.tabs;
    }

    /** @returns {{key: string, label: string}[]} the tabs under «More». */
    get menuTabs() {
        return this.props.more.length === 1 ? [] : this.props.more;
    }

    /** @returns {{key: string, label: string}|undefined} the open tab, when it is under «More». */
    get activeInMenu() {
        return this.menuTabs.find((tab) => tab.key === this.props.active);
    }

    /** @returns {string} the button's text: the open tab's name, or «More». */
    get moreLabel() {
        return this.activeInMenu?.label || _t("More");
    }

    /** @returns {boolean} whether a tab under «More» is marked. */
    get moreMarked() {
        return this.menuTabs.some((tab) => this.isMarked(tab.key));
    }

    /** @returns {string} what a screen reader says for a dot. */
    get markText() {
        return _t("Unsaved changes");
    }

    /** @returns {HTMLElement[]} the list's entries. */
    get items() {
        const el = this.menu.el;
        return el ? [...el.querySelectorAll("button.rl-tab")] : [];
    }

    /**
     * Whether a tab carries the dot.
     *
     * @param {string} key the tab's key
     * @returns {boolean}
     */
    isMarked(key) {
        return this.props.marked.includes(key);
    }

    /**
     * Open a tab, closing the list.
     *
     * @param {string} key the tab's key
     */
    select(key) {
        const fromMenu = this.state.open;
        this.state.open = false;
        this.props.onSelect(key);
        if (fromMenu) {
            this.moreButton.el?.focus();
        }
    }

    /** Open the list when closed, close it otherwise. */
    toggle() {
        this.state.open = !this.state.open;
    }

    /**
     * Place the open list under the button (``TabBar.place``), measured
     * at its natural width.
     */
    position() {
        const menu = this.menu.el;
        const button = this.moreButton.el;
        const root = this.root.el;
        if (!menu || !button || !root) {
            return;
        }
        const { left, top } = TabBar.place({
            button: button.getBoundingClientRect(),
            wrap: root.getBoundingClientRect(),
            width: menu.getBoundingClientRect().width,
            viewport: document.documentElement.clientWidth,
        });
        this.state.menuStyle = `left:${left}px;top:${top}px`;
    }

    /** Keep the open list under its button while the bar scrolls. */
    onBarScroll() {
        if (this.state.open) {
            this.position();
        }
    }

    /**
     * Keys while the list is open: Esc or Tab closes it and gives the focus
     * back to the button; the arrows, Home and End move between its entries.
     *
     * @param {KeyboardEvent} ev
     */
    onKeydown(ev) {
        if (!this.state.open) {
            return;
        }
        if (ev.key === "Escape" || ev.key === "Tab") {
            // Tab too: the list is gone on the next render, and the focus
            // would land on an entry that no longer exists.
            ev.preventDefault();
            ev.stopPropagation();
            this.state.open = false;
            this.moreButton.el?.focus();
            return;
        }
        const items = this.items;
        const current = items.indexOf(document.activeElement);
        let next = -1;
        if (ev.key === "ArrowDown") {
            next = current < 0 ? 0 : (current + 1) % items.length;
        } else if (ev.key === "ArrowUp") {
            next = current <= 0 ? items.length - 1 : current - 1;
        } else if (ev.key === "Home") {
            next = 0;
        } else if (ev.key === "End") {
            next = items.length - 1;
        }
        if (next >= 0 && items[next]) {
            ev.preventDefault();
            items[next].focus();
        }
    }
}
