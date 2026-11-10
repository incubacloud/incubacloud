import { onMounted, onWillUnmount } from "@odoo/owl";

/**
 * Keep a ``position:fixed`` menu glued to its trigger.
 *
 * Fixed-positioned dropdowns are placed once, from the trigger's
 * ``getBoundingClientRect()`` at open time. Those are viewport
 * coordinates, so any later scroll or resize moves the trigger while the
 * menu stays put — it visibly unsticks, and worst inside a modal whose
 * body scrolls under a menu that does not.
 *
 * This hook wires the missing listeners once, in one place, for every
 * component that anchors a menu. ``scroll`` is captured (``true``) so
 * scrolls inside ANY ancestor are seen — a bubbling listener on
 * ``window`` never hears them, which is exactly why the bug survived.
 *
 * The component stays in charge of the geometry: it passes a callback
 * that repositions (or closes) and decides whether anything is open.
 * Listeners live for the component's lifetime rather than being attached
 * on open — two idle listeners are far cheaper than the bookkeeping to
 * add and remove them per interaction.
 *
 * @param {() => void} onReposition Called on scroll/resize. Must be a
 *   no-op when the menu is closed.
 */
export function useAnchoredMenu(onReposition) {
    const handler = () => onReposition();
    onMounted(() => {
        window.addEventListener("scroll", handler, true);
        window.addEventListener("resize", handler);
    });
    onWillUnmount(() => {
        window.removeEventListener("scroll", handler, true);
        window.removeEventListener("resize", handler);
    });
}

/**
 * Where a ``position:fixed`` menu goes beside its trigger, and how tall it may be.
 *
 * The menu opens below the trigger, as a native select does. When the
 * height it needs does not fit there and the space above is larger it
 * opens upwards instead, anchored by ``bottom`` so it grows away from the
 * trigger whatever its real height. Its height is limited to the space of
 * the chosen side (at least *minHeight*, at most *maxHeight*), so a long
 * list scrolls inside the window rather than leaving it.
 *
 * @param {{top: number, bottom: number}} rect the trigger's rectangle, in
 *   viewport coordinates
 * @param {number} viewportHeight the height of the viewport, in px
 * @param {object} menu the menu's geometry, in px
 * @param {number} menu.height the height the open menu needs
 * @param {number} menu.maxHeight the stylesheet's cap on its height
 * @param {number} [menu.minHeight=96] the least height it is given
 * @param {number} [menu.gap=4] the gap between the trigger and the menu
 * @param {number} [menu.edge=8] the margin kept to the window's edge
 * @returns {{upwards: boolean, side: string, maxHeight: number}} whether it
 *   opens upwards, its ``top`` or ``bottom`` declaration, and its height cap
 */
export function placeAnchoredMenu(
    rect,
    viewportHeight,
    { height, maxHeight, minHeight = 96, gap = 4, edge = 8 }
) {
    const below = viewportHeight - rect.bottom - gap - edge;
    const above = rect.top - gap - edge;
    const upwards = below < Math.min(height, maxHeight) && above > below;
    return {
        upwards,
        side: upwards
            ? `bottom:${Math.round(viewportHeight - rect.top + gap)}px`
            : `top:${Math.round(rect.bottom + gap)}px`,
        maxHeight: Math.round(
            Math.max(minHeight, Math.min(maxHeight, upwards ? above : below))
        ),
    };
}

/**
 * Height of the viewport a ``position:fixed`` menu is placed in.
 *
 * @returns {number} pixels
 */
export function viewportHeight() {
    return document.documentElement.clientHeight;
}
