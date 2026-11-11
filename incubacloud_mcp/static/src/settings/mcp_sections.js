/** @odoo-module **/

/** The agents' section of Settings, and their limit among the rate limits. */

import { _t } from "@web/core/l10n/translation";
import { settingsBlocks, settingsSections } from "@incubacloud/components/settings/settings_registry";
import { McpRatesSection } from "./mcp_rates_section";
import { McpSettingsTab } from "./mcp_settings_tab";

settingsSections.add("agents", {
    group: "integrations", sequence: 30, title: _t("Agents (MCP)"), legacyTab: "agents",
    description: _t("AI agents that operate the panel on behalf of a user, and the rules that let one act without asking."),
});
settingsBlocks.add("mcp-agents", {
    section: "agents", sequence: 10, Component: McpSettingsTab,
    fields: [
        {
            id: "mcp_oauth_issuer", type: "text", width: "l", mono: true,
            placeholder: "https://login.example.com", block: _t("Connection"),
            label: _t("Agents' OAuth issuer"),
            help: _t("Who signs the tokens agents present. Empty: agents connect with API keys only."),
            synonyms: ["mcp", "oauth", "ai", "claude", "login"],
        },
        {
            id: "mcp_resource", type: "text", width: "l", mono: true, block: _t("Connection"),
            label: _t("MCP address"),
            help: _t("The address to give the agent."),
            synonyms: ["mcp", "endpoint", "url"],
        },
        {
            id: "mcp_auto_approval", type: "custom", block: _t("Auto-approval"),
            label: _t("Auto-approval rules"),
            help: _t("Let one credential of one person act without asking, until a date or for good."),
            synonyms: ["mcp", "approve", "approval", "agent", "rule"],
        },
    ],
});
settingsBlocks.add("mcp-rates", {
    section: "rates", sequence: 20, Component: McpRatesSection,
    fields: [
        {
            id: "rate_limit_mcp_user_per_min", type: "number", unit: _t("/min"), min: 1,
            block: _t("Agents (MCP)"), label: _t("Agent calls (per user, all their agents)"),
            help: _t("Tool calls per minute one user's agents may make together."),
            synonyms: ["rate", "throttle", "limit", "mcp", "ai"],
        },
    ],
});
