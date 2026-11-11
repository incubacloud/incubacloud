/** @odoo-module **/

/** The assistant's section of Settings. */

import { _t } from "@web/core/l10n/translation";
import { settingsBlocks, settingsSections } from "@incubacloud/components/settings/settings_registry";
import { AssistantSettingsTab } from "./assistant_settings_tab";

const ai = ["ai", "assistant", "llm", "chat"];

settingsSections.add("assistant", {
    group: "integrations", sequence: 40, title: _t("Assistant"),
    description: _t("The built-in assistant: its provider, its spending and who may use it."),
});
settingsBlocks.add("assistant", {
    section: "assistant", sequence: 10, Component: AssistantSettingsTab,
    fields: [
        {
            id: "accept_ai_terms", type: "custom", block: _t("Assistant"),
            label: _t("Acceptance before switching on"),
            synonyms: ["terms", "acceptance", ...ai],
        },
        {
            id: "enabled", type: "toggle", block: _t("Assistant"), label: _t("Assistant on"),
            help: _t("Off: nobody can ask the assistant anything."),
            synonyms: ai,
        },
        {
            id: "provider", type: "custom", block: _t("Assistant"), label: _t("AI provider"),
            synonyms: ["anthropic", "openai", "claude", "gpt", ...ai],
        },
        {
            id: "api_key", type: "custom", block: _t("Assistant"), label: _t("Provider key"),
            help: _t("Never shown back. Typing one replaces the saved key."),
            synonyms: ["api key", "token", ...ai],
        },
        {
            id: "monthly_limit", type: "number", unit: "EUR", min: 0, decimals: true,
            block: _t("Assistant"), label: _t("Monthly limit per user"),
            help: _t("The same for every user, who waits for next month past it. 0 is no limit."),
            synonyms: ["budget", "cost", "spending", "money", ...ai],
        },
        {
            id: "step_cap", type: "number", min: 1, max: 200, advanced: true,
            block: _t("Assistant"), label: _t("Steps per task"),
            help: _t("A task that reaches it stops and asks whether to go on."),
            synonyms: ["limit", ...ai],
        },
        {
            id: "models", type: "custom", block: _t("Models"), label: _t("Provider's models"),
            help: _t("Read with the key, typed or saved; then choose the ones allowed."),
            synonyms: ["model", ...ai],
        },
        {
            id: "allowed_models", type: "custom", block: _t("Models"), label: _t("Allowed models"),
            synonyms: ["model", ...ai],
        },
        {
            id: "default_model", type: "custom", block: _t("Models"), label: _t("Default model"),
            synonyms: ["model", ...ai],
        },
        {
            id: "user_ids", type: "custom", block: _t("Users"), label: _t("Users of the assistant"),
            help: _t("Only the users chosen here see the assistant."),
            synonyms: ["access", "who", "users", ...ai],
        },
        {
            id: "assistant_usage", type: "custom", block: _t("Usage"), label: _t("Usage"),
            help: _t("Tokens and approximate cost of each user, month by month."),
            synonyms: ["cost", "spending", "tokens", ...ai],
        },
    ],
});
