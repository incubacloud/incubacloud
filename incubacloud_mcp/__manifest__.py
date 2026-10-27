{
    "name": "IncubaCloud MCP",
    "version": "1.0.0",
    "summary": (
        "AI agents operate the panel through the Model Context "
        "Protocol, with the signed-in user's role"
    ),
    "description": """
Model Context Protocol server for the IncubaCloud panel.

Agents (Claude Code, claude.ai, Claude Desktop, Cursor, VS Code, Codex…)
connect to POST /mcp and act with the role and project rules of the user
they sign in as: OAuth tokens of the configured issuer, or the panel's own
API keys. Destructive and security-sensitive actions wait for that user's
approval. Secrets never go in or out.
""",
    "category": "Tools",
    "website": "https://www.incubacloud.io",
    "author": "IncubaCloud",
    "license": "Other proprietary",
    "depends": ["incubacloud", "auth_oauth"],
    "external_dependencies": {"python": ["joserfc"]},
    "data": [
        "security/ir.model.access.csv",
        "data/mcp_defaults.xml",
        "views/auth_oauth_provider_views.xml",
        "views/approval_page.xml",
    ],
    "assets": {
        "incubacloud.assets_prod": [
            "incubacloud_mcp/static/src/**/*",
        ],
        # JS unit tests (Hoot framework), beside core's.
        "web.assets_unit_tests": [
            "incubacloud_mcp/static/src/**/*.js",
            "incubacloud_mcp/static/tests/**/*.test.js",
        ],
    },
    "installable": True,
    "auto_install": False,
}
