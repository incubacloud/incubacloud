/** @odoo-module **/

/** Registers the MCP's route and components in the panel's SPA. */
import { Chrome, registerRoute } from "@incubacloud/app/app";
import { Settings } from "@incubacloud/components/settings/settings";
import { ConnectAgent } from "./connect_agent/connect_agent";
import { McpRatesSection } from "./settings/mcp_rates_section";
import { McpSettingsTab } from "./settings/mcp_settings_tab";

registerRoute(
    "connect_agent",
    (parts) => (parts[0] === "connect-agent" && parts.length === 1 ? {} : null),
    () => "/cloud/connect-agent",
);

Object.assign(Chrome.components, { ConnectAgent });

// Rendered inside the Settings template through mcp_templates.xml, so
// they must be among Settings' components for OWL to resolve them.
Settings.components = Object.assign(Settings.components || {}, {
    McpSettingsTab,
    McpRatesSection,
});
