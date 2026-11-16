/** @odoo-module **/
import { Component, reactive, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";

// ── Extension registries ─────────────────────────────────────────────────────
// A layer opens a dialog of its own from a step (an action with
// ``client: true``) and hangs a header of its own over the steps.
const _actionHandlers = new Map();
let _headerComponent = null;

/**
 * Register what an action with ``client: true`` does when clicked.
 *
 * @param {string} code - the action's code, as ``cloud.first.steps._steps`` gives it
 * @param {function} handler - ``(env, action) => void``
 */
export function registerFirstStepAction(code, handler) {
    _actionHandlers.set(code, handler);
}

/**
 * Register the component drawn over the steps, with the state's ``header``
 * as its ``header`` prop. One at most: the last registered wins.
 *
 * @param {typeof Component|null} component
 */
export function registerFirstStepsHeader(component) {
    _headerComponent = component;
}

/** @returns {typeof Component|null} the header a layer registered, if any */
export function firstStepsHeader() {
    return _headerComponent;
}

// ── Shared state ─────────────────────────────────────────────────────────────
// The home page's card, its link under the title and the entry of the user
// menu read the same answer of ``/cloud/first_steps``.
export const firstStepsStore = reactive({ loaded: false, data: { enabled: false } });

/**
 * Ask ``/cloud/first_steps`` again. A failure leaves the card out: the
 * home page works the same without it.
 *
 * @returns {Promise<object>} the state, ``{enabled: false}`` on a failure
 */
export async function loadFirstSteps() {
    try {
        firstStepsStore.data = (await rpc("/cloud/first_steps", {}, { silent: true }))
            || { enabled: false };
    } catch {
        firstStepsStore.data = { enabled: false };
    }
    firstStepsStore.loaded = true;
    return firstStepsStore.data;
}

/**
 * Close the card for the signed-in user, or open it again.
 *
 * @param {boolean} hidden - close it
 * @returns {Promise<object>} the state after the change
 */
export async function setFirstStepsHidden(hidden) {
    try {
        firstStepsStore.data = (await rpc("/cloud/first_steps/set_hidden", { hidden }))
            || { enabled: false };
    } catch {
        // Kept as it was: the click did nothing, the page goes on.
    }
    firstStepsStore.loaded = true;
    return firstStepsStore.data;
}

/**
 * Whether there is a way back to a card the user does not see now: it is
 * on, something is left and it does not show.
 *
 * @param {object} data - a state of ``/cloud/first_steps``
 * @returns {boolean}
 */
export function canReopenFirstSteps(data) {
    return !!(data && data.enabled && data.pending && !data.visible);
}

const STATE_LABELS = {
    done: () => _t("Done"),
    started: () => _t("Started"),
    todo: () => _t("To do"),
};

/**
 * The first steps card of the home page.
 *
 * Draws the state ``/cloud/first_steps`` answers: the steps in order, the
 * one that comes next with the primary button, the count, a layer's
 * header. Folded to a line once the required steps are done; ``×``
 * closes it for this user.
 */
export class FirstSteps extends Component {
    static template = "incubacloud.FirstSteps";
    static props = {};

    setup() {
        this.store = useState(firstStepsStore);
        this.state = useState({ expanded: false });
    }

    /** @returns {object} the state drawn */
    get data() {
        return this.store.data || { enabled: false };
    }

    /** @returns {boolean} whether the card is drawn */
    get shown() {
        return !!(this.data.enabled && this.data.visible);
    }

    /** @returns {boolean} whether the steps are folded to one line */
    get folded() {
        return !!this.data.collapsed && !this.state.expanded;
    }

    /** @returns {typeof Component|null} a layer's header */
    get headerComponent() {
        return firstStepsHeader();
    }

    /** @returns {string} how many required steps are done */
    get countText() {
        return _t("%(done)s of %(required)s done", {
            done: this.data.done || 0,
            required: this.data.required || 0,
        });
    }

    /** @returns {string} the folded line: the count and the optional steps left */
    get foldText() {
        const left = this.data.optional_left || 0;
        const optional = left === 1
            ? _t("1 optional left")
            : _t("%(left)s optional left", { left });
        return _t("First steps · %(count)s · %(optional)s", {
            count: this.countText, optional,
        });
    }

    /** @returns {string} what follows an optional step's title */
    get optionalMark() {
        return ` ${_t("· optional")}`;
    }

    /**
     * @param {string} state - ``done``, ``started`` or ``todo``
     * @returns {string} what the state says in words
     */
    stepStateLabel(state) {
        return (STATE_LABELS[state] || STATE_LABELS.todo)();
    }

    /**
     * @param {object} step
     * @param {number} index - the action's place in the step
     * @returns {string} the button's classes: primary only for the next step's first
     */
    actionClass(step, index) {
        const primary = step.code === this.data.next && index === 0;
        return `rl-btn ${primary ? "rl-btn-primary" : "rl-btn-ghost"} ic-fs-action`;
    }

    /**
     * Run an action: a page of the panel, or what a layer registered.
     *
     * @param {object} action - ``{code, label, route, params}`` or ``{code, label, client: true}``
     */
    runAction(action) {
        if (action.client) {
            const handler = _actionHandlers.get(action.code);
            if (handler) {
                handler(this.env, action);
            }
            return;
        }
        this.env.navigate(action.route, action.params || {});
    }

    /** Unfold or fold the steps once the required ones are done. */
    toggleExpanded() {
        this.state.expanded = !this.state.expanded;
    }

    /** Close the card for this user; the home page offers the way back. */
    async hide() {
        await setFirstStepsHidden(true);
        // The card is gone: the link under the title takes the focus, once
        // the page is drawn again (the render waits for the next frame).
        requestAnimationFrame(() => document.querySelector(".ic-fs-reopen")?.focus());
    }
}
