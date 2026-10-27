/** @odoo-module **/

/**
 * What "Connect an agent" says, built from the panel's MCP URL: one pure
 * function per client, so hoot checks every command the page shows.
 *
 * Each form follows the client's documentation (checked 27-oct-2026):
 * Claude Code's `claude mcp add`, Cursor's install link and `mcp.json`,
 * VS Code's `code --add-mcp` and Codex's `config.toml`.
 */

/** The name each client gives this panel's server. */
export const SERVER_NAME = "incubacloud";
/** Where the commands say the user's API key goes. */
export const API_KEY_PLACEHOLDER = "<API_KEY>";
/** The environment variable Codex reads the API key from. */
export const API_KEY_ENV = "INCUBACLOUD_API_KEY";

const BEARER = `Bearer ${API_KEY_PLACEHOLDER}`;

/**
 * A URL the commands may carry: http(s), a plain host, an optional port and
 * a plain path. Anything else could be read by the user's shell.
 */
const SAFE_URL = /^https?:\/\/[a-z0-9._-]+(:\d+)?(\/[A-Za-z0-9._~%/-]*)?$/;

/**
 * Quote *text* for a POSIX shell: between single quotes, each quote in it
 * closing, escaped and reopening (`'\''`).
 *
 * @param {string} text what the shell must read as one word
 * @returns {string} the quoted word
 */
export function shellQuote(text) {
    return `'${String(text).replaceAll("'", "'\\''")}'`;
}

/**
 * Claude Code: `claude mcp add`, with the key's header when there is no OAuth.
 *
 * @param {string} url the panel's MCP URL
 * @param {{apiKey?: boolean}} [options] whether the command carries an API key
 * @returns {string} the command to run in a terminal
 */
export function claudeCodeCommand(url, { apiKey = false } = {}) {
    const header = apiKey ? ` --header "Authorization: ${BEARER}"` : "";
    return `claude mcp add --transport http ${SERVER_NAME} ${shellQuote(url)}${header}`;
}

/**
 * VS Code: its documented `code --add-mcp`.
 *
 * @param {string} url the panel's MCP URL
 * @param {{apiKey?: boolean}} [options] whether the command carries an API key
 * @returns {string} the command to run in a terminal
 */
export function vscodeCommand(url, { apiKey = false } = {}) {
    const config = { name: SERVER_NAME, type: "http", url };
    if (apiKey) {
        config.headers = { Authorization: BEARER };
    }
    return `code --add-mcp ${shellQuote(JSON.stringify(config))}`;
}

/**
 * Cursor: its install link, the server's configuration as base64 JSON.
 *
 * @param {string} url the panel's MCP URL
 * @returns {string} the `cursor://` link that adds the server in one click
 */
export function cursorInstallLink(url) {
    const config = btoa(JSON.stringify({ url }));
    return (
        `cursor://anysphere.cursor-deeplink/mcp/install?name=${SERVER_NAME}` +
        `&config=${encodeURIComponent(config)}`
    );
}

/**
 * Cursor: the `mcp.json` entry, for a key the link must not carry.
 *
 * The key is read from the environment, as Codex reads it: Cursor
 * documents `${env:NAME}` in a header, and the file keeps no secret.
 *
 * @param {string} url the panel's MCP URL
 * @param {{apiKey?: boolean}} [options] whether the entry carries an API key
 * @returns {string} the JSON to add to `~/.cursor/mcp.json`
 */
export function cursorConfig(url, { apiKey = false } = {}) {
    const server = { url };
    if (apiKey) {
        server.headers = { Authorization: `Bearer \${env:${API_KEY_ENV}}` };
    }
    return JSON.stringify({ mcpServers: { [SERVER_NAME]: server } }, null, 2);
}

/**
 * Codex: its `config.toml` table; a key travels in an environment variable.
 *
 * @param {string} url the panel's MCP URL
 * @param {{apiKey?: boolean}} [options] whether Codex sends an API key
 * @returns {string} the table to add to `~/.codex/config.toml`
 */
export function codexConfig(url, { apiKey = false } = {}) {
    const lines = [`[mcp_servers.${SERVER_NAME}]`, `url = ${JSON.stringify(url)}`];
    if (apiKey) {
        lines.push(`bearer_token_env_var = "${API_KEY_ENV}"`);
    }
    return lines.join("\n");
}

/**
 * Everything the page shows, from `/cloud/mcp/config`'s answer.
 *
 * Without an issuer, agents sign in with an API key: every command
 * carries it, and Cursor's one-click link, which would put it in a URL,
 * gives way to its `mcp.json` entry. With neither an issuer nor API keys,
 * no agent can sign in yet. Without a URL, or with one a shell could
 * read, no command at all: `urlProblem` says which.
 *
 * @param {Object} [config] the answer of `/cloud/mcp/config`
 * @returns {Object} the URL, the notices, the links and each client's command
 */
export function connectView(config = {}) {
    const url = config.resource || "";
    let urlProblem = "";
    if (!url) {
        urlProblem = "missing";
    } else if (!SAFE_URL.test(url)) {
        urlProblem = "unsafe";
    }
    const usable = !urlProblem;
    const needsApiKey = !config.issuer;
    const options = { apiKey: needsApiKey };
    return {
        url,
        urlProblem,
        needsApiKey,
        apiKeysRefused: !config.issuer && !config.api_keys,
        agentsUrl: config.agents_url || "",
        docsUrl: config.docs_url || "",
        preferencesUrl: config.preferences_url || "",
        claudeCode: usable ? claudeCodeCommand(url, options) : "",
        vscode: usable ? vscodeCommand(url, options) : "",
        cursorLink: usable && !needsApiKey ? cursorInstallLink(url) : "",
        cursorJson: usable ? cursorConfig(url, options) : "",
        codex: usable ? codexConfig(url, options) : "",
        codexLogin: usable && !needsApiKey ? `codex mcp login ${SERVER_NAME}` : "",
    };
}

/**
 * How Settings shows the issuer and the URL: read-only where set from above.
 *
 * @param {{managed?: boolean}} [settings] the answer of `/cloud/mcp/get_settings`
 * @returns {{readonly: boolean}} whether the two fields are read-only
 */
export function settingsView(settings = {}) {
    return { readonly: Boolean(settings.managed) };
}

/**
 * What Settings saves: the fields the user changed since they were read.
 *
 * A field left alone is not sent: an MCP URL the panel could not work
 * out stays empty, and saving another tab must not fail on it.
 *
 * @param {Object|null} loaded the values as read, or null when the read failed
 * @param {Object} form the values in the form
 * @returns {Object} the changed fields; none when nothing was read
 */
export function changedSettings(loaded, form) {
    if (!loaded) {
        return {};
    }
    return Object.fromEntries(
        Object.entries(form).filter(([key, value]) => value !== loaded[key])
    );
}
