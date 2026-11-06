import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { patchTranslations, patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import { AssistantSettingsTab } from "@incubacloud_assistant/settings/assistant_settings_tab";

/**
 * Settings → Assistant (3-nov-2026): the models are read with the key typed,
 * before it is saved; they are chosen in selectors; "Assistant on" waits for
 * the key, the models and the box, and says why; and a save that could not
 * switch it on keeps the rest and says what is missing.
 *
 * No mounting (see ic_confirm_dialog.test.js): the getters and handlers are
 * the shipped ones, on a stand-in holding the state they read; the server is
 * ``rpc`` answering what each test says.
 */

const MODELS = [
    { id: "opus", name: "Claude Opus", recommended: true, price_known: true },
    { id: "sonnet", name: "Claude Sonnet", recommended: true, price_known: true },
    { id: "new-x", name: "New X", recommended: false, price_known: false },
];

/**
 * A tab stand-in, as loaded: the assistant off, the key saved or not.
 *
 * @param {object} [options]
 * @param {boolean} [options.hasKey=false] a key saved for the provider
 * @param {object[]} [options.models=[]] the listing kept
 * @param {object} [options.form] what the form holds beyond the defaults
 * @returns {object} the stand-in; ``registered`` counts its registrations
 */
function tab({ hasKey = false, models = [], form = {} } = {}) {
    const comp = Object.create(AssistantSettingsTab.prototype);
    comp.registered = 0;
    comp.warnings = [];
    comp.env = { toast: { warning: (text) => comp.warnings.push(text) } };
    comp._register = () => comp.registered++;
    comp.state = {
        refreshing: false,
        notice: null,
        saveWarning: "",
        blocked: "",
        hasKey,
        models: [...models],
        candidates: [],
        form: {
            enabled: false,
            provider: "anthropic",
            api_key: "",
            clear_api_key: "",
            accept_ai_terms: "",
            default_model: "",
            allowed_models: [],
            user_ids: [],
            monthly_limit: 0,
            step_cap: 30,
            ...form,
        },
    };
    comp._loaded = { ...comp.state.form, ...form, enabled: false, api_key: "" };
    comp._savedModels = [...models];
    return comp;
}

/**
 * Have the server answer *answers* by url; return the calls it got.
 *
 * @param {object} answers ``url -> answer``
 * @returns {Array} ``[url, params]`` of each call
 */
function server(answers) {
    const calls = [];
    patchWithCleanup(rpc, {
        _rpc(url, params) {
            calls.push([url, params]);
            return Promise.resolve(answers[url]);
        },
    });
    return calls;
}

describe("Settings → Assistant", () => {
    beforeEach(() => patchTranslations());

    describe("the switch", () => {
        test("waits for a key, its models, the models chosen and the box", () => {
            const comp = tab();
            expect(String(comp.switchOnBlocker)).toInclude("Type the provider's key");
            expect(comp.switchDisabled).toBe(true);
            comp.state.form.api_key = "sk-typed";
            expect(String(comp.switchOnBlocker)).toInclude("Read the provider's models");
            comp.state.models = [...MODELS];
            expect(String(comp.switchOnBlocker)).toInclude("Choose the allowed models");
            comp.setAllowedModels(["sonnet"]);
            expect(comp.state.form.default_model).toBe("sonnet");
            expect(String(comp.switchOnBlocker)).toInclude("acceptance box");
            comp.state.form.accept_ai_terms = true;
            expect(comp.switchOnBlocker).toBe("");
            expect(comp.switchDisabled).toBe(false);
        });

        test("once on, it can always go off", () => {
            const comp = tab({ form: { enabled: true } });
            expect(comp.switchOnBlocker).toBe("");
            expect(comp.switchDisabled).toBe(false);
        });

        test("another provider asks for the box again", () => {
            const comp = tab();
            comp._loaded.enabled = true;
            expect(comp.needsTerms).toBe(false);
            comp.state.form.provider = "openai";
            expect(comp.needsTerms).toBe(true);
        });

        test("no models left allowed puts it off", () => {
            const comp = tab({ hasKey: true, models: MODELS, form: { enabled: true } });
            comp.setAllowedModels(["opus"]);
            expect(comp.state.form.enabled).toBe(true);
            comp.setAllowedModels([]);
            expect(comp.state.form.default_model).toBe("");
            expect(comp.state.form.enabled).toBe(false);
        });
    });

    describe("the models' selectors", () => {
        test("say what the list said, and lock the unpriced ones under a limit", () => {
            const comp = tab({ models: MODELS, form: { monthly_limit: 10 } });
            const options = comp.allowedModelOptions;
            expect(options.map((option) => option.value)).toEqual(["opus", "sonnet", "new-x"]);
            expect(options[0].hint).toBe("Recommended");
            expect(options[2].hint).toInclude("Price unknown");
            expect(options[2].hint).toInclude("Not usable with a monthly limit");
            expect(options[2].disabled).toBe(true);
            comp.state.form.monthly_limit = 0;
            expect(comp.allowedModelOptions[2].disabled).toBe(false);
        });

        test("the default is one of the allowed, in the listing's order", () => {
            const comp = tab({ models: MODELS });
            comp.setAllowedModels(["sonnet", "opus"]);
            expect(comp.defaultModelOptions.map((option) => option.value)).toEqual(["opus", "sonnet"]);
            expect(comp.state.form.default_model).toBe("sonnet");
            comp.setAllowedModels(["opus"]);
            expect(comp.state.form.default_model).toBe("opus");
        });
    });

    describe("reading the models", () => {
        test("with the key typed: read with it, for the form's provider, kept nowhere", async () => {
            const calls = server({ "/cloud/assistant/read_models": { ok: true, models: MODELS } });
            const comp = tab({ form: { provider: "openai", api_key: "sk-typed" } });
            expect(comp.canReadModels).toBe(true);
            await comp.readModels();
            expect(calls).toEqual([
                ["/cloud/assistant/read_models", { provider: "openai", api_key: "sk-typed" }],
            ]);
            expect(comp.state.models).toEqual(MODELS);
            expect(comp._savedModels).toEqual([]);
        });

        test("without one: the saved key reads them again, and they are kept", async () => {
            const calls = server({ "/cloud/assistant/refresh_models": { ok: true, models: MODELS } });
            const comp = tab({ hasKey: true });
            await comp.readModels();
            expect(calls.map(([url]) => url)).toEqual(["/cloud/assistant/refresh_models"]);
            expect(comp._savedModels).toEqual(MODELS);
        });

        test("no key at all: nothing to read with", async () => {
            const calls = server({});
            const comp = tab();
            expect(comp.canReadModels).toBe(false);
            await comp.readModels();
            expect(calls).toEqual([]);
        });

        test("an allowed model the new listing lacks goes; a refusal is said", async () => {
            server({ "/cloud/assistant/read_models": { ok: true, models: MODELS.slice(1) } });
            const comp = tab({ models: MODELS, form: { api_key: "sk-typed" } });
            comp.setAllowedModels(["opus", "sonnet"]);
            await comp.readModels();
            expect(comp.state.form.allowed_models).toEqual(["sonnet"]);
            expect(comp.state.form.default_model).toBe("sonnet");
            server({ "/cloud/assistant/read_models": { ok: false, error: "Anthropic refused this key." } });
            await comp.readModels();
            expect(comp.state.notice.text).toBe("Anthropic refused this key.");
        });
    });

    describe("saving", () => {
        test("switched on without models: the rest kept, it stays off, the tab says why", async () => {
            const warning = "Key saved. Choose the allowed models and a default model, then switch the assistant on.";
            const calls = server({
                "/cloud/assistant/save_settings": { ok: true, warning },
                "/cloud/assistant/get_settings": {
                    providers: [],
                    models: MODELS,
                    candidates: [],
                    has_api_key: true,
                    models_at: "2026-11-03 10:00:00",
                },
            });
            const comp = tab({ form: { api_key: "sk-typed", enabled: true, accept_ai_terms: true } });
            const result = await comp._save();
            expect(result.ok).toBe(true);
            expect(calls[0][1].vals).toEqual({ enabled: true, api_key: "sk-typed", accept_ai_terms: true });
            expect(comp.state.form.enabled).toBe(false);
            expect(comp._loaded.enabled).toBe(false);
            expect(comp.state.form.api_key).toBe("");
            expect(comp.state.saveWarning).toBe(warning);
            expect(comp.warnings).toEqual([warning]);
            expect(comp.state.models).toEqual(MODELS);
            // Settings is told the form as it is now is the saved one.
            expect(comp.registered).toBe(1);
        });

        test("a save that switched it on leaves no warning", async () => {
            server({
                "/cloud/assistant/save_settings": { ok: true },
                "/cloud/assistant/get_settings": { providers: [], models: MODELS, candidates: [] },
            });
            const comp = tab({ models: MODELS, form: { enabled: true, accept_ai_terms: true } });
            comp.state.saveWarning = "before";
            await comp._save();
            expect(comp.state.saveWarning).toBe("");
            expect(comp.state.form.enabled).toBe(true);
            expect(comp.registered).toBe(0);
        });
    });
});
