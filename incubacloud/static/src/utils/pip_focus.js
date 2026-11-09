/** @odoo-module **/

import { onWillUpdateProps, useEffect, useRef } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

/**
 * Point at one package in a pip dependency list.
 *
 * "Edit Manually" in the pip conflict dialog takes the person to the
 * list the conflict is in (an instance's or a project's Dependencies
 * tab) with the package's lines selected: the whole conflict block when
 * the list still holds its markers, otherwise the package's own line.
 */

const CONFLICT_BLOCK_RE =
    /<<<<<<< [^\n]*\n([^\n]*)\n=======\n[^\n]*\n>>>>>>> [^\n]*/g;

/**
 * A package's name as pip compares it (PEP 503): case and runs of
 * ``-``, ``_`` and ``.`` do not tell two names apart.
 *
 * @param {string} name
 * @returns {string}
 */
function normalizeName(name) {
    return (name || "").toLowerCase().replace(/[-_.]+/g, "-");
}

/**
 * The package a requirement line names, normalized, or "".
 *
 * @param {string} line one line of a pip list
 * @returns {string}
 */
function lineName(line) {
    const m = line.split("#")[0].trim().match(/^([A-Za-z0-9_.-]+)/);
    return m ? normalizeName(m[1]) : "";
}

/**
 * Where *name*'s lines are in *text*.
 *
 * @param {string} text the pip list
 * @param {string} name the package
 * @returns {{start: number, end: number}|null} the conflict block
 *   whose stored line names it, else its first own line; null when the
 *   list does not name it
 */
export function pipFocusRange(text, name) {
    const wanted = normalizeName(name);
    if (!text || !wanted) return null;
    for (const m of text.matchAll(CONFLICT_BLOCK_RE)) {
        if (lineName(m[1]) === wanted) {
            return { start: m.index, end: m.index + m[0].length };
        }
    }
    let start = 0;
    for (const line of text.split("\n")) {
        if (lineName(line) === wanted) {
            return { start, end: start + line.length };
        }
        start += line.length + 1;
    }
    return null;
}

/**
 * Focus *textarea* with *name*'s lines selected and scrolled into view.
 *
 * @param {HTMLTextAreaElement} textarea the pip list's editor
 * @param {string} name the package
 * @returns {boolean} whether the list names the package; the editor is
 *   focused either way
 */
export function focusPipLines(textarea, name) {
    textarea.focus();
    const range = pipFocusRange(textarea.value, name);
    if (!range) return false;
    textarea.setSelectionRange(range.start, range.end);
    const lineHeight = parseFloat(getComputedStyle(textarea).lineHeight) || 20;
    const line = textarea.value.slice(0, range.start).split("\n").length - 1;
    textarea.scrollTop = Math.max(0, line * lineHeight - textarea.clientHeight / 3);
    return true;
}

/**
 * What the Dependencies tab says above the list it opened on *name* for.
 *
 * @param {string} name the package in conflict
 * @returns {string}
 */
export function pipFocusHint(name) {
    return _t(
        "%s is selected below: leave a single line for it, without the <<<<<<<, ======= and >>>>>>> lines, and save.",
        name
    );
}

/**
 * Open a page's Dependencies tab on the package its ``pipFocus`` prop names.
 *
 * For a page that holds the pip list in ``state.pipDeps``, edits it in a
 * textarea with ``t-ref="pipArea"`` under ``state.tab === "deps"``, and
 * has ``pipFocusName`` and ``pipFocusSeq`` in its state. Given at mount
 * or by a later navigation to the same page (a new ``pipFocus`` object),
 * the prop moves the page to that tab and, once the list is loaded and
 * drawn, selects the package's lines (:func:`focusPipLines`).
 * ``state.pipFocusName`` keeps the name for the tab's hint.
 *
 * @param {object} comp the page component, from its ``setup``
 */
export function usePipFocus(comp) {
    const ref = useRef("pipArea");
    let pending = null;
    const ask = (focus) => {
        pending = focus?.name || null;
        if (!pending) return;
        comp.state.tab = "deps";
        comp.state.pipFocusName = pending;
        comp.state.pipFocusSeq += 1;
    };
    ask(comp.props.pipFocus);
    onWillUpdateProps((next) => {
        if (next.pipFocus && next.pipFocus !== comp.props.pipFocus) {
            ask(next.pipFocus);
        }
    });
    useEffect(
        () => {
            if (pending && !comp.state.loading && comp.state.tab === "deps" && ref.el) {
                focusPipLines(ref.el, pending);
                pending = null;
            }
        },
        () => [comp.state.loading, comp.state.tab, comp.state.pipFocusSeq]
    );
}
