import { describe, expect, test } from "@odoo/hoot";
import {
    API_KEY_ENV,
    API_KEY_PLACEHOLDER,
    changedSettings,
    claudeCodeCommand,
    codexConfig,
    connectView,
    cursorConfig,
    cursorInstallLink,
    settingsView,
    shellQuote,
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
        expect(view.claudeCode).toBe(
            `claude mcp add --transport http --scope user incubacloud '${URL}'`
        );
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
        expect(view.cursorJson).toInclude(
            `"Authorization": "Bearer \${env:${API_KEY_ENV}}"`
        );
        expect(view.cursorJson).not.toInclude(API_KEY_PLACEHOLDER);
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
        expect(settingsView({ managed: 1 }).readonly).toBe(true);
        expect(settingsView({ managed: "" }).readonly).toBe(false);
        expect(settingsView().readonly).toBe(false);
    });

    test("without an MCP address, it says so and shows no command", () => {
        const view = connectView({ resource: "", issuer: "https://incubacloud.io" });
        expect(view.urlProblem).toBe("missing");
        for (const key of ["claudeCode", "vscode", "cursorLink", "cursorJson", "codex", "codexLogin"]) {
            expect(view[key]).toBe("");
        }
    });

    test("Settings saves only the fields the user changed", () => {
        const loaded = { mcp_oauth_issuer: "", mcp_resource: "" };
        expect(changedSettings(loaded, { ...loaded })).toEqual({});
        expect(
            changedSettings(loaded, { mcp_oauth_issuer: "https://login.example.com", mcp_resource: "" })
        ).toEqual({ mcp_oauth_issuer: "https://login.example.com" });
    });

    test("nothing is saved over settings that could not be read", () => {
        expect(changedSettings(null, { mcp_oauth_issuer: "x" })).toEqual({});
    });
});

describe("Connect an agent — what a shell reads", () => {
    test("a URL a shell would read gives no command, only a notice", () => {
        for (const resource of ["https://x.example.com/mcp$(id)", "https://x.example.com/mcp'x"]) {
            const view = connectView({ resource, issuer: "", api_keys: true });
            expect(view.urlProblem).toBe("unsafe");
            for (const key of [
                "claudeCode",
                "vscode",
                "cursorLink",
                "cursorJson",
                "codex",
                "codexLogin",
            ]) {
                expect(view[key]).toBe("");
            }
        }
    });

    test("a plain URL is safe", () => {
        expect(connectView({ resource: URL, issuer: "x" }).urlProblem).toBe("");
        expect(
            connectView({ resource: "https://my_panel.example.com/mcp", issuer: "x" }).urlProblem
        ).toBe("");
        expect(connectView({ resource: "http://[::1]:8069/mcp", issuer: "x" }).urlProblem).toBe(
            "unsafe"
        );
    });

    test("the commands quote the URL for the shell", () => {
        expect(shellQuote("a'b")).toBe(`'a'\\''b'`);
        expect(claudeCodeCommand("https://x/a'b")).toInclude(`'https://x/a'\\''b'`);
        expect(vscodeCommand("https://x/a'b")).toInclude(`a'\\''b`);
        expect(codexConfig('https://x/a"b')).toInclude('url = "https://x/a\\"b"');
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

    test("Cursor reads the API key from the environment, as Codex", () => {
        expect(JSON.parse(cursorConfig(URL, { apiKey: true }))).toEqual({
            mcpServers: {
                incubacloud: {
                    url: URL,
                    headers: { Authorization: `Bearer \${env:${API_KEY_ENV}}` },
                },
            },
        });
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
