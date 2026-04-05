import { describe, expect, test } from "@odoo/hoot";
import { createToastService, DURATIONS, ICONS } from "@incubacloud/toast/toast";

/**
 * Toast service pure-logic tests.
 *
 * Tests the reactive toast array, auto-dismiss behaviour, and API surface
 * without mounting any OWL component.
 */

describe("createToastService — API surface", () => {
    test("returns toastApi, toasts, and dismissToast", () => {
        const svc = createToastService();
        expect(typeof svc.toastApi.success).toBe("function");
        expect(typeof svc.toastApi.error).toBe("function");
        expect(typeof svc.toastApi.warning).toBe("function");
        expect(typeof svc.toastApi.info).toBe("function");
        expect(Array.isArray(svc.toasts)).toBe(true);
        expect(typeof svc.dismissToast).toBe("function");
    });
});

describe("createToastService — adding toasts", () => {
    test("success adds a toast with correct type", () => {
        const { toastApi, toasts } = createToastService();
        toastApi.success("saved");
        expect(toasts.length).toBe(1);
        expect(toasts[0].type).toBe("success");
        expect(toasts[0].message).toBe("saved");
    });

    test("error adds a toast with correct type", () => {
        const { toastApi, toasts } = createToastService();
        toastApi.error("failed");
        expect(toasts[0].type).toBe("error");
    });

    test("warning adds a toast with correct type", () => {
        const { toastApi, toasts } = createToastService();
        toastApi.warning("watch out");
        expect(toasts[0].type).toBe("warning");
    });

    test("info adds a toast with correct type", () => {
        const { toastApi, toasts } = createToastService();
        toastApi.info("fyi");
        expect(toasts[0].type).toBe("info");
    });

    test("each toast gets a unique id", () => {
        const { toastApi, toasts } = createToastService();
        toastApi.success("a");
        toastApi.success("b");
        expect(toasts[0].id).not.toBe(toasts[1].id);
    });

    test("multiple toasts accumulate", () => {
        const { toastApi, toasts } = createToastService();
        toastApi.error("e1");
        toastApi.error("e2");
        toastApi.success("s1");
        expect(toasts.length).toBe(3);
    });
});

describe("createToastService — icons", () => {
    test("each type gets the correct icon", () => {
        const { toastApi, toasts } = createToastService();
        toastApi.success("s");
        toastApi.error("e");
        toastApi.warning("w");
        toastApi.info("i");
        expect(toasts[0].icon).toBe(ICONS.success);
        expect(toasts[1].icon).toBe(ICONS.error);
        expect(toasts[2].icon).toBe(ICONS.warning);
        expect(toasts[3].icon).toBe(ICONS.info);
    });
});

describe("createToastService — dismissing", () => {
    test("dismissToast sets dismissing flag immediately", () => {
        const { toastApi, toasts, dismissToast } = createToastService();
        toastApi.error("err");
        const id = toasts[0].id;
        dismissToast(id);
        // Animation in progress: toast still present but flagged
        expect(toasts.length).toBe(1);
        expect(toasts[0].dismissing).toBe(true);
    });

    test("calling dismiss twice on the same id is a no-op", () => {
        const { toastApi, toasts, dismissToast } = createToastService();
        toastApi.error("err");
        const id = toasts[0].id;
        dismissToast(id);
        dismissToast(id); // should not throw
        expect(toasts[0].dismissing).toBe(true);
    });

    test("dismissing non-existent id is a no-op", () => {
        const { toasts, dismissToast } = createToastService();
        dismissToast(999);
        expect(toasts.length).toBe(0);
    });
});

describe("DURATIONS constants", () => {
    test("error has zero duration (persistent)", () => {
        expect(DURATIONS.error).toBe(0);
    });

    test("success has positive duration (auto-dismiss)", () => {
        expect(DURATIONS.success).toBeGreaterThan(0);
    });

    test("warning has positive duration", () => {
        expect(DURATIONS.warning).toBeGreaterThan(0);
    });

    test("info has positive duration", () => {
        expect(DURATIONS.info).toBeGreaterThan(0);
    });
});
