/**
 * useNavGuard — intercept SPA navigation when the current component
 * has unsaved form changes.
 *
 * Why this exists:
 *   The SPA navigates internally via ``env.navigate()``, which only
 *   calls ``history.pushState`` + mutates component state. The
 *   browser's ``beforeunload`` event does NOT fire for these
 *   transitions, so the existing "unsaved changes" warning only
 *   protects against closing the tab / pressing F5. Clicking a
 *   different instance in the sidebar silently drops the user's
 *   edits.
 *
 *   This hook plugs into the ``env.setNavGuard`` slot (registered in
 *   ``app.js``) so ``env.navigate`` awaits a confirm dialog before
 *   committing the route change. It also re-registers a
 *   ``beforeunload`` listener so the browser-level warning keeps
 *   working when the tab is closed.
 *
 * Contract:
 *   Only one component at a time can hold the nav guard. Since the
 *   SPA renders a single "page" per route, this is fine — when the
 *   user navigates away the component unmounts and its guard is
 *   cleared. If two components ever try to register simultaneously,
 *   the second wins (mirrors the native behavior where the most
 *   recent component is the one the user is interacting with).
 *
 * Usage:
 *   setup() {
 *       useNavGuard(
 *           () => this.hasUnsavedChanges,
 *           (opts) => this._confirm(opts),
 *       );
 *   }
 *
 *   ``isDirty`` is evaluated every time the user tries to navigate,
 *   so consumers pass a closure that reads fresh state — not a
 *   cached boolean.
 *
 *   ``confirm`` is the component's own dialog helper (returning a
 *   Promise<boolean>) — we don't ship a shared confirmation service
 *   because each detail component already owns an ``IcConfirmDialog``
 *   slot and a matching ``_confirm`` helper.
 */
import { onMounted, onWillUnmount, useEnv } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";

const CONFIRM_OPTS = () => ({
    title: _t("Unsaved changes"),
    message: _t(
        "You have unsaved changes. Leave this page without saving?",
    ),
    confirmLabel: _t("Discard changes"),
    isDanger: true,
});

export function useNavGuard(isDirty, confirm) {
    const env = useEnv();

    const onBeforeUnload = (ev) => {
        if (!isDirty()) return;
        ev.preventDefault();
        ev.returnValue = "";
    };

    const navGuard = async () => {
        if (!isDirty()) return true;
        return await confirm(CONFIRM_OPTS());
    };

    onMounted(() => {
        window.addEventListener("beforeunload", onBeforeUnload);
        env.setNavGuard?.(navGuard);
    });

    onWillUnmount(() => {
        window.removeEventListener("beforeunload", onBeforeUnload);
        env.clearNavGuard?.();
    });
}
