/** @odoo-module **/

import { Component, onMounted, onWillStart, onWillUnmount, status, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { user } from "@web/core/user";
import { MultiSelect } from "@incubacloud/components/multi_select/multi_select";
import { RlSelect } from "@incubacloud/components/rl_select/rl_select";
import { modelOptions, money, roleLabel, settingsChanges } from "../assistant_format";

/** Settings → Assistant: provider, key, models, users, limits and usage. */
export class AssistantSettingsTab extends Component {
    static template = "incubacloud_assistant.AssistantSettingsTab";
    static components = { MultiSelect, RlSelect };
    static props = {};

    /** Read the settings and the usage; save them with the page's Save button. */
    setup() {
        this.state = useState({
            loading: true,
            loadError: null,
            refreshing: false,
            notice: null,
            // What the last save could not do (switch it on), or "".
            saveWarning: "",
            providers: [],
            models: [],
            candidates: [],
            hasKey: false,
            modelsAt: "",
            // Why the assistant can never be switched on here, or "".
            blocked: "",
            // Who ticked the box to switch it on, when, from where; or null.
            termsAcceptance: null,
            // The box's text, and the address of the Terms it accepts or "".
            termsLabel: "",
            termsUrl: "",
            usage: [],
            form: {
                enabled: false,
                provider: "anthropic",
                // Write-only: empty at rest, as Settings leaves them once saved.
                api_key: "",
                clear_api_key: "",
                accept_ai_terms: "",
                default_model: "",
                allowed_models: [],
                user_ids: [],
                monthly_limit: 0,
                step_cap: 30,
            },
        });
        // What was read, to save only what changed (null until read).
        this._loaded = null;
        this.locale = (user.lang || "en_US").replace("_", "-");
        onWillStart(() => this.load());
        onMounted(() => this._register());
        onWillUnmount(() => this.env.unregisterSettingsSave?.("AssistantSettings"));
    }

    /** Read the settings, the provider's models, the users and the usage. */
    async load() {
        this.state.loading = true;
        this.state.loadError = null;
        try {
            const data = await rpc("/cloud/assistant/get_settings", {});
            this._applyListing(data);
            Object.assign(this.state.form, {
                enabled: data.enabled,
                provider: data.provider,
                api_key: "",
                clear_api_key: "",
                accept_ai_terms: "",
                default_model: data.default_model || "",
                allowed_models: [...data.allowed_models],
                user_ids: [...data.user_ids],
                monthly_limit: data.monthly_limit,
                step_cap: data.step_cap,
            });
            this._loaded = { ...this.state.form };
            this.state.usage = (await rpc("/cloud/assistant/usage", {})).rows;
        } catch {
            this.state.loadError = _t("Failed to load the assistant's settings.");
        } finally {
            this.state.loading = false;
        }
    }

    /** Keep what the server says outside the form: providers, models, users, key. */
    _applyListing(data) {
        this.state.providers = data.providers;
        this.state.models = data.models;
        this._savedModels = data.models;
        this.state.candidates = data.candidates;
        this.state.hasKey = data.has_api_key;
        this.state.modelsAt = data.models_at;
        this.state.blocked = data.blocked || "";
        this.state.termsAcceptance = data.terms_acceptance || null;
        this.state.termsLabel = data.terms_label || "";
        this.state.termsUrl = data.terms_url || "";
    }

    /** Join Settings' save registry once the form holds what the server has. */
    _register() {
        if (this._loaded && status(this) === "mounted") {
            this.env.registerSettingsSave?.("AssistantSettings", () => this._save(), this.state, [
                "api_key",
                "clear_api_key",
                "accept_ai_terms",
            ], "assistant");
        }
    }

    /** Load again after a failure, and join Settings if it worked. */
    async reload() {
        await this.load();
        this._register();
    }

    /**
     * Allow the models *ids*. The default stays among them (the first one
     * usable, when it is not); with none left the assistant cannot stay on.
     *
     * @param {string[]} ids the allowed models, as the selector lists them
     */
    setAllowedModels(ids) {
        const form = this.state.form;
        form.allowed_models = [...ids];
        if (!form.allowed_models.includes(form.default_model)) {
            const usable = new Set(this.defaultModelOptions.filter((o) => !o.disabled).map((o) => o.value));
            form.default_model = form.allowed_models.find((id) => usable.has(id)) || "";
        }
        if (!form.allowed_models.length || !form.default_model) {
            form.enabled = false;
        }
    }

    /**
     * Let the users *ids* use the assistant.
     *
     * @param {number[]} ids
     */
    setUsers(ids) {
        this.state.form.user_ids = [...ids];
    }

    /** Remove the key on the next Save: the assistant goes off. */
    removeKey() {
        this.state.form.clear_api_key = true;
        this.state.form.enabled = false;
    }

    /**
     * Pick another provider. Its models are others and its key is its own:
     * the allowed models and the default empty, the assistant goes off (and
     * cannot go on until its key is typed and its models read and chosen),
     * and the server refuses the change unless its key is typed with it.
     * Back to the saved provider, its models show again.
     */
    changeProvider(value) {
        const form = this.state.form;
        if (value === form.provider) {
            return;
        }
        // A key typed for one provider is never sent with another.
        Object.assign(form, {
            provider: value,
            allowed_models: [],
            default_model: "",
            api_key: "",
        });
        if (value === this._loaded?.provider) {
            Object.assign(form, {
                allowed_models: [...this._loaded.allowed_models],
                default_model: this._loaded.default_model,
                enabled: this._loaded.enabled,
            });
            this.state.models = this._savedModels || [];
        } else {
            form.enabled = false;
            this.state.models = [];
        }
    }

    /** Whether the form's provider is not the saved one: no models to use yet. */
    get providerChanged() {
        const loaded = this._loaded;
        return Boolean(loaded) && this.state.form.provider !== loaded.provider;
    }

    /** Whether the saved key is the selected provider's (one to keep or remove). */
    get keySaved() {
        const form = this.state.form;
        return (
            this.state.hasKey &&
            !form.clear_api_key &&
            form.provider === this._loaded?.provider
        );
    }

    /** Whether a key is typed in the form, to be tried and kept on Save. */
    get keyTyped() {
        return Boolean(String(this.state.form.api_key || "").trim());
    }

    /** The key field's hint: a key is saved, typing replaces it. */
    get keyPlaceholder() {
        return this.keySaved ? _t("Saved — type to replace it") : "";
    }

    /**
     * Whether switching on needs the box ticked (a section of the Terms
     * where a module on top has them): the assistant is saved off, or the form changes its
     * provider, on a database that does not block it.
     */
    get needsTerms() {
        return !this.state.blocked && (!this._loaded?.enabled || this.providerChanged);
    }

    /**
     * Why "Assistant on" cannot be ticked yet, or "". Switching on needs a
     * key (typed or saved) whose models were read, the allowed models, a
     * default model, and the box when it is asked for. Once ticked, it can
     * always be unticked.
     *
     * @returns {string}
     */
    get switchOnBlocker() {
        const form = this.state.form;
        if (form.enabled || this.state.blocked) {
            return "";
        }
        if (!this.keyTyped && !this.keySaved) {
            return _t("Type the provider's key and read its models to switch the assistant on.");
        }
        if (!this.state.models.length) {
            return _t("Read the provider's models to switch the assistant on.");
        }
        if (!form.allowed_models.length || !form.default_model) {
            return _t("Choose the allowed models and a default model to switch the assistant on.");
        }
        if (this.needsTerms && !form.accept_ai_terms) {
            return _t("Tick the acceptance box to switch the assistant on.");
        }
        return "";
    }

    /** Whether "Assistant on" is disabled: off, and it cannot go on yet. */
    get switchDisabled() {
        return !this.state.form.enabled && (Boolean(this.state.blocked) || Boolean(this.switchOnBlocker));
    }

    /**
     * Who ticked the box, when and from where; or "". With a version, it was
     * the Terms of a module on top; without one, the
     * acknowledgement of a self-hosted panel.
     */
    get termsLine() {
        const accepted = this.state.termsAcceptance;
        if (!accepted) {
            return "";
        }
        const values = {
            date: `${accepted.accepted_on} UTC`,
            user: accepted.user,
            ip: accepted.ip,
            version: accepted.version,
        };
        return accepted.version
            ? _t(
                  "Section 12A of the Terms accepted on %(date)s by %(user)s from %(ip)s (terms of %(version)s).",
                  values
              )
            : _t("Last switched on, with the box ticked, on %(date)s by %(user)s from %(ip)s.", values);
    }

    /** Whether the models can be read now: with the key typed, or the saved one. */
    get canReadModels() {
        return !this.state.refreshing && (this.keyTyped || this.keySaved);
    }

    /**
     * Read the provider's models, to choose from them before saving. With a
     * key typed, with that key, and nothing is kept: Save tries it again and
     * keeps it with its listing. Without one, with the saved key, and the
     * listing is kept, as before.
     */
    async readModels() {
        if (!this.canReadModels) {
            return;
        }
        const form = this.state.form;
        const typed = this.keyTyped;
        this.state.refreshing = true;
        this.state.notice = null;
        try {
            const result = typed
                ? await rpc("/cloud/assistant/read_models", {
                      provider: form.provider,
                      api_key: form.api_key,
                  })
                : await rpc("/cloud/assistant/refresh_models", {});
            if (result.ok) {
                this._setModels(result.models);
                if (!typed) {
                    this._savedModels = result.models;
                }
            } else {
                this.state.notice = { type: "error", text: result.error };
            }
        } catch {
            this.state.notice = {
                type: "error",
                text: _t("The panel could not reach the assistant. Try again."),
            };
        } finally {
            this.state.refreshing = false;
        }
    }

    /**
     * Show the listing *models*; an allowed model it no longer has goes.
     *
     * @param {object[]} models the provider's listing, tagged by the server
     */
    _setModels(models) {
        this.state.models = models;
        const known = new Set(models.map((model) => model.id));
        const allowed = this.state.form.allowed_models;
        if (allowed.some((id) => !known.has(id))) {
            this.setAllowedModels(allowed.filter((id) => known.has(id)));
        }
    }

    /**
     * The models with what the form says of them. ``blocked``: no known price
     * while the form has a monthly limit, so users could not use it.
     */
    get modelRows() {
        const form = this.state.form;
        const prices = Object.fromEntries(this.state.models.map((m) => [m.id, m.price_known]));
        return modelOptions(this.state.models).map((model) => ({
            ...model,
            allowed: form.allowed_models.includes(model.id),
            isDefault: form.default_model === model.id,
            priceKnown: prices[model.id],
            blocked: form.monthly_limit > 0 && !prices[model.id],
        }));
    }

    /**
     * The allowed models' selector: every model, with what the list said of
     * it (recommended or untested, price unknown); one that cannot be used
     * under the form's monthly limit cannot be picked.
     */
    get allowedModelOptions() {
        return this.modelRows.map((model) => {
            const hint = [model.tag];
            if (!model.priceKnown) {
                hint.push(_t("Price unknown"));
            }
            if (model.blocked) {
                hint.push(_t("Not usable with a monthly limit"));
            }
            return {
                value: model.id,
                label: model.label,
                hint: hint.map(String).join(" · "),
                disabled: model.blocked,
            };
        });
    }

    /** The default model's selector: the allowed models, in the listing's order. */
    get defaultModelOptions() {
        return this.modelRows
            .filter((model) => model.allowed)
            .map((model) => ({
                value: model.id,
                label: model.label,
                disabled: model.blocked && !model.isDefault,
            }));
    }

    /** The users' selector: the panel's people, each with their role. */
    get userOptions() {
        return this.state.candidates.map((candidate) => ({
            value: candidate.id,
            label: candidate.name,
            hint: roleLabel(candidate.role),
        }));
    }

    /** The usage table, its costs in euros. */
    get usageRows() {
        return this.state.usage.map((row) => ({
            ...row,
            tokens: row.input_tokens + row.output_tokens + row.cache_read_tokens + row.cache_write_tokens,
            costLabel: money(row.cost, this.locale) + (row.unpriced_tokens ? " *" : ""),
        }));
    }

    /**
     * Save what changed. What is sent, not what the form holds once the answer
     * is in, is what counts as saved. A new key or provider brings a new listing.
     * The box goes only with the switch-on, and is asked for again at the
     * next one.
     *
     * Switched on without the models chosen, the server keeps the rest (a
     * new key and its listing) and answers why it stayed off: the tab says
     * so, and tells Settings the form it has now is the saved one.
     *
     * @returns {Promise<{ok: boolean, error?: string, warning?: string}>} the save's answer
     */
    async _save() {
        const vals = settingsChanges(this._loaded, this.state.form);
        if (!Object.keys(vals).length) {
            return { ok: true };
        }
        if (vals.enabled === true) {
            // The box (the Terms, where a module on top has them): each switch-on ticks it again.
            vals.accept_ai_terms = this.state.form.accept_ai_terms === true;
        }
        const sent = { ...this.state.form, api_key: "", clear_api_key: "", accept_ai_terms: "" };
        const result = await rpc("/cloud/assistant/save_settings", { vals });
        if (result?.ok) {
            this._loaded = sent;
            // Settings counts the write-only fields as empty once saved: so
            // does the form, or it would stay "unsaved" for ever.
            this.state.form.api_key = "";
            this.state.form.clear_api_key = "";
            this.state.form.accept_ai_terms = "";
            this.state.saveWarning = result.warning || "";
            if (result.warning) {
                this.state.form.enabled = false;
                this._loaded.enabled = false;
            }
            if (vals.api_key || vals.provider || vals.clear_api_key || vals.enabled === true) {
                this._applyListing(await rpc("/cloud/assistant/get_settings", {}));
            }
            if (result.warning) {
                this.env.toast?.warning?.(result.warning);
                // Settings keeps what was sent, the switch on, as saved: the
                // switch stayed off, and the form as it is now is what is saved.
                this._register();
            }
        }
        return result;
    }
}
