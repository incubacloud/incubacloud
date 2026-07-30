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
