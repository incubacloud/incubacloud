/** @odoo-module **/

/** Registers the assistant's components in the panel's SPA. */
import { AppHeader } from "@incubacloud/components/app_header/app_header";
import { Settings } from "@incubacloud/components/settings/settings";
import { AssistantButton } from "./button/assistant_button";
import { AssistantSettingsTab } from "./settings/assistant_settings_tab";

AppHeader.components = Object.assign({}, AppHeader.components, { AssistantButton });

// Rendered inside the Settings template through assistant_templates.xml, so
// they must be among Settings' components for OWL to resolve them.
Settings.components = Object.assign(Settings.components || {}, { AssistantSettingsTab });
