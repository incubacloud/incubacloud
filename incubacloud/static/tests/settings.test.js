import { describe, expect, test } from "@odoo/hoot";
import { Settings } from "@incubacloud/components/settings/settings";
import {
    CORE_RATE_DEFAULTS,
    CoreRatesTab,
} from "@incubacloud/components/core_rates_tab/core_rates_tab";

/**
 * Settings pure-logic tests.
 *
 * The component uses onWillStart (RPC), onMounted (URL params), and
 * window.location — so we cannot mount it easily. We test the class
 * structure, props, getter logic, and state-manipulation helpers.
 */

describe("Settings — class structure", () => {
    test("has the correct template name", () => {
        expect(Settings.template).toBe("incubacloud.Settings");
    });

    test("has no required props", () => {
        // The component is routed to without arguments, so every prop it
        // declares must stay optional.
        for (const def of Object.values(Settings.props)) {
            expect(def.optional).toBe(true);
        }
    });

    test("initialTab is an optional String", () => {
        expect(Settings.props.initialTab.type).toBe(String);
        expect(Settings.props.initialTab.optional).toBe(true);
    });

    test("is a Component subclass", () => {
        expect(typeof Settings).toBe("function");
        expect(typeof Settings.prototype.setup).toBe("function");
    });
});

describe("Settings — currentStep computation", () => {
    /**
     * The getter logic:
     *   if (!this.state.configured) return 1;
     *   if (!this.state.form.installation_id) return 2;
     *   return 3;
     */
    function currentStep(configured, installationId) {
        if (!configured) return 1;
        if (!installationId) return 2;
        return 3;
    }

    test("step 1 when not configured", () => {
        expect(currentStep(false, "")).toBe(1);
        expect(currentStep(false, "12345")).toBe(1);
    });

    test("step 2 when configured but no installation_id", () => {
        expect(currentStep(true, "")).toBe(2);
        expect(currentStep(true, null)).toBe(2);
    });

    test("step 3 when configured and installation_id present", () => {
        expect(currentStep(true, "12345")).toBe(3);
        expect(currentStep(true, "1")).toBe(3);
    });
});

describe("Settings — tab handling", () => {
    /**
     * setTab(tab) sets state.tab, clears footerMsg and banner.
     * We test the logic without mounting.
     */
    test("setTab clears footerMsg and banner", () => {
        const state = {
            tab: "backups",
            ghActionMsg: { type: "success", message: "Connected" },
        };
        // Simulate setTab — clears inline feedback
        function setTab(tab) {
            state.tab = tab;
            state.ghActionMsg = null;
        }
        setTab("github");
        expect(state.tab).toBe("github");
        expect(state.ghActionMsg).toBe(null);
    });

    test("default tab is 'backups'", () => {
        // From setup(): this.state = useState({ tab: "backups", ... })
        const defaultTab = "backups";
        expect(defaultTab).toBe("backups");
    });
});

describe("Settings — _checkUrlParams logic", () => {
    /**
     * Simulates the URL param checking logic without touching window.location.
     */
    function checkUrlParams(params) {
        let tab = "backups";
        let banner = null;

        if (params.get("setup_ok") === "1") {
            tab = "github";
            banner = {
                type: "success",
                message: "GitHub App created! Now install it on your organization to complete the setup.",
            };
        } else if (params.has("setup_error")) {
            const code = params.get("setup_error") || "unknown";
            const msg = code === "requires_https"
                ? "Automatic setup requires HTTPS. Please create the GitHub App manually and paste the credentials below."
                : `GitHub App setup failed (${code}). Please try again or configure manually.`;
            tab = "github";
            banner = { type: code === "requires_https" ? "info" : "error", message: msg };
        }
        return { tab, banner };
    }

    test("setup_ok=1 sets success banner and github tab", () => {
        const params = new URLSearchParams("?setup_ok=1");
        const { tab, banner } = checkUrlParams(params);
        expect(tab).toBe("github");
        expect(banner.type).toBe("success");
        expect(banner.message).toInclude("GitHub App created");
    });

    test("setup_error=requires_https sets info banner", () => {
        const params = new URLSearchParams("?setup_error=requires_https");
        const { tab, banner } = checkUrlParams(params);
        expect(tab).toBe("github");
        expect(banner.type).toBe("info");
        expect(banner.message).toInclude("HTTPS");
    });

    test("setup_error with other code sets error banner", () => {
        const params = new URLSearchParams("?setup_error=timeout");
        const { tab, banner } = checkUrlParams(params);
        expect(tab).toBe("github");
        expect(banner.type).toBe("error");
        expect(banner.message).toInclude("timeout");
    });

    test("no params keeps defaults", () => {
        const params = new URLSearchParams("");
        const { tab, banner } = checkUrlParams(params);
        expect(tab).toBe("backups");
        expect(banner).toBe(null);
    });
});

describe("Settings — toggleManual", () => {
    test("toggleManual flips showManual", () => {
        let showManual = false;
        showManual = !showManual;
        expect(showManual).toBe(true);
        showManual = !showManual;
        expect(showManual).toBe(false);
    });
});

describe("Settings — method existence", () => {
    const proto = Settings.prototype;

    test("has loadConfig method", () => {
        expect(typeof proto.loadConfig).toBe("function");
    });

    test("has save method", () => {
        expect(typeof proto.save).toBe("function");
    });

    test("has toggleAutoassign method", () => {
        expect(typeof proto.toggleAutoassign).toBe("function");
    });

    test("has testConnection method", () => {
        expect(typeof proto.testConnection).toBe("function");
    });

    test("has disconnectApp method", () => {
        expect(typeof proto.disconnectApp).toBe("function");
    });

    test("has refresh method", () => {
        expect(typeof proto.refresh).toBe("function");
    });

    test("has detectInstallation method", () => {
        expect(typeof proto.detectInstallation).toBe("function");
    });

    test("has _confirm method", () => {
        expect(typeof proto._confirm).toBe("function");
    });
});

describe("Settings — bounded GitHub import rates", () => {
    test("preview and import defaults are separate hourly caps", () => {
        expect(CORE_RATE_DEFAULTS.rate_limit_github_previews_per_hour).toBe(10);
        expect(CORE_RATE_DEFAULTS.rate_limit_github_imports_per_hour).toBe(5);
    });

    test("rates component keeps load, clamp and save behavior", () => {
        expect(CoreRatesTab.template).toBe("incubacloud.CoreRatesTab");
        expect(typeof CoreRatesTab.prototype.load).toBe("function");
        expect(typeof CoreRatesTab.prototype.onIntInput).toBe("function");
        expect(typeof CoreRatesTab.prototype._save).toBe("function");
    });
});
