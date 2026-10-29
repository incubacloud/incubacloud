/** @odoo-module **/

import { Component, onMounted, onWillStart, onWillUnmount, status, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { _t } from "@web/core/l10n/translation";
import { user } from "@web/core/user";
import { modelOptions, money, roleLabel, settingsChanges } from "../assistant_format";

/** Settings → Assistant: provider, key, models, users, limits and usage. */
export class AssistantSettingsTab extends Component {
    static template = "incubacloud_assistant.AssistantSettingsTab";
    static props = {};

    /** Read the settings and the usage; save them with the page's Save button. */
    setup() {
        this.state = useState({
            loading: true,
            loadError: null,
            refreshing: false,
            notice: null,
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

    /** Allow or disallow a model; the default stays among the allowed. */
    toggleModel(id) {
        const form = this.state.form;
        form.allowed_models = form.allowed_models.includes(id)
            ? form.allowed_models.filter((model) => model !== id)
            : [...form.allowed_models, id];
        if (!form.allowed_models.includes(form.default_model)) {
            form.default_model = form.allowed_models[0] || "";
        }
    }

    /** Let a user use the assistant, or not. */
    toggleUser(id) {
        const form = this.state.form;
        form.user_ids = form.user_ids.includes(id)
            ? form.user_ids.filter((userId) => userId !== id)
            : [...form.user_ids, id];
    }

    /** Remove the key on the next Save: the assistant goes off. */
    removeKey() {
        this.state.form.clear_api_key = true;
        this.state.form.enabled = false;
    }

    /**
     * Pick another provider. Its models are others and its key is its own:
     * the allowed models and the default empty, the assistant goes off (and
     * cannot go on until it is saved), and the server refuses the change
     * unless its key is typed with it. Back to the saved provider, its
     * models show again.
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

    /** The key field's hint: a key is saved, typing replaces it. */
    get keyPlaceholder() {
        return this.keySaved ? _t("Saved — type to replace it") : "";
    }

    /**
     * Whether switching on needs the box ticked (section 12A of the Terms on
     * a tenant panel): the assistant is saved off, on a database that does
     * not block it.
     */
    get needsTerms() {
        return !this.state.blocked && !this._loaded?.enabled;
    }

    /**
     * Who ticked the box, when and from where; or "". With a version, it was
     * section 12A of IncubaCloud's Terms (a tenant panel); without one, the
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

    /** Read the provider's models again with the key kept. */
    async refreshModels() {
        this.state.refreshing = true;
        this.state.notice = null;
        try {
            const result = await rpc("/cloud/assistant/refresh_models", {});
            if (result.ok) {
                this.state.models = result.models;
                this._savedModels = result.models;
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

    /** The users who may be chosen, with their role's name. */
    get userRows() {
        return this.state.candidates.map((candidate) => ({
            ...candidate,
            roleLabel: roleLabel(candidate.role),
            allowed: this.state.form.user_ids.includes(candidate.id),
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
     * @returns {Promise<{ok: boolean, error?: string}>} the save's answer
     */
    async _save() {
        const vals = settingsChanges(this._loaded, this.state.form);
        if (!Object.keys(vals).length) {
            return { ok: true };
        }
        if (vals.enabled === true) {
            // The box (section 12A on a tenant panel): each switch-on ticks it again.
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
            if (vals.api_key || vals.provider || vals.clear_api_key || vals.enabled === true) {
                this._applyListing(await rpc("/cloud/assistant/get_settings", {}));
            }
        }
        return result;
    }
}
