/** @odoo-module **/

/** Registers the MCP's route and components in the panel's SPA. */
import { Chrome, registerRoute } from "@incubacloud/app/app";
import { ConnectAgent } from "./connect_agent/connect_agent";
import "./settings/mcp_sections";

registerRoute(
    "connect_agent",
    (parts) => (parts[0] === "connect-agent" && parts.length === 1 ? {} : null),
    () => "/cloud/connect-agent",
);

Object.assign(Chrome.components, { ConnectAgent });
