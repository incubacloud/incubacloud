import { describe, expect, test } from "@odoo/hoot";
import {
    API_KEY_ENV,
    claudeCodeCommand,
    codexConfig,
    connectView,
    cursorConfig,
    cursorInstallLink,
    settingsView,
    vscodeCommand,
} from "@incubacloud_mcp/connect_agent/connect_agent_format";

/**
 * connect_agent_format — what "Connect an agent" tells each client.
 *
 * Every command and link the page shows comes from these functions, so
 * each is checked here against the client's documented form.
 */

const URL = "https://acme.incubacloud.io/mcp";

describe("Connect an agent — what it shows", () => {
    test("the panel's URL and the Claude Code command with it", () => {
        const view = connectView({
            resource: URL,
            issuer: "https://incubacloud.io",
            api_keys: false,
        });
        expect(view.url).toBe(URL);
        expect(view.claudeCode).toBe(`claude mcp add --transport http incubacloud ${URL}`);
        expect(view.needsApiKey).toBe(false);
        expect(view.apiKeysRefused).toBe(false);
    });

    test("without an issuer, the API key notice and the header in every command", () => {
        const view = connectView({ resource: URL, issuer: "", api_keys: true });
        expect(view.needsApiKey).toBe(true);
        expect(view.claudeCode).toInclude('--header "Authorization: Bearer <API_KEY>"');
        expect(view.vscode).toInclude('"headers":{"Authorization":"Bearer <API_KEY>"}');
        expect(view.codex).toInclude(`bearer_token_env_var = "${API_KEY_ENV}"`);
        expect(view.cursorLink).toBe("");
        expect(view.cursorJson).toInclude('"Authorization": "Bearer <API_KEY>"');
        expect(view.codexLogin).toBe("");
    });

    test("with neither an issuer nor API keys, it says agents cannot connect yet", () => {
        expect(connectView({ resource: URL, issuer: "", api_keys: false }).apiKeysRefused).toBe(
            true
        );
    });

    test("with OAuth, Codex signs in with its login command", () => {
        const view = connectView({ resource: URL, issuer: "https://incubacloud.io" });
        expect(view.codexLogin).toBe("codex mcp login incubacloud");
        expect(view.codex).not.toInclude("bearer_token_env_var");
    });

    test("the links of the answer reach the page, empty when missing", () => {
        const view = connectView({
            resource: URL,
            issuer: "",
            api_keys: true,
            docs_url: "https://docs.incubacloud.io/agents/",
            preferences_url: "/odoo/action-base.action_res_users_my",
        });
        expect(view.docsUrl).toBe("https://docs.incubacloud.io/agents/");
        expect(view.preferencesUrl).toBe("/odoo/action-base.action_res_users_my");
        expect(view.agentsUrl).toBe("");
        expect(connectView().url).toBe("");
    });

    test("on a tenant, the issuer and the URL are read-only", () => {
        expect(settingsView({ managed: true }).readonly).toBe(true);
        expect(settingsView({ managed: false }).readonly).toBe(false);
    });
});

describe("Connect an agent — each client's documented form", () => {
    test("Cursor's link carries the URL as base64 JSON", () => {
        const link = cursorInstallLink(URL);
        expect(link).toInclude(
            "cursor://anysphere.cursor-deeplink/mcp/install?name=incubacloud&config="
        );
        const config = decodeURIComponent(link.split("config=")[1]);
        expect(JSON.parse(atob(config))).toEqual({ url: URL });
    });

    test("Cursor's mcp.json entry names the server under mcpServers", () => {
        expect(JSON.parse(cursorConfig(URL))).toEqual({
            mcpServers: { incubacloud: { url: URL } },
        });
    });

    test("VS Code's command is its documented --add-mcp", () => {
        expect(vscodeCommand(URL)).toBe(
            `code --add-mcp '{"name":"incubacloud","type":"http","url":"${URL}"}'`
        );
    });

    test("Codex's configuration names the URL", () => {
        expect(codexConfig(URL)).toBe(`[mcp_servers.incubacloud]\nurl = "${URL}"`);
    });

    test("the Claude Code command has no header when OAuth is there", () => {
        expect(claudeCodeCommand(URL)).not.toInclude("--header");
    });
});
