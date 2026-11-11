/** @odoo-module **/

/** Registers the assistant's components in the panel's SPA. */
import { AppHeader } from "@incubacloud/components/app_header/app_header";
import { AssistantButton } from "./button/assistant_button";
import "./settings/assistant_sections";

AppHeader.components = Object.assign({}, AppHeader.components, { AssistantButton });
