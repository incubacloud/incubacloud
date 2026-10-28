/** @odoo-module **/

/** Registers the assistant's components in the panel's SPA. */
import { Settings } from "@incubacloud/components/settings/settings";
import { AssistantSettingsTab } from "./settings/assistant_settings_tab";

// Rendered inside the Settings template through assistant_templates.xml, so
// they must be among Settings' components for OWL to resolve them.
Settings.components = Object.assign(Settings.components || {}, { AssistantSettingsTab });
