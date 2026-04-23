/**
 * useVisibilityRefresh — fire ``callback`` whenever the page comes back
 * from hidden to visible.
 *
 * Why this exists:
 *   Components that used to poll every 8–30 s as a belt-and-suspenders
 *   safety net — on top of their ``cloud_jobs`` bus subscription — can
 *   now drop the timer and rely on the bus plus this one-shot focus
 *   refresh. The bus delivers ~all updates; the refresh catches the
 *   rare missed event (bus disconnect, longpoll timeout, sleep/wake).
 *
 *   Without this, a user who alt-tabs for 10 minutes comes back to
 *   stale data if any bus notification was dropped while the tab was
 *   backgrounded.
 *
 * Why ``visibilitychange`` and not ``focus``:
 *   ``focus`` fires on individual element interactions in some browsers
 *   and would trigger multiple times per user interaction. ``visibilit
 *   ychange`` fires once per tab switch / window minimize, which is
 *   exactly what we want.
 *
 * Usage:
 *   setup() {
 *       useVisibilityRefresh(() => this._silentRefresh());
 *   }
 */
import { onMounted, onWillUnmount } from "@odoo/owl";

export function useVisibilityRefresh(callback) {
    const handler = () => {
        if (!document.hidden) {
            try { callback(); } catch (_) { /* swallow — best effort */ }
        }
    };

    onMounted(() => {
        document.addEventListener("visibilitychange", handler);
    });

    onWillUnmount(() => {
        document.removeEventListener("visibilitychange", handler);
    });
}
